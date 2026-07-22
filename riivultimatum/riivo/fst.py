"""Apply `<file>` and `<folder>` patches to an extracted disc image.

Mirrors Dolphin's `ApplyFilePatchToFST` / `ApplyFolderPatchToFST`.

A file patch does not necessarily replace a whole file: `offset`, `fileoffset`,
`length` and `resize` together describe a *region* splice. So each target file
accumulates an ordered list of layers, and layers are only flattened to bytes
once every patch has been seen. Flattening eagerly would be wrong whenever two
patches touch disjoint regions of the same file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..report import Report
from .parser import File, Folder, Patch
from .resolve import ExternalPathError, ExternalResolver, disc_path_to_fst


@dataclass
class _Layer:
    """One splice of external bytes into a disc file."""

    source: Path
    #: Where in the disc file the bytes land.
    offset: int
    #: Where in the external file the bytes come from.
    fileoffset: int
    #: 0 means "to the end of the external file".
    length: int
    resize: bool


@dataclass
class _Target:
    path: PurePosixPath
    layers: list[_Layer] = field(default_factory=list)
    created: bool = False


class FstPatcher:
    """Collects file/folder patches, then writes them into the staging tree."""

    def __init__(self, image_root: Path, resolver: ExternalResolver, report: Report):
        #: Root of the extracted image; contains `sys/` and `files/`.
        self.image_root = image_root
        self.resolver = resolver
        self.report = report
        self._targets: dict[PurePosixPath, _Target] = {}
        self._fst_index: dict[str, list[PurePosixPath]] | None = None

    # -- collection ------------------------------------------------------

    def apply(self, patches: list[Patch]) -> None:
        for patch in patches:
            for f in patch.file_patches:
                self._collect_file(patch, f)
            for f in patch.folder_patches:
                self._collect_folder(patch, f)
            for s in patch.savegame_patches:
                self.report.unsupported(
                    "savegame",
                    s.external,
                    "save redirection cannot be baked into an ISO. Change the "
                    "game ID (--game-id) so the mod writes to its own save slot "
                    "instead of overwriting the retail one.",
                )
        self._flush()

    def _collect_file(self, patch: Patch, f: File) -> None:
        label = f"{f.disc} <- {f.external}"
        try:
            external = self.resolver.resolve(f.external, patch.root)
            disc_rel = disc_path_to_fst(f.disc)
        except ExternalPathError as exc:
            self.report.failed("file", label, str(exc))
            return

        if not external.is_file():
            self.report.skipped("file", label, f"external file not found: {external}")
            return

        self._add_layer(disc_rel, external, f, label)

    def _collect_folder(self, patch: Patch, f: Folder) -> None:
        label = f"{f.disc or '<by filename>'} <- {f.external}"
        try:
            external = self.resolver.resolve(f.external, patch.root)
        except ExternalPathError as exc:
            self.report.failed("folder", label, str(exc))
            return

        if not external.is_dir():
            self.report.skipped("folder", label, f"external folder not found: {external}")
            return

        pattern = "**/*" if f.recursive else "*"
        matched = 0
        for source in sorted(external.glob(pattern)):
            if not source.is_file():
                continue
            relative = source.relative_to(external).as_posix()

            if f.disc:
                disc_rel = disc_path_to_fst(f"{f.disc.rstrip('/')}/{relative}")
            else:
                # No disc attribute: Riivolution searches the whole FST for a
                # file with this name, wherever it lives.
                found = self._find_by_name(source.name)
                if len(found) != 1:
                    reason = (
                        "no disc file with this name"
                        if not found
                        else f"{len(found)} disc files share this name: "
                        + ", ".join(str(p) for p in found[:4])
                    )
                    self.report.skipped("folder", f"{relative} <- {source}", reason)
                    continue
                disc_rel = found[0]

            synthetic = File(
                disc=str(disc_rel),
                external=str(source),
                resize=f.resize,
                create=f.create,
                length=f.length,
            )
            self._add_layer(disc_rel, source, synthetic, f"{relative} <- {source}")
            matched += 1

        if matched:
            self.report.applied("folder", label, f"{matched} file(s) queued")

    def _add_layer(self, disc_rel: PurePosixPath, external: Path, f: File, label: str) -> None:
        absolute = self.image_root / disc_rel
        exists = absolute.is_file()

        if not exists and not f.create:
            self.report.skipped(
                "file", label, "target does not exist on disc and create=false"
            )
            return

        target = self._targets.setdefault(disc_rel, _Target(disc_rel))
        if not exists:
            target.created = True

        target.layers.append(
            _Layer(
                source=external,
                offset=f.offset,
                fileoffset=f.fileoffset,
                length=f.length,
                resize=f.resize,
            )
        )

    # -- flattening ------------------------------------------------------

    def _flush(self) -> None:
        for disc_rel, target in sorted(self._targets.items()):
            absolute = self.image_root / disc_rel
            try:
                content = self._flatten(target, absolute)
            except OSError as exc:
                self.report.failed("file", str(disc_rel), str(exc))
                continue

            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(content)
            verb = "created" if target.created else "patched"
            self.report.applied(
                "file", str(disc_rel), f"{verb}, {len(content)} bytes"
            )

    @staticmethod
    def _flatten(target: _Target, absolute: Path) -> bytes:
        """Apply layers in order, matching Dolphin's `ApplyPatchToFile`.

        The size rule is the subtle part:

            target_filesize = resize ? patch_end : max(current_size, patch_end)

        so `resize=true` *truncates* the file to `offset + patch_size` rather
        than merely allowing it to grow. That is why a whole-file replacement
        (offset 0) yields exactly the external file, and why mods that splice a
        region must set `resize="false"` to keep the tail.
        """
        buffer = bytearray(absolute.read_bytes()) if absolute.is_file() else bytearray()

        for layer in target.layers:
            data = layer.source.read_bytes()
            start = layer.fileoffset
            patch_size = layer.length if layer.length else max(0, len(data) - start)
            chunk = data[start : start + patch_size]
            patch_size = len(chunk)

            current_size = len(buffer)
            if not layer.resize:
                if layer.offset >= current_size:
                    # Nothing of this write lands inside the existing file.
                    continue
                chunk = chunk[: current_size - layer.offset]
                patch_size = len(chunk)

            patch_end = layer.offset + patch_size
            target_size = patch_end if layer.resize else max(current_size, patch_end)

            if target_size > len(buffer):
                buffer.extend(b"\x00" * (target_size - len(buffer)))
            buffer[layer.offset : patch_end] = chunk
            del buffer[target_size:]

        return bytes(buffer)

    # -- FST index -------------------------------------------------------

    def _find_by_name(self, name: str) -> list[PurePosixPath]:
        if self._fst_index is None:
            index: dict[str, list[PurePosixPath]] = {}
            files_root = self.image_root / "files"
            if files_root.is_dir():
                for path in files_root.rglob("*"):
                    if path.is_file():
                        rel = PurePosixPath(path.relative_to(self.image_root).as_posix())
                        index.setdefault(path.name.lower(), []).append(rel)
            self._fst_index = index
        return self._fst_index.get(name.lower(), [])
