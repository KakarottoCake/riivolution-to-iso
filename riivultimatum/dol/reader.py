"""Wii/GameCube DOL executable reading and surgery.

The DOL header is 0x100 bytes:

    0x00  u32[7]   text section file offsets
    0x1C  u32[11]  data section file offsets
    0x48  u32[7]   text section load addresses
    0x64  u32[11]  data section load addresses
    0x90  u32[7]   text section sizes
    0xAC  u32[11]  data section sizes
    0xD8  u32      bss load address
    0xDC  u32      bss size
    0xE0  u32      entry point
    0xE4  0x1C     padding

The apploader copies each declared section from the image to its load address
before branching to the entry point. That is the whole trick this project rests
on: adding a section at address X is equivalent to Riivolution writing bytes to
address X just before boot.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

TEXT_SECTION_COUNT = 7
DATA_SECTION_COUNT = 11
HEADER_SIZE = 0x100

_OFF_TEXT_OFFSETS = 0x00
_OFF_DATA_OFFSETS = 0x1C
_OFF_TEXT_ADDRESSES = 0x48
_OFF_DATA_ADDRESSES = 0x64
_OFF_TEXT_SIZES = 0x90
_OFF_DATA_SIZES = 0xAC
_OFF_BSS_ADDRESS = 0xD8
_OFF_BSS_SIZE = 0xDC
_OFF_ENTRY_POINT = 0xE0

#: DOL sections are conventionally aligned to 32 bytes in the file. The
#: apploader does not require it, but staying aligned keeps DMA happy and
#: matches what every other tool emits.
FILE_ALIGNMENT = 32


class DolError(Exception):
    """The DOL is malformed, or the requested surgery is impossible."""


@dataclass
class Section:
    index: int
    is_text: bool
    address: int
    data: bytearray

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def end(self) -> int:
        return self.address + self.size

    def contains(self, address: int, length: int = 1) -> bool:
        return self.address <= address and address + length <= self.end

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "text" if self.is_text else "data"
        return f"<{kind}{self.index} {self.address:#010x}..{self.end:#010x} ({self.size} bytes)>"


def _mask(address: int) -> int:
    """Normalise a Wii address to the cached MEM1/MEM2 view.

    Riivolution offsets are written both as `0x80001800` and `0x00001800`;
    Dolphin ORs in 0x80000000. Addresses at or above 0x80000000 are left alone
    so that MEM2 (0x90000000+) still works.
    """
    if address < 0x80000000:
        return address | 0x80000000
    return address


class Dol:
    """A parsed DOL, mutable in memory, re-serialisable."""

    def __init__(self, sections: list[Section], bss_address: int, bss_size: int, entry_point: int):
        self.sections = sections
        self.bss_address = bss_address
        self.bss_size = bss_size
        self.entry_point = entry_point

    # -- parsing ---------------------------------------------------------

    @classmethod
    def parse(cls, blob: bytes) -> "Dol":
        if len(blob) < HEADER_SIZE:
            raise DolError(f"file is {len(blob)} bytes, too short to be a DOL")

        def u32_array(offset: int, count: int) -> list[int]:
            return list(struct.unpack_from(f">{count}I", blob, offset))

        text_offsets = u32_array(_OFF_TEXT_OFFSETS, TEXT_SECTION_COUNT)
        data_offsets = u32_array(_OFF_DATA_OFFSETS, DATA_SECTION_COUNT)
        text_addresses = u32_array(_OFF_TEXT_ADDRESSES, TEXT_SECTION_COUNT)
        data_addresses = u32_array(_OFF_DATA_ADDRESSES, DATA_SECTION_COUNT)
        text_sizes = u32_array(_OFF_TEXT_SIZES, TEXT_SECTION_COUNT)
        data_sizes = u32_array(_OFF_DATA_SIZES, DATA_SECTION_COUNT)

        (bss_address,) = struct.unpack_from(">I", blob, _OFF_BSS_ADDRESS)
        (bss_size,) = struct.unpack_from(">I", blob, _OFF_BSS_SIZE)
        (entry_point,) = struct.unpack_from(">I", blob, _OFF_ENTRY_POINT)

        sections: list[Section] = []
        groups = (
            (True, text_offsets, text_addresses, text_sizes),
            (False, data_offsets, data_addresses, data_sizes),
        )
        for is_text, offsets, addresses, sizes in groups:
            for index, (offset, address, size) in enumerate(zip(offsets, addresses, sizes)):
                # A slot is free when its size is zero; offset/address may hold
                # stale junk, so size is the only reliable liveness signal.
                if size == 0:
                    continue
                if offset + size > len(blob):
                    kind = "text" if is_text else "data"
                    raise DolError(
                        f"{kind} section {index} runs past end of file "
                        f"(offset {offset:#x} + size {size:#x} > {len(blob):#x})"
                    )
                sections.append(
                    Section(
                        index=index,
                        is_text=is_text,
                        address=address,
                        data=bytearray(blob[offset : offset + size]),
                    )
                )

        if not sections:
            raise DolError("DOL declares no sections")

        return cls(sections, bss_address, bss_size, entry_point)

    # -- serialisation ---------------------------------------------------

    def serialize(self) -> bytes:
        """Rebuild the DOL file.

        Sections are emitted in their original (kind, index) order so that an
        unmodified DOL round-trips to a byte-identical file for the common case
        where the source was already 32-byte aligned and gap-free.
        """
        header = bytearray(HEADER_SIZE)
        body = bytearray()
        cursor = HEADER_SIZE

        def slot_key(s: Section) -> tuple[int, int]:
            return (0 if s.is_text else 1, s.index)

        for section in sorted(self.sections, key=slot_key):
            padding = (-cursor) % FILE_ALIGNMENT
            body.extend(b"\x00" * padding)
            cursor += padding

            if section.is_text:
                base_off, base_addr, base_size = (
                    _OFF_TEXT_OFFSETS,
                    _OFF_TEXT_ADDRESSES,
                    _OFF_TEXT_SIZES,
                )
                limit = TEXT_SECTION_COUNT
            else:
                base_off, base_addr, base_size = (
                    _OFF_DATA_OFFSETS,
                    _OFF_DATA_ADDRESSES,
                    _OFF_DATA_SIZES,
                )
                limit = DATA_SECTION_COUNT

            if section.index >= limit:
                raise DolError(f"section index {section.index} out of range")

            struct.pack_into(">I", header, base_off + section.index * 4, cursor)
            struct.pack_into(">I", header, base_addr + section.index * 4, section.address)
            struct.pack_into(">I", header, base_size + section.index * 4, section.size)

            body.extend(section.data)
            cursor += section.size

        struct.pack_into(">I", header, _OFF_BSS_ADDRESS, self.bss_address)
        struct.pack_into(">I", header, _OFF_BSS_SIZE, self.bss_size)
        struct.pack_into(">I", header, _OFF_ENTRY_POINT, self.entry_point)

        return bytes(header + body)

    # -- address lookup --------------------------------------------------

    def section_containing(self, address: int, length: int = 1) -> Section | None:
        address = _mask(address)
        for section in self.sections:
            if section.contains(address, length):
                return section
        return None

    def read(self, address: int, length: int) -> bytes:
        section = self.section_containing(address, length)
        if section is None:
            raise DolError(
                f"address range {_mask(address):#010x}+{length:#x} is not in any section"
            )
        start = _mask(address) - section.address
        return bytes(section.data[start : start + length])

    def write(self, address: int, data: bytes) -> None:
        """Overwrite bytes at a virtual address that already lives in a section."""
        section = self.section_containing(address, len(data))
        if section is None:
            raise DolError(
                f"address range {_mask(address):#010x}+{len(data):#x} is not in any section"
            )
        start = _mask(address) - section.address
        section.data[start : start + len(data)] = data

    def in_bss(self, address: int, length: int = 1) -> bool:
        address = _mask(address)
        return (
            self.bss_size > 0
            and self.bss_address <= address
            and address + length <= self.bss_address + self.bss_size
        )

    # -- section creation ------------------------------------------------

    def free_slots(self, is_text: bool) -> list[int]:
        limit = TEXT_SECTION_COUNT if is_text else DATA_SECTION_COUNT
        used = {s.index for s in self.sections if s.is_text == is_text}
        return [i for i in range(limit) if i not in used]

    def compact(self, is_text: bool, max_gap: int = 0) -> bool:
        """Free a slot by merging two address-contiguous sections of one kind.

        Games ship DOLs whose sections are already laid out back to back, so two
        of them frequently describe one continuous address range for no reason
        other than how the linker emitted them. Merging such a pair is
        semantically identical from the apploader's point of view -- the same
        bytes reach the same addresses -- and it buys a slot.

        `max_gap` allows merging across a hole, which fills those addresses with
        zeroes that the apploader previously left untouched. That is a real
        behaviour change, so it defaults to off.

        Returns True if a merge happened.
        """
        group = sorted(
            (s for s in self.sections if s.is_text == is_text),
            key=lambda s: s.address,
        )
        for left, right in zip(group, group[1:]):
            gap = right.address - left.end
            if 0 <= gap <= max_gap:
                merged = bytearray(right.end - left.address)
                merged[0 : left.size] = left.data
                merged[right.address - left.address :] = right.data
                left.data = merged
                self.sections.remove(right)
                return True
        return False

    def add_section(self, address: int, data: bytes, is_text: bool = False) -> Section:
        """Declare a new section so the apploader loads `data` to `address`.

        If the new range abuts or overlaps an existing section of the same
        kind, it is merged into it instead of consuming a slot -- slots are the
        scarce resource here (7 text, 11 data).
        """
        address = _mask(address)
        if not data:
            raise DolError("refusing to add an empty section")

        new_end = address + len(data)

        for section in self.sections:
            if section.is_text != is_text:
                continue
            # Touching or overlapping: extend in place.
            if address <= section.end and new_end >= section.address:
                merged_start = min(section.address, address)
                merged_end = max(section.end, new_end)
                merged = bytearray(merged_end - merged_start)
                merged[section.address - merged_start : section.end - merged_start] = section.data
                merged[address - merged_start : new_end - merged_start] = data
                section.address = merged_start
                section.data = merged
                return section

        slots = self.free_slots(is_text)
        if not slots and self.compact(is_text):
            slots = self.free_slots(is_text)
        if not slots:
            kind = "text" if is_text else "data"
            limit = TEXT_SECTION_COUNT if is_text else DATA_SECTION_COUNT
            raise DolError(
                f"no free {kind} section slot (all {limit} are in use, and none "
                f"could be merged); cannot place {len(data)} bytes at {address:#010x}. "
                f"Consider relocating the blob into an existing section's "
                f"address range, or use wstrt --gct-move."
            )

        section = Section(index=slots[0], is_text=is_text, address=address, data=bytearray(data))
        self.sections.append(section)
        return section

    def write_or_add(self, address: int, data: bytes, is_text: bool = False) -> Section:
        """Patch in place if the range is already loaded, otherwise add a section."""
        section = self.section_containing(address, len(data))
        if section is not None:
            self.write(address, data)
            return section
        return self.add_section(address, data, is_text=is_text)

    # -- searching -------------------------------------------------------

    def find(self, pattern: bytes, align: int = 1, start_address: int = 0) -> int | None:
        """Find the first occurrence of `pattern`, returning its address.

        Sections are searched in ascending address order so the result does not
        depend on slot ordering. Matches never straddle a section boundary,
        which is correct: the gaps between sections are not loaded memory.
        """
        if not pattern:
            return None
        align = max(1, align)
        start_address = _mask(start_address) if start_address else 0

        for section in sorted(self.sections, key=lambda s: s.address):
            if section.end <= start_address:
                continue
            begin = max(0, start_address - section.address)
            # Honour alignment relative to the absolute address, not the offset.
            begin += (-(section.address + begin)) % align
            limit = section.size - len(pattern)
            for offset in range(begin, limit + 1, align):
                if section.data[offset : offset + len(pattern)] == pattern:
                    return section.address + offset
        return None

    def find_all(self, pattern: bytes, align: int = 1) -> list[int]:
        results: list[int] = []
        address = 0
        while True:
            found = self.find(pattern, align=align, start_address=address)
            if found is None:
                return results
            results.append(found)
            address = found + max(align, 1)
