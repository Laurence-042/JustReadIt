/*
 * The hook-search concept (bulk function hooking + stack-frame string
 * sniffing for VN text extraction) was pioneered by Textractor (GPL-3.0).
 * This file is an independent implementation with a different architecture.
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

#define MODE_SEARCH   0
#define MODE_HOOK     1
#define MAX_HOOKS     200000  /* hard safety ceiling; 0 in config means scan all pdata */
#define MAX_STR_LEN   400
#define MIN_STR_LEN   2
#define SEND_REG_LO   16
#define SEND_STK_HI   9

/* Trampoline byte offsets */
#define TRAMPOLINE_ADDR_OFFSET  50
#define TRAMPOLINE_SEND_OFFSET  60
#define TRAMPOLINE_ORIG_OFFSET  134
#define TRAMPOLINE_SIZE         142

#pragma pack(push, 1)
typedef struct { uint8_t mode; uint8_t _p0[3]; uint32_t max_hooks;
                 uint64_t hook_address; uint8_t arg_idx; uint8_t deref;
                 uint16_t byte_offset; uint16_t encoding;
                 uint16_t batch_size;   /* MODE_SEARCH: hooks per batch (0 = all at once) */ } Config;
typedef struct { uint64_t hook_va; int32_t slot_i;
                 uint16_t encoding; uint16_t text_len; } ResultHdr;
#pragma pack(pop)

static void log_phase(const char *msg);  /* forward decl */

static HANDLE        g_pipe       = INVALID_HANDLE_VALUE;
static Config        g_cfg;
static uint64_t     *g_hook_addrs = NULL;
static void         *g_trampolines= NULL;
static long          g_hook_count = 0;

typedef UINT64 (__fastcall *GenericFn)(UINT64,UINT64,UINT64,UINT64);
static GenericFn g_orig = NULL;

/* ---- batch-scan state (set once in do_search, used by scan_next_batch) ---- */
static RUNTIME_FUNCTION *g_pdata       = NULL;
static DWORD             g_pdata_count = 0;
static DWORD             g_pdata_pos   = 0;   /* next .pdata entry to process  */
static uintptr_t         g_img_base    = 0;
static uintptr_t         g_ts_base     = 0;   /* .text section VA              */
static uintptr_t         g_ts_size     = 0;
static BYTE              g_tpl[TRAMPOLINE_SIZE]; /* Send-patched template       */
static DWORD             g_mh          = 0;   /* total hook capacity           */

/* ---- ring buffer ---- */
#define RING_SLOTS 4096
typedef struct { volatile LONG state; ResultHdr hdr;
                 uint8_t text[MAX_STR_LEN*2]; } RingSlot;
static RingSlot      g_ring[RING_SLOTS];
static volatile LONG g_ring_seq = 0;

/* ---- sig dedup ---- */
#define SIG_CACHE_SIZE 65521u
static volatile LONG g_sig_cache[SIG_CACHE_SIZE];

/* ---- per-address call-rate suppressor ----
 * Each entry counts raw invocations of a given address (hashed).
 * Once the count exceeds SEND_CALL_LIMIT the slot is marked
 * SUPPRESSED (0x7FFFFFFF) and Send() returns immediately without
 * doing any memory scanning.  No thread suspension required.
 * Hash collisions can silence a second address that shares the
 * slot -- acceptable: a slot holds ~7000 unique addresses on
 * average across 60000 hooks, so collisions are rare.
 */
#define CALL_RATE_SIZE   65521u
#define SEND_CALL_LIMIT  150        /* raw calls before auto-disable        */
#define SUPPRESSED       0x7FFFFFFF
static volatile LONG g_call_rate[CALL_RATE_SIZE];
static volatile LONG g_need_apply = 0; /* set in Send(), consumed in ring_drain() */

/* ---- pending-disable queue (game threads → worker thread only) ----------
 *
 * CRITICAL: MH_QueueDisableHook / MH_ApplyQueued are NOT thread-safe.
 * Game threads must NEVER call them directly.  Instead they write the
 * target VA into g_pdisable[], and the worker thread (ring_drain) drains
 * the buffer and calls the MinHook APIs single-threadedly.
 *
 * Sizing: PDISABLE_SLOTS must be a power of 2.  4096 slots × 8 B = 32 kB.
 * In the worst case a full batch of 500 addresses hits the limit between
 * two consecutive ring_drain() calls (10 ms apart) -- comfortably fits.
 * Overflow is safe: the producer wraps around and overwrites an already-
 * queued (non-zero) slot, which is just a harmless duplicate disable.
 */
#define PDISABLE_SLOTS   4096u      /* must be power of 2                  */
#define PDISABLE_MASK    (PDISABLE_SLOTS - 1)
static volatile LONG64 g_pdisable[PDISABLE_SLOTS]; /* 0 = empty, else VA  */
static volatile LONG   g_pdisable_seq = 0;         /* rolling producer idx */

/* ================================================================
 * pipe helpers (worker thread only -- no concurrency)
 * ============================================================= */
static bool pipe_read_all(void *buf, DWORD len) {
    for (DWORD t=0; t<len;) {
        DWORD got=0;
        if (!ReadFile(g_pipe,(char*)buf+t,len-t,&got,NULL)||!got) return false;
        t+=got;
    }
    return true;
}

