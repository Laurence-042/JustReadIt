/*
 * hook_engine.c
 * Injected DLL for JustReadIt hook discovery and text capture.
 *
 * Two modes selected by Config.mode:
 *
 *   MODE_SEARCH (0) — bulk prologue-scanner
 *       Enumerates .text section of <module>, installs a 14-byte absolute
 *       JMP at each candidate function prologue, executes a custom x64
 *       trampoline stub that scans the call-stack frame for UTF-16LE / UTF-8
 *       CJK strings and forwards them via Named Pipe.  Uses no MinHook.
 *
 *   MODE_HOOK (1) — single confirmed hook via MinHook
 *       Installs one safe hook at the address given in Config.hook_address
 *       using MH_CreateHook.  The detour reads the text argument according
 *       to {arg_idx, deref, byte_offset, encoding} and forwards via Pipe.
 *
 * Named Pipe protocol
 * -------------------
 * Pipe name: \\.\pipe\JRI-<target_pid>   (target resolved by GetCurrentProcessId)
 * Python creates the server end before injecting the DLL.
 *
 * Python → DLL  (Config, 20 bytes + optional padding, sent once):
 *   [1]  mode
 *   [3]  padding
 *   [4]  max_hooks    (search only)
 *   [8]  hook_address (hook only, absolute VA)
 *   [1]  arg_idx      (hook only: 0-3 register, 0xFF stack-relative)
 *   [1]  deref        (hook only: 0=direct, 1=*(ptr+offset))
 *   [2]  byte_offset  (hook only: struct member offset)
 *   [2]  encoding     (hook only: 0=utf16le, 1=utf8)
 *   [2]  padding
 *   Total = 24 bytes
 *
 * DLL → Python  (Result, header 16 bytes + variable text):
 *   [8]  hook_va      absolute VA of function that fired
 *   [4]  slot_i       stack-slot index (negative = saved register copy)
 *   [2]  encoding     0=utf16le, 1=utf8
 *   [2]  text_len     byte count of text (not including null)
 *   [N]  text         UTF-16LE or UTF-8 bytes (no null)
 *
 * Build (MSVC x64 Developer Command Prompt):
 *   cl /LD /O2 /GS- hook_engine.c /I ..\..\..\3rd\hook\MinHook_134_bin\include ^
 *      /link ..\..\..\3rd\hook\MinHook_134_bin\bin\MinHook.x64.lib ^
 *      /OUT:hook_engine.dll
 *
 * License: MPL-2.0 (this file)  +  BSD-2-Clause (MinHook, dynamically linked)
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <psapi.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include "MinHook.h"

#pragma comment(lib, "psapi.lib")

/* =========================================================================
 * Constants
 * ========================================================================= */

#define MODE_SEARCH   0
#define MODE_HOOK     1

#define MAX_HOOKS     60000
#define STUB_BYTES    64        /* bytes per stub in stub forest            */
#define PATCH_BYTES   14        /* absolute JMP: FF25 00000000 + 8-byte ptr */
#define MAX_STR_LEN   400       /* max wchar_t / char count to accept       */
#define MIN_STR_LEN   2
#define SCAN_STACK_LO (-4)      /* stack slots relative to orig RSP to scan */
#define SCAN_STACK_HI 20
#define DEBOUNCE_US   80000     /* don't re-send same hook within 80 ms     */
#define RESULT_MAGIC  0x4A5249  /* "JRI" sanity tag (unused, reserved)      */

/* =========================================================================
 * Config / Result structs (must match Python pack layout exactly)
 * ========================================================================= */

#pragma pack(push, 1)

typedef struct {
    uint8_t  mode;
    uint8_t  _pad0[3];
    uint32_t max_hooks;
    uint64_t hook_address;
    uint8_t  arg_idx;
    uint8_t  deref;
    uint16_t byte_offset;
    uint16_t encoding;
    uint8_t  _pad1[2];
} Config;   /* 24 bytes */

