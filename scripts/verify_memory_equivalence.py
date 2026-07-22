#!/usr/bin/env python3
"""Prove that our DOL surgery equals Riivolution writing RAM before boot.

The project's central claim is that lowering `<memory>` patches into DOL
sections produces the *same MEM1 image* the console would have had if
Riivolution had written those bytes just before the entry point ran.

That claim is directly checkable without a console and without Dolphin:

  reference = sections of the ORIGINAL main.dol, laid into a flat MEM1 image,
              then every <memory> patch applied as a plain RAM write
  ours      = sections of our PATCHED main.dol, laid into a flat MEM1 image

If the two images agree byte for byte -- and cover exactly the same addresses
-- the lowering is equivalent. The reference applier below is deliberately
naive and shares no code with `riivultimatum.dol.memory`, so agreement is
evidence rather than tautology.

What this does NOT prove: that a real apploader honours a section loaded at
0x80001800. Only booting on hardware shows that.

Usage:
    python scripts/verify_memory_equivalence.py \
        --original-dol original/sys/main.dol \
        --patched-dol  staging/image/sys/main.dol \
        --xml "mod/riivolution/mod.xml" --sd-root mod \
        --choice "Section/Option=Choice" --game-id RMGE01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riivultimatum.dol.reader import Dol  # noqa: E402
from riivultimatum.riivo import parser as riivo_parser  # noqa: E402
from riivultimatum.riivo import resolve as riivo_resolve  # noqa: E402

MEM1_BASE = 0x80000000
MEM1_SIZE = 0x01800000  # 24 MiB
BLR = 0x4E800020


class Memory:
    """A flat MEM1 image plus a map of which bytes were ever written."""

    def __init__(self) -> None:
        self.data = bytearray(MEM1_SIZE)
        self.covered = bytearray(MEM1_SIZE)

    def write(self, address: int, blob: bytes) -> None:
        offset = (address | 0x80000000) - MEM1_BASE
        if offset < 0 or offset + len(blob) > MEM1_SIZE:
            raise ValueError(f"{address:#010x}+{len(blob):#x} is outside MEM1")
        self.data[offset : offset + len(blob)] = blob
        self.covered[offset : offset + len(blob)] = b"\x01" * len(blob)

    def read(self, address: int, length: int) -> bytes:
        offset = (address | 0x80000000) - MEM1_BASE
        return bytes(self.data[offset : offset + length])

    def find(self, pattern: bytes, align: int = 1, start: int = 0) -> int | None:
        """Search only bytes the apploader actually loaded."""
        offset = self.data.find(pattern, start)
        while offset != -1:
            address = MEM1_BASE + offset
            if address % align == 0 and all(self.covered[offset : offset + len(pattern)]):
                return address
            offset = self.data.find(pattern, offset + 1)
        return None


def lay_out(dol: Dol) -> Memory:
    """Do what the apploader does: copy each section to its load address."""
    memory = Memory()
    for section in sorted(dol.sections, key=lambda s: s.address):
        memory.write(section.address, bytes(section.data))
    return memory


def apply_reference_patches(memory: Memory, patches, resolver) -> list[str]:
    """A deliberately dumb Riivolution memory patcher: just write RAM."""
    notes: list[str] = []
    for patch in patches:
        for m in patch.memory_patches:
            value = m.value
            if not value and m.valuefile:
                path = resolver.resolve(m.valuefile, patch.root)
                if not path.is_file():
                    notes.append(f"missing valuefile {path}")
                    continue
                value = path.read_bytes()
            if not value and not m.ocarina:
                continue

            if m.ocarina:
                found = memory.find(m.value, align=4)
                if found is None:
                    notes.append(f"ocarina pattern {m.value.hex()} not found")
                    continue
                address = found
                while int.from_bytes(memory.read(address, 4), "big") != BLR:
                    address += 4
                target = m.offset | 0x80000000
                memory.write(
                    address,
                    ((((target - address) & 0x03FFFFFC) | 0x48000000)).to_bytes(4, "big"),
                )
            elif m.search:
                found = memory.find(m.original, align=m.align)
                if found is None:
                    notes.append(f"search pattern {m.original.hex()} not found")
                    continue
                memory.write(found, value)
            else:
                if m.original and memory.read(m.offset, len(m.original)) != m.original:
                    notes.append(f"guard mismatch at {m.offset | 0x80000000:#010x}")
                    continue
                memory.write(m.offset, value)
    return notes


def compare(reference: Memory, ours: Memory) -> int:
    """Report differing runs. Returns the number of differing bytes."""
    value_diff = [i for i in range(MEM1_SIZE) if reference.data[i] != ours.data[i]]
    cover_diff = [i for i in range(MEM1_SIZE) if reference.covered[i] != ours.covered[i]]

    def summarise(name: str, indices: list[int]) -> None:
        if not indices:
            print(f"  {name}: none")
            return
        print(f"  {name}: {len(indices):,} bytes in runs:")
        start = previous = indices[0]
        runs = []
        for i in indices[1:]:
            if i != previous + 1:
                runs.append((start, previous))
                start = i
            previous = i
        runs.append((start, previous))
        for lo, hi in runs[:20]:
            print(f"    {MEM1_BASE + lo:#010x}..{MEM1_BASE + hi + 1:#010x}  ({hi - lo + 1} bytes)")
        if len(runs) > 20:
            print(f"    ... and {len(runs) - 20} more runs")

    summarise("value differences", value_diff)
    summarise("coverage differences", cover_diff)
    return len(value_diff) + len(cover_diff)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--original-dol", type=Path, required=True)
    ap.add_argument("--patched-dol", type=Path, required=True)
    ap.add_argument("--xml", type=Path, required=True)
    ap.add_argument("--sd-root", type=Path, required=True)
    ap.add_argument("--game-id", required=True)
    ap.add_argument("--choice", action="append", default=[])
    args = ap.parse_args()

    disc = riivo_parser.parse(args.xml)
    selections = dict(c.split("=", 1) for c in args.choice)
    riivo_resolve.apply_selections(disc, selections)
    patches = riivo_resolve.selected_patches(disc, args.game_id)
    resolver = riivo_resolve.ExternalResolver(args.sd_root, disc.root)

    print(f"Reference: {args.original_dol} + {sum(len(p.memory_patches) for p in patches)} memory patches")
    reference = lay_out(Dol.parse(args.original_dol.read_bytes()))
    notes = apply_reference_patches(reference, patches, resolver)

    print(f"Ours:      {args.patched_dol}")
    ours = lay_out(Dol.parse(args.patched_dol.read_bytes()))

    if notes:
        print(f"\nReference applier notes ({len(notes)}):")
        for note in notes[:20]:
            print(f"  {note}")

    print("\nComparison:")
    total = compare(reference, ours)

    print()
    if total == 0:
        print("EQUIVALENT: the patched DOL produces exactly the memory image")
        print("Riivolution would have produced by writing RAM before boot.")
        return 0
    print(f"NOT EQUIVALENT: {total:,} differing bytes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
