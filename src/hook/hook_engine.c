/*
 * hook_engine.c
 * Injected DLL for JustReadIt hook discovery and text capture.
 *
 * Architecture follows Textractor's hookfinder.cc approach:
 *   - MODE_SEARCH uses MinHook (MH_QueueEnableHook / MH_ApplyQueued) for bulk
 *     hook installation.  Each hook gets its own copy of a per-trampoline
 *     template with the hook VA and "original" pointer baked in.  MinHook
 *     handles thread suspension and safe patching internally.
 *   - MODE_HOOK uses a single MinHook hook for the confirmed address.
 *
 * Two modes selected by Config.mode:
 *
 *   MODE_SEARCH (0) -- bulk hook search
 *       Scans .text for common x64 MSVC prologues.
 *       Each matching address is hooked via MH_CreateHook+MH_QueueEnableHook.
 *       The per-hook trampoline saves all GPRs, calls Send(stack, hook_va),
 *       then jumps to the original function via MinHook's relay.
 *       Send writes CJK UTF-16LE matches to the Named Pipe immediately.
 *
 *   MODE_HOOK (1) -- single confirmed hook via MinHook
 *       Installs one hook at Config.hook_address.  The detour reads the text
 *       argument via {arg_idx, deref, byte_offset, encoding} and writes to
 *       the pipe.
 *
 * Trampoline layout (x64, TRAMPOLINE_SIZE bytes):
 *   +0   push rflags + all 16 GPRs + save xmm4/xmm5
 *   +48  lea rcx, [rsp+0xa8]          ; stack pointer (entry RSP)
 *   +50  mov rdx, <hook_va>           ; ADDR_OFFSET -- patched per hook
 *   +60  mov rax, <Send>              ; SEND_OFFSET -- same for all
 *   +68  mov rbx,rsp / align / shadow / call / restore
 *   +88  restore xmm5/xmm4 + all GPRs + pop rflags
 *  +128  jmp qword ptr [rip+0]
 *  +134  <original ptr>               ; ORIG_OFFSET -- filled by MH_CreateHook
 *
 * Named Pipe protocol (unchanged):
 *   Python -> DLL: Config (24 bytes)
 *   DLL -> Python: ResultHdr (16 bytes) + text bytes; hook_va==0 = control msg
 *
 * Build (MSVC x64 Developer Command Prompt):
 *   cl /LD /O2 /GS- hook_engine.c /I <MinHook>/include
 *      /link <MinHook>/bin/MinHook.x64.lib psapi.lib /SUBSYSTEM:WINDOWS /DLL
 *
 * License: MPL-2.0 (this file)  +  BSD-2-Clause (MinHook, dynamically linked)
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include "MinHook.h"

#pragma comment(lib, "psapi.lib")

/* ===========================================================================
 * Constants
 * ========================================================================= */

#define MODE_SEARCH   0
#define MODE_HOOK     1

#define MAX_HOOKS     60000
#define MAX_STR_LEN   400
#define MIN_STR_LEN   2
#define SEND_REG_LO   16    /* GPRs saved below entry-RSP: rax..r15          */
#define SEND_STK_HI   9     /* stack slots above entry-RSP to probe          */

/*
 * x64 per-hook trampoline, 142 bytes.
 * Modelled after Textractor hookfinder.cc (x64 variant).
 *
 * Patchable offsets (patch after memcpy, all little-endian 64-bit values):
 *   ADDR_OFFSET  50  : 8-byte hook virtual address   (per hook)
 *   SEND_OFFSET  60  : 8-byte pointer to Send()      (same for all)
 *   ORIG_OFFSET  134 : 8-byte pointer to MinHook relay (per hook)
 *
 * stack[-16] = rax, stack[-15] = rbx, stack[-14] = rcx (arg0),
 * stack[-13] = rdx (arg1), stack[-8] = r8 (arg2), stack[-7] = r9 (arg3),
 * stack[0]   = return addr, stack[1..N] = caller stack frame.
 */
#define TRAMPOLINE_ADDR_OFFSET  50
#define TRAMPOLINE_SEND_OFFSET  60
#define TRAMPOLINE_ORIG_OFFSET  134
#define TRAMPOLINE_SIZE         142

