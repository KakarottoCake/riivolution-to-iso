"""DOL parsing, surgery, and the ocarina hook encoding."""

from __future__ import annotations

import pytest

from riivultimatum.dol.gecko import BLR, OcarinaError, apply_ocarina, encode_branch, find_hook
from riivultimatum.dol.reader import Dol, DolError

from .conftest import NOP, build_dol


# -- parsing / serialisation ------------------------------------------------


def test_roundtrip_is_byte_identical(simple_dol):
    assert Dol.parse(simple_dol).serialize() == simple_dol


def test_parse_reads_header_fields(simple_dol):
    dol = Dol.parse(simple_dol)
    assert dol.entry_point == 0x80003400
    assert dol.bss_address == 0x80400000
    assert dol.bss_size == 0x1000
    assert len(dol.sections) == 2


def test_zero_size_slots_are_free_not_sections(simple_dol):
    dol = Dol.parse(simple_dol)
    assert dol.free_slots(is_text=True) == [1, 2, 3, 4, 5, 6]
    assert dol.free_slots(is_text=False) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_truncated_section_is_rejected():
    blob = bytearray(build_dol([(0x80003100, NOP * 4)], []))
    # Claim the text section is far larger than the file.
    blob[0x90:0x94] = (0x10000).to_bytes(4, "big")
    with pytest.raises(DolError, match="past end of file"):
        Dol.parse(bytes(blob))


# -- address lookup ---------------------------------------------------------


def test_read_and_write_in_place(simple_dol):
    dol = Dol.parse(simple_dol)
    assert dol.read(0x80003110, 4) == b"\xde\xad\xbe\xef"
    dol.write(0x80003110, b"\xca\xfe\xba\xbe")
    assert dol.read(0x80003110, 4) == b"\xca\xfe\xba\xbe"


def test_addresses_are_masked_to_the_cached_view(simple_dol):
    """Riivolution XMLs write offsets both with and without the 0x8 prefix."""
    dol = Dol.parse(simple_dol)
    assert dol.read(0x00003110, 4) == dol.read(0x80003110, 4)


def test_write_straddling_a_section_end_is_rejected(simple_dol):
    dol = Dol.parse(simple_dol)
    section = next(s for s in dol.sections if s.is_text)
    with pytest.raises(DolError, match="not in any section"):
        dol.write(section.end - 2, b"\x01\x02\x03\x04")


def test_in_bss(simple_dol):
    dol = Dol.parse(simple_dol)
    assert dol.in_bss(0x80400010, 4)
    assert not dol.in_bss(0x80003100, 4)


# -- section creation -------------------------------------------------------


def test_add_section_makes_the_address_loadable(simple_dol):
    dol = Dol.parse(simple_dol)
    payload = b"\x11\x22\x33\x44"
    dol.add_section(0x80001800, payload)
    assert dol.read(0x80001800, 4) == payload

    # And it survives a serialise/parse cycle -- this is what the apploader sees.
    reloaded = Dol.parse(dol.serialize())
    assert reloaded.read(0x80001800, 4) == payload


def test_adjacent_sections_merge_instead_of_consuming_slots(simple_dol):
    dol = Dol.parse(simple_dol)
    before = len(dol.free_slots(is_text=False))
    dol.add_section(0x80001800, b"\xaa" * 16)
    dol.add_section(0x80001810, b"\xbb" * 16)  # exactly abutting
    assert len(dol.free_slots(is_text=False)) == before - 1
    assert dol.read(0x80001800, 32) == b"\xaa" * 16 + b"\xbb" * 16


def test_overlapping_section_add_lets_the_later_write_win(simple_dol):
    dol = Dol.parse(simple_dol)
    dol.add_section(0x80001800, b"\xaa" * 16)
    dol.add_section(0x80001808, b"\xbb" * 16)
    assert dol.read(0x80001800, 24) == b"\xaa" * 8 + b"\xbb" * 16


def test_exhausted_slots_produce_an_actionable_error():
    """All slots used AND no two sections contiguous, so compaction can't help."""
    text = [(0x80003000 + i * 0x1000, NOP) for i in range(7)]
    data = [(0x80100000 + i * 0x1000, b"\x00") for i in range(11)]
    dol = Dol.parse(build_dol(text, data))
    with pytest.raises(DolError, match="no free data section slot"):
        dol.add_section(0x81000000, b"\xff" * 4)


def test_compaction_merges_contiguous_sections_to_free_a_slot():
    data = [(0x80100000, b"\xaa" * 16), (0x80100010, b"\xbb" * 16)]
    dol = Dol.parse(build_dol([(0x80003000, NOP)], data))
    assert dol.compact(is_text=False) is True
    assert len([s for s in dol.sections if not s.is_text]) == 1
    assert dol.read(0x80100000, 32) == b"\xaa" * 16 + b"\xbb" * 16


