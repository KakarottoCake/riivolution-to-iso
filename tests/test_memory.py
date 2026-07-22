"""Lowering Riivolution <memory> patches onto a DOL.

These are the tests that guard the project's core claim: that a static DOL edit
is equivalent to Riivolution writing RAM just before the entry point runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riivultimatum.dol.gecko import CODEHANDLER_ADDRESS, encode_branch
from riivultimatum.dol.memory import MemoryLowerer
from riivultimatum.dol.reader import Dol
from riivultimatum.report import Outcome, Report
from riivultimatum.riivo.parser import Memory, Patch
from riivultimatum.riivo.resolve import ExternalResolver

from .conftest import BLR, NOP, build_dol


@pytest.fixture
def env(tmp_path: Path, simple_dol: bytes):
    dol = Dol.parse(simple_dol)
    report = Report()
    resolver = ExternalResolver(tmp_path, "/riivolution")
    return dol, report, resolver, tmp_path


def lower(env, *memories: Memory, root: str = "") -> None:
    dol, report, resolver, _ = env
    MemoryLowerer(dol, resolver, report).apply([Patch(id="p", root=root, memory_patches=list(memories))])


def outcomes(report: Report) -> list[Outcome]:
    return [e.outcome for e in report.entries]


# -- direct writes ----------------------------------------------------------


def test_write_inside_a_section_is_patched_in_place(env):
    dol, report, *_ = env
    slots = len(dol.free_slots(is_text=False))
    lower(env, Memory(offset=0x80003110, value=b"\xca\xfe\xba\xbe"))

    assert dol.read(0x80003110, 4) == b"\xca\xfe\xba\xbe"
    assert outcomes(report) == [Outcome.APPLIED]
    assert len(dol.free_slots(is_text=False)) == slots, "should not have added a section"


def test_write_outside_every_section_becomes_a_new_section(env):
    """The apploader loads the new section to that address before boot --
    exactly what Riivolution's memory write accomplishes."""
    dol, report, *_ = env
    lower(env, Memory(offset=CODEHANDLER_ADDRESS, value=b"\x11\x22\x33\x44"))

    assert outcomes(report) == [Outcome.APPLIED]
    reloaded = Dol.parse(dol.serialize())
    assert reloaded.read(CODEHANDLER_ADDRESS, 4) == b"\x11\x22\x33\x44"


def test_original_guard_matching_applies(env):
    dol, report, *_ = env
    lower(env, Memory(offset=0x80003110, value=b"\x60\x00\x00\x00", original=b"\xde\xad\xbe\xef"))
    assert outcomes(report) == [Outcome.APPLIED]
    assert dol.read(0x80003110, 4) == b"\x60\x00\x00\x00"


def test_original_guard_mismatching_skips_rather_than_fails(env):
    """Riivolution treats a failed guard as a no-op; mods rely on that for
    conditional patching across game revisions."""
    dol, report, *_ = env
    lower(env, Memory(offset=0x80003110, value=b"\x60\x00\x00\x00", original=b"\x00\x00\x00\x00"))
    assert outcomes(report) == [Outcome.SKIPPED]
    assert dol.read(0x80003110, 4) == b"\xde\xad\xbe\xef", "must be untouched"


def test_original_guard_outside_a_section_compares_against_zeroes(env):
    """Before the game runs, MEM1 outside the loaded DOL reads as zero."""
    _, report, *_ = env
    lower(env, Memory(offset=CODEHANDLER_ADDRESS, value=b"\xaa\xbb", original=b"\x00\x00"))
    assert outcomes(report) == [Outcome.APPLIED]


def test_nonzero_guard_outside_a_section_skips(env):
    dol, report, *_ = env
    lower(env, Memory(offset=CODEHANDLER_ADDRESS, value=b"\xaa\xbb", original=b"\x99\x99"))
    assert outcomes(report) == [Outcome.SKIPPED]
    assert dol.section_containing(CODEHANDLER_ADDRESS) is None


def test_mismatched_original_length_is_a_failure(env):
    _, report, *_ = env
    lower(env, Memory(offset=0x80003110, value=b"\x60\x00", original=b"\xde\xad\xbe\xef"))
    assert outcomes(report) == [Outcome.FAILED]


def test_bss_target_is_reported_unsupported_not_silently_dropped(env):
    dol, report, *_ = env
    lower(env, Memory(offset=0x80400010, value=b"\x01\x02\x03\x04"))
    assert outcomes(report) == [Outcome.UNSUPPORTED]
    assert "bss" in report.entries[0].reason