/* ===========================================================================
 * Config / Result structs (must match Python pack layout exactly)
 * ========================================================================= */

#pragma pack(push, 1)

typedef struct Config_t {
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

typedef struct ResultHdr_t {
    uint64_t hook_va;
    int32_t  slot_i;
    uint16_t encoding;
    uint16_t text_len;
    /* followed by text_len bytes */
} ResultHdr;  /* 16 bytes */

#pragma pack(pop)

/* ===========================================================================
 * Global state
 * ========================================================================= */

static HANDLE           g_pipe        = INVALID_HANDLE_VALUE;
static Config           g_cfg;

/* ===========================================================================
 * Lock-free ring buffer -- decouples trampolines from pipe I/O.
 *
 * Send() (called from hooked game threads) does only CAS + memcpy: no locks,
 * no kernel calls, no blocking. The worker thread drains the ring to the
 * Named Pipe synchronously inside its 10ms sleep loop.
 *
 * State machine per slot: 0=empty  1=filling(claimed by writer)  2=ready
 * Drainer: atomically transitions 2→3 (draining), writes to pipe, sets 0.
 * ========================================================================= */
#define RING_SLOTS 4096     /* must be power of 2; ~3.2 MB total            */

typedef struct {
    volatile LONG  state;           /* 0=empty 1=filling 2=ready 3=draining */
    ResultHdr      hdr;
    uint8_t        text[MAX_STR_LEN * 2];
} RingSlot;

static RingSlot        g_ring[RING_SLOTS];
static volatile LONG   g_ring_seq = 0;  /* monotone write cursor             */

/* Search-mode tracking */
static uint64_t        *g_hook_addrs  = NULL;
static void            *g_trampolines = NULL;
static long             g_hook_count  = 0;

/* MinHook single-hook state */
typedef UINT64 (__fastcall *GenericFn)(UINT64, UINT64, UINT64, UINT64);
static GenericFn g_mh_original_fn = NULL;

/* Signature dedup cache -- prevents the same (address, slot, char-sig) tuple
 * from flooding the ring.  Uses InterlockedCompareExchange (CPU intrinsic,
 * no kernel call) so it is safe to call from inside hooked functions.      */
#define SIG_CACHE_SIZE 65521u   /* prime */
static volatile LONG g_sig_cache[SIG_CACHE_SIZE];

/* ===========================================================================
 * Pipe helpers
 * ========================================================================= */

static bool pipe_read_all(void *buf, DWORD len) {
    DWORD total = 0;
    while (total < len) {
        DWORD got = 0;
        if (!ReadFile(g_pipe, (char *)buf + total, len - total, &got, NULL)
            || got == 0)
            return false;
        total += got;
    }
    return true;
}

static void pipe_send_text(uintptr_t hook_va, int slot_i,
                            int encoding, const void *text, DWORD text_bytes) {
    if (g_pipe == INVALID_HANDLE_VALUE) return;
    if (text_bytes == 0 || text_bytes > MAX_STR_LEN * 2) return;

    uint8_t buf[sizeof(ResultHdr) + MAX_STR_LEN * 2];
    ResultHdr *hdr = (ResultHdr *)buf;
    hdr->hook_va  = (uint64_t)hook_va;
    hdr->slot_i   = (int32_t)slot_i;
    hdr->encoding = (uint16_t)encoding;
    hdr->text_len = (uint16_t)text_bytes;
    memcpy(buf + sizeof(ResultHdr), text, text_bytes);

    DWORD to_write = (DWORD)(sizeof(ResultHdr) + text_bytes);
    DWORD written  = 0;
    WriteFile(g_pipe, buf, to_write, &written, NULL);
    /* Only called from the worker thread (control messages), never from
     * trampolines -- no concurrent writers, no lock required.            */
}

/* ===========================================================================
 * Ring buffer push / drain
 * ========================================================================= */

/* Called from trampolines (any game thread): CAS-claim a slot, fill, mark
 * ready.  If the ring is saturated, the hit is silently dropped.           */
static void ring_push(uintptr_t hook_va, int slot_i,
                      int encoding, const void *text, DWORD text_bytes) {
    if (text_bytes == 0 || text_bytes > MAX_STR_LEN * 2) return;
    LONG seq = InterlockedIncrement(&g_ring_seq) - 1;
    RingSlot *s = &g_ring[seq & (RING_SLOTS - 1)];
    /* Claim slot; drop if still occupied by previous write cycle */
    if (InterlockedCompareExchange(&s->state, 1, 0) != 0) return;
    s->hdr.hook_va  = (uint64_t)hook_va;
    s->hdr.slot_i   = (int32_t)slot_i;
    s->hdr.encoding = (uint16_t)encoding;
    s->hdr.text_len = (uint16_t)text_bytes;
    memcpy(s->text, text, text_bytes);
    _WriteBarrier();
    s->state = 2;  /* visible to drainer */
}

/* Called from the worker thread: write all ready slots to the pipe, then
 * release them back to the pool.  Single drainer -- no concurrency.       */
static void ring_drain(void) {
    if (g_pipe == INVALID_HANDLE_VALUE) return;
    for (int i = 0; i < RING_SLOTS; i++) {
        RingSlot *s = &g_ring[i];
        if (s->state != 2) continue;
        if (InterlockedCompareExchange(&s->state, 3, 2) != 2) continue;
        uint8_t buf[sizeof(ResultHdr) + MAX_STR_LEN * 2];
        memcpy(buf, &s->hdr, sizeof(ResultHdr));
        memcpy(buf + sizeof(ResultHdr), s->text, s->hdr.text_len);
        DWORD to_write = (DWORD)(sizeof(ResultHdr) + s->hdr.text_len);
        DWORD written  = 0;
        WriteFile(g_pipe, buf, to_write, &written, NULL);
        _WriteBarrier();
        s->state = 0;
    }
}

/* ===========================================================================
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

/* ===========================================================================
 * Send -- called from every search-mode trampoline (any game thread).
 *
 * MUST NOT call any external DLL function -- only compiler intrinsics and
 * direct memory reads are safe here.  (External calls risk recursion if the
 * callee was itself hooked, and kernel calls block under high hook load.)
 *
 * Stack layout relative to `stack` (= entry RSP when trampoline fires):
 *   push order: pushfq, rax, rbx, rcx, rdx, rsp, rbp, rsi, rdi,
 *               r8, r9, r10, r11, r12, r13, r14, r15
 *   stack[ 0] = return address          (caller's return address)
 *   stack[-1] = rflags
 *   stack[-2] = rax
 *   stack[-3] = rbx
 *   stack[-4] = rcx   -- arg0 (r0)
 *   stack[-5] = rdx   -- arg1 (r1)
 *   stack[-6] = rsp
 *   stack[-7] = rbp
 *   stack[-8] = rsi
 *   stack[-9] = rdi
 *   stack[-10]= r8    -- arg2 (r2)
 *   stack[-11]= r9    -- arg3 (r3)
 *   stack[-12...-17] = r10-r15
 *   stack[1..N] = caller's stack frame slots
 * ========================================================================= */
void __cdecl Send(char **stack, uintptr_t address) {
    for (int i = -(int)SEND_REG_LO; i < (int)SEND_STK_HI; i++) {
        char *candidate = stack[i];
        if ((uintptr_t)candidate < 0x10000) continue;

        /* Read first 4 WCHARs under a single SEH frame (cheap) */
        WCHAR c0, c1, c2, c3;
        __try {
            c0 = ((const WCHAR *)candidate)[0];
            c1 = ((const WCHAR *)candidate)[1];
            c2 = ((const WCHAR *)candidate)[2];
            c3 = ((const WCHAR *)candidate)[3];
        } __except (EXCEPTION_EXECUTE_HANDLER) { continue; }

        if (!c0 || !c1) continue;

        /* Quick CJK pre-filter: at least one of the first two WCHARs must
         * be in CJK Unified / Hiragana / Katakana / Halfwidth range.     */
        if (!((c0 >= 0x3000 && c0 <= 0x9FFF) || (c0 >= 0xFF00 && c0 <= 0xFFEF) ||
              (c1 >= 0x3000 && c1 <= 0x9FFF) || (c1 >= 0xFF00 && c1 <= 0xFFEF)))
            continue;

        /* Signature dedup: hash (address, slot_i, c2, c3) to a cache slot.
         * InterlockedCompareExchange / InterlockedExchange compile to
         * LOCK CMPXCHG / LOCK XCHG -- no kernel call, no blocking.      */
        LONG sig = (LONG)(
            (address * 2654435761UL) ^
            ((ULONG)i  * 1234567891UL) ^
            ((ULONG)(c2 << 16) | (ULONG)c3));
        if (sig == 0) sig = 1;   /* 0 is the empty-slot sentinel */
        ULONG idx = (ULONG)((sig & 0x7FFFFFFFL) % SIG_CACHE_SIZE);
        if (InterlockedCompareExchange(&g_sig_cache[idx], sig, sig) == sig)
            continue;   /* identical signature seen before, skip */
        InterlockedExchange(&g_sig_cache[idx], sig);

        /* Full length scan under SEH */
        int wlen;
        __try {
            const WCHAR *p = (const WCHAR *)candidate;
            wlen = 0;
            while (wlen < MAX_STR_LEN && p[wlen]) wlen++;
        } __except (EXCEPTION_EXECUTE_HANDLER) { continue; }

        if (wlen < MIN_STR_LEN) continue;

        /* Full CJK density check */
        if (!has_cjk_w((const WCHAR *)candidate, wlen)) continue;

        ring_push(address, i, 0, candidate, (DWORD)(wlen * sizeof(WCHAR)));
    }
}

/* ===========================================================================
 * x64 trampoline template (TRAMPOLINE_SIZE = 142 bytes)
 *
 * Bytewise layout (offsets match TRAMPOLINE_*_OFFSET constants above):
 *
 *  +0   9c           pushfq
 *  +1   50           push rax
 *  +2   53           push rbx
 *  +3   51           push rcx
 *  +4   52           push rdx
 *  +5   54           push rsp
 *  +6   55           push rbp
 *  +7   56           push rsi
 *  +8   57           push rdi
 *  +9   41 50        push r8
 *  +11  41 51        push r9
 *  +13  41 52        push r10
 *  +15  41 53        push r11
 *  +17  41 54        push r12
 *  +19  41 55        push r13
 *  +21  41 56        push r14
 *  +23  41 57        push r15
 *  +25  48 83 EC 20  sub  rsp, 0x20       ; allocate space for xmm saves
 *  +29  F3 0F 7F 24 24        movdqu [rsp], xmm4
 *  +34  F3 0F 7F 6C 24 10     movdqu [rsp+0x10], xmm5
 *  +40  48 8D 8C 24 A8 00 00 00  lea rcx, [rsp+0xa8]  ; entry RSP
 *  +48  48 BA xx..xx  mov rdx, @addr       ; ADDR_OFFSET=50
 *  +58  48 B8 xx..xx  mov rax, @Send       ; SEND_OFFSET=60
 *  +68  48 89 E3      mov rbx, rsp
 *  +71  48 83 E4 F0   and rsp, -16         ; align
 *  +75  48 83 EC 28   sub rsp, 0x28        ; shadow(0x20)+align(0x8)
 *  +79  FF D0         call rax             ; Send(stack, hook_va)
 *  +81  48 83 C4 28   add rsp, 0x28
 *  +85  48 89 DC      mov rsp, rbx
 *  +88  F3 0F 6F 6C 24 10  movdqu xmm5, [rsp+0x10]
 *  +94  F3 0F 6F 24 24     movdqu xmm4, [rsp]
 *  +99  48 83 C4 20   add rsp, 0x20
 * +103  41 5F         pop r15
 * +105  41 5E         pop r14
 * +107  41 5D         pop r13
 * +109  41 5C         pop r12
 * +111  41 5B         pop r11
 * +113  41 5A         pop r10
 * +115  41 59         pop r9
 * +117  41 58         pop r8
 * +119  5F            pop rdi
 * +120  5E            pop rsi
 * +121  5D            pop rbp
 * +122  5C            pop rsp
 * +123  5A            pop rdx
 * +124  59            pop rcx
 * +125  5B            pop rbx
 * +126  58            pop rax
 * +127  9D            popfq
 * +128  FF 25 00 00 00 00  jmp qword ptr [rip+0]
 * +134  xx xx xx xx xx xx xx xx  @original  (ORIG_OFFSET=134)
 * ========================================================================= */

static const BYTE s_trampoline_template[TRAMPOLINE_SIZE] = {
    /* save rflags + 16 GPRs */
    0x9C,                                    /* +0   pushfq                         */
    0x50,                                    /* +1   push rax                       */
    0x53,                                    /* +2   push rbx                       */
    0x51,                                    /* +3   push rcx                       */
    0x52,                                    /* +4   push rdx                       */
    0x54,                                    /* +5   push rsp                       */
    0x55,                                    /* +6   push rbp                       */
    0x56,                                    /* +7   push rsi                       */
    0x57,                                    /* +8   push rdi                       */
    0x41,0x50,                               /* +9   push r8                        */
    0x41,0x51,                               /* +11  push r9                        */
    0x41,0x52,                               /* +13  push r10                       */
    0x41,0x53,                               /* +15  push r11                       */
    0x41,0x54,                               /* +17  push r12                       */
    0x41,0x55,                               /* +19  push r13                       */
    0x41,0x56,                               /* +21  push r14                       */
    0x41,0x57,                               /* +23  push r15                       */
    /* save xmm4/xmm5 (caller-save high XMMs) */
    0x48,0x83,0xEC,0x20,                     /* +25  sub  rsp, 0x20                 */
    0xF3,0x0F,0x7F,0x24,0x24,               /* +29  movdqu [rsp], xmm4             */
    0xF3,0x0F,0x7F,0x6C,0x24,0x10,          /* +34  movdqu [rsp+0x10], xmm5        */
    /* set up Send(stack=entry_rsp, address=hook_va) */
    0x48,0x8D,0x8C,0x24,0xA8,0x00,0x00,0x00,/* +40  lea rcx,[rsp+0xa8]             */
    0x48,0xBA, 0,0,0,0,0,0,0,0,             /* +48  mov rdx, @addr   [data@+50]    */
    0x48,0xB8, 0,0,0,0,0,0,0,0,             /* +58  mov rax, @Send   [data@+60]    */
    /* align stack, add shadow space (0x28 keeps 16-byte align at call entry) */
    0x48,0x89,0xE3,                          /* +68  mov rbx, rsp                   */
    0x48,0x83,0xE4,0xF0,                     /* +71  and rsp, -16                   */
    0x48,0x83,0xEC,0x28,                     /* +75  sub rsp, 0x28                  */
    0xFF,0xD0,                               /* +79  call rax                       */
    0x48,0x83,0xC4,0x28,                     /* +81  add rsp, 0x28                  */
    0x48,0x89,0xDC,                          /* +85  mov rsp, rbx                   */
    /* restore xmm4/xmm5 */
    0xF3,0x0F,0x6F,0x6C,0x24,0x10,          /* +88  movdqu xmm5, [rsp+0x10]        */
    0xF3,0x0F,0x6F,0x24,0x24,               /* +94  movdqu xmm4, [rsp]             */
    0x48,0x83,0xC4,0x20,                     /* +99  add rsp, 0x20                  */
    /* restore GPRs (reverse order) */
    0x41,0x5F,                               /* +103 pop r15                        */
    0x41,0x5E,                               /* +105 pop r14                        */
    0x41,0x5D,                               /* +107 pop r13                        */
    0x41,0x5C,                               /* +109 pop r12                        */
    0x41,0x5B,                               /* +111 pop r11                        */
    0x41,0x5A,                               /* +113 pop r10                        */
    0x41,0x59,                               /* +115 pop r9                         */
    0x41,0x58,                               /* +117 pop r8                         */
    0x5F,                                    /* +119 pop rdi                        */
    0x5E,                                    /* +120 pop rsi                        */
    0x5D,                                    /* +121 pop rbp                        */
    0x5C,                                    /* +122 pop rsp                        */
    0x5A,                                    /* +123 pop rdx                        */
    0x59,                                    /* +124 pop rcx                        */
    0x5B,                                    /* +125 pop rbx                        */
    0x58,                                    /* +126 pop rax                        */
    0x9D,                                    /* +127 popfq                          */
    /* jump to original function via MinHook relay */
    0xFF,0x25,0x00,0x00,0x00,0x00,           /* +128 jmp qword ptr [rip+0]          */
    0,0,0,0,0,0,0,0                          /* +134 @original  (ORIG_OFFSET=134)   */
};

/* ===========================================================================
 * SEARCH MODE
 * ========================================================================= */

/* Prologue filter -- only unambiguous multi-byte sequences that can only
 * appear at a real function entry, not inside a function body.            */
static bool is_prologue(const uint8_t *b) {
    /* sub rsp, imm8  (48 83 EC xx) */
    if (b[0]==0x48 && b[1]==0x83 && b[2]==0xEC) return true;
    /* sub rsp, imm32 (48 81 EC xx xx xx xx) */
    if (b[0]==0x48 && b[1]==0x81 && b[2]==0xEC) return true;
    /* push rbp; mov rbp, rsp  (55 48 89 E5) */
    if (b[0]==0x55 && b[1]==0x48 && b[2]==0x89 && b[3]==0xE5) return true;
    /* push rbp; mov rbp, rsp  -- REX variant (55 48 8B EC) */
    if (b[0]==0x55 && b[1]==0x48 && b[2]==0x8B && b[3]==0xEC) return true;
    return false;
}

/* Allocate executable memory within ±1.8 GB of `target` so MinHook's
 * 32-bit relative JMP can always reach our trampolines.                  */
static BYTE *alloc_near(uintptr_t target, size_t size) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    /* Search window: ±0x70000000 (safely within the ±0x7FFFFFFF limit)   */
    uintptr_t lo = (target > 0x70000000ULL) ? target - 0x70000000ULL
                                             : (uintptr_t)si.lpMinimumApplicationAddress;
    uintptr_t hi = target + 0x70000000ULL;
    if (hi > (uintptr_t)si.lpMaximumApplicationAddress)
        hi = (uintptr_t)si.lpMaximumApplicationAddress;
    for (uintptr_t addr = lo; addr < hi; addr += si.dwAllocationGranularity) {
        BYTE *p = (BYTE *)VirtualAlloc((LPVOID)addr, size,
                                        MEM_COMMIT | MEM_RESERVE,
                                        PAGE_EXECUTE_READWRITE);
        if (p) return p;
    }
    return NULL;
}