typedef struct {
    uint64_t hook_va;
    int32_t  slot_i;
    uint16_t encoding;
    uint16_t text_len;
    /* followed by text_len bytes */
} ResultHdr;  /* 16 bytes */

#pragma pack(pop)

/* =========================================================================
 * Global state
 * ========================================================================= */

static HANDLE          g_pipe         = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_pipe_cs;

static volatile LONG   g_slot_count   = 0;
static uint8_t        *g_stubs        = NULL;   /* executable stub forest  */

typedef struct {
    uintptr_t    va;            /* original hooked function VA              */
    uint8_t      saved[PATCH_BYTES]; /* original bytes we overwrote         */
    ULONGLONG    last_sent_us;  /* GetTickCount64*1000 approximation        */
} HookSlot;

static HookSlot g_slots[MAX_HOOKS];

/* MinHook single-hook state */
static void  *g_mh_original  = NULL;
static Config g_cfg;

/* =========================================================================
 * Pipe helpers
 * ========================================================================= */

static bool pipe_read_all(void *buf, DWORD len) {
    DWORD total = 0;
    while (total < len) {
        DWORD got = 0;
        if (!ReadFile(g_pipe, (char *)buf + total, len - total, &got, NULL) || got == 0)
            return false;
        total += got;
    }
    return true;
}

static void pipe_send_text(uintptr_t hook_va, int slot_i,
                            int encoding, const void *text, DWORD text_bytes) {
    if (g_pipe == INVALID_HANDLE_VALUE) return;
    if (text_bytes == 0 || text_bytes > MAX_STR_LEN * 2) return;

    /* Allocate on stack: header + max text */
    uint8_t buf[sizeof(ResultHdr) + MAX_STR_LEN * 2];
    ResultHdr *hdr = (ResultHdr *)buf;
    hdr->hook_va  = (uint64_t)hook_va;
    hdr->slot_i   = (int32_t)slot_i;
    hdr->encoding = (uint16_t)encoding;
    hdr->text_len = (uint16_t)text_bytes;
    memcpy(buf + sizeof(ResultHdr), text, text_bytes);

    DWORD to_write = sizeof(ResultHdr) + text_bytes;
    DWORD written  = 0;

    EnterCriticalSection(&g_pipe_cs);
    WriteFile(g_pipe, buf, to_write, &written, NULL);
    LeaveCriticalSection(&g_pipe_cs);
}

/* =========================================================================
 * String probing
 * ========================================================================= */

static bool has_cjk_w(const WCHAR *ws, int len) {
    for (int i = 0; i < len; i++) {
        WCHAR c = ws[i];
        if ((c >= 0x3000 && c <= 0x9FFF) || (c >= 0xFF00 && c <= 0xFFEF))
            return true;
    }
    return false;
}

/* Safe probe: returns string length, or -1 if unreadable / out-of-range. */
static int probe_wstr(const WCHAR *p) {
    if ((uintptr_t)p < 0x10000) return -1;
    __try {
        int len = 0;
        while (len < MAX_STR_LEN && p[len]) len++;
        if (len < MIN_STR_LEN) return -1;
        if (p[len] != 0) return -1;   /* no null terminator within limit */
        return len;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -1;
    }
}

static int probe_str8(const char *p) {
    if ((uintptr_t)p < 0x10000) return -1;
    __try {
        int len = 0;
        while (len < MAX_STR_LEN && (unsigned char)p[len] >= 0x80) len++;
        /* require at least MIN_STR_LEN non-ASCII bytes and a null */
        if (len < MIN_STR_LEN) return -1;
        if (p[len] != 0) return -1;
        return len;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -1;
    }
}

/* =========================================================================
 * Stack scanner — called from every search-mode stub
 * ========================================================================= */

/*
 * Called with:
 *   slot_idx  — index into g_slots
 *   orig_rsp  — pointer to the original RSP at function entry
 *              orig_rsp[-5] = saved rax  (don't care)
 *              orig_rsp[-4] = saved rcx = arg0
 *              orig_rsp[-3] = saved rdx = arg1
 *              orig_rsp[-2] = saved r8  = arg2
 *              orig_rsp[-1] = saved r9  = arg3
 *              orig_rsp[ 0] = return address
 *              orig_rsp[1..SCAN_STACK_HI] = caller stack slots
 */
