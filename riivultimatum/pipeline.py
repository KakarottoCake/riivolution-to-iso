"""The build pipeline, shared by the CLI and the GUI.

`cli.py` used to hold this inline, interleaved with `print()`. Pulling it out
buys two things: the GUI drives the exact same code the CLI does, and a build
can stack *several* mods onto one disc, not just one.

Stacking works because the underlying steps already compose:

  * `FstPatcher._flush` re-reads each target file from disk, so running one
    patcher per mod in order layers a later mod's files onto an earlier mod's.
  * `Dol.add_section` coalesces overlapping sections, so two mods that both
    place a loader at 0x80001800 merge instead of colliding.

The one thing sequential application cannot tell you is when two mods
*disagree* -- both replace the same file, or write the same bytes. That is not
fatal (the later mod deterministically wins), but it is the whole risk surface
of merging, so `detect_conflicts` surfaces it before a byte is written.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .disc.backend import BootInfo, DiscError, read_boot_info, usb_loader_gx_path
from .disc.wit import WitBackend
from .dol.memory import MemoryLowerer
from .dol.reader import Dol, _mask
from .report import Outcome, Report
from .riivo import parser as riivo_parser
from .riivo import resolve as riivo_resolve
from .riivo.fst import FstPatcher
from .riivo.parser import Disc, Patch
from .riivo.resolve import ExternalResolver, SelectionError, disc_path_to_fst

#: A callback the caller passes to watch progress. The CLI prints it; the GUI
#: appends it to a log pane. Keep messages one line each.
Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    pass


class PipelineError(Exception):
    """A build could not proceed. The message is user-facing."""


@dataclass
class ModSpec:
    """One mod to apply: an XML plus how to resolve and configure it."""

    xml: Path
    sd_root: Path | None = None
    selections: dict[str, str] = field(default_factory=dict)
    #: Accept the XML's own defaults instead of requiring explicit selections.
    all_defaults: bool = False

    @property
    def name(self) -> str:
        return self.xml.stem


@dataclass
class BuildRequest:
    iso: Path
    mods: list[ModSpec]
    out: Path | None = None
    wbfs: bool = False
    game_id: str | None = None
    keep_game_id: bool = False
    title: str | None = None
    wit_path: str | None = None
    work_dir: Path | None = None
    dry_run: bool = False


@dataclass
class BuildResult:
    report: Report
    boot: BootInfo
    output: Path | None
    game_id: str | None

    @property
    def ok(self) -> bool:
        return not self.report.has_failures


@dataclass
class _ResolvedMod:
    spec: ModSpec
    disc: Disc
    patches: list[Patch]
    resolver: ExternalResolver


# --------------------------------------------------------------------------
# Front-end helpers (used by CLI and GUI before a full build)
# --------------------------------------------------------------------------


def default_sd_root(xml: Path) -> Path:
    """Guess the SD root from the XML's location.

    Mods ship as `<sd>/riivolution/mod.xml`, so the SD root is normally two
    levels up; otherwise fall back to the XML's own folder.
    """
    if xml.parent.name.lower() == "riivolution":
        return xml.parent.parent
    return xml.parent


def load_disc(spec: ModSpec) -> Disc:
    """Parse a mod's XML into a Disc, raising PipelineError on trouble."""
    if not spec.xml.is_file():
        raise PipelineError(f"no such XML: {spec.xml}")
    try:
        return riivo_parser.parse(spec.xml)
    except riivo_parser.RiivolutionXmlError as exc:
        raise PipelineError(f"{spec.xml.name}: {exc}") from exc


def read_boot(iso: Path) -> BootInfo:
    """Read the disc header. Needs no external tool -- the header is plaintext."""
    if not iso.is_file():
        raise PipelineError(f"no such ISO: {iso}")
    try:
        return read_boot_info(iso)
    except DiscError as exc:
        raise PipelineError(str(exc)) from exc


def bump_game_id(game_id: str) -> str:
    """Give a mod its own save slot by altering the *game code*.

    A game ID is `CCCRMM`: three game-code characters, one region character,
    two maker characters. Only the game code is touched -- changing the region
    character (position 4) would re-flag the disc's region and force 50Hz /
    region-check breakage.
    """
    replacement = "Z" if game_id[2] != "Z" else "Y"
    return game_id[:2] + replacement + game_id[3:6]


# --------------------------------------------------------------------------
# Resolution + conflict detection
# --------------------------------------------------------------------------


def _resolve_mod(spec: ModSpec, boot: BootInfo) -> _ResolvedMod:
    disc = load_disc(spec)

    if not disc.is_valid_for_game(boot.game_id, boot.disc_number, boot.revision):
        f = disc.game_filter
        raise PipelineError(
            f"{spec.xml.name} does not apply to {boot.game_id}. It targets "
            f"game={f.game!r} developer={f.developer!r} regions={f.regions or 'any'}."
        )

    try:
        riivo_resolve.apply_selections(disc, spec.selections)
    except SelectionError as exc:
        raise PipelineError(f"{spec.xml.name}: {exc}") from exc

    if not spec.selections and not spec.all_defaults and disc.sections:
        raise PipelineError(
            f"{spec.xml.name} has options but none were selected. Choose options, "
            "or accept the XML's defaults (usually 'disabled')."
        )

    try:
        patches = riivo_resolve.selected_patches(disc, boot.game_id)
    except SelectionError as exc:
        raise PipelineError(f"{spec.xml.name}: {exc}") from exc

    sd_root = spec.sd_root or default_sd_root(spec.xml)
    resolver = ExternalResolver(sd_root, disc.root)
    return _ResolvedMod(spec, disc, patches, resolver)


