"""Choice selection, ${param} substitution, and path resolution.

Turning an XML plus a set of user choices into a flat, fully-substituted list of
`Patch` objects with real filesystem paths attached.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .parser import Disc, Patch, Section, substitute


class SelectionError(Exception):
    """The requested option/choice does not exist, or is ambiguous."""


class ExternalPathError(Exception):
    """An external (SD-card) path could not be resolved safely."""


# --------------------------------------------------------------------------
# Choice selection
# --------------------------------------------------------------------------


def builtin_params(game_id: str) -> dict[str, str]:
    """`${__gameid}` / `${__region}` / `${__maker}` from a 6-char game ID."""
    if len(game_id) < 6:
        raise SelectionError(f"invalid game id {game_id!r}")
    return {
        "__gameid": game_id[:3],
        "__region": game_id[3],
        "__maker": game_id[4:6],
    }


def option_key(section: Section, option) -> str:
    """The `Section/Option` string used to address an option on the CLI."""
    return f"{section.name}/{option.name}"


def describe_options(disc: Disc) -> str:
    """Human-readable dump of the option tree for `--list-options`."""
    lines: list[str] = []
    for section in disc.sections:
        lines.append(f"[{section.name}]")
        for option in section.options:
            marker = "" if option.id == "" else f"  (id: {option.id})"
            lines.append(f"  {option.name}{marker}")
            for index, choice in enumerate(option.choices, start=1):
                selected = " *" if index == option.selected_choice else "  "
                lines.append(f"   {selected} {index}. {choice.name}")
            disabled = " *" if option.selected_choice == 0 else "  "
            lines.append(f"   {disabled} 0. <disabled>")
        lines.append("")
    if not lines:
        lines.append("(this XML declares no options; all patches apply unconditionally)")
    return "\n".join(lines)


def apply_selections(disc: Disc, selections: dict[str, str]) -> None:
    """Override `option.selected_choice` in-place from CLI `--choice` values.

    Keys may be `Section/Option`, or a bare option `id`. Values may be a choice
    name (case-insensitive), a 1-based index, or `off`/`disabled`.
    """
    for raw_key, raw_value in selections.items():
        key = raw_key.strip()
        matches = []
        for section in disc.sections:
            for option in section.options:
                if key == option_key(section, option) or (option.id and key == option.id):
                    matches.append((section, option))

        if not matches:
            raise SelectionError(
                f"no option matches {key!r}; run --list-options to see valid keys"
            )
        if len(matches) > 1:
            raise SelectionError(
                f"{key!r} matches {len(matches)} options; qualify it as Section/Option"
            )

        _, option = matches[0]
        option.selected_choice = _resolve_choice_value(option, raw_value.strip())


def _resolve_choice_value(option, value: str) -> int:
    # An empty value means "leave this option disabled". The GUI stores a
    # disabled option as "", and on the CLI `--choice "Section/Option="` reads
    # the same way; both must resolve to choice 0, not fall through to the
    # "no such choice ''" error.
    if value.strip().lower() in {"", "off", "disabled", "none"}:
        return 0
    if value.isdigit():
        index = int(value)
        if index > len(option.choices):
            raise SelectionError(
                f"option {option.name!r} has {len(option.choices)} choices, got index {index}"
            )
        return index
    for index, choice in enumerate(option.choices, start=1):
        if choice.name.lower() == value.lower():
            return index
    names = ", ".join(repr(c.name) for c in option.choices)
    raise SelectionError(
        f"option {option.name!r} has no choice {value!r}; valid choices are {names}"
    )


# --------------------------------------------------------------------------
# Flattening the selection into concrete patches
# --------------------------------------------------------------------------


def _substitute_patch(patch: Patch, params: dict[str, str]) -> Patch:
    """Deep-copy `patch` with every path string variable-substituted."""
    out = copy.deepcopy(patch)
    out.root = substitute(out.root, params)
    for f in out.file_patches:
        f.disc = substitute(f.disc, params)
        f.external = substitute(f.external, params)
    for f in out.folder_patches:
        f.disc = substitute(f.disc, params)
        f.external = substitute(f.external, params)
    for s in out.savegame_patches:
        s.external = substitute(s.external, params)
    for m in out.memory_patches:
        m.valuefile = substitute(m.valuefile, params)
    return out


def selected_patches(disc: Disc, game_id: str) -> list[Patch]:
    """Flatten the current selection into an ordered list of concrete patches.

    Order follows the XML: sections, then options, then the referenced patches
    within the selected choice. Riivolution applies memory patches in this
    order, and later patches legitimately overwrite earlier ones.
    """
    by_id = {p.id: p for p in disc.patches if p.id}
    builtins = builtin_params(game_id)
    result: list[Patch] = []

    if not disc.sections:
        # No option tree: every declared patch applies unconditionally.
        return [_substitute_patch(p, builtins) for p in disc.patches]

    for section in disc.sections:
        for option in section.options:
            index = option.selected_choice
            if index == 0 or index > len(option.choices):
                continue
            choice = option.choices[index - 1]
            for patch_id, params in choice.patch_references:
                patch = by_id.get(patch_id)
                if patch is None:
                    raise SelectionError(
                        f"choice {choice.name!r} references undefined patch id {patch_id!r}"
                    )
                merged = {**builtins, **params}
                result.append(_substitute_patch(patch, merged))

    return result


# --------------------------------------------------------------------------
# External (SD-card) path resolution
# --------------------------------------------------------------------------


@dataclass
class ExternalResolver:
    """Maps Riivolution external paths onto the local mod folder.

    `sd_root` stands in for the root of the SD card the mod was designed for.
    An absolute Riivolution path is relative to that root; a relative path is
    relative to the patch root, which itself falls back to the `<wiidisc root>`.
    """

    sd_root: Path
    disc_root: str = "/riivolution"

    def resolve(self, external: str, patch_root: str = "") -> Path:
        if not external:
            raise ExternalPathError("empty external path")

        path = external.replace("\\", "/")
        if path.startswith("/"):
            rel = path
        else:
            base = (patch_root or self.disc_root).replace("\\", "/")
            if not base.startswith("/"):
                base = f"{self.disc_root.rstrip('/')}/{base}"
            rel = f"{base.rstrip('/')}/{path}"

        # A mod XML is untrusted input; refuse to walk out of the mod folder.
        normalized = PurePosixPath(rel.lstrip("/"))
        parts = [p for p in normalized.parts if p not in (".", "")]
        if any(p == ".." for p in parts):
            raise ExternalPathError(f"path escapes the mod root: {external!r}")

        return self.sd_root.joinpath(*parts)


# --------------------------------------------------------------------------
# Disc path resolution
# --------------------------------------------------------------------------


def disc_path_to_fst(disc_path: str) -> PurePosixPath:
    """Map a Riivolution `disc` path onto an extracted-image relative path.

    `wit extract` lays an image out as `sys/` (boot.bin, main.dol, ...) plus
    `files/` (the FST proper). Riivolution's disc namespace is the FST, but
    real-world XMLs also reference the executable as `/main.dol` or
    `/sys/main.dol`, so both spellings are honoured.
    """
    path = disc_path.replace("\\", "/").strip()
    if not path.startswith("/"):
        path = "/" + path

    parts = [p for p in PurePosixPath(path).parts if p not in ("/", ".", "")]
    if any(p == ".." for p in parts):
        raise ExternalPathError(f"disc path escapes the image: {disc_path!r}")

    if not parts:
        raise ExternalPathError("empty disc path")

    _SYS_FILES = {"main.dol", "boot.bin", "bi2.bin", "apploader.img", "fst.bin"}
    if parts[0] == "sys":
        return PurePosixPath("sys", *parts[1:])
    if len(parts) == 1 and parts[0] in _SYS_FILES:
        return PurePosixPath("sys", parts[0])
    return PurePosixPath("files", *parts)
