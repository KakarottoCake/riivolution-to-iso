"""Lower Riivolution `<memory>` patches onto a DOL.

The whole project rests on one fact from the Riivolution patch format spec:
memory patches "execute immediately before game boot, after disc mounting but
before executable initialization". They run once, before the DOL entry point --
not continuously. Dolphin agrees; `ApplyGeneralMemoryPatches` is the last thing
`CBoot::BootUp` does.

So the apploader's own section loading is a semantically exact substitute:

  * offset inside an existing section -> patch those bytes in place
  * offset outside every section      -> declare a new section at that address
  * search=true                       -> resolve the scan statically, patch in place
  * ocarina=true                       -> resolve the blr hook statically

No runtime stub, no PPC assembly, no cache-coherency hazards.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..report import Outcome, Report
from ..riivo.parser import Memory, Patch
from ..riivo.resolve import ExternalResolver
from . import gecko
from .reader import Dol, DolError, _mask


@dataclass
class _Pending:
    """A blob destined for a new DOL section."""

    address: int
    data: bytes
    is_text: bool = False


class MemoryLowerer:
    def __init__(self, dol: Dol, resolver: ExternalResolver, report: Report):
        self.dol = dol
        self.resolver = resolver
        self.report = report
        self._pending: list[_Pending] = []
        self._code_addresses: set[int] = set()

    # -- public entry point ---------------------------------------------

    def apply(self, patches: list[Patch]) -> None:
        """Apply every memory patch across `patches`, in order."""
        items = [(p, m) for p in patches for m in p.memory_patches]

        # Pass 1: any address an ocarina hook branches to holds code, so its
        # blob must land in a *text* section to be icache-coherent at boot.
        for _, memory in items:
            if memory.ocarina:
                self._code_addresses.add(_mask(memory.offset))

        # Pass 2: in-place edits, queuing out-of-section blobs.
        for patch, memory in items:
            try:
                self._apply_one(patch, memory)
            except (DolError, gecko.OcarinaError, OSError) as exc:
                self.report.failed("memory", self._describe(memory), str(exc))

        # Pass 3: materialise the queued blobs as sections.
        self._flush_pending()

    # -- one patch -------------------------------------------------------

    def _apply_one(self, patch: Patch, memory: Memory) -> None:
        label = self._describe(memory)

        if memory.ocarina:
            hook = gecko.apply_ocarina(self.dol, memory.value, memory.offset)
            self.report.applied(
                "ocarina",
                label,
                f"hooked blr at {hook:#010x} -> {_mask(memory.offset):#010x}",
            )
            return

        value = self._resolve_value(patch, memory)
        if not value:
            self.report.skipped("memory", label, "patch resolves to zero bytes")
            return

        if memory.search:
            self._apply_search(memory, value, label)
            return

        self._apply_direct(memory, value, label)

    def _apply_direct(self, memory: Memory, value: bytes, label: str) -> None:
        address = _mask(memory.offset)
        section = self.dol.section_containing(address, len(value))

        # A loaded section outranks the declared .bss range. Real DOLs overlap
        # the two: SMG2 declares bss at 0x80728680+0xbab08 yet places two
        # initialised data sections inside that span, so checking .bss first
        # would refuse patches to bytes that genuinely ship in the executable.
        if section is None and self.dol.in_bss(address, len(value)):
            self.report.unsupported(
                "memory",
                label,
                f"{address:#010x} is in .bss and in no loaded section; the game "
                "zeroes it during startup, so this patch would be wiped. "
                "Riivolution has the same limitation.",
            )
            return

        if memory.original:
            if len(memory.original) != len(value):
                self.report.failed(
                    "memory",
                    label,
                    f"original is {len(memory.original)} bytes but value is {len(value)}",
                )
                return
            current = self._current_bytes(address, len(value))
            if current != memory.original:
                self.report.skipped(
                    "memory",
                    label,
                    f"original guard did not match (expected {memory.original.hex()}, "
                    f"found {current.hex()})",
                )
                return

        if section is not None:
            self.dol.write(address, value)
            self.report.applied(
                "memory", label, f"patched in place ({len(value)} bytes)"
            )
            return

        is_text = self._is_code(address, len(value))
        self._pending.append(_Pending(address, value, is_text))
        kind = "text" if is_text else "data"
        self.report.applied(
            "memory",
            label,
            f"queued {len(value)} bytes for a new {kind} section at {address:#010x}",
        )

    def _apply_search(self, memory: Memory, value: bytes, label: str) -> None:
        if not memory.original:
            self.report.failed("memory", label, "search patch has no original pattern")
            return
        if len(memory.original) != len(value):
            self.report.failed(
                "memory",
                label,
                f"search original is {len(memory.original)} bytes but value is "
                f"{len(value)}; they must match",
            )
            return

        found = self.dol.find(memory.original, align=memory.align)
        if found is None:
            self.report.skipped(
                "memory",
                label,
                f"pattern {memory.original.hex()} not found (align {memory.align})",
            )
            return

        self.dol.write(found, value)
        self.report.applied("memory", label, f"search hit at {found:#010x}, patched")

    # -- helpers ---------------------------------------------------------

    def _resolve_value(self, patch: Patch, memory: Memory) -> bytes:
        if memory.value:
            return memory.value
        if not memory.valuefile:
            return b""
        path = self.resolver.resolve(memory.valuefile, patch.root)
        if not path.is_file():
            raise OSError(f"valuefile not found: {path}")
        return Path(path).read_bytes()

    def _current_bytes(self, address: int, length: int) -> bytes:
        """What Riivolution would have read at boot time.

        Inside a loaded section: the section's bytes. Outside: zeroes, which is
        what MEM1 holds before the game touches it.
        """
        if self.dol.section_containing(address, length) is not None:
            return self.dol.read(address, length)
        for pending in self._pending:
            if pending.address <= address and address + length <= pending.address + len(pending.data):
                start = address - pending.address
                return pending.data[start : start + length]
        return b"\x00" * length

    def _is_code(self, address: int, length: int) -> bool:
        """Should this out-of-section blob become a *text* section?

        It matters: the apploader invalidates the instruction cache over text
        sections but not data ones, so code landing in a data section can be
        executed from stale icache lines left by the apploader or IOS.

        Two signals. An ocarina hook branches to it, so it is definitionally
        code. Or it sits in the 0x80001800-0x80003000 window, which by long
        ecosystem convention holds nothing but loaders -- the Gecko codehandler
        under Riivolution, the Kamek/Syati loader under SMG2 and NSMBW mods.
        Those mods carry no ocarina patch, so the address window is the only
        signal available for them.
        """
        end = address + length
        if any(address <= a < end for a in self._code_addresses):
            return True
        return address < gecko.GECKO_WINDOW_END and end > gecko.CODEHANDLER_ADDRESS

    def _flush_pending(self) -> None:
        """Coalesce queued blobs and add them as sections.

        Blobs are merged by adjacency before allocation because DOL slots are
        scarce (7 text, 11 data) and the codehandler/codelist pair is normally
        contiguous-ish. Later blobs win on overlap, matching patch order.
        """
        if not self._pending:
            return

        for is_text in (True, False):
            group = [p for p in self._pending if p.is_text == is_text]
            if not group:
                continue
            for blob in _coalesce(group):
                try:
                    self.dol.add_section(blob.address, blob.data, is_text=is_text)
                except DolError as exc:
                    self.report.failed(
                        "memory",
                        f"section at {blob.address:#010x}",
                        str(exc),
                    )

        self._pending.clear()
        self._warn_on_gecko_overflow()

    def _warn_on_gecko_overflow(self) -> None:
        """Flag a Gecko payload that spills past the conventional 0x80003000."""
        section = self.dol.section_containing(gecko.CODEHANDLER_ADDRESS)
        if section is None:
            return
        warning = gecko.check_gecko_window(section.end, section.end)
        if warning:
            self.report.add(Outcome.UNSUPPORTED, "gecko", "code window", warning)

    @staticmethod
    def _describe(memory: Memory) -> str:
        if memory.ocarina:
            return f"ocarina hook -> {_mask(memory.offset):#010x}"
        if memory.search:
            return f"search {memory.original.hex()[:16]} (align {memory.align})"
        source = memory.valuefile or memory.value.hex()[:16]
        return f"{_mask(memory.offset):#010x} <- {source}"


def _coalesce(blobs: list[_Pending]) -> list[_Pending]:
    """Merge overlapping/adjacent blobs; later entries win on overlap."""
    ordered = sorted(enumerate(blobs), key=lambda pair: (pair[1].address, pair[0]))
    merged: list[_Pending] = []

    for _, blob in ordered:
        if merged and blob.address <= merged[-1].address + len(merged[-1].data):
            last = merged[-1]
            start = last.address
            end = max(last.address + len(last.data), blob.address + len(blob.data))
            buffer = bytearray(end - start)
            buffer[0 : len(last.data)] = last.data
            buffer[blob.address - start : blob.address - start + len(blob.data)] = blob.data
            merged[-1] = _Pending(start, bytes(buffer), last.is_text)
        else:
            merged.append(_Pending(blob.address, bytes(blob.data), blob.is_text))

    return merged
