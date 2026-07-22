"""Command-line entry point.

Thin wrapper over `pipeline.build`. All the real work -- resolving mods,
stacking them, conflict detection, extract/patch/compose -- lives in
`pipeline.py` so the GUI runs the identical code path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, pipeline
from .disc.backend import DiscError
from .dol.reader import DolError
from .pipeline import BuildRequest, ModSpec, PipelineError
from .riivo import resolve as riivo_resolve
from .riivo.parser import RiivolutionXmlError
from .riivo.resolve import SelectionError


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="riivultimatum",
        description=(
            "Bake one or more Riivolution mods into a standalone Wii ISO that "
            "boots under USB Loader GX, including memory/code patches."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  riivultimatum --iso game.iso --xml mod/riivolution/mod.xml --list-options\n"
            "  riivultimatum --iso game.iso --xml mod/riivolution/mod.xml "
            '--choice "Section/Option=Choice" --out modded.iso\n'
            "  # stack two mods (later ones win on conflict):\n"
            "  riivultimatum --iso game.iso --xml a.xml --xml b.xml "
            "--all-defaults --out combined.iso\n"
            "  # launch the graphical interface:\n"
            "  riivultimatum --gui\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"riivultimatum {__version__}")
    p.add_argument("--gui", action="store_true", help="launch the graphical interface")
    p.add_argument("--iso", type=Path, help="source (unmodified) disc image")
    p.add_argument(
        "--xml",
        type=Path,
        action="append",
        default=[],
        metavar="XML",
        help="a mod's Riivolution XML; repeat to stack several mods in order",
    )
    p.add_argument(
        "--sd-root",
        type=Path,
        action="append",
        default=[],
        help=(
            "root of a mod's SD-card layout (the folder containing riivolution/). "
            "Repeatable, paired with --xml in order; defaults per-mod to the "
            "XML's parent's parent."
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
        help=(
            "select an option; repeatable. With multiple mods this applies to "
            "every mod that has a matching option. Use --list-options to see them."
        ),
    )
    p.add_argument(
        "--list-options",
        action="store_true",
        help="print the option tree of each --xml and exit",
    )
    p.add_argument(
        "--check-wit",
        action="store_true",
        help="report where wit was found and whether it runs, then exit",
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
            "do not auto-change the game ID. By default the game code is bumped "
            "so the mod gets its own save slot, since Riivolution's savegame "
            "redirection cannot be baked into an ISO."
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
        help="report what would be patched without building an image",
    )
    return p


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


def _mod_specs(args: argparse.Namespace, selections: dict[str, str]) -> list[ModSpec]:
    """Pair each --xml with its optional --sd-root, in order."""
    specs: list[ModSpec] = []
    for i, xml in enumerate(args.xml):
        sd_root = args.sd_root[i] if i < len(args.sd_root) else None
        specs.append(
            ModSpec(
                xml=xml,
                sd_root=sd_root,
                selections=dict(selections),
                all_defaults=args.all_defaults,
            )
        )
    return specs


def _check_wit(wit_path: str | None) -> int:
    """Report where wit was found and whether it actually runs."""
    from .disc.wit import WitBackend, find_wit

    found = wit_path or find_wit()
    if not found:
        print("wit: NOT FOUND", file=sys.stderr)
        print(
            "  Put a wit/ folder next to the program, add wit to PATH, or pass "
            "--wit-path.",
            file=sys.stderr,
        )
        return 1
    print(f"wit: {found}")
    try:
        print(WitBackend(found).version())
    except Exception as exc:  # noqa: BLE001 - diagnostic, surface anything
        print(f"  found, but it failed to run: {exc}", file=sys.stderr)
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    if args.check_wit:
        return _check_wit(args.wit_path)

    if not args.xml:
        print("error: at least one --xml is required", file=sys.stderr)
        return 2

    # --list-options is pure XML inspection; it needs no ISO and no wit.
    if args.list_options:
        for spec in _mod_specs(args, {}):
            disc = pipeline.load_disc(spec)
            print(f"\n=== {spec.name} ===")
            print(riivo_resolve.describe_options(disc))
        return 0

    if not args.iso:
        print("error: --iso is required", file=sys.stderr)
        return 2

    selections = _parse_choices(args.choice)
    request = BuildRequest(
        iso=args.iso,
        mods=_mod_specs(args, selections),
        out=args.out,
        wbfs=args.wbfs,
        game_id=args.game_id,
        keep_game_id=args.keep_game_id,
        title=args.title,
        wit_path=args.wit_path,
        work_dir=args.work_dir,
        dry_run=args.dry_run,
    )

    result = pipeline.build(request, progress=print)

    print()
    print(result.report.render())
    print()

    if not result.ok:
        print("error: some patches failed; no image was written.", file=sys.stderr)
        return 1
    if result.output and args.wbfs:
        print(
            "Eject the drive safely, then in USB Loader GX set this game's IOS "
            "to your cIOS slot (usually 249) and turn Ocarina/cheats OFF -- the "
            "mod's code is already baked in."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.gui:
        from . import gui

        return gui.main()

    try:
        return run(args)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (RiivolutionXmlError, SelectionError, DiscError, DolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
