"""Interface for reading and rebuilding Wii disc images."""

from __future__ import annotations

import abc
import struct
from dataclasses import dataclass
from pathlib import Path


class DiscError(Exception):
    """Extraction or composition failed."""


@dataclass
class BootInfo:
    """The plaintext disc header fields we need. All live at offset 0."""

    game_id: str  # 6 chars
    disc_number: int
    revision: int
    title: str

    @classmethod
    def from_header(cls, header: bytes) -> "BootInfo":
        if len(header) < 0x60:
            raise DiscError("disc header too short")
        game_id = header[0:6].decode("ascii", errors="replace")
        disc_number = header[6]
        revision = header[7]
        title = header[0x20:0x60].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        return cls(game_id, disc_number, revision, title.strip())


def read_boot_info(iso: Path) -> BootInfo:
    """Read the disc header straight from an ISO.

    The first 0x60 bytes of a Wii image are unencrypted, so this needs no
    crypto and no external tool. WBFS images carry a 0x200-byte wrapper header
    whose magic is 'WBFS'; skip to the embedded disc header in that case.
    """
    with iso.open("rb") as fh:
        head = fh.read(0x800)
    if len(head) < 0x60:
        raise DiscError(f"{iso} is too small to be a disc image")

    if head[0:4] == b"WBFS":
        return BootInfo.from_header(head[0x200 : 0x200 + 0x60])

    info = BootInfo.from_header(head)
    # A Wii image carries the magic 0x5D1C9EA3 at 0x18; GameCube uses
    # 0xC2339F3D at 0x1C. Neither is fatal here, but a file with neither is
    # almost certainly not a disc image and the user should hear about it.
    (wii_magic,) = struct.unpack_from(">I", head, 0x18)
    (gc_magic,) = struct.unpack_from(">I", head, 0x1C)
    if wii_magic != 0x5D1C9EA3 and gc_magic != 0xC2339F3D:
        raise DiscError(
            f"{iso} has neither the Wii nor GameCube disc magic; "
            "is it really an unscrubbed disc image?"
        )
    return info


class DiscBackend(abc.ABC):
    """Extract an image to a staging tree, and compose one back."""

    @abc.abstractmethod
    def extract(self, iso: Path, dest: Path) -> Path:
        """Extract `iso` into `dest`; returns the directory holding sys/ and files/."""

    @abc.abstractmethod
    def compose(
        self,
        image_root: Path,
        output: Path,
        *,
        game_id: str | None = None,
        title: str | None = None,
        wbfs: bool = False,
    ) -> None:
        """Rebuild a disc image from a staging tree, re-signing as needed.

        With `wbfs=True` the output is a split WBFS image instead of an ISO.
        """


#: Characters FAT32 refuses in a filename. USB drives are usually FAT32, and a
#: disc title like "SUPER MARIO GALAXY 2: SPECIAL" would otherwise fail to
#: create with a confusing error from wit rather than from us.
_ILLEGAL_FAT32 = '\\/:*?"<>|'


def usb_loader_gx_path(wbfs_dir: Path, game_id: str, title: str) -> Path:
    """Build the path USB Loader GX expects on a FAT32 drive.

    The layout is `wbfs/<Title> [<GAMEID>]/<GAMEID>.wbfs`, with splits landing
    alongside as .wbf1, .wbf2 and so on.
    """
    # Substitute rather than delete: dropping the separator in "GAME|EVER"
    # would silently weld two words together.
    clean = "".join(" " if c in _ILLEGAL_FAT32 else c for c in title)
    clean = " ".join(clean.split())  # collapse the runs that just created
    if not clean:
        clean = game_id
    # FAT32 caps a name at 255 chars; leave room for " [GAMEID]".
    clean = clean[:200]
    return wbfs_dir / f"{clean} [{game_id}]" / f"{game_id}.wbfs"
