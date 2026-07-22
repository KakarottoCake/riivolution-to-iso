"""File and folder patching against an extracted image tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from riivultimatum.report import Outcome, Report
from riivultimatum.riivo.fst import FstPatcher
from riivultimatum.riivo.parser import File, Folder, Patch, Savegame
from riivultimatum.riivo.resolve import ExternalResolver


@pytest.fixture
def image(tmp_path: Path) -> Path:
    """A minimal extracted image: sys/main.dol plus a couple of FST files."""
    root = tmp_path / "image"
    (root / "sys").mkdir(parents=True)
    (root / "files" / "Stage").mkdir(parents=True)
    (root / "sys" / "main.dol").write_bytes(b"DOL" * 8)
    (root / "files" / "Stage" / "01.arc").write_bytes(b"\xaa" * 32)
    (root / "files" / "shared.bin").write_bytes(b"\xbb" * 16)
    return root


@pytest.fixture
def sd(tmp_path: Path) -> Path:
    root = tmp_path / "sd"
    (root / "Mod").mkdir(parents=True)
    return root


def run(image: Path, sd: Path, *patches: Patch) -> Report:
    report = Report()
    FstPatcher(image, ExternalResolver(sd, "/riivolution"), report).apply(list(patches))
    return report


def outcomes(report: Report) -> list[Outcome]:
    return [e.outcome for e in report.entries]


# -- whole-file replacement -------------------------------------------------


def test_replaces_a_file(image, sd):
    (sd / "Mod" / "01.arc").write_bytes(b"\xcc" * 8)
    run(image, sd, Patch(file_patches=[File(disc="/Stage/01.arc", external="/Mod/01.arc")]))
    assert (image / "files" / "Stage" / "01.arc").read_bytes() == b"\xcc" * 8


def test_replaces_main_dol_via_either_spelling(image, sd):
    (sd / "Mod" / "main.dol").write_bytes(b"NEW")
    run(image, sd, Patch(file_patches=[File(disc="/main.dol", external="/Mod/main.dol")]))
    assert (image / "sys" / "main.dol").read_bytes() == b"NEW"

    (sd / "Mod" / "main2.dol").write_bytes(b"NEWER")
    run(image, sd, Patch(file_patches=[File(disc="/sys/main.dol", external="/Mod/main2.dol")]))
    assert (image / "sys" / "main.dol").read_bytes() == b"NEWER"


def test_missing_external_file_is_a_skip(image, sd):
    report = run(image, sd, Patch(file_patches=[File(disc="/Stage/01.arc", external="/Mod/absent")]))
    assert outcomes(report) == [Outcome.SKIPPED]


def test_missing_disc_target_without_create_is_a_skip(image, sd):
    (sd / "Mod" / "new.bin").write_bytes(b"\x01")
    report = run(image, sd, Patch(file_patches=[File(disc="/new.bin", external="/Mod/new.bin")]))
    assert outcomes(report) == [Outcome.SKIPPED]
    assert not (image / "files" / "new.bin").exists()


def test_create_true_adds_a_new_disc_file(image, sd):
    (sd / "Mod" / "new.bin").write_bytes(b"\x01\x02")
    report = run(
        image, sd, Patch(file_patches=[File(disc="/new.bin", external="/Mod/new.bin", create=True)])
    )
    assert outcomes(report) == [Outcome.APPLIED]
    assert (image / "files" / "new.bin").read_bytes() == b"\x01\x02"


# -- partial splices --------------------------------------------------------


def test_resize_true_truncates_to_the_patch_end(image, sd):
    """Dolphin: target_filesize = resize ? patch_end : max(size, patch_end).

    So resize=true does not merely permit growth, it sets the size -- which is
    what makes a plain offset-0 replacement yield exactly the external file.
    """
    (sd / "Mod" / "patch.bin").write_bytes(b"\xff\xff")
    run(image, sd, Patch(file_patches=[File(disc="/shared.bin", external="/Mod/patch.bin", offset=4)]))
    assert (image / "files" / "shared.bin").read_bytes() == b"\xbb" * 4 + b"\xff\xff"


def test_resize_false_splices_and_keeps_the_tail(image, sd):
    (sd / "Mod" / "patch.bin").write_bytes(b"\xff\xff")
    run(
        image,
        sd,
        Patch(file_patches=[File(disc="/shared.bin", external="/Mod/patch.bin", offset=4, resize=False)]),
    )
    assert (image / "files" / "shared.bin").read_bytes() == b"\xbb" * 4 + b"\xff\xff" + b"\xbb" * 10


def test_fileoffset_and_length_select_a_slice_of_the_source(image, sd):
    (sd / "Mod" / "src.bin").write_bytes(bytes(range(16)))
    run(
        image,
        sd,
        Patch(file_patches=[File(disc="/shared.bin", external="/Mod/src.bin", offset=0,
                                 fileoffset=4, length=2)]),
    )
    assert (image / "files" / "shared.bin").read_bytes() == b"\x04\x05"


def test_resize_true_grows_the_file(image, sd):
    (sd / "Mod" / "tail.bin").write_bytes(b"\xee" * 8)
    run(image, sd, Patch(file_patches=[File(disc="/shared.bin", external="/Mod/tail.bin", offset=12)]))
    assert len((image / "files" / "shared.bin").read_bytes()) == 20


def test_resize_false_clips_to_the_original_size(image, sd):
    (sd / "Mod" / "tail.bin").write_bytes(b"\xee" * 8)
    run(
        image,
        sd,
        Patch(file_patches=[File(disc="/shared.bin", external="/Mod/tail.bin", offset=12, resize=False)]),
    )
    content = (image / "files" / "shared.bin").read_bytes()
    assert len(content) == 16
    assert content[12:] == b"\xee" * 4


def test_two_patches_on_disjoint_regions_both_survive(image, sd):
    """Layers must accumulate; flattening after each patch would lose the first."""
    (sd / "Mod" / "a.bin").write_bytes(b"\x11\x11")
    (sd / "Mod" / "b.bin").write_bytes(b"\x22\x22")
    run(
        image,
        sd,
        Patch(
            file_patches=[
                File(disc="/shared.bin", external="/Mod/a.bin", offset=0, resize=False),
                File(disc="/shared.bin", external="/Mod/b.bin", offset=8, resize=False),
            ]
        ),
    )
    content = (image / "files" / "shared.bin").read_bytes()
    assert len(content) == 16
    assert content[0:2] == b"\x11\x11"
    assert content[8:10] == b"\x22\x22"
    assert content[2:8] == b"\xbb" * 6, "untouched regions must be preserved"


# -- folders ----------------------------------------------------------------


def test_folder_patch_maps_relative_paths_under_disc(image, sd):
    src = sd / "Mod" / "files" / "Stage"
    src.mkdir(parents=True)
    (src / "01.arc").write_bytes(b"\x77")
    run(image, sd, Patch(folder_patches=[Folder(disc="/", external="/Mod/files")]))
    assert (image / "files" / "Stage" / "01.arc").read_bytes() == b"\x77"


def test_non_recursive_folder_ignores_subdirectories(image, sd):
    src = sd / "Mod" / "flat"
    (src / "Stage").mkdir(parents=True)
    (src / "Stage" / "01.arc").write_bytes(b"\x77")
    report = run(
        image, sd, Patch(folder_patches=[Folder(disc="/", external="/Mod/flat", recursive=False)])
    )
    assert (image / "files" / "Stage" / "01.arc").read_bytes() == b"\xaa" * 32
    assert Outcome.APPLIED not in outcomes(report)


def test_folder_without_disc_searches_the_fst_by_filename(image, sd):
    src = sd / "Mod" / "any"
    src.mkdir(parents=True)
    (src / "shared.bin").write_bytes(b"\x88")
    run(image, sd, Patch(folder_patches=[Folder(external="/Mod/any")]))
    assert (image / "files" / "shared.bin").read_bytes() == b"\x88"


def test_ambiguous_filename_search_is_skipped_not_guessed(image, sd):
    (image / "files" / "Stage" / "shared.bin").write_bytes(b"\x00")
    src = sd / "Mod" / "any"
    src.mkdir(parents=True)
    (src / "shared.bin").write_bytes(b"\x88")
    report = run(image, sd, Patch(folder_patches=[Folder(external="/Mod/any")]))
    assert Outcome.SKIPPED in outcomes(report)
    assert "share this name" in report.render()


def test_unmatched_filename_search_is_skipped(image, sd):
    src = sd / "Mod" / "any"
    src.mkdir(parents=True)
    (src / "nowhere.bin").write_bytes(b"\x88")
    report = run(image, sd, Patch(folder_patches=[Folder(external="/Mod/any")]))
    assert Outcome.SKIPPED in outcomes(report)


# -- savegame ---------------------------------------------------------------


def test_savegame_is_reported_unsupported_with_a_workaround(image, sd):
    report = run(image, sd, Patch(savegame_patches=[Savegame(external="/Mod/save")]))
    assert outcomes(report) == [Outcome.UNSUPPORTED]
    assert "--game-id" in report.entries[0].reason
