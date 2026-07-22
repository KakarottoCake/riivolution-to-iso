"""XML parsing, option selection, and path resolution."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from riivultimatum.riivo.parser import (
    RiivolutionXmlError,
    parse_bool,
    parse_hex_string,
    parse_int,
    parse_tree,
    substitute,
)
from riivultimatum.riivo.resolve import (
    ExternalPathError,
    ExternalResolver,
    SelectionError,
    apply_selections,
    builtin_params,
    disc_path_to_fst,
    selected_patches,
)


def load(xml: str):
    return parse_tree(ET.fromstring(xml))


# -- scalar parsing ---------------------------------------------------------


@pytest.mark.parametrize("text,expected", [("true", True), ("yes", True), ("1", True),
                                           ("false", False), ("no", False), ("0", False)])
def test_parse_bool(text, expected):
    assert parse_bool(text, not expected) is expected


def test_parse_bool_rejects_garbage():
    with pytest.raises(RiivolutionXmlError):
        parse_bool("maybe", False)


@pytest.mark.parametrize("text,expected", [("16", 16), ("0x10", 16), ("0X10", 16), ("", 0)])
def test_parse_int(text, expected):
    assert parse_int(text) == expected


def test_parse_hex_string_strips_prefix():
    assert parse_hex_string("0x4E800020") == b"\x4e\x80\x00\x20"
    assert parse_hex_string("4e800020") == b"\x4e\x80\x00\x20"


def test_parse_hex_string_is_lenient_about_odd_lengths():
    """Dolphin returns empty rather than raising; mods in the wild contain both."""
    assert parse_hex_string("4e8") == b""
    assert parse_hex_string(None) == b""


# -- substitution -----------------------------------------------------------


def test_substitute_accepts_both_spellings():
    params = {"name": "Newer", "__gameid": "SMN"}
    assert substitute("/${name}/${__gameid}", params) == "/Newer/SMN"
    assert substitute("/{$name}/{$__gameid}", params) == "/Newer/SMN"


def test_substitute_leaves_unknown_variables_visible():
    """A silently-emptied variable becomes an unfindable path; keep it loud."""
    assert substitute("/${nope}/x", {}) == "/${nope}/x"


def test_builtin_params():
    assert builtin_params("SMNE01") == {"__gameid": "SMN", "__region": "E", "__maker": "01"}


# -- document structure -----------------------------------------------------

BASIC = """
<wiidisc version="1" root="/Newer">
  <id game="SMN" developer="01"><region type="E"/><region type="P"/></id>
  <options>
    <section name="Newer">
      <option name="Game" default="1">
        <choice name="Enabled"><patch id="main"/></choice>
        <choice name="Alt"><patch id="alt"/></choice>
      </option>
    </section>
  </options>
  <patch id="main">
    <folder external="/Newer/files" disc="/"/>
    <memory offset="0x80001800" valuefile="/codes/handler.bin"/>
    <memory offset="0x80003100" value="60000000" original="4E800020"/>
  </patch>
  <patch id="alt"><file disc="/x.bin" external="/Newer/x.bin"/></patch>
