/*
 * The hook-search concept (bulk function hooking + stack-frame string
 * sniffing for VN text extraction) was pioneered by Textractor (GPL-3.0).
 * This file is an independent implementation with a different architecture.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <psapi.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "MinHook.h"

#pragma comment(lib, "psapi.lib")

#define MODE_SEARCH   0
#define MODE_HOOK     1
#define MAX_HOOKS     200000  /* hard safety ceiling; 0 in config means scan all pdata */
#define MAX_STR_LEN   400
#define MIN_STR_LEN   2
#define SEND_REG_LO   16     /* UNUSED — kept for reference; arg regs use explicit slots now */
#define SEND_STK_HI   9

/* Pre-hook static filters (see fn_calls_only_blacklisted_imports, scan_next_batch).
 * MinHook rejects functions shorter than its own patch sequence (~14 B), so
 * MIN_FN_BYTES mainly avoids the overhead of a doomed MH_CreateHook call.  */
#define MIN_FN_BYTES  16

/* Trampoline byte offsets */
#define TRAMPOLINE_ADDR_OFFSET  74
#define TRAMPOLINE_SEND_OFFSET  84
#define TRAMPOLINE_ORIG_OFFSET  182
#define TRAMPOLINE_SIZE         190

#pragma pack(push, 1)
typedef struct { uint8_t mode; uint8_t _p0[3]; uint32_t max_hooks;
                 uint64_t hook_address; uint8_t arg_idx; uint8_t deref;
                 uint16_t byte_offset; uint16_t encoding;
                 uint16_t batch_size;   /* MODE_SEARCH: hooks per batch (0 = all at once) */ } Config;
typedef struct { uint64_t hook_va; uint64_t str_ptr;  /* VA of the string in game memory */
                 int32_t slot_i;
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

/* ---- per-address call-rate suppressor (time-decayed) ----
 * Each slot stores {epoch(16b), count(16b)} packed in one LONG.
 * Count is limited per epoch-window (SEND_CALL_LIMIT); when the worker
 * advances epoch, slots lazily reset on next hit (epoch mismatch).
 *
 * This avoids the old "monotonic forever" behavior where long sessions
 * eventually suppressed nearly everything due to ever-increasing counters.
 *
 * NOTE: hashing collisions still exist by design; decay bounds their impact
 * to one window instead of the whole session lifetime.
 */
#define CALL_RATE_SIZE   65521u
#define SEND_CALL_LIMIT  150        /* raw calls before auto-disable        */
#define RATE_WINDOW_MS   3000u      /* per-slot count decay window          */
static volatile LONG g_call_rate[CALL_RATE_SIZE];
static volatile LONG g_need_apply = 0; /* set in Send(), consumed in ring_drain() */
static volatile LONG g_rate_epoch = 1;  /* low 16 bits used in packed slots    */
static volatile LONG64 g_rate_next_decay_ms = 0;

/* ---- struct-pointer dedup cache ----
 * Prevents redundant scan_struct_deref calls when the same (address,
 * base_ptr, reg_code) combination repeats within a single rate epoch.
 *
 * Key: hash(address, base_ptr, reg_code, g_rate_epoch).
 * Natural invalidation: epoch rotation every RATE_WINDOW_MS causes all
 * stale entries to become automatic misses — text changes are picked up
 * within one epoch window (3 s).
 *
 * Memory: 65521 * 8 B = 512 KB.
 */
#define SPTR_CACHE_SIZE  65521u
static volatile LONG64 g_sptr_cache[SPTR_CACHE_SIZE];

/* ---- L1-offset layout cache ----
 * Learns which struct-field offsets (at DEREF_STEP granularity) ever held
 * a pointer-like value at each hook address.  After LAYOUT_WARMUP calls
 * the scan only probes those offsets, pruning ~80-90% of L1 memory reads.
 * Also tracks whether L2 (nested-struct dereference) ever found text for
 * each argument register, skipping entire L2 loops when fruitless.
 *
 * Struct field layouts are determined at compile time, so the learned
 * mask is permanent across epochs.  Hash collisions fall through to the
 * full-scan path: safe but slower.
 *
 * Memory: 16384 * 56 B = 896 KB.
 */
#define LAYOUT_CACHE_SIZE  16384u
#define LAYOUT_CACHE_MASK  (LAYOUT_CACHE_SIZE - 1)
#define LAYOUT_WARMUP      16u          /* ~4 Send() calls * 4 regs          */

typedef struct {
    uintptr_t addr;          /* hook VA (key); 0 = empty slot              */
    uint32_t  warmup;        /* call count; saturates at LAYOUT_WARMUP     */
    uint32_t  _pad;
    uint64_t  l1_mask[4];    /* per-reg: bits 0-63 -> offsets 0x000..0x1F8 */
    uint8_t   l2_hit[4];     /* 1 if L2 ever found text for this register  */
    uint8_t   _pad2[4];
} LayoutEntry;

static LayoutEntry g_layout_cache[LAYOUT_CACHE_SIZE];

static void rate_decay_tick(void) {
    ULONGLONG now = GetTickCount64();
    LONG64 due = InterlockedCompareExchange64(&g_rate_next_decay_ms, 0, 0);
    if (due == 0) {
        InterlockedExchange64(&g_rate_next_decay_ms, (LONG64)(now + RATE_WINDOW_MS));
        return;
    }
    if ((ULONGLONG)due > now) return;

    LONG64 next_due = (LONG64)(now + RATE_WINDOW_MS);
    if (InterlockedCompareExchange64(&g_rate_next_decay_ms, next_due, due) == due) {
        InterlockedIncrement(&g_rate_epoch);
    }
}

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
 * Stable freeze / thaw
 *
 * Suspend every thread in the current process except the caller,
 * looping until two consecutive snapshots show no new threads.
 * This guarantees the process is fully quiescent before any
 * MH_ApplyQueued() call, eliminating the race between MinHook's
 * internal SuspendThread and game CPUs mid-instruction.
 *
 * MinHook also suspends threads internally; that is harmless here
 * because SuspendThread on an already-suspended thread merely
 * increments the suspend count, and ResumeThread decrements it.
 * ============================================================= */
#define MAX_FROZEN_THREADS 4096u
static DWORD  g_frozen_tids   [MAX_FROZEN_THREADS];
static HANDLE g_frozen_handles[MAX_FROZEN_THREADS];
static DWORD  g_frozen_count = 0;

static void freeze_all_threads(void) {
    DWORD self_tid = GetCurrentThreadId();
    DWORD pid      = GetCurrentProcessId();
    g_frozen_count = 0;
    bool found_new;
    do {
        found_new = false;
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if (snap == INVALID_HANDLE_VALUE) break;
        THREADENTRY32 te; te.dwSize = sizeof(te);
        if (Thread32First(snap, &te)) {
            do {
                if (te.th32OwnerProcessID != pid)      continue;
                if (te.th32ThreadID       == self_tid) continue;
                if (g_frozen_count >= MAX_FROZEN_THREADS) continue;
                /* already suspended by us? */
                bool already = false;
                for (DWORD k = 0; k < g_frozen_count; k++)
                    if (g_frozen_tids[k] == te.th32ThreadID) { already = true; break; }
                if (already) continue;
                HANDLE th = OpenThread(THREAD_SUSPEND_RESUME, FALSE, te.th32ThreadID);
                if (!th) continue;
                SuspendThread(th);
                g_frozen_tids   [g_frozen_count] = te.th32ThreadID;
                g_frozen_handles[g_frozen_count] = th;
                g_frozen_count++;
                found_new = true;
            } while (Thread32Next(snap, &te));
        }
        CloseHandle(snap);
    } while (found_new);
}

static void thaw_all_threads(void) {
    for (DWORD i = 0; i < g_frozen_count; i++) {
        ResumeThread(g_frozen_handles[i]);
        CloseHandle(g_frozen_handles[i]);
    }
    g_frozen_count = 0;
}

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
    h->hook_va=(uint64_t)va; h->str_ptr=0; h->slot_i=(int32_t)slot;
    h->encoding=(uint16_t)enc; h->text_len=(uint16_t)nb;
    memcpy(buf+sizeof(ResultHdr),txt,nb);
    DWORD wr=0;
    WriteFile(g_pipe,buf,(DWORD)(sizeof(ResultHdr)+nb),&wr,NULL);
}

