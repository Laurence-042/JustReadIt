'use strict';

/*
 * Diagnostic Frida script for JustReadIt.
 *
 * Enumerates all loaded modules, scans for font/text-related exports
 * (FreeType, HarfBuzz, engine-internal), and hooks glyph rendering APIs
 * to determine how the target engine renders text.
 *
 * Message shapes (all sent as { type: 'diag', value: '…' }):
 *
 *   Phase 1 — ALL LOADED MODULES        (immediate)
 *   Phase 2 — GAME MODULE EXPORTS       (immediate, non-system DLLs only)
 *   Phase 2 — FREETYPE / HARFBUZZ SCAN  (immediate, all DLLs)
 *   Phase 3 — FONT CREATED              (live, as CreateFontIndirectW fires)
 *   Phase 3 — MBTOWC CJK               (live, CJK MultiByteToWideChar hits)
 *   Phase 3 — GLYPH OUTLINE CHARS       (periodic, every 2 s)
 *   Phase 3 — DIAGNOSTIC HOOKS          (immediate, summary of attached hooks)
 */

/* ═══════════════════════════════════════════════════════════════════════
 * Helpers
 * ═══════════════════════════════════════════════════════════════════════ */

function _findExport(moduleName, exportName) {
    try {
        var mod = Process.getModuleByName(moduleName);
        return mod ? mod.findExportByName(exportName) : null;
    } catch (e) {
        return null;
    }
}

/* ═══════════════════════════════════════════════════════════════════════
 * Phase 1 — Full module enumeration
 * ═══════════════════════════════════════════════════════════════════════ */

var _allMods = Process.enumerateModules();

send({
    type: 'diag',
    value: '── ALL LOADED MODULES (' + _allMods.length + ') ──\n'
          + _allMods.map(function(m) { return m.name; }).join('\n')
});

/* ═══════════════════════════════════════════════════════════════════════
 * Phase 2 — Export scanning
 *
 *  • Non-system modules (not under \Windows\): enumerate ALL exports.
 *    These reveal the engine's internal API surface.
 *  • System modules: only look for FreeType (FT_*) / HarfBuzz (hb_*)
 *    exports (in case they are loaded from a system path).
 * ═══════════════════════════════════════════════════════════════════════ */

var _gameMods = _allMods.filter(function(m) {
    return m.path.toLowerCase().indexOf('\\windows\\') === -1;
});

// Report game module exports (grouped by module)
_gameMods.forEach(function(mod) {
    try {
        var exps = mod.enumerateExports();
        if (exps.length === 0) return;
        var names = exps.map(function(e) { return e.type + ' ' + e.name; });
        send({
            type: 'diag',
            value: '── EXPORTS: ' + mod.name + ' (' + exps.length + ') ──\n'
                  + names.join('\n')
        });
    } catch(e) { /* module may deny enumeration */ }
});

// Scan all modules for FreeType / HarfBuzz
var _ftExports = [];
var _hbExports = [];

_allMods.forEach(function(mod) {
    try {
        var exps = mod.enumerateExports();
        exps.forEach(function(e) {
            if (/^FT_/.test(e.name))  _ftExports.push(mod.name + '!' + e.name);
            if (/^hb_/.test(e.name))  _hbExports.push(mod.name + '!' + e.name);
        });
    } catch(e) {}
});

send({
    type: 'diag',
    value: '── FREETYPE EXPORTS (' + _ftExports.length + ') ──\n'
          + (_ftExports.length ? _ftExports.join('\n') : '(none found in any module)')
});

if (_hbExports.length) {
    send({
        type: 'diag',
        value: '── HARFBUZZ EXPORTS (' + _hbExports.length + ') ──\n'
              + _hbExports.join('\n')
    });
}

/* ═══════════════════════════════════════════════════════════════════════
 * Phase 3 — Live hooks on glyph / font / encoding APIs
 * ═══════════════════════════════════════════════════════════════════════ */

var _attached = [];

// ── GetGlyphOutlineW ─────────────────────────────────────────────────
// DWORD GetGlyphOutlineW(HDC, UINT uChar, UINT fuFormat,
//                        LPGLYPHMETRICS, DWORD, LPVOID, MAT2*)
// uChar — the character code point being rasterised.

var _glyphChars = {};
var _glyphCount = 0;
var _glyphReported = 0;

function _onGetGlyphOutline(args) {
    var uChar = args[1].toUInt32();
    if (uChar > 0x20) {
        var ch = String.fromCharCode(uChar);
        if (!_glyphChars[ch]) {
            _glyphChars[ch] = true;
            _glyphCount++;
        }
    }
}

['gdi32.dll', 'gdi32full.dll'].forEach(function(dll) {
    var addr = _findExport(dll, 'GetGlyphOutlineW');
    if (addr) {
        try {
            Interceptor.attach(addr, { onEnter: _onGetGlyphOutline });
            _attached.push(dll.replace('.dll', '') + '!GetGlyphOutlineW');
        } catch(e) {}
    }
});

