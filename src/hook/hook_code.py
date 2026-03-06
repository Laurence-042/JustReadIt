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
        Memory-access pattern string as emitted by ``hook_search.js``.
        Examples: ``"r0"``, ``"*(r1+0x14)"``, ``"*(s+0x28)"``.
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
        ``r0``           → (0, 0, 0, enc)
        ``r2``           → (2, 0, 0, enc)
        ``*(r0+0x14)``   → (0, 1, 0x14, enc)
        ``*(r1)``        → (1, 1, 0, enc)
        ``*(s+0x28)``    → (0xFF, 1, 0x28, enc)  stack-relative
        """
        enc_int = 0 if self.encoding == "utf16" else 1
        p = self.access_pattern.strip()

        # Normalise "*(base+off)" → "*base+off"
        if p.startswith("*(") and p.endswith(")"):
            p = "*" + p[2:-1]

        deref = 1 if p.startswith("*") else 0
        if deref:
            p = p[1:]

        base, _, offset_str = p.partition("+")
        byte_offset = int(offset_str, 16) if offset_str else 0

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
            f"+{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}  {preview!r}"
        )

    def display_label_scored(self) -> str:
        """Label with score prefix, used in the Recommended tab."""
        preview = self.text[:55].replace("\n", " ")
        return (
            f"[{self.score:6.0f}]  +{self.rva:#x}  {self.access_pattern}  hits={self.hit_count}  {preview!r}"
        )
