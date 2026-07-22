"""CLI paths that do not require wit to be installed."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from riivultimatum.cli import _bump_game_id, _default_sd_root, main
from riivultimatum.disc.backend import DiscError, read_boot_info, usb_loader_gx_path

XML = """
<wiidisc version="1" root="/Newer">
  <id game="SMN"><region type="E"/></id>
  <options>
    <section name="Newer">
      <option name="Game" default="1">
        <choice name="Enabled"><patch id="main"/></choice>
      </option>
    </section>
  </options>
  <patch id="main"><memory offset="0x80001800" value="60000000"/></patch>
</wiidisc>
"""


def make_iso(path: Path, game_id: str = "SMNE01", title: str = "NEWER SMBW") -> Path:
    """A stub image carrying only the plaintext disc header we read."""
    header = bytearray(0x800)
    header[0:6] = game_id.encode()
    header[6] = 0  # disc number
    header[7] = 0  # revision
    struct.pack_into(">I", header, 0x18, 0x5D1C9EA3)  # Wii magic
    header[0x20 : 0x20 + len(title)] = title.encode()
    path.write_bytes(bytes(header))
    return path


@pytest.fixture
def mod(tmp_path: Path):
    iso = make_iso(tmp_path / "game.iso")
    xml = tmp_path / "sd" / "riivolution" / "mod.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text(XML)
    return iso, xml


# -- disc header ------------------------------------------------------------


def test_read_boot_info(tmp_path: Path):
    info = read_boot_info(make_iso(tmp_path / "g.iso"))
    assert (info.game_id, info.revision, info.title) == ("SMNE01", 0, "NEWER SMBW")


def test_non_disc_image_is_rejected(tmp_path: Path):
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\x00" * 0x800)
    with pytest.raises(DiscError, match="disc magic"):
        read_boot_info(junk)


# -- helpers ----------------------------------------------------------------


def test_bump_game_id_preserves_the_region_character():
    """Character 4 is the region code -- wit derives the disc's region setting
    from it, so touching it turns a USA game into a PAL one."""
    for original in ("SMNE01", "SB4E01", "RMGP01", "SB4J01"):
        bumped = _bump_game_id(original)
        assert len(bumped) == 6
        assert bumped != original
        assert bumped[3] == original[3], "region character must be preserved"
        assert bumped[4:6] == original[4:6], "maker code must be preserved"


def test_bump_game_id_is_stable_when_already_bumped():
    assert _bump_game_id("SBZE01") != "SBZE01"


def test_usb_loader_gx_path_layout(tmp_path: Path):
    got = usb_loader_gx_path(tmp_path / "wbfs", "SB4E01", "SUPER MARIO GALAXY 2")
    assert got == tmp_path / "wbfs" / "SUPER MARIO GALAXY 2 [SB4E01]" / "SB4E01.wbfs"


def test_usb_loader_gx_path_strips_fat32_illegal_characters(tmp_path: Path):
    """USB drives are normally FAT32; a colon in a disc title would otherwise
    fail to create with a confusing error from wit rather than from us."""
    got = usb_loader_gx_path(tmp_path, "RMGE01", 'MARIO: THE "BEST"? <GAME>|EVER*')
    assert ":" not in got.parent.name and '"' not in got.parent.name
    assert got.parent.name == "MARIO THE BEST GAME EVER [RMGE01]"


def test_usb_loader_gx_path_falls_back_to_the_id_for_an_empty_title(tmp_path: Path):
    assert usb_loader_gx_path(tmp_path, "SB4E01", "   ").parent.name == "SB4E01 [SB4E01]"


def test_usb_loader_gx_path_truncates_an_overlong_title(tmp_path: Path):
    got = usb_loader_gx_path(tmp_path, "SB4E01", "X" * 400)
    assert len(got.parent.name) <= 255


def test_default_sd_root_walks_out_of_the_riivolution_folder(tmp_path: Path):
    assert _default_sd_root(tmp_path / "sd" / "riivolution" / "m.xml") == tmp_path / "sd"
    assert _default_sd_root(tmp_path / "m.xml") == tmp_path


# -- CLI --------------------------------------------------------------------


def test_list_options_needs_no_wit(mod, capsys):
    iso, xml = mod
    assert main(["--iso", str(iso), "--xml", str(xml), "--list-options"]) == 0
    out = capsys.readouterr().out
    assert "[Newer]" in out and "Enabled" in out


def test_xml_for_a_different_game_is_rejected(tmp_path: Path, mod, capsys):
    _, xml = mod
    other = make_iso(tmp_path / "other.iso", game_id="RMGE01")
    assert main(["--iso", str(other), "--xml", str(xml), "--list-options"]) == 2
    assert "does not apply to RMGE01" in capsys.readouterr().err


def test_wrong_region_is_rejected(tmp_path: Path, mod, capsys):
    _, xml = mod
    jp = make_iso(tmp_path / "jp.iso", game_id="SMNJ01")
    assert main(["--iso", str(jp), "--xml", str(xml), "--list-options"]) == 2
    assert "does not apply" in capsys.readouterr().err


def test_options_without_a_choice_flag_are_refused(mod, capsys):
    iso, xml = mod
    assert main(["--iso", str(iso), "--xml", str(xml), "--out", "x.iso"]) == 2
    assert "--list-options" in capsys.readouterr().err


def test_bad_choice_syntax_is_reported(mod, capsys):
    iso, xml = mod
    code = main(["--iso", str(iso), "--xml", str(xml), "--choice", "NoEquals", "--out", "x.iso"])
    assert code == 1
    assert "SECTION/OPTION=CHOICE" in capsys.readouterr().err


def test_missing_iso_is_reported(tmp_path: Path, mod, capsys):
    _, xml = mod
    assert main(["--iso", str(tmp_path / "nope.iso"), "--xml", str(xml), "--list-options"]) == 2
    assert "no such ISO" in capsys.readouterr().err
