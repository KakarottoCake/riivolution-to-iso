"""Shared fixtures: synthetic DOLs and mod trees."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from riivultimatum.dol.reader import (
    DATA_SECTION_COUNT,
    HEADER_SIZE,
    TEXT_SECTION_COUNT,
    FILE_ALIGNMENT,
)


def build_dol(
    text: list[tuple[int, bytes]],
    data: list[tuple[int, bytes]],
    *,
    bss_address: int = 0x80400000,
    bss_size: int = 0x1000,
    entry_point: int = 0x80003400,
) -> bytes:
    """Assemble a DOL from (address, bytes) section lists."""
    header = bytearray(HEADER_SIZE)
    body = bytearray()
    cursor = HEADER_SIZE

    groups = ((text, 0x00, 0x48, 0x90, TEXT_SECTION_COUNT),
              (data, 0x1C, 0x64, 0xAC, DATA_SECTION_COUNT))

    for sections, off_base, addr_base, size_base, limit in groups:
        assert len(sections) <= limit
        for index, (address, blob) in enumerate(sections):
            padding = (-cursor) % FILE_ALIGNMENT
            body.extend(b"\x00" * padding)
            cursor += padding
            struct.pack_into(">I", header, off_base + index * 4, cursor)
            struct.pack_into(">I", header, addr_base + index * 4, address)
            struct.pack_into(">I", header, size_base + index * 4, len(blob))
            body.extend(blob)
            cursor += len(blob)

    struct.pack_into(">I", header, 0xD8, bss_address)
    struct.pack_into(">I", header, 0xDC, bss_size)
    struct.pack_into(">I", header, 0xE0, entry_point)
    return bytes(header + body)


#: A `blr`, the instruction ocarina hooks replace.
BLR = b"\x4e\x80\x00\x20"
#: `nop` (ori r0, r0, 0)
NOP = b"\x60\x00\x00\x00"


@pytest.fixture
def simple_dol() -> bytes:
    """One text section at 0x80003100, one data section at 0x80100000."""
    text = NOP * 4 + b"\xde\xad\xbe\xef" + NOP * 2 + BLR + NOP * 4
    data = bytes(range(256))
    return build_dol([(0x80003100, text)], [(0x80100000, data)])


@pytest.fixture
def mod_tree(tmp_path: Path) -> Path:
    """An SD-card-shaped mod folder: <root>/riivolution/mod.xml + <root>/Mod/."""
    root = tmp_path / "sd"
    (root / "riivolution").mkdir(parents=True)
    (root / "Mod" / "files").mkdir(parents=True)
    return root
