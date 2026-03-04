'use strict';

/*
 * Frida instrumentation script for JustReadIt.
 *
 * Hooks Win32 text output functions and sends captured strings back to the
 * Python host via send().
 *
 * Targets (in priority order):
 *   gdi32 : TextOutW, ExtTextOutW
 *   user32: DrawTextW, DrawTextExW
 *   dwrite: IDWriteFactory::CreateTextLayout (captures string at layout time)
 *
 * Message shapes
 * --------------
 * Captured text : { type: 'text', value: '<string>' }
 * Startup diag  : { type: 'diag', value: '<info string>' }
 *
 * Filtering (applied in-process to reduce IPC overhead):
 *   - Empty / whitespace-only strings dropped.
 *   - Pure ASCII strings < 2 chars dropped (separator / glyph-index noise).
 *   - Consecutive identical strings suppressed.
 */

var _lastText = '';

function _onText(str) {
    if (!str || str.length === 0) return;
    var trimmed = str.replace(/^\s+|\s+$/g, '');
    if (trimmed.length === 0) return;
    if (trimmed.length < 2 && /^[\x00-\x7F]*$/.test(trimmed)) return;
    if (trimmed === _lastText) return;
    _lastText = trimmed;
    send({ type: 'text', value: trimmed });
}

// Module.findExportByName(moduleName, exportName) — static two-arg form was
// removed in Frida 16.1.  Use Process.getModuleByName() + instance method.
function _findExport(moduleName, exportName) {
    try {
        var mod = Process.getModuleByName(moduleName);
        return mod ? mod.findExportByName(exportName) : null;
    } catch (e) {
        return null;
    }
}

function _tryAttach(moduleName, exportName, onEnterFn) {
    var addr = _findExport(moduleName, exportName);
    if (addr) {
        try {
            Interceptor.attach(addr, { onEnter: onEnterFn });
            return true;
        } catch (e) { /* already instrumented or inaccessible address */ }
    }
    return false;
}

var _attached = [];

// ── gdi32 / gdi32full : TextOutW ────────────────────────────────────
// On modern Windows (Vista+) gdi32.dll is a thin stub; the real
// implementation lives in gdi32full.dll.  Try both so we intercept
// whichever the engine links against at runtime.
// BOOL TextOutW(HDC, int x, int y, LPCWSTR lpString, int c)
function _onTextOut(args) {
    var cchLen = args[4].toInt32();
    if (cchLen > 0) _onText(args[3].readUtf16String(cchLen));
}
if (_tryAttach('gdi32.dll',     'TextOutW', _onTextOut)) _attached.push('gdi32!TextOutW');
if (_tryAttach('gdi32full.dll', 'TextOutW', _onTextOut)) _attached.push('gdi32full!TextOutW');

// ── gdi32 / gdi32full : ExtTextOutW ──────────────────────────────────
// BOOL ExtTextOutW(HDC, int x, int y, UINT opts, RECT*, LPCWSTR, UINT c, INT*)
function _onExtTextOut(args) {
    var cchLen = args[6].toInt32();
    if (cchLen > 0 && !args[5].isNull()) _onText(args[5].readUtf16String(cchLen));
}
if (_tryAttach('gdi32.dll',     'ExtTextOutW', _onExtTextOut)) _attached.push('gdi32!ExtTextOutW');
if (_tryAttach('gdi32full.dll', 'ExtTextOutW', _onExtTextOut)) _attached.push('gdi32full!ExtTextOutW');

// ── user32!DrawTextW ──────────────────────────────────────────────────
// int DrawTextW(HDC, LPCWSTR lpString, int cchText, RECT*, UINT)
if (_tryAttach('user32.dll', 'DrawTextW', function(args) {
    if (args[1].isNull()) return;
    var cchText = args[2].toInt32();
    var s = cchText < 0 ? args[1].readUtf16String() : args[1].readUtf16String(cchText);
    _onText(s);
})) _attached.push('user32!DrawTextW');

// ── user32!DrawTextExW ────────────────────────────────────────────────
// int DrawTextExW(HDC, LPWSTR lpchText, int cchText, RECT*, UINT, DRAWTEXTPARAMS*)
if (_tryAttach('user32.dll', 'DrawTextExW', function(args) {
    if (args[1].isNull()) return;
    var cchText = args[2].toInt32();
    var s = cchText < 0 ? args[1].readUtf16String() : args[1].readUtf16String(cchText);
    _onText(s);
})) _attached.push('user32!DrawTextExW');

// ── dwrite!IDWriteFactory::CreateTextLayout ───────────────────────────
// Captures the string at layout-creation time (before any Draw call).
// Signature (x64): CreateTextLayout(this, string, stringLength, format,
//                                   maxW, maxH, **layout) -> HRESULT
// 'this' is args[0]; string is args[1]; length is args[2].
if (_tryAttach('dwrite.dll', 'CreateTextLayout', function(args) {
    var len = args[2].toInt32();
    if (len > 0 && !args[1].isNull()) _onText(args[1].readUtf16String(len));
})) _attached.push('dwrite!CreateTextLayout');

// ── Startup diagnostic ────────────────────────────────────────────────
var _loadedMods = Process.enumerateModules()
    .filter(function(m) {
        var n = m.name.toLowerCase();
        return n.indexOf('gdi') !== -1 || n.indexOf('dwrite') !== -1
            || n.indexOf('d2d') !== -1  || n.indexOf('user32') !== -1
            || n.indexOf('font') !== -1 || n.indexOf('text') !== -1;
    })
    .map(function(m) { return m.name; });

send({
    type: 'diag',
    value: 'hooks=' + (_attached.length ? _attached.join(', ') : 'NONE')
          + '  |  relevant-modules=' + _loadedMods.join(', ')
});