</wiidisc>
"""


def test_parses_structure():
    disc = load(BASIC)
    assert disc.version == 1
    assert disc.root == "/Newer"
    assert disc.game_filter.game == "SMN"
    assert disc.game_filter.regions == ["E", "P"]
    assert [s.name for s in disc.sections] == ["Newer"]
    assert len(disc.patches) == 2

    main = next(p for p in disc.patches if p.id == "main")
    assert len(main.memory_patches) == 2
    assert main.memory_patches[1].original == b"\x4e\x80\x00\x20"


def test_rejects_non_version_1():
    with pytest.raises(RiivolutionXmlError, match="unsupported wiidisc version"):
        load('<wiidisc version="2"/>')


def test_rejects_wrong_root_element():
    with pytest.raises(RiivolutionXmlError, match="expected <wiidisc>"):
        load("<patches/>")


def test_game_filter():
    disc = load(BASIC)
    assert disc.is_valid_for_game("SMNE01", 0, 0)
    assert disc.is_valid_for_game("SMNP01", 0, 0)
    assert not disc.is_valid_for_game("SMNJ01", 0, 0)  # region not listed
    assert not disc.is_valid_for_game("RMGE01", 0, 0)  # different game
    assert not disc.is_valid_for_game("SMNE02", 0, 0)  # different developer


# -- selection --------------------------------------------------------------


def test_default_selects_the_first_choice():
    disc = load(BASIC)
    patches = selected_patches(disc, "SMNE01")
    assert [p.id for p in patches] == ["main"]


def test_choice_by_name_and_by_index():
    disc = load(BASIC)
    apply_selections(disc, {"Newer/Game": "Alt"})
    assert [p.id for p in selected_patches(disc, "SMNE01")] == ["alt"]

    apply_selections(disc, {"Newer/Game": "1"})
    assert [p.id for p in selected_patches(disc, "SMNE01")] == ["main"]


def test_choice_off_disables_the_option():
    disc = load(BASIC)
    apply_selections(disc, {"Newer/Game": "off"})
    assert selected_patches(disc, "SMNE01") == []


def test_unknown_option_and_choice_are_errors():
    disc = load(BASIC)
    with pytest.raises(SelectionError, match="no option matches"):
        apply_selections(disc, {"Nope/Nope": "Enabled"})
    with pytest.raises(SelectionError, match="has no choice"):
        apply_selections(disc, {"Newer/Game": "Nope"})


def test_no_option_tree_means_every_patch_applies():
    disc = load(
        '<wiidisc version="1"><patch id="a"><memory offset="0x80001800" value="00"/></patch></wiidisc>'
    )
    assert [p.id for p in selected_patches(disc, "SMNE01")] == ["a"]


def test_dangling_patch_reference_is_an_error():
    disc = load(
        '<wiidisc version="1"><options><section name="S">'
        '<option name="O" default="1"><choice name="C"><patch id="ghost"/></choice></option>'
        "</section></options></wiidisc>"
    )
    with pytest.raises(SelectionError, match="undefined patch id"):
        selected_patches(disc, "SMNE01")


# -- macros -----------------------------------------------------------------

MACRO = """
<wiidisc version="1">
  <options>
    <section name="Base">
      <option id="tpl" name="Template" default="1">
        <choice name="On"><patch id="p"/></choice>
      </option>
    </section>
    <section name="Clones">
      <macro id="tpl" name="Clone A" default="1"><param name="dir" value="A"/></macro>
      <macro id="tpl" name="Clone B" default="1"><param name="dir" value="B"/></macro>
    </section>
  </options>
  <patch id="p"><file disc="/out.bin" external="/${dir}/in.bin"/></patch>
</wiidisc>
"""


def test_macro_clones_an_option_with_its_own_params():
    disc = load(MACRO)
    clones = disc.sections[1]
    assert [o.name for o in clones.options] == ["Clone A", "Clone B"]

    externals = [p.file_patches[0].external for p in selected_patches(disc, "SMNE01")]
    # Base/Template fires too, with no dir param bound.
    assert "/A/in.bin" in externals and "/B/in.bin" in externals


def test_macro_with_unknown_id_is_an_error():
    with pytest.raises(RiivolutionXmlError, match="unknown option id"):
        load(
            '<wiidisc version="1"><options><section name="S">'
            '<macro id="ghost" name="X"/></section></options></wiidisc>'
        )


# -- external path resolution -----------------------------------------------


def test_absolute_external_path_is_relative_to_the_sd_root(tmp_path: Path):
    r = ExternalResolver(tmp_path, "/riivolution")
    assert r.resolve("/Newer/main.dol") == tmp_path / "Newer" / "main.dol"


def test_relative_external_path_uses_the_patch_root(tmp_path: Path):
    r = ExternalResolver(tmp_path, "/riivolution")
    assert r.resolve("main.dol", patch_root="/Newer") == tmp_path / "Newer" / "main.dol"


def test_relative_patch_root_nests_under_the_disc_root(tmp_path: Path):
    r = ExternalResolver(tmp_path, "/riivolution")
    assert r.resolve("a.bin", patch_root="mod") == tmp_path / "riivolution" / "mod" / "a.bin"


def test_relative_external_path_falls_back_to_the_disc_root(tmp_path: Path):
    r = ExternalResolver(tmp_path, "/Newer")
    assert r.resolve("main.dol") == tmp_path / "Newer" / "main.dol"


def test_traversal_out_of_the_mod_root_is_refused(tmp_path: Path):
    """Mod XMLs are untrusted input."""
    r = ExternalResolver(tmp_path, "/riivolution")
    with pytest.raises(ExternalPathError, match="escapes the mod root"):
        r.resolve("/../../etc/passwd")


# -- disc path mapping ------------------------------------------------------


@pytest.mark.parametrize(
    "disc_path,expected",
    [
        ("/Stage/01.arc", "files/Stage/01.arc"),
        ("Stage/01.arc", "files/Stage/01.arc"),
        ("/main.dol", "sys/main.dol"),
        ("/sys/main.dol", "sys/main.dol"),
        ("/sys/bi2.bin", "sys/bi2.bin"),
        ("/Stage/main.dol", "files/Stage/main.dol"),
    ],
)
def test_disc_path_to_fst(disc_path, expected):
    assert str(disc_path_to_fst(disc_path)).replace("\\", "/") == expected


def test_disc_path_traversal_is_refused():
    with pytest.raises(ExternalPathError, match="escapes the image"):
        disc_path_to_fst("/../sys/main.dol")
