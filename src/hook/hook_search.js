'use strict';

/*
 * Hook-search script v4 for JustReadIt.
 *
 * Phase 1 -- Auto memory scan (runs immediately on load):
 *   Scan all rw- ranges for null-terminated UTF-16LE strings containing
 *   CJK characters.  Report each as { type:'string_found', ... }.
 *   Send { type:'scan_done', count } when finished.
 *   No user input required.
 *
 * Phase 2 -- Read surveillance (triggered by Python after user picks strings):
 *   Python posts { type:'watch', addresses:['0x...', ...] } for the
 *   addresses that correspond to the text currently on screen.
 *   MemoryAccessMonitor (read) is armed on those addresses.
 *   The game render loop reads the buffer every frame, so the callback
 *   fires within milliseconds -- the user does not need to do anything.
 *
 * Phase 3 -- Candidate identification:
 *   When a read fires, walk backward to find the enclosing function start,
 *   attach a one-off Interceptor, determine which arg/offset equals the
 *   monitored address, and report { type:'candidate', ... }.
 *
 * Config (injected by Python):
 *   config.maxCandidates  -- stop after N unique hook sites  (default 50)
 *   config.maxStrings     -- stop reporting after N strings   (default 200)
 *
 * Messages emitted:
 *   { type:'string_found', address, encoding, text }
 *   { type:'scan_done',    count }
 *   { type:'candidate',    module, rva, pattern, encoding, text }
 *   { type:'diag',         value }
 */

/* -- Config --------------------------------------------------------------- */
var _cfg        = (typeof config !== 'undefined') ? config : {};
var _MAX_CANDS  = _cfg.maxCandidates || 50;
var _MAX_STRS   = _cfg.maxStrings    || 200;

/* -- Helpers -------------------------------------------------------------- */

/* Maximum bytes read per rw- range (avoids OOM on large mapped files). */
var _MAX_RANGE_BYTES = 8 * 1024 * 1024;

var _MIN_STR_CHARS = 3;
var _MAX_STR_CHARS = 2000;

function _isCJK(c) {
    return (c >= 0x3000 && c <= 0x9FFF) ||
           (c >= 0xFF00 && c <= 0xFFEF) ||
           (c >= 0x2E80 && c <= 0x2FFF);
}

/* Module list captured once. */
var _modules = Process.enumerateModules();

function _modForAddr(addr) {
    for (var i = 0; i < _modules.length; i++) {
        var m = _modules[i];
        if (addr.compare(m.base) >= 0 && addr.compare(m.base.add(m.size)) < 0)
            return m;
    }
    return null;
}

/* Walk backward up to 512 bytes to find the enclosing function start. */
var _BOUNDARY = { 0xCC: true, 0x90: true, 0xC3: true, 0xC2: true };

function _findFunctionStart(instrAddr) {
    for (var back = 4; back <= 512; back++) {
        var a = instrAddr.sub(back);
        var bnd;
        try { bnd = a.sub(1).readU8(); } catch (e) { continue; }
        if (!_BOUNDARY[bnd]) continue;

        var b0, b1, b2;
        try { b0 = a.readU8(); b1 = a.add(1).readU8(); b2 = a.add(2).readU8(); } catch (e) { continue; }

        if (b0 === 0x48 && b1 === 0x83 && b2 === 0xEC) return a; /* sub rsp, imm8  */
        if (b0 === 0x48 && b1 === 0x81 && b2 === 0xEC) return a; /* sub rsp, imm32 */
        if (b0 === 0x55 && b1 === 0x48 && b2 === 0x89) return a; /* push rbp; mov rbp,rsp */
        if (b0 === 0x41 && b1 >= 0x50 && b1 <= 0x57) {
            var d3, d4;
            try { d3 = a.add(2).readU8(); d4 = a.add(3).readU8(); } catch (e) { continue; }
            if (d3 === 0x48 && (d4 === 0x83 || d4 === 0x81)) return a;
        }
        if (b0 === 0x40 && b1 >= 0x50 && b1 <= 0x57) {
            var e3, e4;
            try { e3 = a.add(2).readU8(); e4 = a.add(3).readU8(); } catch (e) { continue; }
            if (e3 === 0x48 && (e4 === 0x83 || e4 === 0x81)) return a;
        }
    }
    try {
        var sym = DebugSymbol.getFunctionByAddress(instrAddr);
        if (sym && sym.address && !sym.address.isNull()) return sym.address;
    } catch (e) {}
    return null;
}

