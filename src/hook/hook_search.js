'use strict';

/*
 * Hook search script v2 for JustReadIt.
 *
 * Scans all r-x memory ranges for function prologues and, for each candidate
 * function, probes multiple memory-access patterns to find CJK strings.
 *
 * Access patterns tried per function call (for each of RCX/RDX/R8/R9 and
 * key RSP slots):
 *
 *   1. Direct read           ptr.read{Utf16,Utf8}String()
 *   2. One deref             ptr.readPointer().read{...}String()
 *   3. Inline at offset      ptr.add(off).read{...}String()
 *   4. Pointer at offset     ptr.add(off).readPointer().read{...}String()
 *   5. Stack slots           rsp.add(N).readPointer().read{...}String()
 *
 * Candidate report: { type:'candidate', module, rva, pattern, encoding, text }
 *   pattern -- compact access expression, e.g. "r0", "*r1", "r0+0x14",
 *              "*(r2+0x8)", "*(s+0x28)"
 *
 * Config vars (injected from Python before this source):
 *   config.maxCandidates, config.maxHooks, config.scanLimitBytes
 *
 * NOTE on Memory.scanSync pattern syntax (frida-gum gum_match_pattern_seal):
 *   The pattern must NOT start or end with a wildcard token.
 *   All prologue patterns below therefore omit trailing wildcard bytes --
 *   the fixed prefix is already sufficient to locate the instruction.
 */

/* -- Config --------------------------------------------------------------- */
var _cfg = (typeof config !== 'undefined') ? config : {};
var _MAX_CANDIDATES = _cfg.maxCandidates  || 500;
var _MAX_HOOKS      = _cfg.maxHooks       || 5000;
var _SCAN_LIMIT     = _cfg.scanLimitBytes || (32 * 1024 * 1024);

/* -- Offsets to try for member-access patterns ----------------------------
 * Covers: raw ptr, std::wstring internal buffer, common struct layouts,
 * and the 0x14 offset seen in Textractor's HQ-14 result.
 */
var _MEMBER_OFFSETS = [
    0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C,
    0x20, 0x24, 0x28, 0x2C, 0x30, 0x38, 0x40, 0x48,
    0x50, 0x58, 0x60, 0x68, 0x70, 0x78, 0x80
];

/* RSP offsets: shadow space starts at +0x20, args 5+ at +0x28 onward */
var _RSP_OFFSETS = [0x20, 0x28, 0x30, 0x38, 0x40, 0x48, 0x50, 0x58];

/* -- State ---------------------------------------------------------------- */
var _seen  = {};
var _found = 0;

/* -- Helpers -------------------------------------------------------------- */

function _hasCJK(s) {
    return /[\u3000-\u9FFF\uFF00-\uFFEF\u2E80-\u2FFF]/.test(s);
}

function _readU16(ptr) {
    try {
        var s = ptr.readUtf16String(256);
        return (s && s.length >= 2) ? s : null;
    } catch (e) { return null; }
}

function _readU8(ptr) {
    try {
        var s = ptr.readUtf8String(256);
        return (s && s.length >= 2) ? s : null;
    } catch (e) { return null; }
}

function _deref(ptr) {
    try { return ptr.readPointer(); } catch (e) { return null; }
}

function _isReadable(ptr) {
    try { ptr.readU8(); return true; } catch (e) { return false; }
}

/* -- Module list (captured once at load time) ----------------------------- */

var _modules = Process.enumerateModules();
var _mainMod = null;
_modules.forEach(function (m) {
    if (m.path.toLowerCase().endsWith('.exe')) {
        if (!_mainMod || m.size > _mainMod.size) _mainMod = m;
    }
});

/* Return the module that contains addr, or null for dynamic allocations. */
function _modForAddr(addr) {
    for (var i = 0; i < _modules.length; i++) {
        var m = _modules[i];
        if (addr.compare(m.base) >= 0 && addr.compare(m.base.add(m.size)) < 0)
            return m;
    }
    return null;
}

/* -- Scan & instrument ---------------------------------------------------- */