def _memory_ranges(mod: _ResolvedMod) -> list[tuple[int, int]]:
    """Address ranges a mod writes with *direct* memory patches.

    Search and ocarina patches resolve their target dynamically, so their
    address is unknown until the DOL is in hand; they are left out of static
    conflict detection rather than guessed at.
    """
    ranges: list[tuple[int, int]] = []
    for patch in mod.patches:
        for m in patch.memory_patches:
            if m.search or m.ocarina or not m.value:
                continue
            start = _mask(m.offset)
            ranges.append((start, start + len(m.value)))
    return ranges


def _disc_targets(mod: _ResolvedMod) -> set[str]:
    """FST paths a mod replaces via `<file>` patches (folders are too broad
    to compare cheaply and rarely collide meaningfully)."""
    targets: set[str] = set()
    for patch in mod.patches:
        for f in patch.file_patches:
            if not f.disc:
                continue
            try:
                targets.add(str(disc_path_to_fst(f.disc)))
            except Exception:
                targets.add(f.disc)
    return targets


def detect_conflicts(mods: list[_ResolvedMod], report: Report) -> None:
    """Warn when two stacked mods write the same file or overlapping memory.

    Only meaningful with two or more mods; the later mod in the list wins, and
    the warning names both so the user can reorder or drop one.
    """
    for i in range(len(mods)):
        for j in range(i + 1, len(mods)):
            a, b = mods[i], mods[j]

            shared_files = _disc_targets(a) & _disc_targets(b)
            for path in sorted(shared_files):
                report.warned(
                    "conflict",
                    path,
                    f"both {a.spec.name!r} and {b.spec.name!r} replace this file; "
                    f"{b.spec.name!r} wins (it is applied later)",
                )

            ranges_b = _memory_ranges(b)
            for start_a, end_a in _memory_ranges(a):
                for start_b, end_b in ranges_b:
                    if start_a < end_b and start_b < end_a:
                        lo, hi = max(start_a, start_b), min(end_a, end_b)
                        report.warned(
                            "conflict",
                            f"{lo:#010x}..{hi:#010x}",
                            f"{a.spec.name!r} and {b.spec.name!r} both write here; "
                            f"{b.spec.name!r} wins",
                        )
                        break


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------


def build(request: BuildRequest, progress: Progress = _noop) -> BuildResult:
    """Run a full build (or dry run) and return its report.

    Raises PipelineError for setup problems the user must fix (bad paths, an
    XML that does not match the disc, no mods). Per-patch problems are recorded
    in the report instead; a report with failures yields ``ok == False``.
    """
    if not request.mods:
        raise PipelineError("no mods to apply")
    if not request.dry_run and not request.out:
        raise PipelineError("an output path is required unless this is a dry run")

    boot = read_boot(request.iso)
    progress(f"Game:  {boot.game_id}  rev {boot.revision}  {boot.title}")

    mods = [_resolve_mod(spec, boot) for spec in request.mods]
    for mod in mods:
        progress(f"Mod:   {mod.spec.name}  ({len(mod.patches)} patch group(s))")
    total_patches = sum(len(m.patches) for m in mods)
    if total_patches == 0:
        raise PipelineError("the current selection activates no patches")

    report = Report()
    if len(mods) > 1:
        detect_conflicts(mods, report)
        conflicts = report.count(Outcome.WARNING)
        if conflicts:
            progress(f"Note:  {conflicts} cross-mod conflict(s); see the report.")

    backend = WitBackend(request.wit_path)

    temp_dir: tempfile.TemporaryDirectory | None = None
    if request.work_dir:
        work = request.work_dir
        work.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="riivultimatum-")
        work = Path(temp_dir.name)

    try:
        progress("Extracting image...")
        image_root = backend.extract(request.iso, work / "image")

        progress("Applying file/folder patches...")
        for mod in mods:
            FstPatcher(image_root, mod.resolver, report).apply(mod.patches)

        progress("Applying memory patches...")
        dol_path = image_root / "sys" / "main.dol"
        if not dol_path.is_file():
            report.failed("memory", "sys/main.dol", "executable missing from image")
        else:
            dol = Dol.parse(dol_path.read_bytes())
            for mod in mods:
                MemoryLowerer(dol, mod.resolver, report).apply(mod.patches)
            dol_path.write_bytes(dol.serialize())

        if report.has_failures:
            progress("Some patches failed; refusing to build a broken ISO.")
            return BuildResult(report, boot, None, None)

        if request.dry_run:
            progress("Dry run complete; no image written.")
            return BuildResult(report, boot, None, None)

        game_id = request.game_id
        if not game_id and not request.keep_game_id:
            game_id = bump_game_id(boot.game_id)
        if game_id and game_id != boot.game_id:
            progress(f"Output game ID: {game_id} (was {boot.game_id})")

        title = request.title or boot.title
        assert request.out is not None  # guarded at entry
        if request.wbfs:
            destination = usb_loader_gx_path(request.out, game_id or boot.game_id, title)
        else:
            destination = request.out

        progress(f"Building {destination}...")
        destination.parent.mkdir(parents=True, exist_ok=True)
        backend.compose(
            image_root, destination, game_id=game_id, title=request.title, wbfs=request.wbfs
        )
        progress(f"Done: {destination}")
        return BuildResult(report, boot, destination, game_id)

    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
        elif request.work_dir:
            progress(f"(staging kept at {request.work_dir})")