static void do_search(const Config *cfg) {
    if (MH_Initialize() != MH_OK) {
        pipe_send_text(0, 0, 1, "ERROR:MH_Initialize failed", 26);
        return;
    }

    /* Bake Send address into our local template copy */
    BYTE tpl[TRAMPOLINE_SIZE];
    memcpy(tpl, s_trampoline_template, TRAMPOLINE_SIZE);
    *(void **)(tpl + TRAMPOLINE_SEND_OFFSET) = (void *)Send;

    /* Locate .text section of game EXE */
    HMODULE hmod = GetModuleHandleW(NULL);
    MODULEINFO mi = {0};
    if (!GetModuleInformation(GetCurrentProcess(), hmod, &mi, sizeof(mi))) {
        pipe_send_text(0, 0, 1, "ERROR:GetModuleInformation failed", 33);
        MH_Uninitialize();
        return;
    }

    uintptr_t base = (uintptr_t)mi.lpBaseOfDll;
    IMAGE_DOS_HEADER  *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS  *nt  = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_SECTION_HEADER *sec = IMAGE_FIRST_SECTION(nt);

    uintptr_t text_start = 0, text_size = 0;
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; i++) {
        if (memcmp(sec[i].Name, ".text", 5) == 0) {
            text_start = base + sec[i].VirtualAddress;
            text_size  = sec[i].Misc.VirtualSize;
            break;
        }
    }
    if (!text_start) { text_start = base; text_size = mi.SizeOfImage; }

    DWORD max_hooks = cfg->max_hooks > 0 ? cfg->max_hooks : MAX_HOOKS;
    if (max_hooks > MAX_HOOKS) max_hooks = MAX_HOOKS;

    /* Allocate RWX block for N trampoline copies, near .text so MinHook's
     * 32-bit relative relay can reach our detours (±2 GB requirement).   */
    size_t tramp_block_size = (size_t)max_hooks * TRAMPOLINE_SIZE;
    BYTE *trampolines = alloc_near(text_start, tramp_block_size);
    if (!trampolines) {
        pipe_send_text(0, 0, 1, "ERROR:trampoline alloc failed", 29);
        MH_Uninitialize();
        return;
    }

    uint64_t *hook_addrs = (uint64_t *)HeapAlloc(
        GetProcessHeap(), 0, (size_t)max_hooks * sizeof(uint64_t));
    if (!hook_addrs) {
        VirtualFree(trampolines, 0, MEM_RELEASE);
        MH_Uninitialize();
        return;
    }

    long hooked = 0;
    const uint8_t *text_ptr = (const uint8_t *)text_start;

    for (size_t off = 0;
         off + 16 < text_size && hooked < (long)max_hooks;
         off++) {   /* byte-by-byte: only real function entries match */


        const uint8_t *p = text_ptr + off;
        LPVOID target = (LPVOID)p;

        __try { if (!is_prologue(p)) continue; }
        __except (EXCEPTION_EXECUTE_HANDLER) { continue; }

        BYTE  *tramp    = trampolines + (size_t)hooked * TRAMPOLINE_SIZE;
        void  *original = NULL;

        /* Copy template; patch hook VA */
        memcpy(tramp, tpl, TRAMPOLINE_SIZE);
        *(uintptr_t *)(tramp + TRAMPOLINE_ADDR_OFFSET) = (uintptr_t)target;

        /* MinHook validates the target with its own disassembler */
        if (MH_CreateHook(target, (LPVOID)tramp, &original) != MH_OK)
            continue;

        /* Patch original relay and queue */
        *(void **)(tramp + TRAMPOLINE_ORIG_OFFSET) = original;
        MH_QueueEnableHook(target);
        hook_addrs[hooked] = (uint64_t)(uintptr_t)target;
        hooked++;
    }

    /* Atomic batch install: MinHook suspends threads internally */
    MH_ApplyQueued();

    g_hook_addrs  = hook_addrs;
    g_trampolines = trampolines;
    g_hook_count  = hooked;

    /* Make trampolines read+execute only now that patching is done */
    DWORD dummy;
    VirtualProtect(trampolines, tramp_block_size, PAGE_EXECUTE_READ, &dummy);

    /* Notify Python */
    char msg[64];
    int mlen = _snprintf_s(msg, sizeof(msg), _TRUNCATE,
                           "scan_done:%ld", (long)hooked);
    pipe_send_text(0, 0, 1, msg, mlen > 0 ? (DWORD)mlen : 0);

    /* Stay alive, draining ring→pipe until Python closes the pipe */
    while (g_pipe != INVALID_HANDLE_VALUE) {
        DWORD avail = 0;
        if (!PeekNamedPipe(g_pipe, NULL, 0, NULL, &avail, NULL)) break;
        ring_drain();
        Sleep(10);
    }
    ring_drain();  /* final pass before uninstalling hooks */

    /* Disable and remove all search hooks */
    for (long i = 0; i < hooked; i++)
        MH_QueueDisableHook((LPVOID)(uintptr_t)hook_addrs[i]);
    MH_ApplyQueued();
    Sleep(500);   /* let in-flight callsites drain */
    for (long i = 0; i < hooked; i++)
        MH_RemoveHook((LPVOID)(uintptr_t)hook_addrs[i]);

    VirtualFree(trampolines, 0, MEM_RELEASE);
    HeapFree(GetProcessHeap(), 0, hook_addrs);
    g_hook_addrs  = NULL;
    g_trampolines = NULL;
    g_hook_count  = 0;

    MH_Uninitialize();
}