def test_compaction_refuses_to_bridge_a_gap_by_default():
    """Filling a gap would write zeroes to addresses the apploader skipped."""
    data = [(0x80100000, b"\xaa" * 16), (0x80200000, b"\xbb" * 16)]
    dol = Dol.parse(build_dol([(0x80003000, NOP)], data))
    assert dol.compact(is_text=False) is False


def test_slot_exhaustion_recovers_by_compacting():
    """The SMG2 risk case: a DOL using every data slot, but with a mergeable pair."""
    text = [(0x80003000, NOP)]
    data = [(0x80100000, b"\xaa" * 16), (0x80100010, b"\xbb" * 16)]
    data += [(0x80200000 + i * 0x1000, b"\x00") for i in range(9)]
    dol = Dol.parse(build_dol(text, data))
    assert dol.free_slots(is_text=False) == []

    dol.add_section(0x80001800, b"\xcc" * 8)
    assert Dol.parse(dol.serialize()).read(0x80001800, 8) == b"\xcc" * 8
    # The merged pair still reaches memory intact.
    assert Dol.parse(dol.serialize()).read(0x80100000, 32) == b"\xaa" * 16 + b"\xbb" * 16


def test_write_or_add_prefers_in_place(simple_dol):
    dol = Dol.parse(simple_dol)
    slots = len(dol.free_slots(is_text=False))
    dol.write_or_add(0x80100000, b"\x99\x99")
    assert len(dol.free_slots(is_text=False)) == slots
    assert dol.read(0x80100000, 2) == b"\x99\x99"


# -- searching --------------------------------------------------------------


def test_find_respects_alignment(simple_dol):
    dol = Dol.parse(simple_dol)
    # 0xdeadbeef sits at 0x80003110, which is 4-aligned.
    assert dol.find(b"\xde\xad\xbe\xef", align=4) == 0x80003110
    # Its second and third bytes are not, so an aligned search must miss.
    assert dol.find(b"\xad\xbe", align=4) is None
    assert dol.find(b"\xad\xbe", align=1) == 0x80003111


def test_find_does_not_match_across_a_section_gap(simple_dol):
    """The gap between sections is not loaded memory, so it cannot host a match."""
    dol = Dol.parse(simple_dol)
    text = next(s for s in dol.sections if s.is_text)
    data = next(s for s in dol.sections if not s.is_text)
    straddling = bytes(text.data[-2:]) + bytes(data.data[:2])
    assert dol.find(straddling) is None


def test_find_scans_in_address_order_not_slot_order():
    dol = Dol.parse(build_dol([(0x80005000, b"\xaa\xbb")], [(0x80001000, b"\xaa\xbb")]))
    assert dol.find(b"\xaa\xbb") == 0x80001000


# -- ocarina ----------------------------------------------------------------


def test_encode_branch_matches_the_documented_formula():
    # ((target - source) & 0x03FFFFFC) | 0x48000000
    assert encode_branch(0x80003120, 0x80001800) == ((0x80001800 - 0x80003120) & 0x03FFFFFC) | 0x48000000


def test_encode_branch_forward_and_back():
    assert encode_branch(0x80000000, 0x80000004) == 0x48000004
    assert encode_branch(0x80000004, 0x80000000) == 0x4BFFFFFC


def test_encode_branch_rejects_out_of_range():
    with pytest.raises(OcarinaError, match="out of range"):
        encode_branch(0x80000000, 0x90000000)


def test_encode_branch_rejects_unaligned_target():
    with pytest.raises(OcarinaError, match="4-byte aligned"):
        encode_branch(0x80000000, 0x80000002)


def test_find_hook_takes_the_next_blr_after_the_pattern(simple_dol):
    dol = Dol.parse(simple_dol)
    # Layout: 4 nops, 0xdeadbeef @0x...110, 2 nops, blr @0x...11c
    assert find_hook(dol, b"\xde\xad\xbe\xef") == 0x8000311C


def test_apply_ocarina_writes_a_branch_over_the_blr(simple_dol):
    dol = Dol.parse(simple_dol)
    hook = apply_ocarina(dol, b"\xde\xad\xbe\xef", 0x80001800)
    assert hook == 0x8000311C
    written = int.from_bytes(dol.read(hook, 4), "big")
    assert written == encode_branch(hook, 0x80001800)
    assert written != BLR


def test_missing_hook_pattern_is_an_error(simple_dol):
    dol = Dol.parse(simple_dol)
    with pytest.raises(OcarinaError, match="not found"):
        apply_ocarina(dol, b"\x12\x34\x56\x78", 0x80001800)


def test_pattern_with_no_following_blr_is_an_error():
    dol = Dol.parse(build_dol([(0x80003100, b"\xde\xad\xbe\xef" + NOP * 4)], []))
    with pytest.raises(OcarinaError, match="no blr follows"):
        apply_ocarina(dol, b"\xde\xad\xbe\xef", 0x80001800)