static void pipe_write(uintptr_t va, int slot, int enc,
                       const void *txt, DWORD nb) {
    if (g_pipe==INVALID_HANDLE_VALUE||!nb||nb>MAX_STR_LEN*2) return;
    uint8_t buf[sizeof(ResultHdr)+MAX_STR_LEN*2];
    ResultHdr *h=(ResultHdr*)buf;
    h->hook_va=(uint64_t)va; h->slot_i=(int32_t)slot;
    h->encoding=(uint16_t)enc; h->text_len=(uint16_t)nb;
    memcpy(buf+sizeof(ResultHdr),txt,nb);
    DWORD wr=0;
    WriteFile(g_pipe,buf,(DWORD)(sizeof(ResultHdr)+nb),&wr,NULL);
}

/* ================================================================
 * ring push (any thread, lock-free)
 * ============================================================= */
static void ring_push(uintptr_t va, int slot, int enc,
                      const void *txt, DWORD nb) {
    if (!nb||nb>MAX_STR_LEN*2) return;
    LONG seq=InterlockedIncrement(&g_ring_seq)-1;
    RingSlot *s=&g_ring[seq&(RING_SLOTS-1)];
    if (InterlockedCompareExchange(&s->state,1,0)!=0) return;
    s->hdr.hook_va=(uint64_t)va; s->hdr.slot_i=(int32_t)slot;
    s->hdr.encoding=(uint16_t)enc; s->hdr.text_len=(uint16_t)nb;
    memcpy(s->text,txt,nb);
    _WriteBarrier(); s->state=2;
}

static void ring_drain(void) {
    if (g_pipe==INVALID_HANDLE_VALUE) return;
    /* Apply any queued disables.
     * All MinHook API calls happen HERE, on the worker thread only.
     * Game threads only write addresses into g_pdisable[]; they never
     * touch MinHook directly.  This eliminates the MinHook thread-safety
     * issue that caused game crashes under heavy concurrent hook firing.
     */
    if (g_trampolines &&
        InterlockedCompareExchange(&g_need_apply, 0, 1) == 1) {
        /* Drain the pending-disable ring: call MH_QueueDisableHook for
         * every non-zero slot, then clear it. */
        bool any = false;
        for (ULONG i = 0; i < PDISABLE_SLOTS; i++) {
            LONG64 va = InterlockedExchange64(&g_pdisable[i], 0);
            if (va) {
                MH_QueueDisableHook((LPVOID)(uintptr_t)va);
                any = true;
            }
        }
        if (any) {
            /* MH_ApplyQueued patches TARGET function prologues (in game .text),
             * NOT the trampoline buffer.  MinHook suspends all threads
             * internally before patching.  The trampoline buffer stays RX.
             * DO NOT VirtualProtect the trampoline buffer here: changing
             * page protection on a buffer that game threads are actively
             * executing causes 0xC0000005 access violations on those threads.
             */
            MH_ApplyQueued();
        }
    }
    /* Cap iterations per call so the command-check loop stays responsive
     * even when many hooks are firing.  Unflushed slots are picked up on
     * the next call (next Sleep(10) tick). */
    int limit = 256;
    for (int i=0;i<RING_SLOTS && limit>0;i++) {
        RingSlot *s=&g_ring[i];
        if (s->state!=2) continue;
        if (InterlockedCompareExchange(&s->state,3,2)!=2) continue;
        uint8_t buf[sizeof(ResultHdr)+MAX_STR_LEN*2];
        memcpy(buf,&s->hdr,sizeof(ResultHdr));
        memcpy(buf+sizeof(ResultHdr),s->text,s->hdr.text_len);
        DWORD wr=0;
        WriteFile(g_pipe,buf,(DWORD)(sizeof(ResultHdr)+s->hdr.text_len),&wr,NULL);
        _WriteBarrier(); s->state=0;
        limit--;
    }
}

/* ================================================================
 * CJK check
 * ============================================================= */
static bool has_cjk(const WCHAR *p, int n) {
    for (int i=0;i<n;i++) {
        WCHAR c=p[i];
        if ((c>=0x3000&&c<=0x9FFF)||(c>=0xFF00&&c<=0xFFEF)) return true;
    }
    return false;
}

/* ================================================================
 * Send  -- called from every trampoline
 *
 * Rules:
 *  1. NO external DLL calls (risk of recursion / kernel-call latency).
 *     Only compiler intrinsics (InterlockedXxx, _WriteBarrier, memcpy).
 *  2. All memory reads from game pointers MUST be inside __try/__except.
 *  3. The __try that covers memcpy into the ring slot is inside Send,
 *     so its .pdata frame covers the copy -- no unhandled fault escapes
 *     to the trampoline frame (which has no .pdata).
 * ============================================================= */