/* ===========================================================================
 * HOOK MODE -- single confirmed address via MinHook
 * ========================================================================= */

static UINT64 __fastcall text_hook_detour(UINT64 a0, UINT64 a1,
                                           UINT64 a2, UINT64 a3) {
    UINT64 args[4] = { a0, a1, a2, a3 };
    const Config *c = &g_cfg;
    uintptr_t ptr = 0;

    if (c->arg_idx < 4)
        ptr = (uintptr_t)args[c->arg_idx];
    if (c->byte_offset)
        ptr += c->byte_offset;
    if (c->deref)
        __try { ptr = *(uintptr_t *)ptr; }
        __except (EXCEPTION_EXECUTE_HANDLER) { ptr = 0; }

    if (ptr && c->encoding == 0 && ptr >= 0x10000) {
        int wlen = 0;
        __try {
            const WCHAR *p = (const WCHAR *)ptr;
            while (wlen < MAX_STR_LEN && p[wlen]) wlen++;
        } __except (EXCEPTION_EXECUTE_HANDLER) { wlen = 0; }
        if (wlen >= MIN_STR_LEN && has_cjk_w((const WCHAR *)ptr, wlen))
            pipe_send_text(c->hook_address, (int)c->arg_idx, 0,
                           (const WCHAR *)ptr, (DWORD)(wlen * 2));
    }

    return g_mh_original_fn ? g_mh_original_fn(a0, a1, a2, a3) : 0;
}