def test_a_loaded_section_outranks_the_declared_bss_range():
    """Real DOLs overlap the two. SMG2 declares bss at 0x80728680+0xbab08 but
    places initialised data sections inside that span; those bytes really ship
    in the executable and must stay patchable."""
    dol = Dol.parse(
        build_dol(
            [(0x80003100, NOP * 4)],
            [(0x80728000, b"\xaa" * 16)],
            bss_address=0x80720000,
            bss_size=0x20000,
        )
    )
    report = Report()
    MemoryLowerer(dol, ExternalResolver(Path(".")), report).apply(
        [Patch(memory_patches=[Memory(offset=0x80728004, value=b"\xbb\xbb")])]
    )
    assert outcomes(report) == [Outcome.APPLIED]
    assert dol.read(0x80728004, 2) == b"\xbb\xbb"


# -- valuefile --------------------------------------------------------------


def test_valuefile_is_read_from_the_mod_folder(env):
    dol, report, _, tmp = env
    blob = tmp / "codes" / "handler.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"\xde" * 64)

    lower(env, Memory(offset=CODEHANDLER_ADDRESS, valuefile="/codes/handler.bin"))
    assert outcomes(report) == [Outcome.APPLIED]
    assert dol.read(CODEHANDLER_ADDRESS, 64) == b"\xde" * 64


def test_missing_valuefile_is_a_failure_not_a_crash(env):
    _, report, *_ = env
    lower(env, Memory(offset=CODEHANDLER_ADDRESS, valuefile="/codes/absent.bin"))
    assert outcomes(report) == [Outcome.FAILED]
    assert "not found" in report.entries[0].reason


# -- search -----------------------------------------------------------------


def test_search_patch_resolves_statically(env):
    dol, report, *_ = env
    lower(env, Memory(search=True, original=b"\xde\xad\xbe\xef", value=b"\x00\x11\x22\x33", align=4))
    assert outcomes(report) == [Outcome.APPLIED]
    assert dol.read(0x80003110, 4) == b"\x00\x11\x22\x33"


def test_search_miss_is_a_skip(env):
    _, report, *_ = env
    lower(env, Memory(search=True, original=b"\x99\x99\x99\x99", value=b"\x00\x00\x00\x00"))
    assert outcomes(report) == [Outcome.SKIPPED]


def test_search_with_mismatched_lengths_is_a_failure(env):
    _, report, *_ = env
    lower(env, Memory(search=True, original=b"\xde\xad\xbe\xef", value=b"\x00"))
    assert outcomes(report) == [Outcome.FAILED]


# -- ocarina ----------------------------------------------------------------


def test_ocarina_hook_is_resolved_against_the_dol(env):
    dol, report, *_ = env
    lower(env, Memory(ocarina=True, value=b"\xde\xad\xbe\xef", offset=CODEHANDLER_ADDRESS))
    assert outcomes(report) == [Outcome.APPLIED]
    assert dol.read(0x8000311C, 4) == encode_branch(0x8000311C, CODEHANDLER_ADDRESS).to_bytes(4, "big")


def test_full_gecko_setup_places_handler_and_hooks_it(env):
    """The realistic three-patch ocarina shape from the Riivolution wiki."""
    dol, report, _, tmp = env
    codes = tmp / "codes"
    codes.mkdir()
    (codes / "codehandler.bin").write_bytes(b"\xc0" * 0x800)
    (codes / "SMNE01.gct").write_bytes(b"\x00\xd0\xc0\xde\x00\xd0\xc0\xde" + b"\x00" * 0x40)

    lower(
        env,
        Memory(offset=0x80001800, valuefile="/codes/codehandler.bin"),
        Memory(offset=0x800028B8, valuefile="/codes/SMNE01.gct"),
        Memory(ocarina=True, value=b"\xde\xad\xbe\xef", offset=0x80001800),
    )

    assert all(o is Outcome.APPLIED for o in outcomes(report)), report.render()

    reloaded = Dol.parse(dol.serialize())
    assert reloaded.read(0x80001800, 4) == b"\xc0\xc0\xc0\xc0"
    assert reloaded.read(0x800028B8, 8) == b"\x00\xd0\xc0\xde\x00\xd0\xc0\xde"
    assert reloaded.read(0x8000311C, 4) == encode_branch(0x8000311C, 0x80001800).to_bytes(4, "big")