/* Probe which argument / RSP slot equals targetAddr.
 * Returns e.g. "r0", "*(r1+0x14)", "*(s+0x28)", or null. */
var _OFFSETS  = [0x00,0x04,0x08,0x0C,0x10,0x14,0x18,0x1C,
                 0x20,0x24,0x28,0x2C,0x30,0x38,0x40,0x48,
                 0x50,0x58,0x60,0x68,0x70,0x78,0x80];
var _RSP_OFFS = [0x20,0x28,0x30,0x38,0x40,0x48,0x50,0x58];

function _probeForAddr(args, rsp, target) {
    var tStr = target.toString();
    var regN = ['r0','r1','r2','r3'];

    for (var i = 0; i < 4; i++) {
        var p = args[i];
        try { if (!p || p.isNull()) continue; } catch (e) { continue; }

        try { if (p.toString() === tStr) return regN[i]; } catch (e) {}

        for (var k = 0; k < _OFFSETS.length; k++) {
            var off = _OFFSETS[k];
            var hex = '0x' + off.toString(16);
            try { if (p.add(off).toString() === tStr) return regN[i] + '+' + hex; } catch (e) {}
            try {
                if (p.add(off).readPointer().toString() === tStr)
                    return '*(' + regN[i] + '+' + hex + ')';
            } catch (e) {}
        }
    }

    if (rsp) {
        try { if (rsp.isNull()) return null; } catch (e) { return null; }
        for (var s = 0; s < _RSP_OFFS.length; s++) {
            var sOff = _RSP_OFFS[s];
            var sHex = '0x' + sOff.toString(16);
            try { if (rsp.add(sOff).toString() === tStr) return 's+' + sHex; } catch (e) {}
            try {
                if (rsp.add(sOff).readPointer().toString() === tStr)
                    return '*(s+' + sHex + ')';
            } catch (e) {}
        }
    }
    return null;
}

function _readText(addr) {
    try {
        var u16 = addr.readUtf16String(512);
        if (u16 && u16.length >= 2) return { text: u16, encoding: 'utf16' };
    } catch (e) {}
    try {
        var u8 = addr.readUtf8String(512);
        if (u8 && u8.length >= 2) return { text: u8, encoding: 'utf8' };
    } catch (e) {}
    return null;
}

/* -- Phase 1: scan rw- memory for CJK strings ----------------------------- */

/* Scan one ArrayBuffer for UTF-16LE CJK strings.
 * Returns array of { byteOffset, text }. */
function _scanBufForCJK(buf, capBytes) {
    var view      = new DataView(buf);
    var wordCount = Math.floor(capBytes / 2);
    var results   = [];
    var i = 0;

    while (i < wordCount) {
        var c = view.getUint16(i * 2, true); /* little-endian */

        if (c === 0 || (c < 0x20 && c !== 0x09 && c !== 0x0A && c !== 0x0D)) {
            i++;
            continue;
        }

        var start  = i;
        var chars  = [];
        var hasCJK = false;

        while (i < wordCount && chars.length < _MAX_STR_CHARS) {
            var cc = view.getUint16(i * 2, true);
            if (cc === 0) break;
            if (cc < 0x20 && cc !== 0x09 && cc !== 0x0A && cc !== 0x0D) break;
            chars.push(cc);
            if (_isCJK(cc)) hasCJK = true;
            i++;
        }

        if (chars.length >= _MIN_STR_CHARS && hasCJK) {
            results.push({
                byteOffset: start * 2,
                text: String.fromCharCode.apply(null, chars)
            });
        }
        i++; /* skip null / break char */
    }
    return results;
}