void __cdecl scan_stack_and_send(uint32_t slot_idx, uintptr_t *orig_rsp) {
    if (slot_idx >= (uint32_t)g_slot_count) return;

    HookSlot *slot = &g_slots[slot_idx];

    /* Debounce: skip if we have sent very recently */
    ULONGLONG now_ms = GetTickCount64();
    if ((now_ms - slot->last_sent_us) < (DEBOUNCE_US / 1000)) return;

    bool sent = false;

    for (int i = SCAN_STACK_LO; i <= SCAN_STACK_HI; i++) {
        if (i == -5) continue;             /* saved rax — uninformative */

        __try {
            uintptr_t val = orig_rsp[i];

            /* --- try as UTF-16LE pointer --- */
            int wlen = probe_wstr((const WCHAR *)val);
            if (wlen > 0 && has_cjk_w((const WCHAR *)val, wlen)) {
                pipe_send_text(slot->va, i, 0,
                               (const WCHAR *)val, (DWORD)(wlen * sizeof(WCHAR)));
                sent = true;
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }

    if (sent)
        slot->last_sent_us = now_ms;
}

/* =========================================================================
 * x64 Stub generation
 *
 * Stub layout (STUB_BYTES = 64):
 *
 * OFF  SZ  INSTRUCTION
 *  0   1   push rax
 *  1   1   push rcx
 *  2   1   push rdx
 *  3   2   push r8
 *  5   2   push r9
 *  7   5   lea rdx, [rsp+40]          ; orig_rsp
 * 12   5   mov ecx, <slot_idx>        ; patched: bytes 13..16
 * 17   4   sub rsp, 0x20              ; shadow space
 * 21  10   mov rax, <scanner_abs64>   ; patched: bytes 23..30
 * 31   2   call rax
 * 33   4   add rsp, 0x20
 * 37   2   pop r9
 * 39   2   pop r8
 * 41   1   pop rdx
 * 42   1   pop rcx
 * 43   1   pop rax
 * 44  14   <saved original bytes>     ; patched
 * 58   5   jmp rel32 back             ; patched: bytes 59..62
 * 63   1   NOP
 * ========================================================================= */

static void write_stub(uint8_t *dst, uint32_t slot_idx,
                        uintptr_t scanner_fn,
                        const uint8_t *saved_bytes,
                        uintptr_t jump_back) {
    static const uint8_t tpl[44] = {
        0x50,                                    /* push rax            */
        0x51,                                    /* push rcx            */
        0x52,                                    /* push rdx            */
        0x41,0x50,                               /* push r8             */
        0x41,0x51,                               /* push r9             */
        0x48,0x8D,0x54,0x24,0x28,               /* lea rdx,[rsp+40]    */
        0xB9,0x00,0x00,0x00,0x00,               /* mov ecx,<slot> @13  */
        0x48,0x83,0xEC,0x20,                     /* sub rsp,32          */
        0x48,0xB8,0x00,0x00,0x00,0x00,
             0x00,0x00,0x00,0x00,               /* mov rax,<fn> @23    */
        0xFF,0xD0,                               /* call rax            */
        0x48,0x83,0xC4,0x20,                     /* add rsp,32          */
        0x41,0x59,                               /* pop r9              */
        0x41,0x58,                               /* pop r8              */
        0x5A,                                    /* pop rdx             */
        0x59,                                    /* pop rcx             */
        0x58,                                    /* pop rax             */
    };

    memcpy(dst, tpl, 44);

    /* patch slot index */
    *(uint32_t *)(dst + 13) = slot_idx;

    /* patch scanner absolute address */
    *(uint64_t *)(dst + 23) = scanner_fn;

    /* saved original bytes */
    memcpy(dst + 44, saved_bytes, PATCH_BYTES);

    /* JMP rel32 back to func+PATCH_BYTES */
    dst[58] = 0xE9;
    *(int32_t *)(dst + 59) = (int32_t)(jump_back - ((uintptr_t)(dst + 58 + 5)));

    /* NOP padding */
    dst[63] = 0x90;
}

/* =========================================================================
 * Inline patcher helpers
 * ========================================================================= */

/* Returns false if the saved bytes contain a relative branch (unsafe to
 * relocate). Only checks opcodes that appear in common prologues. */
static bool safe_to_relocate(const uint8_t *b) {
    for (int i = 0; i < PATCH_BYTES - 1; i++) {
        uint8_t op = b[i];
        if (op == 0xE8 || op == 0xE9 || op == 0xEB) return false;  /* JMP/CALL rel */
        if (op == 0x0F && ((b[i+1] & 0xF0) == 0x80)) return false; /* Jcc near    */
        if ((op & 0xF0) == 0x70) return false;                      /* Jcc short   */
    }
    return true;
}

/* Patch 14 bytes at func_va with an absolute indirect JMP to stub.
 * Saves the original bytes into saved_out (must be PATCH_BYTES long). */
static bool install_patch(uintptr_t func_va, const uint8_t *stub_ptr,
                           uint8_t *saved_out) {
    /* Save original bytes */
    __try {
        memcpy(saved_out, (void *)func_va, PATCH_BYTES);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }

    /* Skip if first byte indicates already-patched or jump table */
    if (saved_out[0] == 0xFF || saved_out[0] == 0xE9 ||
        saved_out[0] == 0xEB || saved_out[0] == 0xCC) return false;

    if (!safe_to_relocate(saved_out)) return false;

    /* Build 14-byte absolute JMP:  FF 25 00000000 <stub64> */
    uint8_t patch[PATCH_BYTES];
    patch[0] = 0xFF; patch[1] = 0x25;
    *(uint32_t *)(patch + 2) = 0;             /* RIP + 0  */
    *(uint64_t *)(patch + 6) = (uint64_t)stub_ptr; /* target  */

    /* Temporarily make the page writable */
    DWORD old_prot = 0;
    if (!VirtualProtect((void *)func_va, PATCH_BYTES,
                         PAGE_EXECUTE_READWRITE, &old_prot)) return false;
    memcpy((void *)func_va, patch, PATCH_BYTES);
    VirtualProtect((void *)func_va, PATCH_BYTES, old_prot, &old_prot);
    FlushInstructionCache(GetCurrentProcess(), (void *)func_va, PATCH_BYTES);

    return true;
}

/* =========================================================================
 * Prologue scanner — returns non-zero if bytes look like a function start
 * ========================================================================= */

static bool is_prologue(const uint8_t *b) {
    /* push rbp */
    if (b[0] == 0x55) return true;
    /* push rbp (REX) */
    if (b[0] == 0x40 && b[1] == 0x55) return true;
    /* sub rsp, N */
    if (b[0] == 0x48 && b[1] == 0x83 && b[2] == 0xEC) return true;
    if (b[0] == 0x48 && b[1] == 0x81 && b[2] == 0xEC) return true;
    /* push rN (r8-r15, REX.B prefix) */
    if (b[0] == 0x41 && (b[1] >= 0x50 && b[1] <= 0x57)) return true;
    /* push rbx / push rsi / push rdi / push rbp */
    if ((b[0] >= 0x53 && b[0] <= 0x57) || b[0] == 0x6A) return true;
    /* mov [rsp+N], rX  (spill args) */
    if (b[0] == 0x48 && b[1] == 0x89 && b[2] == 0x4C) return true;
    if (b[0] == 0x48 && b[1] == 0x89 && b[2] == 0x54) return true;
    /* xchg eax, eax (padding / hot-patch NOP) */
    if (b[0] == 0x90) return false;        /* lone NOP — skip           */
    if (b[0] == 0xCC) return false;        /* INT3 / padding            */
    return false;
}

/* =========================================================================
 * Allocate stub forest near a given VA (within ±2 GB for rel32 back-JMP)
 * ========================================================================= */

static uint8_t *alloc_stub_forest_near(uintptr_t near_va, size_t total_size) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    uintptr_t page = (uintptr_t)si.dwPageSize;

    /* Try above */
    for (uintptr_t try = near_va + page;
         try < near_va + 0x70000000 && try > near_va;
         try += 0x10000) {
        uint8_t *p = VirtualAlloc((void *)try, total_size,
                                   MEM_COMMIT | MEM_RESERVE,
                                   PAGE_EXECUTE_READWRITE);
        if (p) return p;
    }
    /* Try below */
    for (uintptr_t try = near_va - total_size;
         try > near_va - 0x70000000 && try > (uintptr_t)page;
         try -= 0x10000) {
        uint8_t *p = VirtualAlloc((void *)try, total_size,
                                   MEM_COMMIT | MEM_RESERVE,
                                   PAGE_EXECUTE_READWRITE);
        if (p) return p;
    }
    /* Fallback: anywhere */
    return VirtualAlloc(NULL, total_size,
                         MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
}

/* =========================================================================
 * Thread suspension helper (for safe bulk patching)
 * ========================================================================= */

static void suspend_other_threads(HANDLE *handles, DWORD *count) {
    *count = 0;
    DWORD our_tid = GetCurrentThreadId();
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return;

    THREADENTRY32 te;
    te.dwSize = sizeof(te);
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID == GetCurrentProcessId() &&
                te.th32ThreadID != our_tid && *count < 1024) {
                HANDLE h = OpenThread(THREAD_SUSPEND_RESUME, FALSE,
                                       te.th32ThreadID);
                if (h) {
                    SuspendThread(h);
                    handles[(*count)++] = h;
                }
            }
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
}

static void resume_threads(HANDLE *handles, DWORD count) {
    for (DWORD i = 0; i < count; i++) {
        ResumeThread(handles[i]);
        CloseHandle(handles[i]);
    }
}

/* Need TlHelp32 */
#include <tlhelp32.h>

/* =========================================================================
 * SEARCH MODE — bulk prologue scan + install
 * ========================================================================= */

static void do_search(const Config *cfg) {
    /* Locate target module .text section */
    HMODULE hmod = GetModuleHandleW(NULL); /* default: game exe */
    /* (Could accept module name from config, but game EXE is always correct) */

    MODULEINFO mi;
    if (!GetModuleInformation(GetCurrentProcess(), hmod, &mi, sizeof(mi))) {
        /* Signal error via pipe */
        pipe_send_text(0, -99, 0, L"ERROR:GetModuleInformation failed", 34 * 2);
        return;
    }

    uintptr_t base = (uintptr_t)mi.lpBaseOfDll;

    /* Parse PE header to find .text section bounds */
    IMAGE_DOS_HEADER  *dos  = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS  *nt   = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_SECTION_HEADER *sec = IMAGE_FIRST_SECTION(nt);

    uintptr_t text_start = 0, text_size = 0;
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; i++) {
        if (memcmp(sec[i].Name, ".text", 5) == 0) {
            text_start = base + sec[i].VirtualAddress;
            text_size  = sec[i].Misc.VirtualSize;
            break;
        }
    }
    if (!text_start) {
        /* Fallback: treat entire module as .text */
        text_start = base;
        text_size  = mi.SizeOfImage;
    }

    DWORD max_hooks = cfg->max_hooks > 0 ? cfg->max_hooks : MAX_HOOKS;
    if (max_hooks > MAX_HOOKS) max_hooks = MAX_HOOKS;

    size_t forest_size = (size_t)max_hooks * STUB_BYTES;
    g_stubs = alloc_stub_forest_near(text_start, forest_size);
    if (!g_stubs) {
        pipe_send_text(0, -99, 0, L"ERROR:stub forest alloc failed", 30*2);
        return;
    }

    uintptr_t scanner_fn = (uintptr_t)scan_stack_and_send;

    /* Suspend game threads to avoid patching under running code */
    HANDLE   thread_handles[1024];
    DWORD    thread_count = 0;
    suspend_other_threads(thread_handles, &thread_count);

    const uint8_t *text_ptr = (const uint8_t *)text_start;
    LONG hooked = 0;

    /* Stride: scan every 16 bytes as a potential function start.
     * (Textractor uses 16-byte alignment for function search.) */
    for (size_t off = 0; off + PATCH_BYTES + 16 < text_size && hooked < (LONG)max_hooks; off += 16) {
        const uint8_t *p = text_ptr + off;

        if (!is_prologue(p)) continue;

        /* Ensure at least PATCH_BYTES of readable data before next alignment */
        LONG idx = InterlockedIncrement(&g_slot_count) - 1;
        if (idx >= (LONG)max_hooks) {
            InterlockedDecrement(&g_slot_count);
            break;
        }

        uint8_t *stub = g_stubs + (size_t)idx * STUB_BYTES;
        uintptr_t func_va = (uintptr_t)p;

        /* Write stub (before patching, so stub is ready before JMP fires) */
        write_stub(stub, (uint32_t)idx, scanner_fn,
                   /* saved bytes placeholder — filled by install_patch */ NULL,
                   func_va + PATCH_BYTES);

        /* Install patch — this fills saved bytes into the stub */
        uint8_t saved[PATCH_BYTES];
        if (!install_patch(func_va, stub, saved)) {
            InterlockedDecrement(&g_slot_count);
            continue;
        }

        /* Fill saved bytes into stub (at offset 44) */
        memcpy(stub + 44, saved, PATCH_BYTES);

        g_slots[idx].va            = func_va;
        memcpy(g_slots[idx].saved, saved, PATCH_BYTES);
        g_slots[idx].last_sent_us  = 0;

        hooked++;
    }

    resume_threads(thread_handles, thread_count);

    /* Notify Python that patching is done */
    char msg[64];
    int len = _snprintf_s(msg, sizeof(msg), _TRUNCATE,
                          "scan_done:%ld", (long)hooked);
    pipe_send_text(0xFFFFFFFFFFFFFFFFull, 0, 1, msg, len > 0 ? len : 0);
}