def test_ocarina_target_lands_in_a_text_section(env):
    """Code must be in a text section so the apploader treats it as executable."""
    dol, report, _, tmp = env
    (tmp / "codes").mkdir()
    (tmp / "codes" / "h.bin").write_bytes(b"\xc0" * 0x100)

    lower(
        env,
        Memory(offset=0x80001800, valuefile="/codes/h.bin"),
        Memory(ocarina=True, value=b"\xde\xad\xbe\xef", offset=0x80001800),
    )
    section = dol.section_containing(0x80001800)
    assert section is not None and section.is_text


def test_loader_window_blob_becomes_text_without_any_ocarina_patch(env):
    """The SMG2/Syati and NSMBW/Kamek shape: a loader at 0x80001800 with no
    ocarina patch anywhere. It is still code and still needs icache handling."""
    dol, report, _, tmp = env
    (tmp / "c").mkdir()
    (tmp / "c" / "loader.bin").write_bytes(b"\x60" * 2160)

    lower(env, Memory(offset=0x80001800, valuefile="/c/loader.bin"))
    section = dol.section_containing(0x80001800)
    assert section is not None and section.is_text
    assert "new text section" in report.render()


def test_blob_below_the_loader_window_stays_data(env):
    """Mods also park strings just below the loader window; those are data."""
    dol, *_ = env
    lower(env, Memory(offset=0x800017A0, value=b"MBGM_SMG2\x00"))
    section = dol.section_containing(0x800017A0)
    assert section is not None and not section.is_text


def test_ordinary_high_blob_stays_data(env):
    dol, *_ = env
    lower(env, Memory(offset=0x80900000, value=b"\x01\x02\x03\x04"))
    section = dol.section_containing(0x80900000)
    assert section is not None and not section.is_text


def test_missing_ocarina_hook_pattern_is_a_failure(env):
    _, report, *_ = env
    lower(env, Memory(ocarina=True, value=b"\x12\x34\x56\x78", offset=0x80001800))
    assert outcomes(report) == [Outcome.FAILED]


# -- ordering and coalescing ------------------------------------------------


def test_later_patches_win_over_earlier_ones(env):
    dol, _, *_ = env
    lower(
        env,
        Memory(offset=0x80003110, value=b"\x11\x11\x11\x11"),
        Memory(offset=0x80003110, value=b"\x22\x22\x22\x22"),
    )
    assert dol.read(0x80003110, 4) == b"\x22\x22\x22\x22"


def test_adjacent_out_of_section_blobs_share_one_slot(env):
    dol, _, *_ = env
    before = len(dol.free_slots(is_text=False))
    lower(
        env,
        Memory(offset=0x80900000, value=b"\xaa" * 16),
        Memory(offset=0x80900010, value=b"\xbb" * 16),
    )
    assert len(dol.free_slots(is_text=False)) == before - 1
    assert dol.read(0x80900000, 32) == b"\xaa" * 16 + b"\xbb" * 16


def test_distant_blobs_take_separate_slots(env):
    dol, _, *_ = env
    before = len(dol.free_slots(is_text=False))
    lower(
        env,
        Memory(offset=0x80800000, value=b"\xaa" * 4),
        Memory(offset=0x80900000, value=b"\xbb" * 4),
    )
    assert len(dol.free_slots(is_text=False)) == before - 2


def test_text_and_data_blobs_do_not_share_a_slot_pool(env):
    """A common real-mod shape: a loader at 0x80001800 and a string below it."""
    dol, _, *_ = env
    text_before = len(dol.free_slots(is_text=True))
    data_before = len(dol.free_slots(is_text=False))
    lower(
        env,
        Memory(offset=0x800017A0, value=b"MBGM_SMG2_GALAXY\x00\x00"),
        Memory(offset=0x80001800, value=b"\x60" * 64),
    )
    assert len(dol.free_slots(is_text=True)) == text_before - 1
    assert len(dol.free_slots(is_text=False)) == data_before - 1
    assert dol.section_containing(0x80001800).is_text
    assert not dol.section_containing(0x800017A0).is_text


def test_slot_exhaustion_is_reported_not_raised(tmp_path: Path):
    text = [(0x80003000 + i * 0x1000, NOP) for i in range(7)]
    data = [(0x80100000 + i * 0x1000, b"\x00") for i in range(11)]
    dol = Dol.parse(build_dol(text, data))
    report = Report()
    MemoryLowerer(dol, ExternalResolver(tmp_path), report).apply(
        [Patch(id="p", memory_patches=[Memory(offset=0x81000000, value=b"\xff" * 4)])]
    )
    assert Outcome.FAILED in outcomes(report)
    assert "no free data section slot" in report.render()