// ── CreateFontIndirectW ──────────────────────────────────────────────
// HFONT CreateFontIndirectW(const LOGFONTW* lplf)
// LOGFONTW.lfFaceName starts at byte offset 28 (WCHAR[32]).

var _fonts = {};

function _onCreateFontIndirect(args) {
    if (args[0].isNull()) return;
    try {
        var faceName = args[0].add(28).readUtf16String();
        if (faceName && !_fonts[faceName]) {
            _fonts[faceName] = true;
            send({ type: 'diag', value: '── FONT CREATED ── ' + faceName });
        }
    } catch(e) {}
}

['gdi32.dll', 'gdi32full.dll'].forEach(function(dll) {
    var addr = _findExport(dll, 'CreateFontIndirectW');
    if (addr) {
        try {
            Interceptor.attach(addr, { onEnter: _onCreateFontIndirect });
            _attached.push(dll.replace('.dll', '') + '!CreateFontIndirectW');
        } catch(e) {}
    }
});

// ── MultiByteToWideChar (onLeave — read converted wide string) ───────
// int MultiByteToWideChar(UINT CodePage, DWORD dwFlags,
//                         LPCCH lpMBStr, int cbMB,
//                         LPWSTR lpWCStr, int cchWC)
// We capture args in onEnter, then read the output buffer in onLeave.
// Only report strings containing CJK characters (U+3000..U+9FFF,
// fullwidth forms, katakana, etc.) to suppress noise.

var _mbSeen = {};
var _mbCount = 0;
var _MB_REPORT_LIMIT = 100;

var _mbAddr = _findExport('kernel32.dll', 'MultiByteToWideChar');
if (_mbAddr) {
    try {
        Interceptor.attach(_mbAddr, {
            onEnter: function(args) {
                this._outBuf = args[4];
                this._outLen = args[5].toInt32();
            },
            onLeave: function(retval) {
                var len = retval.toInt32();
                if (len <= 2 || this._outBuf.isNull()) return;
                try {
                    var s = this._outBuf.readUtf16String(len);
                    if (!s) return;
                    // Quick CJK check (hiragana, katakana, CJK unified, fullwidth)
                    if (!/[\u3000-\u9FFF\uFF00-\uFFEF]/.test(s)) return;
                    if (_mbSeen[s]) return;
                    _mbSeen[s] = true;
                    _mbCount++;
                    if (_mbCount <= _MB_REPORT_LIMIT) {
                        send({ type: 'diag', value: '── MBTOWC CJK ── ' + s });
                    }
                } catch(e) {}
            }
        });
        _attached.push('kernel32!MultiByteToWideChar');
    } catch(e) {}
}

// ── FreeType hooks (if FreeType was found) ───────────────────────────
// FT_Load_Char(FT_Face face, FT_ULong char_code, FT_Int32 load_flags)
// char_code is args[1] (the character code point).

var _ftChars = {};
var _ftCount = 0;
var _ftReported = 0;

_ftExports.forEach(function(qualified) {
    if (qualified.indexOf('!FT_Load_Char') !== -1 || qualified.indexOf('!FT_Load_Glyph') !== -1) {
        var parts = qualified.split('!');
        var modName = parts[0];
        var fnName  = parts[1];
        var addr = _findExport(modName, fnName);
        if (addr) {
            try {
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        // For FT_Load_Char: args[1] = char_code
                        // For FT_Load_Glyph: args[1] = glyph_index (less useful)
                        if (fnName === 'FT_Load_Char') {
                            var code = args[1].toUInt32();
                            if (code > 0x20) {
                                var ch = String.fromCodePoint(code);
                                if (!_ftChars[ch]) {
                                    _ftChars[ch] = true;
                                    _ftCount++;
                                }
                            }
                        }
                    }
                });
                _attached.push(modName + '!' + fnName);
            } catch(e) {}
        }
    }
});

// ── Periodic glyph reports ───────────────────────────────────────────

setInterval(function() {
    if (_glyphCount > _glyphReported) {
        var chars = Object.keys(_glyphChars).join('');
        send({
            type: 'diag',
            value: '── GLYPH OUTLINE CHARS (' + _glyphCount + ' unique) ──\n' + chars
        });
        _glyphReported = _glyphCount;
    }
    if (_ftCount > _ftReported) {
        var chars = Object.keys(_ftChars).join('');
        send({
            type: 'diag',
            value: '── FREETYPE CHARS (' + _ftCount + ' unique) ──\n' + chars
        });
        _ftReported = _ftCount;
    }
}, 2000);

// ── Phase 3 summary ─────────────────────────────────────────────────

send({
    type: 'diag',
    value: '── DIAGNOSTIC HOOKS ──\n'
          + (_attached.length ? _attached.join('\n') : 'NONE — no glyph/font APIs could be hooked')
});