void __cdecl Send(char **stack, uintptr_t address) {
    /* ---- 0. per-address call-rate gate ---- */
    {
        ULONG ci = (ULONG)((address * 2654435761ULL) % CALL_RATE_SIZE);
        LONG cur = g_call_rate[ci];
        if (cur >= SEND_CALL_LIMIT) return;
        LONG nxt = InterlockedIncrement(&g_call_rate[ci]);
        if (nxt == SEND_CALL_LIMIT) {
            /* First thread to hit the limit for this slot.
             * DO NOT call MH_QueueDisableHook here -- MinHook is NOT
             * thread-safe and multiple game threads may reach this
             * branch concurrently for different addresses, corrupting
             * MinHook's internal state.  Instead, post the VA into the
             * lock-free pending-disable ring; the worker thread drains
             * it and calls MinHook APIs single-threadedly.
             */
            LONG slot = InterlockedIncrement(&g_pdisable_seq) & (LONG)PDISABLE_MASK;
            InterlockedExchange64(&g_pdisable[slot], (LONG64)(uintptr_t)address);
            InterlockedExchange(&g_need_apply, 1);
        }
        if (nxt >= SEND_CALL_LIMIT) return;
    }

    for (int i=-(int)SEND_REG_LO; i<(int)SEND_STK_HI; i++) {

        /* ---- 1. read the candidate pointer ---- */
        uintptr_t val;
        __try { val=(uintptr_t)stack[i]; }
        __except(EXCEPTION_EXECUTE_HANDLER) { continue; }

        if (val < 0x10000 || val > 0x000F000000000000ULL) continue;

        /* ---- 2. quick peek: read first 4 WCHARs ---- */
        WCHAR c0,c1,c2,c3;
        __try {
            const WCHAR *p=(const WCHAR *)val;
            c0=p[0]; c1=p[1]; c2=p[2]; c3=p[3];
        } __except(EXCEPTION_EXECUTE_HANDLER) { continue; }

        if (!c0||!c1) continue;

        /* ---- 3. quick CJK pre-filter (check first 4 chars) ----
         * Some VN strings start with non-CJK punctuation, e.g. U+2026
         * HORIZONTAL ELLIPSIS ("……それでも…"), where c0 and c1 are both
         * 0x2026 (not in the CJK block) but c2/c3 contain actual kana.
         * Checking all four already-read chars avoids false rejection. */
#define _IS_CJK(c) (((c)>=0x3000&&(c)<=0x9FFF)||((c)>=0xFF00&&(c)<=0xFFEF))
        if (!_IS_CJK(c0)&&!_IS_CJK(c1)&&!_IS_CJK(c2)&&!_IS_CJK(c3))
            continue;
#undef _IS_CJK

        /* ---- 4. dedup by (address, slot, c2, c3) ---- */
        LONG sig=(LONG)(
            (address*2654435761UL)^
            ((ULONG)(i+32)*1234567891UL)^
            ((ULONG)(c2<<16)|(ULONG)c3));
        if (!sig) sig=1;
        ULONG idx=(ULONG)((ULONG)sig % SIG_CACHE_SIZE);
        if (InterlockedCompareExchange(&g_sig_cache[idx],sig,sig)==sig)
            continue;
        InterlockedExchange(&g_sig_cache[idx],sig);

        /* ---- 5. copy string into a local buffer (inside __try) ---- */
        /*
         * KEY FIX: the memcpy is inside __try so that if the game frees
         * the buffer between step 2 and now, the fault is caught here
         * (inside Send's .pdata frame) rather than escaping to the
         * trampoline (which has no .pdata, causing RtlFailFast).
         */
        WCHAR local[MAX_STR_LEN];
        int wlen=0;
        __try {
            const WCHAR *p=(const WCHAR *)val;
            while (wlen<MAX_STR_LEN && p[wlen]) { local[wlen]=p[wlen]; wlen++; }
        } __except(EXCEPTION_EXECUTE_HANDLER) {
            if (wlen<MIN_STR_LEN) continue;
            /* use what we managed to copy */
        }
        if (wlen<MIN_STR_LEN) continue;

        /* ---- 6. full CJK check on the local copy ---- */
        if (!has_cjk(local,wlen)) continue;

        /* ---- 7. push to ring (memcpy from local stack -- always safe) ---- */
        ring_push(address, i, 0, local, (DWORD)(wlen*sizeof(WCHAR)));
    }
}

/* ================================================================
 * Trampoline template  (same bytes as before -- verified correct)
 * ============================================================= */
static const BYTE s_tpl[TRAMPOLINE_SIZE] = {
    0x9C,
    0x50,0x53,0x51,0x52,0x54,0x55,0x56,0x57,
    0x41,0x50,0x41,0x51,0x41,0x52,0x41,0x53,
    0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,
    0x48,0x83,0xEC,0x20,
    0xF3,0x0F,0x7F,0x24,0x24,
    0xF3,0x0F,0x7F,0x6C,0x24,0x10,
    0x48,0x8D,0x8C,0x24,0xA8,0x00,0x00,0x00,
    0x48,0xBA, 0,0,0,0,0,0,0,0,   /* +50: @addr  */
    0x48,0xB8, 0,0,0,0,0,0,0,0,   /* +60: @Send  */
    0x48,0x89,0xE3,
    0x48,0x83,0xE4,0xF0,
    0x48,0x83,0xEC,0x28,
    0xFF,0xD0,
    0x48,0x83,0xC4,0x28,
    0x48,0x89,0xDC,
    0xF3,0x0F,0x6F,0x6C,0x24,0x10,
    0xF3,0x0F,0x6F,0x24,0x24,
    0x48,0x83,0xC4,0x20,
    0x41,0x5F,0x41,0x5E,0x41,0x5D,0x41,0x5C,
    0x41,0x5B,0x41,0x5A,0x41,0x59,0x41,0x58,
    0x5F,0x5E,0x5D,0x5C,0x5A,0x59,0x5B,0x58,
    0x9D,
    0xFF,0x25,0x00,0x00,0x00,0x00,
    0,0,0,0,0,0,0,0               /* +134: @original */
};

/* ================================================================
 * alloc_near: VirtualAlloc within ±1.75 GB of target
 * ============================================================= */