static void do_hook(const Config *cfg) {
    if (MH_Initialize() != MH_OK) {
        pipe_send_text(0, 0, 1, "ERROR:MH_Initialize failed", 26);
        return;
    }

    g_mh_original_fn = NULL;
    LPVOID target = (LPVOID)(uintptr_t)cfg->hook_address;

    MH_STATUS st = MH_CreateHook(target, (LPVOID)text_hook_detour,
                                  (LPVOID *)&g_mh_original_fn);
    if (st != MH_OK) {
        char emsg[64];
        int elen = _snprintf_s(emsg, sizeof(emsg), _TRUNCATE,
                               "ERROR:MH_CreateHook %d", (int)st);
        pipe_send_text(0, 0, 1, emsg, elen > 0 ? (DWORD)elen : 0);
        MH_Uninitialize();
        return;
    }
    MH_EnableHook(target);

    char msg[40];
    int mlen = _snprintf_s(msg, sizeof(msg), _TRUNCATE,
                           "hook_ready:%llx",
                           (unsigned long long)cfg->hook_address);
    pipe_send_text(0, 0, 1, msg, mlen > 0 ? (DWORD)mlen : 0);

    while (g_pipe != INVALID_HANDLE_VALUE) {
        DWORD avail = 0;
        if (!PeekNamedPipe(g_pipe, NULL, 0, NULL, &avail, NULL)) break;
        Sleep(200);
    }

    MH_DisableHook(target);
    MH_RemoveHook(target);
    MH_Uninitialize();
}