if (!_mainMod) {
    send({ type: 'diag', value: 'ERROR: Could not find main .exe module.' });
} else {
    send({
        type: 'diag',
        value: 'Main module: ' + _mainMod.name
            + '  base=' + _mainMod.base + '  size=' + _mainMod.size
    });

    /*
     * Prologue patterns -- trailing wildcard bytes are intentionally omitted
     * because frida-gum rejects any pattern whose last token is GUM_MATCH_WILDCARD
     * (gum_match_pattern_seal, gummemory.c).  The fixed-byte prefix is sufficient
     * to identify each prologue variant.
     */
    var _PROLOGUE_PATS = [
        '48 83 EC',         /* sub rsp, imm8          (imm omitted -- wildcard) */
        '48 81 EC',         /* sub rsp, imm32         (imm omitted)             */
        '40 53 48 83 EC',   /* push rbx; sub rsp      (imm omitted)             */
        '40 55 48 83 EC',   /* push rbp; sub rsp      (imm omitted)             */
        '40 56 48 83 EC',   /* push rsi; sub rsp      (imm omitted)             */
        '40 57 48 83 EC',   /* push rdi; sub rsp      (imm omitted)             */
        '41 54 48 83 EC',   /* push r12; sub rsp      (imm omitted)             */
        '41 55 48 83 EC',   /* push r13; sub rsp      (imm omitted)             */
        '41 56 48 83 EC',   /* push r14; sub rsp      (imm omitted)             */
        '41 57 48 83 EC',   /* push r15; sub rsp      (imm omitted)             */
        '55 48 89 E5'       /* push rbp; mov rbp,rsp  (GCC-style, no wildcard)  */
    ];

    var _hookAddrSet   = {};
    var _rangesScanned = 0;

    Process.enumerateRanges('r-x').forEach(function (range) {
        _PROLOGUE_PATS.forEach(function (pat) {
            try {
                Memory.scanSync(range.base, range.size, pat).forEach(function (h) {
                    _hookAddrSet[h.address] = true;
                });
            } catch (e) {}
        });
        _rangesScanned++;
    });

    var _addrs = Object.keys(_hookAddrSet);
    send({
        type: 'diag',
        value: 'Executable ranges scanned: ' + _rangesScanned
            + '  prologues found: ' + _addrs.length
            + '  -- instrumenting first ' + Math.min(_addrs.length, _MAX_HOOKS)
    });

    var _hookCount = 0;

    for (var _i = 0; _i < _addrs.length && _hookCount < _MAX_HOOKS; _i++) {
        (function (addr) {
            try {
                Interceptor.attach(addr, {
                    onEnter: function (args) {
                        if (_found >= _MAX_CANDIDATES) return;
                        _probeArgs(addr, args, this.context.rsp);
                    }
                });
                _hookCount++;
            } catch (e) {}
        })(ptr(_addrs[_i]));
    }

    send({ type: 'scan_done', hookCount: _hookCount });
}

/* -- Multi-pattern probe -------------------------------------------------- */

function _probeArgs(addr, args, rsp) {
    var regNames = ['r0', 'r1', 'r2', 'r3'];

    for (var j = 0; j < 4; j++) {
        var p = args[j];
        if (!p || p.isNull() || !_isReadable(p)) continue;
        var rn = regNames[j];

        /* 1. Direct */
        _tryReport(addr, rn, 'utf16', _readU16(p));
        _tryReport(addr, rn, 'utf8',  _readU8(p));

        /* 2. One deref (*reg = pointer-to-string) */
        var dp = _deref(p);
        if (dp && !dp.isNull() && _isReadable(dp)) {
            _tryReport(addr, '*' + rn, 'utf16', _readU16(dp));
            _tryReport(addr, '*' + rn, 'utf8',  _readU8(dp));
        }

        /* 3+4. Inline / pointer at member offsets */
        for (var k = 0; k < _MEMBER_OFFSETS.length; k++) {
            var off    = _MEMBER_OFFSETS[k];
            var offHex = '0x' + off.toString(16);
            var mp = null;
            try { mp = p.add(off); } catch (e) { continue; }
            if (!mp || !_isReadable(mp)) continue;

            /* 3. Inline string bytes at offset */
            _tryReport(addr, rn + '+' + offHex, 'utf16', _readU16(mp));
            _tryReport(addr, rn + '+' + offHex, 'utf8',  _readU8(mp));

            /* 4. Pointer-to-string stored as member at offset */
            var mpp = _deref(mp);
            if (mpp && !mpp.isNull() && _isReadable(mpp)) {
                _tryReport(addr, '*(' + rn + '+' + offHex + ')', 'utf16', _readU16(mpp));
                _tryReport(addr, '*(' + rn + '+' + offHex + ')', 'utf8',  _readU8(mpp));
            }
        }
    }

    /* 5. Stack slots (RSP-relative): args 5+ passed on the stack */
    if (rsp && !rsp.isNull()) {
        for (var s = 0; s < _RSP_OFFSETS.length; s++) {
            var sOff = _RSP_OFFSETS[s];
            var sp = null;
            try { sp = rsp.add(sOff); } catch (e) { continue; }
            if (!sp || !_isReadable(sp)) continue;
            var spp = _deref(sp);
            if (!spp || spp.isNull() || !_isReadable(spp)) continue;
            var sHex = '0x' + sOff.toString(16);
            _tryReport(addr, '*(s+' + sHex + ')', 'utf16', _readU16(spp));
            _tryReport(addr, '*(s+' + sHex + ')', 'utf8',  _readU8(spp));
        }
    }
}

/* -- Candidate reporter --------------------------------------------------- */

function _tryReport(addr, pattern, encoding, text) {
    if (!text || !_hasCJK(text)) return;
    if (_found >= _MAX_CANDIDATES) return;

    var mod     = _modForAddr(addr);
    var modName = mod ? mod.name : '<dynamic>';
    var rva     = mod ? addr.sub(mod.base).toString() : addr.toString();
    var key     = rva + '|' + modName + '|' + pattern + '|' + encoding + '|' + text.substring(0, 12);
    if (_seen[key]) return;
    _seen[key] = true;
    _found++;

    send({
        type:     'candidate',
        module:   modName,
        rva:      rva,
        pattern:  pattern,
        encoding: encoding,
        text:     text
    });
}

/* EOF */