static BYTE *alloc_near(uintptr_t target, size_t size) {
    SYSTEM_INFO si; GetSystemInfo(&si);
    uintptr_t lo=(target>0x68000000ULL)?target-0x68000000ULL
                                       :(uintptr_t)si.lpMinimumApplicationAddress;
    uintptr_t hi=target+0x68000000ULL;
    if (hi>(uintptr_t)si.lpMaximumApplicationAddress)
        hi=(uintptr_t)si.lpMaximumApplicationAddress;
    /* Try ABOVE target first so that RVAs (allocation - target) are
     * small positive values and always fit in a DWORD. */
    for (uintptr_t a=target; a<hi; a+=si.dwAllocationGranularity) {
        BYTE *p=(BYTE*)VirtualAlloc((LPVOID)a,size,
                                    MEM_COMMIT|MEM_RESERVE,
                                    PAGE_EXECUTE_READWRITE);
        if (p) return p;
    }
    /* Fall back to below target (RVAs wrap as unsigned DWORD but still
     * fit; caller must treat UnwindData as unsigned). */
    for (uintptr_t a=lo; a<target; a+=si.dwAllocationGranularity) {
        BYTE *p=(BYTE*)VirtualAlloc((LPVOID)a,size,
                                    MEM_COMMIT|MEM_RESERVE,
                                    PAGE_EXECUTE_READWRITE);
        if (p) return p;
    }
    return NULL;
}

/* ================================================================
 * Register trampoline block with the PE exception directory
 * so x64 SEH unwinding can traverse the trampoline frames.
 *
 * Each trampoline is treated as a single "function" with no
 * prologue unwind info (UWOP count = 0).  This is safe because:
 *   - The trampoline saves/restores all registers manually.
 *   - If an exception occurs INSIDE the trampoline (before Send
 *     is called), we want the default unwind: pop nothing, just
 *     propagate -- which is exactly what an empty UNWIND_INFO does.
 *   - Exceptions inside Send are caught by Send's own handler.
 * ============================================================= */
typedef struct {
    BYTE Version_Flags;   /* Version:3, Flags:5 */
    BYTE SizeOfProlog;
    BYTE CountOfCodes;
    BYTE FrameRegister_Offset;
} UNWIND_INFO_MINIMAL;

typedef struct {
    RUNTIME_FUNCTION      rf;
    UNWIND_INFO_MINIMAL   ui;
} TrampolineUnwindEntry;

static TrampolineUnwindEntry *g_unwind_table = NULL;
static DWORD                  g_unwind_count  = 0;

/* init_unwind_table: allocate and pre-fill the unwind table for the FULL
 * trampoline capacity, then register it ONCE with RtlAddFunctionTable.
 *
 * Registering the full capacity upfront (even for slots not yet written)
 * is safe because:
 *   - The trampoline buffer is already allocated (RWX → RX later).
 *   - UnwindData for each slot points to the matching embedded ui field
 *     inside g_unwind_table itself (alloc_near ensures it is within ±2 GB
 *     of g_trampolines so UnwindData fits in a DWORD).
 *   - Slots not yet populated have CountOfCodes=0 so unwinding them is a
 *     no-op; they will never be reached until scan_next_batch writes the
 *     real trampoline bytes and MinHook enables the hook.
 *
 * Critically, we NEVER call RtlDeleteFunctionTable + RtlAddFunctionTable
 * again after this point.  The delete→add gap is a race window where an
 * exception propagating through a trampoline frame would find no unwind
 * table, causing RtlFailFast / game crash.  One-shot registration
 * eliminates this window entirely.
 *
 * Must be alloc_near(base) so that UnwindData RVAs fit in 32 bits.        */
static void init_unwind_table(BYTE *base, DWORD capacity, size_t stride) {
    size_t tbl_size = (size_t)capacity * sizeof(TrampolineUnwindEntry);
    g_unwind_table = (TrampolineUnwindEntry *)alloc_near((uintptr_t)base, tbl_size);
    if (!g_unwind_table) return;
    ZeroMemory(g_unwind_table, tbl_size);

    for (DWORD i = 0; i < capacity; i++) {
        BYTE *tramp = base + (size_t)i * stride;
        DWORD rva_begin = (DWORD)((uintptr_t)tramp - (uintptr_t)base);
        g_unwind_table[i].rf.BeginAddress        = rva_begin;
        g_unwind_table[i].rf.EndAddress          = rva_begin + (DWORD)stride;
        g_unwind_table[i].rf.UnwindData          =
            (DWORD)((uintptr_t)&g_unwind_table[i].ui - (uintptr_t)base);
        g_unwind_table[i].ui.Version_Flags        = 1; /* version=1, flags=0 */
        g_unwind_table[i].ui.SizeOfProlog         = 0;
        g_unwind_table[i].ui.CountOfCodes         = 0;
        g_unwind_table[i].ui.FrameRegister_Offset = 0;
    }

    /* Register the full-capacity table once.  ImageBase = g_trampolines so
     * that BeginAddress/EndAddress/UnwindData are all relative to it.     */
    g_unwind_count = capacity;
    RtlAddFunctionTable(
        (PRUNTIME_FUNCTION)g_unwind_table,
        capacity,
        (DWORD64)(uintptr_t)base   /* ImageBase = trampoline buffer */
    );

    /* Diagnostic: log the delta between the two allocations so that we can
     * verify UnwindData RVAs are inside the allocation (delta < blksz).   */
    {
        char dbg[128];
        intptr_t delta = (intptr_t)g_unwind_table - (intptr_t)base;
        _snprintf_s(dbg, sizeof(dbg), _TRUNCATE,
            "unwind_table=0x%llX tpl_base=0x%llX delta=0x%llX",
            (unsigned long long)(uintptr_t)g_unwind_table,
            (unsigned long long)(uintptr_t)base,
            (unsigned long long)(uintptr_t)(delta < 0 ? -delta : delta));
        log_phase(dbg);
    }
}

