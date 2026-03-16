/*
 * mem_scan.c — Fast byte-pattern search for JustReadIt memory scanner.
 *
 * Exports a single function that locates all occurrences of a short byte
 * pattern (needle) inside a larger byte buffer (haystack).  Used by
 * src/memory/_search.py via ctypes as an optional accelerator.
 *
 * The implementation uses memchr (first-byte filter) + memcmp, which
 * the MSVC CRT implements with SIMD (SSE2/AVX) on x64.  This gives
 * ~5-15 GB/s throughput for typical 4-8 byte CJK needles.
 *
 * Build:
 *   powershell -File src\memory\build.ps1
 *
 * Or use the VS Code build task "Build mem_scan.dll" (Ctrl+Shift+B).
 *
 * Copyright (c) JustReadIt contributors — MPL-2.0
 */

#include <stdint.h>
#include <string.h>

#ifdef _MSC_VER
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

/*
 * find_all — locate all occurrences of needle in haystack.
 *
 * Parameters
 * ----------
 * haystack, haystack_len : input buffer
 * needle, needle_len     : pattern to search for
 * out_positions          : caller-allocated array receiving byte offsets
 * max_results            : capacity of out_positions
 *
 * Returns
 * -------
 * Number of matches found (≤ max_results).
 *
 * Matches may overlap (stride = 1), matching the Python fallback behaviour.
 */
EXPORT int find_all(
    const uint8_t *haystack, size_t haystack_len,
    const uint8_t *needle,   size_t needle_len,
    size_t        *out_positions,
    size_t         max_results)
{
    if (!haystack || !needle || needle_len == 0 ||
        haystack_len < needle_len || max_results == 0 || !out_positions)
        return 0;

    int count = 0;
    const uint8_t first = needle[0];
    const size_t  end   = haystack_len - needle_len;
    size_t i = 0;

    while (i <= end && (size_t)count < max_results) {
        /* memchr scans for the first byte using SIMD on modern CRTs. */
        const uint8_t *p = (const uint8_t *)memchr(
            haystack + i, first, end - i + 1);
        if (!p)
            break;

        size_t pos = (size_t)(p - haystack);

        if (needle_len == 1 || memcmp(p + 1, needle + 1, needle_len - 1) == 0) {
            out_positions[count++] = pos;
        }

        i = pos + 1;
    }

    return count;
}
