"""Value objects for hook sites used by :class:`~src.hook.hook_search.HookSearcher`."""
from __future__ import annotations

from copy import copy as _copy
from dataclasses import dataclass, field


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
    first_seen_seq: int = 0  # monotonic sequence number assigned on first hit

    def to_hook_code(self) -> HookCode:
        return HookCode(
            module=self.module,
            rva=self.rva,
            access_pattern=self.access_pattern,
            encoding=self.encoding,
        )

    def display_label(self) -> str:
        """Short label for UI display (sorted by RVA; no score)."""
        preview = self.text[:120].replace("\n", " ")
        return (
            f"+{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}"
            f"  ptr={self.str_ptr:#x}  {preview!r}"
        )

    def display_label_scored(self) -> str:
        """Label with score prefix, used in the Recommended tab."""
        preview = self.text[:120].replace("\n", " ")
        return (
            f"[{self.score:6.0f}]  +{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}"
            f"  ptr={self.str_ptr:#x}  {preview!r}"
        )

    def display_label_aggregated(self, n_sites: int, n_structs: int = 1) -> str:
        """Label for an aggregated text group in the Recommended tab.

        Shows the representative's best access pattern and score, plus the
        total hit count and how many unique hook sites / struct groups
        produced this text.
        """
        preview = self.text[:120].replace("\n", " ")
        return (
            f"[{self.score:6.0f}]  +{self.rva:#x}  {self.access_pattern}"
            f"  hits={self.hit_count}  structs={n_structs}  hooks={n_sites}"
            f"  {preview!r}"
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


# ---------------------------------------------------------------------------
# Three-level aggregation model
# ---------------------------------------------------------------------------


@dataclass
class StructGroup:
    """Middle tier — candidates whose ``str_ptr`` values are nearby.

    Represents a single struct instance seen by one or more hook functions.

    Attributes
    ----------
    leader:
        The candidate with the smallest ``first_seen_seq`` in this group.
        It is the "top-level function" and should be kept active.
    members:
        All candidates belonging to this struct group (including the leader),
        sorted by ``first_seen_seq`` ascending.
    """

    leader: HookCandidate
    members: list[HookCandidate] = field(default_factory=list)

    @property
    def total_hits(self) -> int:
        return sum(m.hit_count for m in self.members)

    @property
    def hook_vas(self) -> set[int]:
        """Distinct ``hook_va`` values across all members."""
        return {m.hook_va for m in self.members if m.hook_va}

    @property
    def text(self) -> str:
        """Current text from the leader."""
        return self.leader.text


@dataclass
class TextGroup:
    """Top tier — struct groups whose leaders produce the same text.

    Attributes
    ----------
    priority_struct:
        The struct group whose leader has the smallest ``first_seen_seq``.
        Its leader is the overall priority hook for this text.
    structs:
        All struct groups with this text, sorted by leader's
        ``first_seen_seq`` ascending.
    text:
        The shared text string.
    """

    priority_struct: StructGroup
    structs: list[StructGroup] = field(default_factory=list)
    text: str = ""

    @property
    def total_hits(self) -> int:
        return sum(sg.total_hits for sg in self.structs)

    @property
    def total_hooks(self) -> int:
        """Number of unique ``hook_va`` across all struct groups."""
        vas: set[int] = set()
        for sg in self.structs:
            vas.update(sg.hook_vas)
        return len(vas)

    @property
    def leader(self) -> HookCandidate:
        return self.priority_struct.leader

    @property
    def score(self) -> float:
        return max((sg.leader.score for sg in self.structs), default=0.0)


def build_struct_groups(
    candidates: list[HookCandidate],
    tolerance: int = 256,
) -> list[StructGroup]:
    """Group *candidates* into struct groups by ``str_ptr`` proximity.

    Within each group the candidate with the smallest ``first_seen_seq`` is
    the leader.  Returns groups sorted by leader's ``first_seen_seq``.

    Candidates with ``str_ptr == 0`` are each placed in their own singleton
    group (we can't tell whether they share a struct).
    """
    resolved = [c for c in candidates if c.str_ptr != 0]
    unresolved = [c for c in candidates if c.str_ptr == 0]

    resolved.sort(key=lambda c: c.str_ptr)

    raw_groups: list[list[HookCandidate]] = []
    for c in resolved:
        if raw_groups and c.str_ptr - raw_groups[-1][0].str_ptr <= tolerance:
            raw_groups[-1].append(c)
        else:
            raw_groups.append([c])

    # Unresolved → each in its own group
    for c in unresolved:
        raw_groups.append([c])

    result: list[StructGroup] = []
    for members in raw_groups:
        members.sort(key=lambda c: c.first_seen_seq)
        result.append(StructGroup(leader=members[0], members=members))

    result.sort(key=lambda sg: sg.leader.first_seen_seq)
    return result


def build_text_groups(struct_groups: list[StructGroup]) -> list[TextGroup]:
    """Aggregate *struct_groups* by their leader's text into text groups.

    Returns text groups sorted by ``score`` descending.
    """
    by_text: dict[str, list[StructGroup]] = {}
    for sg in struct_groups:
        by_text.setdefault(sg.text, []).append(sg)

    result: list[TextGroup] = []
    for text, sgs in by_text.items():
        sgs.sort(key=lambda sg: sg.leader.first_seen_seq)
        result.append(TextGroup(
            priority_struct=sgs[0],
            structs=sgs,
            text=text,
        ))

    result.sort(key=lambda tg: -tg.score)
    return result


def compute_redundant_hook_vas(
    struct_groups: list[StructGroup],
    confirmed_vas: set[int] | None = None,
) -> set[int]:
    """Return ``hook_va`` values that can safely be disabled.

    A ``hook_va`` is redundant if it is **not** the leader's ``hook_va`` in
    **any** struct group.  Confirmed hooks (already in use by the pipeline)
    are never marked redundant.

    Parameters
    ----------
    struct_groups:
        Output of :func:`build_struct_groups`.
    confirmed_vas:
        ``hook_va`` values currently confirmed by the user.  These are
        excluded from the redundant set regardless of leader status.
    """
    leader_vas: set[int] = set()
    all_vas: set[int] = set()
    for sg in struct_groups:
        if sg.leader.hook_va:
            leader_vas.add(sg.leader.hook_va)
        all_vas.update(sg.hook_vas)

    redundant = all_vas - leader_vas
    if confirmed_vas:
        redundant -= confirmed_vas
    return redundant


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