/* update_seh_registration: kept for the unregister-at-shutdown path only.
 * After init_unwind_table() registers the full-capacity table, this
 * function is intentionally NOT called during incremental batch scans.
 * Calling Delete + Add during hook application creates a race window where
 * an exception in a live trampoline frame finds no unwind entry and the
 * runtime calls RtlFailFast, crashing the game.                          */
static void update_seh_registration(long active_count) {
    (void)active_count;  /* no-op -- full-capacity table already registered */
}

static void unregister_trampolines_for_seh(void) {
    if (!g_unwind_table) return;
    RtlDeleteFunctionTable((PRUNTIME_FUNCTION)g_unwind_table);
    VirtualFree(g_unwind_table, 0, MEM_RELEASE);
    g_unwind_table = NULL;
    g_unwind_count = 0;
}

/* ================================================================
 * scan_next_batch
 *
 * Hook up to `batch` more functions from g_pdata starting at
 * g_pdata_pos, appending trampolines/addrs from g_hook_count.
 * The trampoline buffer MUST be PAGE_EXECUTE_READWRITE when called.
 * MH_QueueEnableHook is called for each new hook; caller must call
 * MH_ApplyQueued() once after this returns.
 * Returns the number of hooks newly installed.
 * ============================================================= */
static long scan_next_batch(DWORD batch) {
    long newly = 0;
    for (; g_pdata_pos < g_pdata_count
           && g_hook_count < (long)g_mh
           && (DWORD)newly < batch;
         g_pdata_pos++) {

        uintptr_t fn_addr = g_img_base + g_pdata[g_pdata_pos].BeginAddress;
        if (fn_addr < g_ts_base || fn_addr >= g_ts_base + g_ts_size) continue;

        BYTE *tramp = (BYTE*)g_trampolines + (size_t)g_hook_count * TRAMPOLINE_SIZE;
        void *orig  = NULL;

        memcpy(tramp, g_tpl, TRAMPOLINE_SIZE);
        *(uintptr_t*)(tramp + TRAMPOLINE_ADDR_OFFSET) = fn_addr;

        if (MH_CreateHook((LPVOID)fn_addr, (LPVOID)tramp, &orig) != MH_OK) continue;
        *(void**)(tramp + TRAMPOLINE_ORIG_OFFSET) = orig;
        MH_QueueEnableHook((LPVOID)fn_addr);
        g_hook_addrs[g_hook_count++] = (uint64_t)fn_addr;
        newly++;
    }
    return newly;
}

/* ================================================================
 * do_search
 *
 * Protocol (Python → DLL, after initial Config):
 *   CMD_DISABLE  (1): uint32_t count, then count × uint64_t va
 *     → MH_QueueDisableHook + ApplyQueued; confirms via "disabled:N"
 *   CMD_SCAN_NEXT(2): uint32_t batch_size
 *     → hooks next batch_size functions from current pdata position,
 *       applies, reports "scan_done:N@pos"
 * ============================================================= */
#define CMD_DISABLE   1
#define CMD_SCAN_NEXT 2