/* =========================================================================
 * HOOK MODE — single confirmed address via MinHook
 * ========================================================================= */

/* Generic x64 detour: captures first 4 register args.
 * We use __fastcall so RCX and RDX are the first two arguments in the
 * detour signature.  R8 and R9 come next.  We pass them through. */
typedef UINT64 (__fastcall *GenericFn)(UINT64 a0, UINT64 a1, UINT64 a2, UINT64 a3);
static GenericFn g_mh_original_fn = NULL;

static UINT64 __fastcall text_hook_detour(UINT64 a0, UINT64 a1,
                                           UINT64 a2, UINT64 a3) {
    UINT64 args[4] = { a0, a1, a2, a3 };

    const Config *c = &g_cfg;
    uintptr_t ptr = 0;

    if (c->arg_idx < 4) {
        ptr = (uintptr_t)args[c->arg_idx];
    }

    if (c->byte_offset)
        ptr += c->byte_offset;

    if (c->deref) {
        __try { ptr = *(uintptr_t *)ptr; } __except (EXCEPTION_EXECUTE_HANDLER) { ptr = 0; }
    }

    if (ptr) {
        if (c->encoding == 0) {
            int wlen = probe_wstr((const WCHAR *)ptr);
            if (wlen > 0 && has_cjk_w((const WCHAR *)ptr, wlen))
                pipe_send_text(c->hook_address, c->arg_idx, 0,
                               (const WCHAR *)ptr, (DWORD)(wlen * 2));
        } else {
            int alen = probe_str8((const char *)ptr);
            if (alen > 0)
                pipe_send_text(c->hook_address, c->arg_idx, 1,
                               (const char *)ptr, (DWORD)alen);
        }
    }

    return g_mh_original_fn ? g_mh_original_fn(a0, a1, a2, a3) : 0;
}

