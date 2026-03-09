"""Value objects for hook sites used by :class:`~src.hook.hook_search.HookSearcher`."""
from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# HookCode — serialisable reference to a confirmed hook site
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookCode:
    """Identifies a single hook site discovered by :class:`~src.hook.hook_search.HookSearcher`.

    Attributes
    ----------
    module:
        DLL / EXE name as reported by ``Process.enumerateModules()``,
        e.g. ``"ユニゾンコード.exe"``.
    rva:
        Relative virtual address of the function within the module.
        Stable across process restarts (ASLR changes the base, not the RVA).
    access_pattern:
        Memory-access pattern describing how the text pointer was found.
        Direct register:  ``"r0"``  (RCX value is the text pointer)
        L1 struct deref:  ``"r0+0x48"``  (*(RCX + 0x48) is text pointer)
        L2 struct deref:  ``"r0+0x48->0x10"``  (*(*(RCX+0x48) + 0x10))
        Stack argument:   ``"s+0x28"``  (stack arg at RSP+0x28)
    encoding:
        ``"utf16"`` or ``"utf8"``.
    """

    module: str
    rva: int
    access_pattern: str
    encoding: str

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_str(self) -> str:
        """Compact string for :class:`~src.config.AppConfig` storage.

        Format::

            <module>!<rva_hex>:<access_pattern>:<encoding>

        Example::

            ユニゾンコード.exe!0x1d6910:*(r0+0x14):utf16
        """
        return f"{self.module}!{self.rva:#x}:{self.access_pattern}:{self.encoding}"

    @classmethod
    def from_str(cls, s: str) -> "HookCode":
        """Parse a string produced by :meth:`to_str`.

        Accepts the legacy format where the access field was a bare digit
        (e.g. ``"...!0x1d6910:0:utf16"``) and converts it to ``"r0"``.

        Raises
        ------
        ValueError
            If the string is malformed.
        """
        try:
            module, rest = s.split("!", 1)
            rva_str, pattern, enc = rest.split(":", 2)
            # Backward-compat: single bare digit was the old arg_index.
            if pattern.isdigit():
                pattern = f"r{pattern}"
            return cls(
                module=module,
                rva=int(rva_str, 16),
                access_pattern=pattern,
                encoding=enc,
            )
        except Exception as exc:
            raise ValueError(f"Malformed HookCode string {s!r}: {exc}") from exc

    def to_hook_config_fields(self) -> tuple[int, int, int, int]:
        """Parse *access_pattern* into ``(arg_idx, deref, byte_offset, encoding_int)``.

        Used to build the C DLL ``Config`` struct for ``MODE_HOOK``.

        Supported patterns
        ------------------
        ``r0``              → (0, 0, 0, enc)        direct register
        ``r2``              → (2, 0, 0, enc)        direct register
        ``r0+0x48``         → (0, 1, 0x48, enc)     L1 struct deref
        ``r0+0x48->0x10``   → (0, 2, 0x48, enc)     L2 (off2 in high bits, TODO)
        ``*(r0+0x14)``      → (0, 1, 0x14, enc)     legacy format
        ``*(r1)``           → (1, 1, 0, enc)         legacy format
        ``*(s+0x28)``       → (0xFF, 1, 0x28, enc)   stack-relative (legacy)
        ``s+0x28``          → (0xFF, 0, 0x28, enc)   stack arg direct
        """
        enc_int = 0 if self.encoding == "utf16" else 1
        p = self.access_pattern.strip()

        # Normalise legacy "*(base+off)" → "*base+off"
        if p.startswith("*(") and p.endswith(")"):
            p = "*" + p[2:-1]

        deref = 1 if p.startswith("*") else 0
        if deref:
            p = p[1:]

        # Handle L2 "r0+0x48->0x10" pattern (arrow separates two offsets)
        if "->" in p:
            left, right = p.split("->", 1)
            base, _, off1_str = left.partition("+")
            byte_offset = int(off1_str, 16) if off1_str else 0
            # off2 stored in high 16 bits of byte_offset for now
            # (MODE_HOOK for L2 is not yet implemented — this preserves info)
            off2 = int(right, 16) if right else 0
            byte_offset = byte_offset | (off2 << 16)
            deref = 2
        else:
            base, _, offset_str = p.partition("+")
            byte_offset = int(offset_str, 16) if offset_str else 0
            # New-style "r0+0x48" (no leading *) is also a deref
            if offset_str and not deref:
                deref = 1

        if base == "s":
            arg_idx = 0xFF
        elif base.startswith("r") and base[1:].isdigit():
            arg_idx = int(base[1:])
        else:
            arg_idx = 0

        return (arg_idx, deref, byte_offset, enc_int)


# ---------------------------------------------------------------------------
# HookCandidate
# ---------------------------------------------------------------------------