static void do_search(const Config *cfg) {
    if (MH_Initialize()!=MH_OK) {
        pipe_write(0,0,1,"ERROR:MH_Initialize",19); return;
    }

    /* Patch Send pointer into module-level template (shared by all batches) */
    memcpy(g_tpl, s_tpl, TRAMPOLINE_SIZE);
    *(void**)(g_tpl+TRAMPOLINE_SEND_OFFSET)=(void*)Send;

    {
        char dbg[128];
        int n = _snprintf_s(dbg, sizeof(dbg), _TRUNCATE,
            "tpl_check: ADDR_OFF=%d SEND_OFF=%d ORIG_OFF=%d SIZE=%d "
            "send_ptr=0x%llX tpl[60..67]=%02X%02X%02X%02X%02X%02X%02X%02X",
            TRAMPOLINE_ADDR_OFFSET, TRAMPOLINE_SEND_OFFSET,
            TRAMPOLINE_ORIG_OFFSET, TRAMPOLINE_SIZE,
            (unsigned long long)(uintptr_t)Send,
            g_tpl[60],g_tpl[61],g_tpl[62],g_tpl[63],
            g_tpl[64],g_tpl[65],g_tpl[66],g_tpl[67]);
        pipe_write(0,0,1,dbg,(DWORD)n);
    }

    HMODULE hmod=GetModuleHandleW(NULL);
    MODULEINFO mi={0};
    GetModuleInformation(GetCurrentProcess(),hmod,&mi,sizeof(mi));

    g_img_base=(uintptr_t)mi.lpBaseOfDll;
    IMAGE_DOS_HEADER *dos=(IMAGE_DOS_HEADER*)g_img_base;
    IMAGE_NT_HEADERS *nt=(IMAGE_NT_HEADERS*)(g_img_base+dos->e_lfanew);
    IMAGE_SECTION_HEADER *sec=IMAGE_FIRST_SECTION(nt);

    g_ts_base=0; g_ts_size=0;
    for (WORD i=0;i<nt->FileHeader.NumberOfSections;i++)
        if (!memcmp(sec[i].Name,".text",5))
            { g_ts_base=g_img_base+sec[i].VirtualAddress; g_ts_size=sec[i].Misc.VirtualSize; break; }
    if (!g_ts_base) { g_ts_base=g_img_base; g_ts_size=mi.SizeOfImage; }

    /* Load pdata early so g_pdata_count can drive capacity (must precede alloc) */
    IMAGE_DATA_DIRECTORY *edir =
        &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION];
    if (!edir->VirtualAddress || !edir->Size) {
        pipe_write(0,0,1,"ERROR:no .pdata section",23);
        log_phase("ERROR:no .pdata section");
        MH_Uninitialize(); return;
    }
    g_pdata       = (RUNTIME_FUNCTION *)(g_img_base + edir->VirtualAddress);
    g_pdata_count = edir->Size / sizeof(RUNTIME_FUNCTION);

    /* Total hook capacity:
     *   cfg->max_hooks == 0  → scan ALL pdata (up to hard ceiling MAX_HOOKS)
     *   cfg->max_hooks >  0  → limit to that count                           */
    g_mh = cfg->max_hooks ? cfg->max_hooks : g_pdata_count;
    if (g_mh > MAX_HOOKS) g_mh = MAX_HOOKS;

    /* First-batch size (0 in config → one big batch covering full capacity)  */
    DWORD first_batch = cfg->batch_size ? cfg->batch_size : g_mh;
    if (first_batch > g_mh) first_batch = g_mh;

    size_t blksz = (size_t)g_mh * TRAMPOLINE_SIZE;
    BYTE *trampolines = alloc_near(g_ts_base, blksz);
    if (!trampolines) {
        pipe_write(0,0,1,"ERROR:alloc_near failed",23);
        MH_Uninitialize(); return;
    }

    uint64_t *addrs = (uint64_t*)HeapAlloc(GetProcessHeap(), 0,
                                             (size_t)g_mh * sizeof(uint64_t));
    if (!addrs) {
        VirtualFree(trampolines, 0, MEM_RELEASE);
        MH_Uninitialize(); return;
    }

    /* Set globals before scan_next_batch uses them */
    g_hook_addrs  = addrs;
    g_trampolines = trampolines;
    g_hook_count  = 0;
    g_pdata_pos   = 0;

    pipe_write(0,0,1,"phase:scan_start",16);
    log_phase("phase:scan_start");
    {
        char gb_msg[96];
        _snprintf_s(gb_msg, sizeof(gb_msg), _TRUNCATE,
            "game_base=0x%llX pdata_count=%lu cap=%lu",
            (unsigned long long)g_img_base,
            (unsigned long)g_pdata_count, (unsigned long)g_mh);
        log_phase(gb_msg);
    }

    /* Allocate + pre-fill the full-capacity unwind table and register it
     * ONCE via RtlAddFunctionTable inside init_unwind_table().  We never
     * call RtlDeleteFunctionTable / RtlAddFunctionTable again after this
     * point -- doing so risks a race where an exception in a live trampoline
     * frame finds no unwind entry and the runtime calls RtlFailFast.       */
    init_unwind_table(trampolines, g_mh, TRAMPOLINE_SIZE);
    pipe_write(0,0,1,"phase:seh_registered",20);
    log_phase("phase:seh_registered");

    /* ---- First batch ---- */
    long newly = scan_next_batch(first_batch);

    {
        char tmp2[80];
        int n=_snprintf_s(tmp2,sizeof(tmp2),_TRUNCATE,"phase:scan_done count=%ld",g_hook_count);
        pipe_write(0,0,1,tmp2,(DWORD)n);
        log_phase(tmp2);
    }

    MH_ApplyQueued();
    pipe_write(0,0,1,"phase:hooks_applied",19);
    log_phase("phase:hooks_applied");

    DWORD dummy;
    VirtualProtect(trampolines, blksz, PAGE_EXECUTE_READ, &dummy);
    pipe_write(0,0,1,"phase:rx_protected",18);
    log_phase("phase:rx_protected");

    {
        char msg[80];
        int ml=_snprintf_s(msg,sizeof(msg),_TRUNCATE,
            "scan_done:%ld@%lu", newly, (unsigned long)g_pdata_pos);
        pipe_write(0,0,1,msg,ml>0?(DWORD)ml:0);
        log_phase(msg);
    }

    /* ================================================================
     * Command + drain loop
     * ============================================================= */
    bool pipe_ok = true;
    while (pipe_ok && g_pipe != INVALID_HANDLE_VALUE) {
        DWORD av = 0;
        if (!PeekNamedPipe(g_pipe, NULL, 0, NULL, &av, NULL)) break;

        if (av >= 1) {
            uint8_t cmd = 0;
            if (!pipe_read_all(&cmd, 1)) break;

            if (cmd == CMD_DISABLE) {
                /* Payload: uint32_t count, then count × uint64_t va */
                uint32_t cnt = 0;
                if (!pipe_read_all(&cnt, 4)) break;
                for (uint32_t k = 0; pipe_ok && k < cnt; k++) {
                    uint64_t va = 0;
                    if (!pipe_read_all(&va, 8)) { pipe_ok = false; break; }
                    MH_QueueDisableHook((LPVOID)(uintptr_t)va);
                }
                if (pipe_ok) {
                    MH_ApplyQueued();
                    char dbuf[48];
                    int n = _snprintf_s(dbuf, sizeof(dbuf), _TRUNCATE,
                        "disabled:%lu", (unsigned long)cnt);
                    pipe_write(0,0,1,dbuf, n>0?(DWORD)n:0);
                }

            } else if (cmd == CMD_SCAN_NEXT) {
                /* Payload: uint32_t batch_size */
                uint32_t nbatch = 0;
                if (!pipe_read_all(&nbatch, 4)) break;

                /* Temporarily make buffer writable to write new trampolines */
                VirtualProtect(trampolines, blksz, PAGE_EXECUTE_READWRITE, &dummy);
                newly = scan_next_batch(nbatch);
                if (newly > 0) {
                    /* SEH table already covers full capacity -- no need to
                     * re-register.  Just apply the newly queued hooks.    */
                    MH_ApplyQueued();
                }
                VirtualProtect(trampolines, blksz, PAGE_EXECUTE_READ, &dummy);

                char nbuf[80];
                int n = _snprintf_s(nbuf, sizeof(nbuf), _TRUNCATE,
                    "scan_done:%ld@%lu", newly, (unsigned long)g_pdata_pos);
                pipe_write(0,0,1,nbuf, n>0?(DWORD)n:0);
                log_phase(nbuf);
            }
        }

        ring_drain();
        Sleep(10);
    }
    ring_drain();

    for (long i=0; i<g_hook_count; i++)
        MH_QueueDisableHook((LPVOID)(uintptr_t)addrs[i]);
    MH_ApplyQueued();
    Sleep(500);
    for (long i=0; i<g_hook_count; i++)
        MH_RemoveHook((LPVOID)(uintptr_t)addrs[i]);

    unregister_trampolines_for_seh();
    VirtualFree(trampolines, 0, MEM_RELEASE);
    HeapFree(GetProcessHeap(), 0, addrs);
    g_hook_addrs=NULL; g_trampolines=NULL; g_hook_count=0;
    g_pdata=NULL; g_pdata_count=0; g_pdata_pos=0;
    MH_Uninitialize();
}

