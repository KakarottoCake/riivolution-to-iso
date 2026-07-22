"""Riivolution XML parsing.

The data model mirrors Dolphin's `DiscIO/RiivolutionParser.h` field-for-field so
that behaviour can be compared directly against Dolphin, which is the closest
thing to a reference implementation of the format.

Reference: https://riivolution.github.io/wiki/Patch_Format/
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


class RiivolutionXmlError(Exception):
    """The XML is malformed or uses features we refuse to guess at."""


# --------------------------------------------------------------------------
# Attribute value parsing
# --------------------------------------------------------------------------

_TRUE = {"true", "yes", "1"}
_FALSE = {"false", "no", "0"}


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise RiivolutionXmlError(f"expected a boolean, got {value!r}")


def parse_int(value: str | None, default: int = 0) -> int:
    """Riivolution integers are decimal, or hex with an 0x prefix."""
    if value is None:
        return default
    v = value.strip()
    if not v:
        return default
    try:
        if v.lower().startswith(("0x", "-0x")):
            return int(v, 16)
        return int(v, 10)
    except ValueError as exc:
        raise RiivolutionXmlError(f"expected an integer, got {value!r}") from exc


def parse_hex_string(value: str | None) -> bytes:
    """Parse a Riivolution hex string.

    Matches Dolphin's `ReadHexString`: an optional 0x/0X prefix is stripped and
    an odd number of remaining digits yields empty rather than an error, since
    mods in the wild contain both and Riivolution itself is permissive.
    """
    if value is None:
        return b""
    v = value.strip()
    if v.lower().startswith("0x"):
        v = v[2:]
    if not v or len(v) % 2 == 1:
        return b""
    try:
        return bytes.fromhex(v)
    except ValueError:
        return b""


# --------------------------------------------------------------------------
# Patch item types
# --------------------------------------------------------------------------


@dataclass
class File:
    disc: str = ""
    external: str = ""
    resize: bool = True
    create: bool = False
    offset: int = 0
    fileoffset: int = 0
    length: int = 0


@dataclass
class Folder:
    disc: str = ""
    external: str = ""
    resize: bool = True
    create: bool = False
    recursive: bool = True
    length: int = 0


@dataclass
class Memory:
    offset: int = 0
    value: bytes = b""
    valuefile: str = ""
    original: bytes = b""
    ocarina: bool = False
    search: bool = False
    align: int = 1


@dataclass
class Savegame:
    external: str = ""
    clone: bool = True


@dataclass
class Patch:
    id: str = ""
    root: str = ""
    file_patches: list[File] = field(default_factory=list)
    folder_patches: list[Folder] = field(default_factory=list)
    savegame_patches: list[Savegame] = field(default_factory=list)
    memory_patches: list[Memory] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.file_patches
            or self.folder_patches
            or self.savegame_patches
            or self.memory_patches
        )


# --------------------------------------------------------------------------
# Option tree
# --------------------------------------------------------------------------


@dataclass
class Choice:
    name: str = ""
    #: (patch id, params visible to that reference)
    patch_references: list[tuple[str, dict[str, str]]] = field(default_factory=list)


@dataclass
class Option:
    name: str = ""
    id: str = ""
    choices: list[Choice] = field(default_factory=list)
    #: 1-based index into `choices`; 0 means "disabled" (Dolphin's default).
    selected_choice: int = 0


@dataclass
class Section:
    name: str = ""
    options: list[Option] = field(default_factory=list)


@dataclass
class GameFilter:
    game: str | None = None
    developer: str | None = None
    disc: int | None = None
    version: int | None = None
    regions: list[str] = field(default_factory=list)


@dataclass
class Disc:
    version: int = 0
    game_filter: GameFilter = field(default_factory=GameFilter)
    sections: list[Section] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    xml_path: Path | None = None
    #: `<wiidisc root="...">`, defaulting to /riivolution per the spec.
    root: str = "/riivolution"

    def is_valid_for_game(self, game_id: str, disc_number: int, revision: int) -> bool:
        """Mirror of Dolphin's `Disc::IsValidForGame`.

        `game_id` is the full 6-character ID from the disc header.
        """
        f = self.game_filter
        if len(game_id) < 6:
            raise RiivolutionXmlError(f"invalid game id {game_id!r}")
        region = game_id[3]
        developer = game_id[4:6]

        if f.game is not None and not game_id.startswith(f.game):
            return False
        if f.developer is not None and developer != f.developer:
            return False
        if f.disc is not None and disc_number != f.disc:
            return False
        if f.version is not None and revision != f.version:
            return False
        if f.regions and region not in f.regions:
            return False
        return True


# --------------------------------------------------------------------------
# Variable substitution
# --------------------------------------------------------------------------

# Riivolution's docs spell this ${name}; Dolphin's parser looks for {$name}.
# Real-world XMLs contain both, so accept either.
_VAR_RE = re.compile(r"\$\{([^}]+)\}|\{\$([^}]+)\}")


def substitute(text: str, params: dict[str, str]) -> str:
    """Replace ${name} / {$name} with values from `params`.

    Unknown variables are left verbatim so the resulting path error names the
    variable that was never defined, rather than silently becoming garbage.
    """
    if not text or "{" not in text:
        return text

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return params.get(name, m.group(0))

    return _VAR_RE.sub(repl, text)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _read_params(node: ET.Element, inherited: dict[str, str]) -> dict[str, str]:
    params = dict(inherited)
    for param in node.findall("param"):
        name = param.get("name")
        if name:
            params[name] = param.get("value", "")
    return params


def _parse_patch(node: ET.Element) -> Patch:
    patch = Patch(id=node.get("id", ""), root=node.get("root", ""))

    for child in node:
        tag = child.tag
        if tag == "file":
            patch.file_patches.append(
                File(
                    disc=child.get("disc", ""),
                    external=child.get("external", ""),
                    resize=parse_bool(child.get("resize"), True),
                    create=parse_bool(child.get("create"), False),
                    offset=parse_int(child.get("offset")),
                    fileoffset=parse_int(child.get("fileoffset")),
                    length=parse_int(child.get("length")),
                )
            )
        elif tag == "folder":
            patch.folder_patches.append(
                Folder(
                    disc=child.get("disc", ""),
                    external=child.get("external", ""),
                    resize=parse_bool(child.get("resize"), True),
                    create=parse_bool(child.get("create"), False),
                    recursive=parse_bool(child.get("recursive"), True),
                    length=parse_int(child.get("length")),
                )
            )
        elif tag == "savegame":
            patch.savegame_patches.append(
                Savegame(
                    external=child.get("external", ""),
                    clone=parse_bool(child.get("clone"), True),
                )
            )
        elif tag == "memory":
            patch.memory_patches.append(
                Memory(
                    offset=parse_int(child.get("offset")),
                    value=parse_hex_string(child.get("value")),
                    valuefile=child.get("valuefile", ""),
                    original=parse_hex_string(child.get("original")),
                    ocarina=parse_bool(child.get("ocarina"), False),
                    search=parse_bool(child.get("search"), False),
                    align=max(1, parse_int(child.get("align"), 1)),
                )
            )
        # Unknown elements are ignored, matching Riivolution's own leniency.

    return patch


def _parse_option(node: ET.Element, inherited: dict[str, str]) -> Option:
    option = Option(
        name=node.get("name", ""),
        id=node.get("id", ""),
        selected_choice=parse_int(node.get("default"), 0),
    )
    option_params = _read_params(node, inherited)

    for choice_node in node.findall("choice"):
        choice = Choice(name=choice_node.get("name", ""))
        choice_params = _read_params(choice_node, option_params)
        for ref in choice_node.findall("patch"):
            patch_id = ref.get("id", "")
            if patch_id:
                choice.patch_references.append((patch_id, _read_params(ref, choice_params)))
        option.choices.append(choice)

    return option


def _parse_section(node: ET.Element, options_by_id: dict[str, ET.Element]) -> Section:
    section = Section(name=node.get("name", ""))

    for child in node:
        if child.tag == "option":
            section.options.append(_parse_option(child, {}))
        elif child.tag == "macro":
            # A macro clones an option declared elsewhere (by id), renames it,
            # and supplies params that feed the clone's ${...} substitutions.
            source_id = child.get("id", "")
            source = options_by_id.get(source_id)
            if source is None:
                raise RiivolutionXmlError(
                    f"<macro> references unknown option id {source_id!r}"
                )
            option = _parse_option(source, _read_params(child, {}))
            option.name = child.get("name", option.name)
            option.id = ""  # the clone is a distinct option
            if child.get("default") is not None:
                option.selected_choice = parse_int(child.get("default"), 0)
            section.options.append(option)

    return section


def parse(xml_path: str | Path) -> Disc:
    """Parse a Riivolution XML from disk."""
    xml_path = Path(xml_path)
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        raise RiivolutionXmlError(f"{xml_path}: {exc}") from exc
    return parse_tree(tree.getroot(), xml_path)


def parse_tree(root_node: ET.Element, xml_path: Path | None = None) -> Disc:
    if root_node.tag != "wiidisc":
        raise RiivolutionXmlError(
            f"root element is <{root_node.tag}>, expected <wiidisc>"
        )

    disc = Disc(
        version=parse_int(root_node.get("version"), 0),
        xml_path=xml_path,
        root=root_node.get("root") or "/riivolution",
    )
    if disc.version != 1:
        raise RiivolutionXmlError(
            f"unsupported wiidisc version {disc.version}; only version 1 exists"
        )

    id_node = root_node.find("id")
    if id_node is not None:
        f = disc.game_filter
        f.game = id_node.get("game")
        f.developer = id_node.get("developer")
        f.disc = parse_int(id_node.get("disc")) if id_node.get("disc") else None
        f.version = parse_int(id_node.get("version")) if id_node.get("version") else None
        f.regions = [
            r.get("type", "") for r in id_node.findall("region") if r.get("type")
        ]

    # Macros can reference options declared in any section, so index first.
    options_by_id: dict[str, ET.Element] = {}
    for options_node in root_node.findall("options"):
        for section_node in options_node.findall("section"):
            for option_node in section_node.findall("option"):
                oid = option_node.get("id")
                if oid:
                    options_by_id[oid] = option_node

    for options_node in root_node.findall("options"):
        for section_node in options_node.findall("section"):
            disc.sections.append(_parse_section(section_node, options_by_id))

    for patch_node in root_node.findall("patch"):
        disc.patches.append(_parse_patch(patch_node))

    return disc
