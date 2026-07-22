"""Wiimms ISO Tools (`wit`) backend.

`wit` already implements the parts of Wii disc handling that are easy to get
subtly wrong: partition decryption, FST rebuild, the H0-H3 hash tree, H4, and
trucha (fake) signing of the ticket and TMD. Reimplementing that would be the
buggiest part of this project for no user-visible gain, so we shell out.

Install: https://wit.wiimm.de/
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


from .backend import BootInfo, DiscBackend, DiscError, read_boot_info

_INSTALL_HINT = (
    "wit (Wiimms ISO Tools) was not found.\n"
    "  Download it from https://wit.wiimm.de/ and either add it to PATH,\n"
    "  unpack it into a wit/ or tools/ folder next to the program, or pass --wit-path."
)


def _search_roots() -> list[Path]:
    """Directories to look under for a bundled wit, most specific first.

    In a PyInstaller build the code runs from a temp extraction dir, so the
    repo layout is gone; wit ships next to the .exe instead. Cover both: the
    frozen exe's folder, and the source checkout's root.
    """
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        # The real .exe the user launched (onefile extracts code elsewhere).
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parents[2])
    return roots


def find_wit() -> str | None:
    """Locate a wit executable: explicit PATH first, then a bundled copy.

    A bundled copy is any `wit/.../wit.exe` or `tools/wit-*/bin/wit.exe` under
    the program's folder -- no PATH edit and no running wit's installer.
    """
    on_path = shutil.which("wit")
    if on_path:
        return on_path

    patterns = ("wit/**/wit.exe", "tools/wit-*/bin/wit.exe", "wit/**/wit", "tools/**/wit")
    for root in _search_roots():
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate)
    return None


class WitBackend(DiscBackend):
    def __init__(self, wit_path: str | None = None):
        resolved = wit_path or find_wit()
        if not resolved:
            raise DiscError(_INSTALL_HINT)
        self.wit = str(resolved)

    # -- process plumbing ------------------------------------------------

    def _run(self, args: list[str]) -> str:
        command = [self.wit, *args]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DiscError(_INSTALL_HINT) from exc

        if proc.returncode != 0:
            raise DiscError(
                "wit failed (exit {}):\n  $ {}\n{}".format(
                    proc.returncode,
                    " ".join(command),
                    (proc.stderr or proc.stdout).strip(),
                )
            )
        return proc.stdout

    def version(self) -> str:
        lines = self._run(["version"]).splitlines()
        return lines[0].strip() if lines else "unknown"

    # -- DiscBackend -----------------------------------------------------

    def extract(self, iso: Path, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        # --psel data keeps only the game partition; update/channel partitions
        # are dead weight in a modded ISO and USB Loader GX ignores them.
        self._run(
            [
                "extract",
                str(iso),
                "--dest",
                str(dest),
                "--psel",
                "data",
                "--overwrite",
                "--quiet",
            ]
        )
        return _locate_image_root(dest)

    def compose(
        self,
        image_root: Path,
        output: Path,
        *,
        game_id: str | None = None,
        title: str | None = None,
        wbfs: bool = False,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "copy",
            str(image_root),
            "--dest",
            str(output),
            "--overwrite",
            "--quiet",
        ]
        if wbfs:
            # --split defaults to 4 GB, which is what FAT32 requires.
            args += ["--wbfs", "--split"]
        else:
            args += ["--iso"]
        if game_id:
            args += ["--id", game_id]
        if title:
            args += ["--name", title]
        self._run(args)

        if output.is_file():
            return
        # A split WBFS writes <name>.wbfs plus .wbf1/.wbf2/...; if the first
        # part is missing entirely, nothing was produced.
        if wbfs and any(output.parent.glob(f"{output.stem}.wbf*")):
            return
        raise DiscError(f"wit reported success but {output} was not created")


def _locate_image_root(dest: Path) -> Path:
    """Find the directory that actually holds `sys/` and `files/`.

    Depending on the source image and options, `wit extract` writes either
    `<dest>/sys` directly or nests it under a partition directory such as
    `<dest>/DATA/sys`.
    """
    if (dest / "sys").is_dir():
        return dest
    for child in sorted(dest.iterdir()):
        if child.is_dir() and (child / "sys").is_dir():
            return child
    raise DiscError(
        f"could not find sys/ in the extracted image at {dest}; "
        "wit extract may have produced an unexpected layout"
    )


__all__ = ["WitBackend", "BootInfo", "read_boot_info", "DiscError"]