/* ================================================================
 * Phase heartbeat log -- written at every major step so that even
 * if the crash handler never fires we know where execution stopped.
 * Writes to %TEMP%\jri_phases.txt (append mode).
 * ============================================================= */
static void log_phase(const char *msg) {
    wchar_t tmp[MAX_PATH];
    if (!GetTempPathW(MAX_PATH, tmp)) return;
    wchar_t path[MAX_PATH];
    _snwprintf_s(path, MAX_PATH, _TRUNCATE, L"%sjri_phases.txt", tmp);
    HANDLE f = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    SetFilePointer(f, 0, NULL, FILE_END);
    char buf[256];
    int n = _snprintf_s(buf, sizeof(buf), _TRUNCATE, "%s\r\n", msg);
    DWORD wr = 0;
    if (n > 0) WriteFile(f, buf, (DWORD)n, &wr, NULL);
    CloseHandle(f);
}

/* ================================================================
 * Crash handler -- writes %TEMP%\jri_crash.txt on any exception.
 * Registered as BOTH a Vectored Exception Handler (first in chain)
 * and as the Unhandled Exception Filter, so it fires even if the
 * game installs its own SEH-based crash reporter.
 * ============================================================= */
static LONG WINAPI crash_handler(EXCEPTION_POINTERS *ep) {
    wchar_t tmp[MAX_PATH];
    if (!GetTempPathW(MAX_PATH, tmp)) { tmp[0]=L'C'; tmp[1]=L':'; tmp[2]=L'\\'; tmp[3]=0; }
    wchar_t path[MAX_PATH];
    _snwprintf_s(path, MAX_PATH, _TRUNCATE, L"%sjri_crash.txt", tmp);
    HANDLE f = CreateFileW(path,
                           GENERIC_WRITE, 0, NULL,
                           CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f != INVALID_HANDLE_VALUE) {
        char buf[640];
        EXCEPTION_RECORD *er = ep->ExceptionRecord;
        CONTEXT          *ctx= ep->ContextRecord;
        uintptr_t gb = (uintptr_t)GetModuleHandleW(NULL);
        int n = _snprintf_s(buf, sizeof(buf), _TRUNCATE,
            "CODE:       0x%08X\r\n"
            "ADDRESS:    0x%016llX\r\n"
            "RIP:        0x%016llX\r\n"
            "RSP:        0x%016llX\r\n"
            "RCX:        0x%016llX\r\n"
            "RDX:        0x%016llX\r\n"
            "game_base:  0x%016llX\r\n"
            "RIP_RVA:    0x%016llX\r\n"
            "tpl base:   0x%016llX\r\n"
            "tpl end:    0x%016llX\r\n"
            "hook_count: %ld\r\n"
            "unwind_tbl: 0x%016llX\r\n",
            (unsigned)er->ExceptionCode,
            (unsigned long long)er->ExceptionAddress,
            (unsigned long long)ctx->Rip,
            (unsigned long long)ctx->Rsp,
            (unsigned long long)ctx->Rcx,
            (unsigned long long)ctx->Rdx,
            (unsigned long long)gb,
            (unsigned long long)(ctx->Rip - gb),
            (unsigned long long)(uintptr_t)g_trampolines,
            (unsigned long long)((uintptr_t)g_trampolines
                                 + (size_t)g_hook_count * TRAMPOLINE_SIZE),
            g_hook_count,
            (unsigned long long)(uintptr_t)g_unwind_table);
        DWORD wr = 0;
        WriteFile(f, buf, (DWORD)n, &wr, NULL);
        CloseHandle(f);
    }
    char phase_msg[256];
    EXCEPTION_RECORD *er2 = ep->ExceptionRecord;
    /* Report game module base so caller can compute RVA = crash_RIP - game_base */
    uintptr_t game_base = (uintptr_t)GetModuleHandleW(NULL);
    _snprintf_s(phase_msg, sizeof(phase_msg), _TRUNCATE,
        "crash CODE=0x%08X ADDR=0x%llX RIP=0x%llX game_base=0x%llX RIP_RVA=0x%llX",
        (unsigned)er2->ExceptionCode,
        (unsigned long long)er2->ExceptionAddress,
        (unsigned long long)ep->ContextRecord->Rip,
        (unsigned long long)game_base,
        (unsigned long long)(ep->ContextRecord->Rip - game_base));
    log_phase(phase_msg);
    return EXCEPTION_CONTINUE_SEARCH;
}