/* ================================================================
 * ring push (any thread, lock-free)
 * ============================================================= */
static void ring_push(uintptr_t va, uintptr_t str_ptr, int slot, int enc,
                      const void *txt, DWORD nb) {
    if (!nb||nb>MAX_STR_LEN*2) return;
    LONG seq=InterlockedIncrement(&g_ring_seq)-1;
    RingSlot *s=&g_ring[seq&(RING_SLOTS-1)];
    if (InterlockedCompareExchange(&s->state,1,0)!=0) return;
    s->hdr.hook_va=(uint64_t)va; s->hdr.str_ptr=(uint64_t)str_ptr; s->hdr.slot_i=(int32_t)slot;
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
            /* Freeze all game threads before calling MH_ApplyQueued so that
             * no thread can be executing in a page that MinHook is about to
             * VirtualProtect(RW) → patch → VirtualProtect(RX).  Without this
             * a thread that slips through MinHook's own SuspendThread window
             * on a second CPU core hits 0xC0000005 (no-execute on RW page).
             * freeze_all_threads loops until the snapshot is stable, so
             * threads created between iterations are also caught.           */
            freeze_all_threads();
            MH_ApplyQueued();
            thaw_all_threads();
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
 * CJK / text-quality helpers
 * ============================================================= */
static bool has_cjk(const WCHAR *p, int n) {
    for (int i=0;i<n;i++) {
        WCHAR c=p[i];
        if ((c>=0x3000&&c<=0x9FFF)||(c>=0xFF00&&c<=0xFFEF)) return true;
    }
    return false;
}

/* Returns true if the character is a legitimate dialogue text character.
 *
 * Covers: CJK ideographs, hiragana, katakana, CJK punctuation/symbols,
 *         fullwidth forms, common punctuation (dashes, quotes, ellipsis),
 *         whitespace, ASCII printable (letters, digits, punctuation).
 *
 * Explicitly EXCLUDES:
 *   - Private Use Area (U+E000-F8FF)
 *   - Invisible formatting (U+200B-200F, U+2060-206F, U+FE00-FE0F)
 *   - Variation selectors, specials, surrogates
 *   - Control characters (except whitespace)
 */
static bool is_dialogue_char(WCHAR c) {
    /* Whitespace */
    if (c == L' ' || c == L'\t' || c == L'\n' || c == L'\r') return true;
    /* Ideographic space */
    if (c == 0x3000) return true;

    /* ASCII printable range (0x21-0x7E): letters, digits, punctuation */
    if (c >= 0x21 && c <= 0x7E) return true;

    /* General Punctuation U+2000-2069 (skip invisible formatting) */
    if (c >= 0x2000 && c <= 0x2069) {
        if (c >= 0x200B && c <= 0x200F) return false;  /* ZWSP, ZWNJ, ZWJ, etc. */
        if (c >= 0x2060 && c <= 0x2069) return false;  /* invisible operators */
        return true;  /* em-dash, quotes, ellipsis, etc. */
    }

    /* CJK core: symbols/punct 3000-303F, hiragana 3040-309F,
     * katakana 30A0-30FF, CJK unified ideographs 4E00-9FFF,
     * and everything between (CJK compatibility, etc.) */
    if (c >= 0x3001 && c <= 0x9FFF) return true;

    /* Fullwidth/halfwidth forms FF00-FFEF */
    if (c >= 0xFF00 && c <= 0xFFEF) return true;

    /* CJK Compatibility Ideographs F900-FAFF */
    if (c >= 0xF900 && c <= 0xFAFF) return true;

    /* Everything else: not dialogue text */
    return false;
}

/* Returns true if the character is a "poison" indicator — a character
 * that NEVER appears in genuine CJK game dialogue text.
 * A single occurrence causes immediate rejection of the entire string.
 */
static bool is_poison_char(WCHAR c) {
    /* Control characters (except whitespace) */
    if (c < 0x20 && c != L'\t' && c != L'\n' && c != L'\r') return true;
    /* Private Use Area */
    if (c >= 0xE000 && c <= 0xF8FF) return true;
    /* Invisible formatting / joiners */
    if (c >= 0x200B && c <= 0x200F) return true;
    if (c >= 0x2060 && c <= 0x206F) return true;
    /* Variation selectors */
    if (c >= 0xFE00 && c <= 0xFE0F) return true;
    /* Specials (FFFE, FFFF = BOM / nonchar) */
    if (c >= 0xFFF0) return true;
    return false;
}

/* Minimum string lengths */
#define DEREF_MIN_STR_LEN       4   /* struct deref: lowered since quality filters are strong */
/* Dialogue-char ratio for deref: require >= 80% of characters to be
 * legitimate dialogue characters (CJK, kana, punctuation, whitespace). */
#define DEREF_DIALOGUE_RATIO_PCT 80

/* ================================================================
 * try_push_text — extracted helper for text probing + ring push
 *
 * Checks whether *val* looks like a wchar_t* pointing to CJK text,
 * deduplicates, copies, and pushes to the ring buffer.
 *
 * min_wlen: minimum number of WCHARs for the string to be accepted.
 *           Use MIN_STR_LEN for direct reg/stack, DEREF_MIN_STR_LEN
 *           for struct dereference results.
 * strict:   if true, apply deref-quality filters:
 *           - Reject if ANY poison char (ASCII letter, PUA, invisible)
 *           - Require >= 80% dialogue characters
 *           If false, only require has_cjk (direct reg/stack mode).
 *
 * Same safety rules as Send():
 *   - All game-pointer reads inside __try/__except.
 *   - Only intrinsics (no DLL calls).
 * Returns: 1 if a string was pushed, 0 otherwise.
 * ============================================================= */
static int try_push_text(uintptr_t address, uintptr_t val, int slot_i,
                         int min_wlen, bool strict) {
    if (val < 0x10000 || val > 0x000F000000000000ULL) return 0;

    /* ---- quick peek: read first 4 WCHARs ---- */
    WCHAR c0, c1, c2, c3;
    __try {
        const WCHAR *p = (const WCHAR *)val;
        c0 = p[0]; c1 = p[1]; c2 = p[2]; c3 = p[3];
    } __except(EXCEPTION_EXECUTE_HANDLER) { return 0; }

    if (!c0 || !c1) return 0;

    /* ---- CJK pre-filter (first 4 chars) ---- */
#define _IS_CJK(c) (((c)>=0x3000&&(c)<=0x9FFF)||((c)>=0xFF00&&(c)<=0xFFEF))
    if (!_IS_CJK(c0) && !_IS_CJK(c1) && !_IS_CJK(c2) && !_IS_CJK(c3))
        return 0;
#undef _IS_CJK

    /* ---- early poison check on the 4 peeked chars (strict mode only) ---- */
    if (strict) {
        if (is_poison_char(c0) || is_poison_char(c1) ||
            is_poison_char(c2) || is_poison_char(c3))
            return 0;
    }

    /* ---- dedup by (address, slot_i, c2, c3) ---- */
    LONG sig = (LONG)(
        (address * 2654435761UL) ^
        ((ULONG)((slot_i & 0xFFFF) + 32) * 1234567891UL) ^
        ((ULONG)(c2 << 16) | (ULONG)c3));
    if (!sig) sig = 1;
    ULONG idx = (ULONG)((ULONG)sig % SIG_CACHE_SIZE);
    if (InterlockedCompareExchange(&g_sig_cache[idx], sig, sig) == sig)
        return 0;
    InterlockedExchange(&g_sig_cache[idx], sig);

    /* ---- copy string into a local buffer (inside __try) ---- */
    WCHAR local[MAX_STR_LEN];
    int wlen = 0;
    __try {
        const WCHAR *p = (const WCHAR *)val;
        while (wlen < MAX_STR_LEN && p[wlen]) { local[wlen] = p[wlen]; wlen++; }
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        if (wlen < min_wlen) return 0;
    }
    if (wlen < min_wlen) return 0;

    /* ---- quality check ---- */
    if (strict) {
        /* Poison scan + dialogue-char ratio on the full string */
        int good = 0;
        for (int k = 0; k < wlen; k++) {
            if (is_poison_char(local[k])) return 0;  /* instant kill */
            if (is_dialogue_char(local[k])) good++;
        }
        if (good * 100 / wlen < DEREF_DIALOGUE_RATIO_PCT) return 0;
    } else {
        if (!has_cjk(local, wlen)) return 0;
    }

    /* ---- push to ring ---- */
    ring_push(address, val, slot_i, 0, local, (DWORD)(wlen * sizeof(WCHAR)));
    return 1;
}

/* ================================================================
 * Struct-dereference scan for argument registers
 *
 * DEREF_L1_MAX_OFF: max byte offset in level-1 scan (struct fields).
 * DEREF_L2_MAX_OFF: max byte offset in level-2 scan (nested struct).
 * DEREF_STEP:       pointer-aligned stride (8 bytes on x64).
 *
 * slot_i encoding for deref hits (decoded by Python _handle_hit):
 *   Level-1:  DEREF_L1_BASE + reg_code * 100 + off1/8
 *   Level-2:  DEREF_L2_BASE + reg_code * 10000 + (off1/8)*100 + off2/8
 * where reg_code: 0=RCX, 1=RDX, 2=R8, 3=R9.
 * ============================================================= */
#define DEREF_L1_MAX_OFF  0x200      /* 512 B — 65 pointer slots */
#define DEREF_L2_MAX_OFF  0x100      /* 256 B — 33 pointer slots */
#define DEREF_STEP        8
#define DEREF_L1_BASE     100
#define DEREF_L2_BASE     10000

static void scan_struct_deref(uintptr_t address, uintptr_t base_ptr,
                              int reg_code) {
    /* ---- L1-offset layout cache lookup ----
     * Find (or claim) this hook address's layout entry.  On hash
     * collision the pointer stays NULL and we fall through to a full
     * uncached scan — safe but slower.                               */
    LayoutEntry *le = NULL;
    bool pruning = false;
    {
        ULONG li = (ULONG)(((address >> 4) * 2654435761ULL) & LAYOUT_CACHE_MASK);
        LayoutEntry *slot = &g_layout_cache[li];
        if (slot->addr == 0)
            slot->addr = address;          /* claim empty slot */
        if (slot->addr == address) {
            le = slot;
            if (le->warmup < LAYOUT_WARMUP)
                le->warmup++;
            else
                pruning = true;
        }
    }

    /* ---- struct-pointer dedup ----
     * Skip if the exact (address, base_ptr, reg_code) combination was
     * already scanned in the current rate epoch.  Epoch rotation every
     * RATE_WINDOW_MS naturally invalidates stale entries, so text
     * changes are re-discovered within one window (~3 s).             */
    {
        LONG epoch = g_rate_epoch;          /* volatile read; atomic on x64 */
        uint64_t h = address * 2654435761ULL;
        h ^= base_ptr * 0x9E3779B97F4A7C15ULL;
        h ^= (uint64_t)(unsigned)(reg_code + 1) * 0x517CC1B727220A95ULL;
        h ^= (uint64_t)(ULONG)epoch * 0x9E3779B1ULL;
        LONG64 key = (LONG64)(h | 1);       /* ensure non-zero */
        ULONG ci = (ULONG)((uint64_t)key % SPTR_CACHE_SIZE);
        if (InterlockedCompareExchange64(&g_sptr_cache[ci], key, key) == key)
            return;                         /* cache hit — nothing new */
        InterlockedExchange64(&g_sptr_cache[ci], key);
    }

    /* Level 1: read *(base_ptr + off1) as potential wchar_t* or sub-struct* */
    for (int off1 = 0; off1 <= DEREF_L1_MAX_OFF; off1 += DEREF_STEP) {
        int bit = off1 / DEREF_STEP;        /* 0 .. 64 */

        /* After warmup, skip offsets that never contained a pointer-like
         * value.  Bit 64 (offset 0x200) falls outside the 64-bit mask
         * and is always probed — one extra read is negligible.          */
        if (pruning && bit < 64 &&
            !(le->l1_mask[reg_code] & (1ULL << bit)))
            continue;

        uintptr_t ptr1;
        __try { ptr1 = *(uintptr_t *)((char *)base_ptr + off1); }
        __except(EXCEPTION_EXECUTE_HANDLER) { break; }  /* rest of struct likely unmapped */

        if (ptr1 < 0x10000 || ptr1 > 0x000F000000000000ULL) continue;

        /* Learning: record this offset as ever holding a valid pointer. */
        if (le && bit < 64)
            le->l1_mask[reg_code] |= (1ULL << bit);

        /* Try as direct text pointer */
        int slot_l1 = DEREF_L1_BASE + reg_code * 100 + bit;
        if (try_push_text(address, ptr1, slot_l1,
                          DEREF_MIN_STR_LEN, true))
            continue;  /* found text — skip level 2 for this offset */

        /* Level 2: after warmup, skip entirely if this register has
         * never yielded L2 text.  During learning, always attempt L2. */
        if (pruning && le && !le->l2_hit[reg_code])
            continue;

        /* Level 2: treat ptr1 as a sub-struct, scan its fields */
        for (int off2 = 0; off2 <= DEREF_L2_MAX_OFF; off2 += DEREF_STEP) {
            uintptr_t ptr2;
            __try { ptr2 = *(uintptr_t *)((char *)ptr1 + off2); }
            __except(EXCEPTION_EXECUTE_HANDLER) { break; }

            if (ptr2 < 0x10000 || ptr2 > 0x000F000000000000ULL) continue;

            int slot_l2 = DEREF_L2_BASE + reg_code * 10000
                        + bit * 100
                        + off2 / DEREF_STEP;
            if (try_push_text(address, ptr2, slot_l2,
                              DEREF_MIN_STR_LEN, true)) {
                if (le) le->l2_hit[reg_code] = 1;
            }
        }
    }
}

/* ================================================================
 * Send  -- called from every trampoline
 *
 * Scans argument registers (RCX, RDX, R8, R9) with up to two levels
 * of struct dereference, plus direct stack-argument slots.
 *
 * Callee-saved registers (RBX, RBP, RSI, RDI, R10-R15) are
 * TEMPORARILY DISABLED — they produced unstable "relay" candidates
 * from ancestor-frame register residue.
 *
 * Rules:
 *  1. NO external DLL calls (risk of recursion / kernel-call latency).
 *     Only compiler intrinsics (InterlockedXxx, _WriteBarrier, memcpy).
 *  2. All memory reads from game pointers MUST be inside __try/__except.
 *  3. The __try that covers memcpy into the ring slot is inside
 *     try_push_text, so its .pdata frame covers the copy.
 * ============================================================= */
void __cdecl Send(char **stack, uintptr_t address) {
    /* ---- 0. per-address call-rate gate ---- */
    {
        ULONG ci = (ULONG)((address * 2654435761ULL) % CALL_RATE_SIZE);
        for (;;) {
            LONG cur = g_call_rate[ci];
            ULONG epoch = (ULONG)(InterlockedCompareExchange(&g_rate_epoch, 0, 0)) & 0xFFFFu;
            ULONG cur_epoch = ((ULONG)cur >> 16) & 0xFFFFu;
            ULONG cur_cnt = (ULONG)cur & 0xFFFFu;

            ULONG new_cnt;
            ULONG packed_u;
            LONG nxt;

            if (cur_epoch != epoch) {
                new_cnt = 1u;
            } else {
                if (cur_cnt >= SEND_CALL_LIMIT) return;
                new_cnt = cur_cnt + 1u;
            }

            packed_u = ((epoch & 0xFFFFu) << 16) | (new_cnt & 0xFFFFu);
            nxt = (LONG)packed_u;
            if (InterlockedCompareExchange(&g_call_rate[ci], nxt, cur) != cur) continue;

            if (new_cnt == SEND_CALL_LIMIT) {
                LONG slot = InterlockedIncrement(&g_pdisable_seq) & (LONG)PDISABLE_MASK;
                InterlockedExchange64(&g_pdisable[slot], (LONG64)(uintptr_t)address);
                InterlockedExchange(&g_need_apply, 1);
            }
            if (new_cnt >= SEND_CALL_LIMIT) return;
            break;
        }
    }

    /* ---- 1. Argument registers: direct check + struct dereference ----
     *
     * Trampoline push order → stack slot mapping:
     *   stack[-4]  = RCX (arg0 / this)   reg_code 0
     *   stack[-5]  = RDX (arg1)          reg_code 1
     *   stack[-10] = R8  (arg2)          reg_code 2
     *   stack[-11] = R9  (arg3)          reg_code 3
     */
    {
        static const int  arg_slots[]   = { -4,  -5,  -10,  -11 };
        static const int  arg_regcodes[] = {  0,   1,    2,    3 };
        for (int ai = 0; ai < 4; ai++) {
            uintptr_t val;
            __try { val = (uintptr_t)stack[arg_slots[ai]]; }
            __except(EXCEPTION_EXECUTE_HANDLER) { continue; }

            if (val < 0x10000 || val > 0x000F000000000000ULL) continue;

            /* Direct: treat register value as wchar_t* */
            try_push_text(address, val, arg_slots[ai],
                          MIN_STR_LEN, false);

            /* Struct dereference: treat value as object/struct pointer */
            scan_struct_deref(address, val, arg_regcodes[ai]);
        }
    }

    /* ---- 2. Stack arguments (direct only, no struct deref) ----
     *   stack[0]   = return address (skip)
     *   stack[1-4] = shadow space  (skip — uninitialized)
     *   stack[5+]  = true stack arguments (arg5 and above)
     */
    for (int i = 5; i < (int)SEND_STK_HI; i++) {
        uintptr_t val;
        __try { val = (uintptr_t)stack[i]; }
        __except(EXCEPTION_EXECUTE_HANDLER) { continue; }

        try_push_text(address, val, i, MIN_STR_LEN, false);
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
    0x48,0x83,0xEC,0x60,
    0xF3,0x0F,0x7F,0x04,0x24,
    0xF3,0x0F,0x7F,0x4C,0x24,0x10,
    0xF3,0x0F,0x7F,0x54,0x24,0x20,
    0xF3,0x0F,0x7F,0x5C,0x24,0x30,
    0xF3,0x0F,0x7F,0x64,0x24,0x40,
    0xF3,0x0F,0x7F,0x6C,0x24,0x50,
    0x48,0x8D,0x8C,0x24,0xE8,0x00,0x00,0x00,
    0x48,0xBA, 0,0,0,0,0,0,0,0,   /* +50: @addr  */
    0x48,0xB8, 0,0,0,0,0,0,0,0,   /* +60: @Send  */
    0x48,0x89,0xE3,
    0x48,0x83,0xE4,0xF0,
    0x48,0x83,0xEC,0x28,
    0xFF,0xD0,
    0x48,0x83,0xC4,0x28,
    0x48,0x89,0xDC,
    0xF3,0x0F,0x6F,0x6C,0x24,0x50,
    0xF3,0x0F,0x6F,0x64,0x24,0x40,
    0xF3,0x0F,0x6F,0x5C,0x24,0x30,
    0xF3,0x0F,0x6F,0x54,0x24,0x20,
    0xF3,0x0F,0x6F,0x6C,0x24,0x10,
    0xF3,0x0F,0x6F,0x04,0x24,
    0x48,0x83,0xC4,0x60,
    0x41,0x5F,0x41,0x5E,0x41,0x5D,0x41,0x5C,
    0x41,0x5B,0x41,0x5A,0x41,0x59,0x41,0x58,
    0x5F,0x5E,0x5D,0x5C,0x5A,0x59,0x5B,0x58,
    0x9D,
    0xFF,0x25,0x00,0x00,0x00,0x00,
    0,0,0,0,0,0,0,0               /* +182: @original */
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
 * Static pre-hook filter - IAT blacklist
 *
 * Functions whose EVERY resolvable direct import call (FF 15 / FF 25)
 * targets a "clearly unrelated" API are skipped before MH_CreateHook.
 *
 * CONSERVATIVE semantics:
 *   - Skipped only when: (a) at least one FF-15/FF-25 call decoded, AND
 *     (b) every such call resolves to a blacklisted IAT slot.
 *   - Unresolvable bytes, indirect/virtual calls (FF 10 / FF 50), and
 *     intra-module relative calls (E8) are NOT counted as blacklisted.
 *   - Zero decoded calls -> function is KEPT (relevance unknown).
 *
 * --- Extension point ---
 * Edit SKIP_IAT_KEYWORDS to tune the filter.  Entries are matched with
 * case-sensitive strstr; keep them specific to avoid false-positive
 * skips of legitimate text-processing functions.
 * ============================================================= */

/* Case-sensitive substrings matched against each imported function name.  */
static const char * const SKIP_IAT_KEYWORDS[] = {
    /* GDI rasterisation */
    "BitBlt", "StretchBlt", "PatBlt", "MaskBlt", "PlgBlt",
    /* DXGI present / flip */
    "Present",
    /* High-res timing - pure arithmetic, no string handling */
    "QueryPerformanceCounter", "QueryPerformanceFrequency",
    "timeGetTime", "timeBeginPeriod", "timeEndPeriod",
    /* Win32 waveform audio */
    "waveOutWrite", "waveOutOpen", "waveOutClose",
    "waveOutPrepareHeader", "waveOutUnprepareHeader",
    NULL  /* sentinel */
};

/* Sorted array of IAT *slot* VAs whose import name matched a keyword.
 * Built once by build_iat_blacklist(); freed on DLL_PROCESS_DETACH. */
static uintptr_t *g_iat_bl     = NULL;
static DWORD      g_iat_bl_cnt = 0;

static int _cmp_uptr(const void *a, const void *b) {
    uintptr_t x = *(const uintptr_t *)a;
    uintptr_t y = *(const uintptr_t *)b;
    return (x > y) - (x < y);
}

/* Walk the PE import directory; build g_iat_bl sorted by slot VA.
 * Must be called after g_img_base is set.                                 */
static void build_iat_blacklist(void) {
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)g_img_base;
    IMAGE_NT_HEADERS *nt  = (IMAGE_NT_HEADERS *)(g_img_base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY *idir =
        &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!idir->VirtualAddress || !idir->Size) { log_phase("iat_bl_built:0"); return; }

    /* Pass 1 - count matches to size the heap allocation. */
    DWORD capacity = 0;
    IMAGE_IMPORT_DESCRIPTOR *imp =
        (IMAGE_IMPORT_DESCRIPTOR *)(g_img_base + idir->VirtualAddress);
    for (; imp->Name; imp++) {
        if (!imp->OriginalFirstThunk) continue;
        uintptr_t *int_tbl = (uintptr_t *)(g_img_base + imp->OriginalFirstThunk);
        for (DWORD i = 0; int_tbl[i]; i++) {
            if (int_tbl[i] & IMAGE_ORDINAL_FLAG64) continue;
            const char *fname = (const char *)
                (g_img_base + (DWORD)int_tbl[i] + offsetof(IMAGE_IMPORT_BY_NAME, Name));
            for (const char * const *kw = SKIP_IAT_KEYWORDS; *kw; kw++)
                if (strstr(fname, *kw)) { capacity++; break; }
        }
    }
    if (!capacity) { log_phase("iat_bl_built:0"); return; }

    uintptr_t *buf = (uintptr_t *)HeapAlloc(
        GetProcessHeap(), 0, (size_t)capacity * sizeof(uintptr_t));
    if (!buf) return;

    /* Pass 2 - record the IAT slot VAs that matched. */
    DWORD filled = 0;
    imp = (IMAGE_IMPORT_DESCRIPTOR *)(g_img_base + idir->VirtualAddress);
    for (; imp->Name && filled < capacity; imp++) {
        if (!imp->OriginalFirstThunk) continue;
        uintptr_t *int_tbl = (uintptr_t *)(g_img_base + imp->OriginalFirstThunk);
        uintptr_t *iat_tbl = (uintptr_t *)(g_img_base + imp->FirstThunk);
        for (DWORD i = 0; int_tbl[i] && filled < capacity; i++) {
            if (int_tbl[i] & IMAGE_ORDINAL_FLAG64) continue;
            const char *fname = (const char *)
                (g_img_base + (DWORD)int_tbl[i] + offsetof(IMAGE_IMPORT_BY_NAME, Name));
            for (const char * const *kw = SKIP_IAT_KEYWORDS; *kw; kw++) {
                if (strstr(fname, *kw)) {
                    buf[filled++] = (uintptr_t)&iat_tbl[i];
                    break;
                }
            }
        }
    }
    qsort(buf, (size_t)filled, sizeof(uintptr_t), _cmp_uptr);
    g_iat_bl     = buf;
    g_iat_bl_cnt = filled;
    char mbuf[48];
    _snprintf_s(mbuf, sizeof(mbuf), _TRUNCATE, "iat_bl_built:%lu", (unsigned long)filled);
    log_phase(mbuf);
}

/* Returns true if every direct import call (FF 15 / FF 25 RIP-relative)
 * decoded from [fn, fn+fn_size) resolves to a blacklisted IAT slot, AND
 * at least one such call was found.  Conservatively returns false when
 * unsure (zero calls found, or any call resolves outside the blacklist). */
static bool fn_calls_only_blacklisted_imports(
        const BYTE *fn, DWORD fn_size) {
    if (!g_iat_bl_cnt || fn_size < 6) return false;
    DWORD total = 0, matched = 0;
    const BYTE *end = fn + fn_size - 5; /* need 6 bytes: FF 15/25 xx xx xx xx */
    for (const BYTE *p = fn; p < end; p++) {
        if (p[0] != 0xFF) continue;
        if (p[1] != 0x15 && p[1] != 0x25) continue;
        /* RIP-relative: next-insn address + signed disp32 = IAT slot VA */
        uintptr_t slot = (uintptr_t)(p + 6) + *(const int32_t *)(p + 2);
        total++;
        /* Binary search in sorted blacklist */
        DWORD lo = 0, hi = g_iat_bl_cnt;
        while (lo < hi) {
            DWORD mid = lo + (hi - lo) / 2;
            if      (g_iat_bl[mid] < slot) lo = mid + 1;
            else if (g_iat_bl[mid] > slot) hi = mid;
            else { matched++; break; }
        }
    }
    return total > 0 && total == matched;
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
    long newly                = 0;
    long skipped_outside_text = 0;
    long skipped_size         = 0;  /* below MIN_FN_BYTES */
    long skipped_iat          = 0;  /* IAT-blacklist filter */
    for (; g_pdata_pos < g_pdata_count
           && g_hook_count < (long)g_mh
           && (DWORD)newly < batch;
         g_pdata_pos++) {

        uintptr_t fn_addr = g_img_base + g_pdata[g_pdata_pos].BeginAddress;
        if (fn_addr < g_ts_base || fn_addr >= g_ts_base + g_ts_size) {
            skipped_outside_text++;
            continue;
        }

        /* ---- Size filter: skip obvious stubs MinHook would reject anyway ---- */
        DWORD fn_size = g_pdata[g_pdata_pos].EndAddress
                      - g_pdata[g_pdata_pos].BeginAddress;
        if (fn_size < MIN_FN_BYTES) { skipped_size++; continue; }

        /* ---- IAT blacklist: skip functions that only call unrelated APIs ---- */
        if (fn_calls_only_blacklisted_imports((const BYTE *)fn_addr, fn_size)) {
            skipped_iat++; continue;
        }

        DWORD fn_rva = g_pdata[g_pdata_pos].BeginAddress;

        BYTE *tramp = (BYTE*)g_trampolines + (size_t)g_hook_count * TRAMPOLINE_SIZE;
        void *orig  = NULL;

        memcpy(tramp, g_tpl, TRAMPOLINE_SIZE);
        *(uintptr_t*)(tramp + TRAMPOLINE_ADDR_OFFSET) = fn_addr;

        MH_STATUS mhs = MH_CreateHook((LPVOID)fn_addr, (LPVOID)tramp, &orig);
        if (mhs != MH_OK) {
            /* Log failures so unusual functions can be identified and added
             * to the skip list. MH_ERROR_UNSUPPORTED_FUNCTION is common for
             * very short functions; other errors may indicate relocate bugs. */
            char emsg[64];
            _snprintf_s(emsg, sizeof(emsg), _TRUNCATE,
                "mh_fail:0x%X st=%d", fn_rva, (int)mhs);
            log_phase(emsg);
            continue;
        }
        *(void**)(tramp + TRAMPOLINE_ORIG_OFFSET) = orig;
        MH_QueueEnableHook((LPVOID)fn_addr);
        g_hook_addrs[g_hook_count++] = (uint64_t)fn_addr;
        newly++;
    }

    /* Log per-batch filtering statistics. */
    {
        char sbuf[128];
        int n = _snprintf_s(sbuf, sizeof(sbuf), _TRUNCATE,
            "skip_nontext:%ld skip_size:%ld skip_iat:%ld",
            skipped_outside_text, skipped_size, skipped_iat);
        if (n > 0) log_phase(sbuf);
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
    log_phase("DO_SEARCH_ENTER");
    if (MH_Initialize()!=MH_OK) {
        pipe_write(0,0,1,"ERROR:MH_Initialize",19); return;
    }
    log_phase("MH_INIT_OK");

    /* Reset decayed call-rate limiter state for each search session. */
    ZeroMemory((void*)g_call_rate, sizeof(g_call_rate));
    ZeroMemory((void*)g_sptr_cache, sizeof(g_sptr_cache));
    ZeroMemory((void*)g_layout_cache, sizeof(g_layout_cache));
    InterlockedExchange(&g_rate_epoch, 1);
    InterlockedExchange64(&g_rate_next_decay_ms, (LONG64)(GetTickCount64() + RATE_WINDOW_MS));

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

    log_phase("BEFORE_TEXT_SCAN");
    g_ts_base=0; g_ts_size=0;
    for (WORD i=0;i<nt->FileHeader.NumberOfSections;i++)
        if (!memcmp(sec[i].Name,".text\0\0\0",8))
            { g_ts_base=g_img_base+sec[i].VirtualAddress; g_ts_size=sec[i].Misc.VirtualSize; break; }
    log_phase("AFTER_TEXT_SCAN");
    if (!g_ts_base) {
        pipe_write(0,0,1,"ERROR:no .text section found",29);
        log_phase("ERROR:no .text section found");
        MH_Uninitialize(); return;
    }
    
    /* Log .text segment range for verification */
    {
        char tbuf[80];
        int n = _snprintf_s(tbuf, sizeof(tbuf), _TRUNCATE,
            "text_seg:0x%llX-0x%llX sz=%lu",
            (unsigned long long)g_ts_base,
            (unsigned long long)(g_ts_base + g_ts_size),
            (unsigned long)g_ts_size);
        if (n > 0) log_phase(tbuf);
    }

    /* Build IAT blacklist from import directory (uses g_img_base). */
    build_iat_blacklist();

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

    DWORD dummy;
    freeze_all_threads();
    MH_ApplyQueued();
    VirtualProtect(trampolines, blksz, PAGE_EXECUTE_READ, &dummy);
    thaw_all_threads();
    pipe_write(0,0,1,"phase:hooks_applied",19);
    log_phase("phase:hooks_applied");
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
        rate_decay_tick();
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
                    freeze_all_threads();
                    MH_ApplyQueued();
                    thaw_all_threads();
                    char dbuf[48];
                    int n = _snprintf_s(dbuf, sizeof(dbuf), _TRUNCATE,
                        "disabled:%lu", (unsigned long)cnt);
                    pipe_write(0,0,1,dbuf, n>0?(DWORD)n:0);
                }

            } else if (cmd == CMD_SCAN_NEXT) {
                /* Payload: uint32_t batch_size */
                uint32_t nbatch = 0;
                if (!pipe_read_all(&nbatch, 4)) break;

                /* Freeze all threads first so the VirtualProtect(RWX) →
                 * trampoline write → MH_ApplyQueued → VirtualProtect(RX)
                 * sequence runs with no game thread able to race against
                 * MinHook's own page-permission transitions.               */
                freeze_all_threads();
                VirtualProtect(trampolines, blksz, PAGE_EXECUTE_READWRITE, &dummy);
                newly = scan_next_batch(nbatch);
                if (newly > 0) {
                    /* SEH table already covers full capacity -- no need to
                     * re-register.  Just apply the newly queued hooks.    */
                    MH_ApplyQueued();
                }
                VirtualProtect(trampolines, blksz, PAGE_EXECUTE_READ, &dummy);
                thaw_all_threads();

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
        char buf[1200];
        EXCEPTION_RECORD *er = ep->ExceptionRecord;
        CONTEXT          *ctx= ep->ContextRecord;
        uintptr_t gb = (uintptr_t)GetModuleHandleW(NULL);
        MEMORY_BASIC_INFORMATION mbi;
        SIZE_T vq = VirtualQuery((LPCVOID)(uintptr_t)ctx->Rip, &mbi, sizeof(mbi));
        long hook_slot = -1;
        for (long i = 0; i < g_hook_count; i++) {
            if ((uintptr_t)g_hook_addrs[i] == (uintptr_t)ctx->Rip) {
                hook_slot = i;
                break;
            }
        }
        uint32_t rip_rva32 = (uint32_t)((uintptr_t)ctx->Rip - gb);
        uint32_t fn_begin_rva = 0;
        uint32_t fn_end_rva = 0;
        long pdata_idx = -1;
        for (DWORD i = 0; i < g_pdata_count; i++) {
            uint32_t b = g_pdata[i].BeginAddress;
            uint32_t e = g_pdata[i].EndAddress;
            if (rip_rva32 >= b && rip_rva32 < e) {
                fn_begin_rva = b;
                fn_end_rva = e;
                pdata_idx = (long)i;
                break;
            }
        }
        long fn_hook_slot = -1;
        if (fn_begin_rva != 0) {
            uintptr_t fn_begin_va = gb + fn_begin_rva;
            for (long i = 0; i < g_hook_count; i++) {
                if ((uintptr_t)g_hook_addrs[i] == fn_begin_va) {
                    fn_hook_slot = i;
                    break;
                }
            }
        }
        BYTE rip_bytes[16] = {0};
        int rip_n = 0;
        __try {
            memcpy(rip_bytes, (const void*)(uintptr_t)ctx->Rip, sizeof(rip_bytes));
            rip_n = 16;
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            rip_n = 0;
        }
        BYTE fn_entry_bytes[16] = {0};
        int fn_entry_n = 0;
        if (fn_begin_rva != 0) {
            __try {
                memcpy(fn_entry_bytes, (const void*)(uintptr_t)(gb + fn_begin_rva), sizeof(fn_entry_bytes));
                fn_entry_n = 16;
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                fn_entry_n = 0;
            }
        }
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
            "unwind_tbl: 0x%016llX\r\n"
            "vq_ok:      %llu\r\n"
            "mbi.Base:   0x%016llX\r\n"
            "mbi.AllocB: 0x%016llX\r\n"
            "mbi.State:  0x%08X\r\n"
            "mbi.Prot:   0x%08X\r\n"
            "mbi.AProt:  0x%08X\r\n"
            "mbi.Type:   0x%08X\r\n"
            "hook_slot:  %ld\r\n"
            "rip_16:     %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X\r\n"
            "rip_n:      %d\r\n"
            "pdata_idx:  %ld\r\n"
            "fn_rva:     0x%08X-0x%08X\r\n"
            "fn_hook:    %ld\r\n"
            "fn_entry16: %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X %02X\r\n"
            "fn_entry_n: %d\r\n",
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
            (unsigned long long)(uintptr_t)g_unwind_table,
            (unsigned long long)vq,
            (unsigned long long)(uintptr_t)mbi.BaseAddress,
            (unsigned long long)(uintptr_t)mbi.AllocationBase,
            (unsigned)mbi.State,
            (unsigned)mbi.Protect,
            (unsigned)mbi.AllocationProtect,
            (unsigned)mbi.Type,
            hook_slot,
            rip_bytes[0], rip_bytes[1], rip_bytes[2], rip_bytes[3],
            rip_bytes[4], rip_bytes[5], rip_bytes[6], rip_bytes[7],
            rip_bytes[8], rip_bytes[9], rip_bytes[10], rip_bytes[11],
            rip_bytes[12], rip_bytes[13], rip_bytes[14], rip_bytes[15],
            rip_n,
            pdata_idx,
            fn_begin_rva,
            fn_end_rva,
            fn_hook_slot,
            fn_entry_bytes[0], fn_entry_bytes[1], fn_entry_bytes[2], fn_entry_bytes[3],
            fn_entry_bytes[4], fn_entry_bytes[5], fn_entry_bytes[6], fn_entry_bytes[7],
            fn_entry_bytes[8], fn_entry_bytes[9], fn_entry_bytes[10], fn_entry_bytes[11],
            fn_entry_bytes[12], fn_entry_bytes[13], fn_entry_bytes[14], fn_entry_bytes[15],
            fn_entry_n);
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
    log_phase("WORKER_THREAD_START");
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
    {
        char mbuf[64];
        _snprintf_s(mbuf, sizeof(mbuf), _TRUNCATE, "CFG_MODE=%d", (int)cfg.mode);
        log_phase(mbuf);
    }
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
        if(g_iat_bl)    HeapFree(GetProcessHeap(),0,g_iat_bl);
    }
    return TRUE;
}