var _strCount  = 0;
var _addrIndex = {}; /* hex-string → encoding, used in Phase 2 */

send({ type: 'diag', value: 'Phase 1: scanning rw memory for CJK strings...' });

Process.enumerateRanges('rw-').forEach(function (range) {
    if (_strCount >= _MAX_STRS) return;

    var cap = Math.min(range.size, _MAX_RANGE_BYTES);
    var buf;
    try { buf = range.base.readByteArray(cap); } catch (e) { return; }
    if (!buf) return;

    var found = _scanBufForCJK(buf, cap);
    for (var fi = 0; fi < found.length && _strCount < _MAX_STRS; fi++) {
        var addr = range.base.add(found[fi].byteOffset);
        var key  = addr.toString();
        if (!_addrIndex[key]) {
            _addrIndex[key] = 'utf16';
            _strCount++;
            send({
                type:     'string_found',
                address:  key,
                encoding: 'utf16',
                text:     found[fi].text
            });
        }
    }
});

send({ type: 'scan_done', count: _strCount });
send({ type: 'diag', value: 'Scan complete: ' + _strCount + ' CJK string(s) found.' });

/* -- Phase 2: wait for Python to post the addresses to watch -------------- */

var _probed    = {};
var _candCount = 0;

function _armMonitor(addresses) {
    if (!addresses || addresses.length === 0) return;

    var monRanges = [];
    for (var ai = 0; ai < addresses.length; ai++)
        monRanges.push({ base: ptr(addresses[ai]), size: 4 });

    function arm() {
        try {
            MemoryAccessMonitor.enable(monRanges, {
                onAccess: function (details) {
                    if (_candCount >= _MAX_CANDS) return;
                    if (details.operation !== 'read') return;

                    var instrAddr = details.from;
                    var textAddr  = details.address;

                    if (!_modForAddr(instrAddr)) return;

                    var fnStart = _findFunctionStart(instrAddr);
                    if (!fnStart) return;

                    var fnKey = fnStart.toString();
                    if (_probed[fnKey]) return;
                    _probed[fnKey] = true;

                    var enc = _addrIndex[textAddr.toString()] || 'utf16';
                    (function (capturedFn, capturedTarget, capturedEnc) {
                        try {
                            Interceptor.attach(capturedFn, {
                                onEnter: function (args) {
                                    var pattern = _probeForAddr(
                                        args, this.context.rsp, capturedTarget
                                    );
                                    if (!pattern) return;

                                    _candCount++;
                                    this.detach(); /* one-shot */

                                    var read = _readText(capturedTarget);
                                    var text = read ? read.text : '';
                                    var enc2 = read ? read.encoding : capturedEnc;

                                    var fnMod = _modForAddr(capturedFn);
                                    send({
                                        type:     'candidate',
                                        module:   fnMod ? fnMod.name : '<dynamic>',
                                        rva:      fnMod
                                                    ? capturedFn.sub(fnMod.base).toString()
                                                    : capturedFn.toString(),
                                        pattern:  pattern,
                                        encoding: enc2,
                                        text:     text
                                    });
                                }
                            });
                        } catch (e) {
                            _probed[capturedFn.toString()] = false;
                            send({
                                type:  'diag',
                                value: 'Interceptor.attach failed @ ' + capturedFn + ': ' + e.message
                            });
                        }
                    })(fnStart, textAddr, enc);

                    if (_candCount < _MAX_CANDS) {
                        try { arm(); } catch (e) {}
                    }
                }
            });
        } catch (e) {
            send({ type: 'diag', value: 'MemoryAccessMonitor.enable failed: ' + e.message });
        }
    }

    arm();
    send({
        type:  'diag',
        value: 'Phase 2: watching ' + addresses.length + ' address(es) for reads.'
    });
}

recv('watch', function (msg) {
    _armMonitor(msg.addresses || []);
});

/* EOF */