/* ================================================================
 * do_hook / worker_thread / DllMain  (unchanged logic)
 * ============================================================= */
static UINT64 __fastcall hook_detour(UINT64 a0,UINT64 a1,UINT64 a2,UINT64 a3){
    UINT64 args[4]={a0,a1,a2,a3};
    const Config *c=&g_cfg;
    uintptr_t ptr=c->arg_idx<4?(uintptr_t)args[c->arg_idx]:0;
    if (c->byte_offset) ptr+=c->byte_offset;
    if (c->deref) __try{ptr=*(uintptr_t*)ptr;}__except(EXCEPTION_EXECUTE_HANDLER){ptr=0;}
    if (ptr>=0x10000&&c->encoding==0) {
        WCHAR local[MAX_STR_LEN]; int wlen=0;
        __try{const WCHAR*p=(const WCHAR*)ptr;
              while(wlen<MAX_STR_LEN&&p[wlen]){local[wlen]=p[wlen];wlen++;}}
        __except(EXCEPTION_EXECUTE_HANDLER){}
        if (wlen>=MIN_STR_LEN&&has_cjk(local,wlen))
            pipe_write(c->hook_address,(int)c->arg_idx,0,local,(DWORD)(wlen*2));
    }
    return g_orig?g_orig(a0,a1,a2,a3):0;
}

static void do_hook(const Config *cfg){
    if (MH_Initialize()!=MH_OK){pipe_write(0,0,1,"ERROR:MH_Initialize",19);return;}
    g_orig=NULL;
    LPVOID tgt=(LPVOID)(uintptr_t)cfg->hook_address;
    MH_STATUS st=MH_CreateHook(tgt,(LPVOID)hook_detour,(LPVOID*)&g_orig);
    if (st!=MH_OK){char e[48];int n=_snprintf_s(e,48,_TRUNCATE,"ERROR:MH_CreateHook %d",(int)st);
        pipe_write(0,0,1,e,n>0?(DWORD)n:0);MH_Uninitialize();return;}
    MH_EnableHook(tgt);
    char msg[40];int n=_snprintf_s(msg,40,_TRUNCATE,"hook_ready:%llx",(unsigned long long)cfg->hook_address);
    pipe_write(0,0,1,msg,n>0?(DWORD)n:0);
    while(g_pipe!=INVALID_HANDLE_VALUE){DWORD av=0;
        if(!PeekNamedPipe(g_pipe,NULL,0,NULL,&av,NULL))break;Sleep(200);}
    MH_DisableHook(tgt);MH_RemoveHook(tgt);MH_Uninitialize();
}

static DWORD WINAPI worker(LPVOID _){
    (void)_;
    /* Boost this thread so it stays responsive even when Windows throttles
     * the game process (e.g. when the game window is in the background or
     * the VN engine is idle / waiting for user input).  ABOVE_NORMAL is
     * sufficient to escape process-class throttling without starving the
     * game at TIME_CRITICAL priority.                                      */
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
    wchar_t name[64];
    _snwprintf_s(name,64,_TRUNCATE,L"\\\\.\\pipe\\JRI-%lu",(unsigned long)GetCurrentProcessId());
    for(int r=0;r<50;r++){
        g_pipe=CreateFileW(name,GENERIC_READ|GENERIC_WRITE,0,NULL,OPEN_EXISTING,0,NULL);
        if(g_pipe!=INVALID_HANDLE_VALUE)break; Sleep(100);
    }
    if(g_pipe==INVALID_HANDLE_VALUE)return 1;
    Config cfg;
    if(!pipe_read_all(&cfg,sizeof(cfg))){CloseHandle(g_pipe);g_pipe=INVALID_HANDLE_VALUE;return 1;}
    g_cfg=cfg;
    if(cfg.mode==MODE_SEARCH)do_search(&cfg); else do_hook(&cfg);
    CloseHandle(g_pipe);g_pipe=INVALID_HANDLE_VALUE;
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h,DWORD reason,LPVOID _){
    (void)_; (void)h;
    if(reason==DLL_PROCESS_ATTACH){
        DisableThreadLibraryCalls(h);
        g_pipe=INVALID_HANDLE_VALUE;
        /* UEHF only -- catches real unhandled crashes.
         * Do NOT use AddVectoredExceptionHandler: it would intercept
         * every AV from Send's own __try/__except probes, causing
         * hundreds of spurious crash_handler invocations and massive
         * file-I/O overhead on every game thread. */
        SetUnhandledExceptionFilter(crash_handler);
        log_phase("DLL_PROCESS_ATTACH");
        CreateThread(NULL,0,worker,NULL,0,NULL);
    } else if(reason==DLL_PROCESS_DETACH){
        if(g_hook_count&&g_hook_addrs){
            for(long i=0;i<g_hook_count;i++)
                MH_QueueDisableHook((LPVOID)(uintptr_t)g_hook_addrs[i]);
            MH_ApplyQueued();
        }
        unregister_trampolines_for_seh();
        if(g_trampolines)VirtualFree(g_trampolines,0,MEM_RELEASE);
        if(g_hook_addrs)HeapFree(GetProcessHeap(),0,g_hook_addrs);
    }
    return TRUE;
}