@dataclass
class HookCandidate:
    """A candidate hook site returned by :class:`~src.hook.hook_search.HookSearcher`.

    Attributes
    ----------
    module, rva, access_pattern, encoding:
        Same as :class:`HookCode`.
    text:
        A representative text string captured at this site.
    hit_count:
        Number of times this site fired with CJK text.
    score:
        Ranking score; higher is better.
    """

    module: str
    rva: int
    access_pattern: str
    encoding: str
    text: str
    hit_count: int = 0
    score: float = 0.0
    hook_va: int = 0  # absolute VA as received from the pipe; used for confirmed-set filtering
    str_ptr: int = 0  # VA of the string in game memory (from Send's stack scan)

    def to_hook_code(self) -> HookCode:
        return HookCode(
            module=self.module,
            rva=self.rva,
            access_pattern=self.access_pattern,
            encoding=self.encoding,
        )

    def display_label(self) -> str:
        """Short label for UI display (sorted by RVA; no score)."""
        preview = self.text[:60].replace("\n", " ")
        return (
            f"+{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}"
            f"  ptr={self.str_ptr:#x}  {preview!r}"
        )

    def display_label_scored(self) -> str:
        """Label with score prefix, used in the Recommended tab."""
        preview = self.text[:55].replace("\n", " ")
        return (
            f"[{self.score:6.0f}]  +{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}"
            f"  ptr={self.str_ptr:#x}  {preview!r}"
        )

    def display_label_aggregated(self, n_sites: int) -> str:
        """Label for an aggregated group in the Recommended tab.

        Shows the representative's best access pattern and score, plus the
        total hit count and how many unique hook sites produced this text.
        """
        preview = self.text[:50].replace("\n", " ")
        return (
            f"[{self.score:6.0f}]  +{self.rva:#x}  {self.access_pattern}"
            f"  hits={self.hit_count}  sites={n_sites}  {preview!r}"
        )


# ---------------------------------------------------------------------------
# Proximity analysis
# ---------------------------------------------------------------------------


def group_by_str_ptr(
    candidates: list[HookCandidate],
    tolerance: int = 256,
) -> list[list[HookCandidate]]:
    """Group *candidates* whose ``str_ptr`` values fall within *tolerance* bytes.

    Useful for identifying hook sites that share the same string buffer
    (e.g. two call sites that both receive a pointer into the same object).

    Algorithm: sort by ``str_ptr``, then merge into a group while consecutive
    entries are within *tolerance* of the group's minimum address.  Candidates
    with ``str_ptr == 0`` are placed in a separate ``[unresolved]`` group at
    the end.

    Parameters
    ----------
    candidates:
        Any iterable of :class:`HookCandidate`.
    tolerance:
        Maximum byte distance between two ``str_ptr`` values to be considered
        "close".  Default 256 (covers typical struct-field offsets).

    Returns
    -------
    list[list[HookCandidate]]
        Groups sorted by the minimum ``str_ptr`` of each group, largest group
        first within the same address range.  Unresolved (ptr=0) group, if
        any, is appended last.
    """
    resolved   = [c for c in candidates if c.str_ptr != 0]
    unresolved = [c for c in candidates if c.str_ptr == 0]

    resolved.sort(key=lambda c: c.str_ptr)

    groups: list[list[HookCandidate]] = []
    for c in resolved:
        if groups and c.str_ptr - groups[-1][0].str_ptr <= tolerance:
            groups[-1].append(c)
        else:
            groups.append([c])

    # Sort each group by str_ptr, then sort groups: larger groups first,
    # tie-break by minimum str_ptr so the output is deterministic.
    for g in groups:
        g.sort(key=lambda c: c.str_ptr)
    groups.sort(key=lambda g: (-len(g), g[0].str_ptr))

    if unresolved:
        groups.append(unresolved)

    return groups


def aggregate_by_text(
    candidates: list[HookCandidate],
) -> list[tuple[HookCandidate, list[HookCandidate]]]:
    """Group *candidates* by their current ``text`` value.

    Returns a list of ``(representative, members)`` pairs sorted by
    representative score descending.  The *representative* is a **shallow
    copy** of the highest-scored member in each group, with ``hit_count``
    set to the sum across all members.  *members* is the full list of
    original :class:`HookCandidate` objects sharing that text.

    This is a pure presentation helper — the underlying candidate objects
    are not modified.
    """
    from copy import copy as _copy

    groups: dict[str, list[HookCandidate]] = {}
    for c in candidates:
        groups.setdefault(c.text, []).append(c)

    result: list[tuple[HookCandidate, list[HookCandidate]]] = []
    for _text, members in groups.items():
        best = max(members, key=lambda c: c.score)
        rep = _copy(best)
        rep.hit_count = sum(m.hit_count for m in members)
        result.append((rep, members))

    result.sort(key=lambda t: -t[0].score)
    return result


def format_ptr_groups(
    groups: list[list[HookCandidate]],
    *,
    min_group_size: int = 2,
) -> str:
    """Format the output of :func:`group_by_str_ptr` as a human-readable string.

    Parameters
    ----------
    groups:
        Return value of :func:`group_by_str_ptr`.
    min_group_size:
        Groups smaller than this are omitted from the output.  Set to 1 to
        show all.
    """
    lines: list[str] = []
    for i, group in enumerate(groups):
        if len(group) < min_group_size:
            continue
        ptrs = [c.str_ptr for c in group if c.str_ptr]
        span = max(ptrs) - min(ptrs) if len(ptrs) > 1 else 0
        header = (
            f"[group {i}]  {len(group)} candidates"
            + (f"  ptr range {min(ptrs):#x}–{max(ptrs):#x}  span={span} B" if ptrs else "  (unresolved)")
        )
        lines.append(header)
        for c in group:
            lines.append(f"    {c.display_label()}")
    return "\n".join(lines) if lines else "(no groups with >= {} members)".format(min_group_size)