static void do_hook(const Config *cfg) {
    if (MH_Initialize() != MH_OK) {
        pipe_send_text(0, -99, 0, L"ERROR:MH_Initialize failed", 26*2);
        return;
    }

    g_mh_original_fn = NULL;
    LPVOID target = (LPVOID)cfg->hook_address;

    MH_STATUS st = MH_CreateHook(target, (LPVOID)text_hook_detour,
                                  (LPVOID *)&g_mh_original_fn);
    if (st != MH_OK) {
        char emsg[64];
        int len = _snprintf_s(emsg, sizeof(emsg), _TRUNCATE,
                              "ERROR:MH_CreateHook %d", (int)st);
        pipe_send_text(0, -99, 1, emsg, len > 0 ? len : 0);
        return;
    }

    MH_EnableHook(target);

    /* Signal ready */
    char msg[32];
    int len = _snprintf_s(msg, sizeof(msg), _TRUNCATE,
                          "hook_ready:%llx", (unsigned long long)cfg->hook_address);
    pipe_send_text(0, 0, 1, msg, len > 0 ? len : 0);

    /* Stay alive until pipe is closed by Python */
    while (g_pipe != INVALID_HANDLE_VALUE) {
        DWORD avail = 0;
        if (!PeekNamedPipe(g_pipe, NULL, 0, NULL, &avail, NULL)) break;
        Sleep(200);
    }

    MH_DisableHook(target);
    MH_Uninitialize();
}

