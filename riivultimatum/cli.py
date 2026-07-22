"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import __version__
from .disc.backend import DiscError, read_boot_info, usb_loader_gx_path
from .disc.wit import WitBackend
from .dol.memory import MemoryLowerer
from .dol.reader import Dol, DolError
from .report import Report
from .riivo import parser as riivo_parser
from .riivo import resolve as riivo_resolve
from .riivo.fst import FstPatcher
from .riivo.parser import RiivolutionXmlError
from .riivo.resolve import SelectionError


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="riivultimatum",
        description=(
            "Bake a Riivolution mod into a standalone Wii ISO that boots under "
            "USB Loader GX, including memory/code patches."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  riivultimatum --iso game.iso --xml sd/riivolution/mod.xml "
            "--sd-root sd --list-options\n"
            "  riivultimatum --iso game.iso --xml sd/riivolution/mod.xml "
            '--sd-root sd --choice "Newer/Game=Enabled" --out newer.iso\n'
        ),
    )
    p.add_argument("--version", action="version", version=f"riivultimatum {__version__}")
    p.add_argument("--iso", type=Path, required=True, help="source (unmodified) disc image")
    p.add_argument("--xml", type=Path, required=True, help="the mod's Riivolution XML")
    p.add_argument(
        "--sd-root",
        type=Path,
        help=(
            "root of the mod's SD-card layout (the folder containing "
            "riivolution/). Defaults to the XML's parent's parent."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        help=(
            "output path. An .iso file by default; with --wbfs, the drive's "
            "wbfs/ directory, into which '<Title> [<ID>]/<ID>.wbfs' is written."
        ),
    )
    p.add_argument(
        "--wbfs",
        action="store_true",
        help=(
            "write a split WBFS image in USB Loader GX's layout instead of an "
            "ISO, so --out can point straight at the wbfs/ folder on the drive"
        ),
    )
    p.add_argument(
        "--choice",
        action="append",
        default=[],
        metavar="SECTION/OPTION=CHOICE",
        help="select an option; repeatable. Use --list-options to see the tree.",
    )
    p.add_argument(
        "--list-options",
        action="store_true",
        help="print the option tree and exit",
    )
    p.add_argument(
        "--all-defaults",
        action="store_true",
        help=(
            "keep every option at its XML default instead of requiring explicit "
            "--choice flags. Options default to disabled unless the XML says otherwise."
        ),
    )
    p.add_argument("--game-id", help="override the 6-character game ID in the output")
    p.add_argument(
        "--keep-game-id",
        action="store_true",
        help=(
            "do not auto-change the game ID. By default the 4th character is "
            "bumped so the mod gets its own save slot, since Riivolution's "
            "savegame redirection cannot be baked into an ISO."
        ),
    )
    p.add_argument("--title", help="override the disc title in the output")
    p.add_argument("--wit-path", help="path to the wit executable")
    p.add_argument(
        "--work-dir",
        type=Path,
        help="staging directory (kept on exit; otherwise a temp dir is used)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be patched without building an ISO",
    )
    return p


def _default_sd_root(xml: Path) -> Path:
    """Guess the SD root from the XML's location.

    Mods ship as `<sd>/riivolution/mod.xml`, so the SD root is normally two
    levels up. If the XML is not inside a `riivolution` folder, fall back to
    its parent.
    """
    if xml.parent.name.lower() == "riivolution":
        return xml.parent.parent
    return xml.parent


def _parse_choices(raw: list[str]) -> dict[str, str]:
    selections: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise SelectionError(
                f"--choice {item!r} must be of the form SECTION/OPTION=CHOICE"
            )
        key, value = item.split("=", 1)
        selections[key.strip()] = value.strip()
    return selections


def _bump_game_id(game_id: str) -> str:
    """Give the mod its own save slot by altering the *game code*.

    A game ID is `CCCRMM`: three game-code characters, one region character,
    two maker characters. Only the game code may be touched. Changing the
    region character re-flags the disc -- wit derives the region setting from
    it, so bumping SB4E01 to SB4R01 turns a USA game into a PAL one, with the
    50Hz and region-check breakage that implies.
    """
    replacement = "Z" if game_id[2] != "Z" else "Y"
    return game_id[:2] + replacement + game_id[3:6]


def run(args: argparse.Namespace) -> int:
    report = Report()

    # -- inputs ---------------------------------------------------------
    if not args.iso.is_file():
        print(f"error: no such ISO: {args.iso}", file=sys.stderr)
        return 2
    if not args.xml.is_file():
        print(f"error: no such XML: {args.xml}", file=sys.stderr)
        return 2

    boot = read_boot_info(args.iso)
    disc = riivo_parser.parse(args.xml)
    sd_root = args.sd_root or _default_sd_root(args.xml)

    print(f"Game:  {boot.game_id}  rev {boot.revision}  {boot.title}")
    print(f"Mod:   {args.xml}")
    print(f"SD:    {sd_root}")

    if not disc.is_valid_for_game(boot.game_id, boot.disc_number, boot.revision):
        f = disc.game_filter
        print(
            f"error: this XML does not apply to {boot.game_id}. It targets "
            f"game={f.game!r} developer={f.developer!r} regions={f.regions or 'any'}.",
            file=sys.stderr,
        )
        return 2

    # -- option selection ------------------------------------------------
    if args.list_options:
        print()
        print(riivo_resolve.describe_options(disc))
        return 0

    selections = _parse_choices(args.choice)
    riivo_resolve.apply_selections(disc, selections)

    if not selections and not args.all_defaults and disc.sections:
        print(
            "error: this XML has options but no --choice was given. Run with "
            "--list-options to see them, or pass --all-defaults to accept the "
            "XML's own defaults (usually 'disabled').",
            file=sys.stderr,
        )
        return 2

    patches = riivo_resolve.selected_patches(disc, boot.game_id)
    if not patches:
        print("error: the current selection activates no patches.", file=sys.stderr)
        return 2
    print(f"Active patches: {len(patches)}")

    if not args.dry_run and not args.out:
        print("error: --out is required unless --dry-run is given", file=sys.stderr)
        return 2

    # -- staging ---------------------------------------------------------
    backend = WitBackend(args.wit_path)
    temp_dir: tempfile.TemporaryDirectory | None = None
    if args.work_dir:
        work = args.work_dir
        work.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="riivultimatum-")
        work = Path(temp_dir.name)

    try:
        resolver = riivo_resolve.ExternalResolver(sd_root, disc.root)

        if args.dry_run:
            # Without an extracted image we cannot check disc-side targets, so
            # only the memory patches are meaningfully verifiable here.
            print("\n(dry run: extracting image to evaluate patches)")

        print("Extracting image...")
        image_root = backend.extract(args.iso, work / "image")

        print("Applying file/folder patches...")
        FstPatcher(image_root, resolver, report).apply(patches)

        print("Applying memory patches...")
        dol_path = image_root / "sys" / "main.dol"
        if not dol_path.is_file():
            report.failed("memory", "sys/main.dol", "executable missing from image")
        else:
            dol = Dol.parse(dol_path.read_bytes())
            MemoryLowerer(dol, resolver, report).apply(patches)
            dol_path.write_bytes(dol.serialize())

        print()
        print(report.render())
        print()

        if report.has_failures:
            print(
                "error: some patches failed; refusing to build a broken ISO.",
                file=sys.stderr,
            )
            return 1

        if args.dry_run:
            print("Dry run complete; no ISO written.")
            return 0

        game_id = args.game_id
        if not game_id and not args.keep_game_id:
            game_id = _bump_game_id(boot.game_id)
        if game_id:
            print(f"Output game ID: {game_id} (was {boot.game_id})")

        title = args.title or boot.title
        if args.wbfs:
            destination = usb_loader_gx_path(args.out, game_id or boot.game_id, title)
        else:
            destination = args.out

        print(f"Building {destination}...")
        destination.parent.mkdir(parents=True, exist_ok=True)
        backend.compose(
            image_root, destination, game_id=game_id, title=args.title, wbfs=args.wbfs
        )
        print(f"Done: {destination}")
        if args.wbfs:
            print(
                "Eject the drive safely, then in USB Loader GX set this game's "
                "IOS to your cIOS slot (usually 249) and turn Ocarina/cheats "
                "OFF -- the mod's code is already baked in."
            )
        return 0

    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
        elif args.work_dir:
            print(f"(staging kept at {args.work_dir})")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return run(args)
    except (RiivolutionXmlError, SelectionError, DiscError, DolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
