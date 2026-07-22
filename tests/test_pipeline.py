"""Pipeline-level behaviour that does not need wit: resolution, stacking,
and cross-mod conflict detection."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from riivultimatum.pipeline import (
    BuildRequest,
    ModSpec,
    PipelineError,
    bump_game_id,
    detect_conflicts,
    read_boot,
    _resolve_mod,
)
from riivultimatum.report import Outcome, Report


def make_iso(path: Path, game_id: str = "SMNE01") -> Path:
    header = bytearray(0x800)
    header[0:6] = game_id.encode()
    struct.pack_into(">I", header, 0x18, 0x5D1C9EA3)
    header[0x20:0x2A] = b"NEWER SMBW"
    path.write_bytes(bytes(header))
    return path


def write_xml(path: Path, body: str, game: str = "SMN") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<wiidisc version="1"><id game="{game}"/>{body}</wiidisc>'
    )
    return path


def _mem(offset: str, value: str) -> str:
    return f'<memory offset="{offset}" value="{value}"/>'


def _resolve(xml: Path, iso: Path):
    boot = read_boot(iso)
    return _resolve_mod(ModSpec(xml=xml, all_defaults=True), boot)


# -- resolution -------------------------------------------------------------


def test_unconditional_patches_resolve_without_selection(tmp_path: Path):
    iso = make_iso(tmp_path / "g.iso")
    xml = write_xml(
        tmp_path / "m.xml",
        f'<patch id="p">{_mem("0x80003000", "60000000")}</patch>',
    )
    mod = _resolve(xml, iso)
    assert len(mod.patches) == 1


def test_mod_for_another_game_raises(tmp_path: Path):
    iso = make_iso(tmp_path / "g.iso", game_id="RMGE01")
    xml = write_xml(tmp_path / "m.xml", '<patch id="p"/>')
    with pytest.raises(PipelineError, match="does not apply"):
        _resolve(xml, iso)


# -- conflict detection -----------------------------------------------------


def test_overlapping_memory_writes_are_flagged(tmp_path: Path):
    iso = make_iso(tmp_path / "g.iso")
    a = write_xml(
        tmp_path / "a.xml", f'<patch id="p">{_mem("0x80003000", "11223344")}</patch>'
    )
    b = write_xml(
        tmp_path / "b.xml", f'<patch id="p">{_mem("0x80003002", "AABBCCDD")}</patch>'
    )
    mods = [_resolve(a, iso), _resolve(b, iso)]
    report = Report()
    detect_conflicts(mods, report)
    assert report.count(Outcome.WARNING) == 1
    assert "wins" in report.entries[0].reason


def test_disjoint_memory_writes_do_not_conflict(tmp_path: Path):
    iso = make_iso(tmp_path / "g.iso")
    a = write_xml(
        tmp_path / "a.xml", f'<patch id="p">{_mem("0x80003000", "11223344")}</patch>'
    )
    b = write_xml(
        tmp_path / "b.xml", f'<patch id="p">{_mem("0x80004000", "AABBCCDD")}</patch>'
    )
    mods = [_resolve(a, iso), _resolve(b, iso)]
    report = Report()
    detect_conflicts(mods, report)
    assert report.count(Outcome.WARNING) == 0


def test_same_disc_file_replacement_is_flagged(tmp_path: Path):
    iso = make_iso(tmp_path / "g.iso")
    a = write_xml(
        tmp_path / "a.xml",
        '<patch id="p"><file disc="/Stage/Course1.arc" external="a.arc"/></patch>',
    )
    b = write_xml(
        tmp_path / "b.xml",
        '<patch id="p"><file disc="/Stage/Course1.arc" external="b.arc"/></patch>',
    )
    mods = [_resolve(a, iso), _resolve(b, iso)]
    report = Report()
    detect_conflicts(mods, report)
    assert report.count(Outcome.WARNING) == 1
    assert "Course1.arc" in report.entries[0].detail


def test_search_and_ocarina_are_excluded_from_static_conflicts(tmp_path: Path):
    """Their target address is unknown until the DOL is in hand, so they must
    not produce phantom conflicts against a direct write at the same offset."""
    iso = make_iso(tmp_path / "g.iso")
    a = write_xml(
        tmp_path / "a.xml",
        '<patch id="p"><memory offset="0x80003000" value="60000000" '
        'original="4E800020" search="true"/></patch>',
    )
    b = write_xml(
        tmp_path / "b.xml", f'<patch id="p">{_mem("0x80003000", "60000000")}</patch>'
    )
    mods = [_resolve(a, iso), _resolve(b, iso)]
    report = Report()
    detect_conflicts(mods, report)
    assert report.count(Outcome.WARNING) == 0


# -- request validation -----------------------------------------------------


def test_build_without_mods_raises(tmp_path: Path):
    from riivultimatum.pipeline import build

    request = BuildRequest(iso=make_iso(tmp_path / "g.iso"), mods=[], out=tmp_path / "o.iso")
    with pytest.raises(PipelineError, match="no mods"):
        build(request)


def test_build_without_output_or_dry_run_raises(tmp_path: Path):
    from riivultimatum.pipeline import build

    xml = write_xml(tmp_path / "m.xml", '<patch id="p"/>')
    request = BuildRequest(iso=make_iso(tmp_path / "g.iso"), mods=[ModSpec(xml=xml)])
    with pytest.raises(PipelineError, match="output path is required"):
        build(request)


def test_bump_game_id_touches_only_the_game_code():
    for gid in ("SMNE01", "SB4E01", "RMGP01"):
        out = bump_game_id(gid)
        assert out[3:] == gid[3:] and out != gid