/* =========================================================================
 * Worker thread — connects pipe and dispatches
 * ========================================================================= */

static DWORD WINAPI worker_thread(LPVOID param) {
    (void)param;

    /* Connect to Python Named Pipe (Python is server, we are client) */
    DWORD pid = GetCurrentProcessId();
    wchar_t pipe_name[64];
    _snwprintf_s(pipe_name, 64, _TRUNCATE, L"\\\\.\\pipe\\JRI-%lu", (unsigned long)pid);

    for (int retry = 0; retry < 50; retry++) {
        g_pipe = CreateFileW(pipe_name,
                              GENERIC_READ | GENERIC_WRITE,
                              0, NULL, OPEN_EXISTING, 0, NULL);
        if (g_pipe != INVALID_HANDLE_VALUE) break;
        Sleep(100);
    }

    if (g_pipe == INVALID_HANDLE_VALUE) return 1;

    /* Read config */
    Config cfg;
    if (!pipe_read_all(&cfg, sizeof(cfg))) {
        CloseHandle(g_pipe);
        g_pipe = INVALID_HANDLE_VALUE;
        return 1;
    }

    g_cfg = cfg;

    if (cfg.mode == MODE_SEARCH)
        do_search(&cfg);
    else
        do_hook(&cfg);

    CloseHandle(g_pipe);
    g_pipe = INVALID_HANDLE_VALUE;
    return 0;
}

/* =========================================================================
 * DLL entry point
 * ========================================================================= */

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    switch (reason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hDll);
        InitializeCriticalSection(&g_pipe_cs);
        g_slot_count = 0;
        g_stubs = NULL;
        g_pipe = INVALID_HANDLE_VALUE;
        /* Spawn worker thread — do NOT do I/O in DllMain */
        CreateThread(NULL, 0, worker_thread, NULL, 0, NULL);
        break;

    case DLL_PROCESS_DETACH:
        /* Unhook all search patches */
        if (g_stubs && g_slot_count > 0) {
            for (LONG i = 0; i < g_slot_count; i++) {
                HookSlot *s = &g_slots[i];
                DWORD old;
                if (VirtualProtect((void *)s->va, PATCH_BYTES,
                                    PAGE_EXECUTE_READWRITE, &old)) {
                    memcpy((void *)s->va, s->saved, PATCH_BYTES);
                    VirtualProtect((void *)s->va, PATCH_BYTES, old, &old);
                }
            }
            VirtualFree(g_stubs, 0, MEM_RELEASE);
        }
        DeleteCriticalSection(&g_pipe_cs);
        break;
    }
    return TRUE;
}