/* ===========================================================================
 * Worker thread
 * ========================================================================= */

static DWORD WINAPI worker_thread(LPVOID param) {
    (void)param;

    wchar_t pipe_name[64];
    _snwprintf_s(pipe_name, 64, _TRUNCATE,
                 L"\\\\.\\pipe\\JRI-%lu",
                 (unsigned long)GetCurrentProcessId());

    for (int retry = 0; retry < 50; retry++) {
        g_pipe = CreateFileW(pipe_name,
                              GENERIC_READ | GENERIC_WRITE,
                              0, NULL, OPEN_EXISTING, 0, NULL);
        if (g_pipe != INVALID_HANDLE_VALUE) break;
        Sleep(100);
    }
    if (g_pipe == INVALID_HANDLE_VALUE) return 1;

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

/* ===========================================================================
 * DLL entry point
 * ========================================================================= */

BOOL WINAPI DllMain(HINSTANCE hDll, DWORD reason, LPVOID reserved) {
    (void)reserved;
    switch (reason) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hDll);
        g_pipe = INVALID_HANDLE_VALUE;
        CreateThread(NULL, 0, worker_thread, NULL, 0, NULL);
        break;

    case DLL_PROCESS_DETACH:
        if (g_hook_count > 0 && g_hook_addrs) {
            for (long i = 0; i < g_hook_count; i++)
                MH_QueueDisableHook((LPVOID)(uintptr_t)g_hook_addrs[i]);
            MH_ApplyQueued();
        }
        if (g_trampolines) VirtualFree(g_trampolines, 0, MEM_RELEASE);
        if (g_hook_addrs)  HeapFree(GetProcessHeap(), 0, g_hook_addrs);
        break;
    }
    return TRUE;
}
