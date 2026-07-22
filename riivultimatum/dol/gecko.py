"""Ocarina (Gecko code) support.

Riivolution's ocarina support is not special machinery -- it is three ordinary
memory patches:

    <memory offset="0x80001800" valuefile="/codes/codehandler.bin" />
    <memory offset="0x800028B8" valuefile="/codes/RMGE01.gct" />
    <memory offset="0x80001800" value="<hook pattern>" ocarina="true" />

The first two are handled by the generic memory lowering (they land outside any
DOL section, so they become new DOL sections). Only the third needs bespoke
logic, which is what lives here.

Reference: https://riivolution.github.io/wiki/Ocarina_Codes/
"""

from __future__ import annotations

from .reader import Dol, DolError, _mask

#: `blr` -- the instruction an ocarina hook replaces.
BLR = 0x4E800020

#: Conventional Gecko layout. The codehandler is loaded at CODEHANDLER_ADDRESS
#: and the code list immediately follows in the region below the game's own
#: sections, which the OS leaves alone.
CODEHANDLER_ADDRESS = 0x80001800
CODELIST_ADDRESS = 0x800028B8
#: The classic handler window ends here; beyond it the OS starts using memory.
GECKO_WINDOW_END = 0x80003000


class OcarinaError(Exception):
    """The hook pattern could not be located, or the branch is unencodable."""


def encode_branch(source: int, target: int, link: bool = False) -> int:
    """Encode an unconditional PowerPC `b`/`bl` from `source` to `target`.

    Matches Dolphin's `ApplyOcarinaMemoryPatch`:
    `((target - source) & 0x03FFFFFC) | 0x48000000`.
    """
    delta = target - source
    # A PPC I-form branch carries a signed 26-bit displacement (24 stored bits
    # plus an implicit two zero low bits), so +/-32MiB.
    if delta < -0x2000000 or delta >= 0x2000000:
        raise OcarinaError(
            f"branch from {source:#010x} to {target:#010x} is {delta:#x} bytes, "
            "out of range for a PPC branch (+/-32MiB)"
        )
    if delta % 4 != 0:
        raise OcarinaError(f"branch target {target:#010x} is not 4-byte aligned")
    return ((delta & 0x03FFFFFC) | 0x48000000) | (1 if link else 0)


def find_hook(dol: Dol, pattern: bytes) -> int:
    """Locate the `blr` an ocarina patch should overwrite.

    Riivolution searches the executable for `pattern`, then takes the *next*
    `blr` at or after that point. The search is bounded to the section the
    pattern was found in; a `blr` in a different section is not the end of the
    same function.
    """
    if not pattern:
        raise OcarinaError("ocarina patch has no hook pattern (value attribute)")

    match = dol.find(pattern, align=4)
    if match is None:
        raise OcarinaError(f"hook pattern {pattern.hex()} not found in the executable")

    section = dol.section_containing(match, len(pattern))
    assert section is not None  # find() only returns in-section addresses

    address = match
    while address + 4 <= section.end:
        offset = address - section.address
        if int.from_bytes(section.data[offset : offset + 4], "big") == BLR:
            return address
        address += 4

    raise OcarinaError(
        f"found hook pattern at {match:#010x} but no blr follows it before the "
        f"end of its section ({section.end:#010x})"
    )


def apply_ocarina(dol: Dol, pattern: bytes, target: int) -> int:
    """Patch the hook `blr` into a branch to `target`. Returns the hook address."""
    target = _mask(target)
    hook = find_hook(dol, pattern)
    instruction = encode_branch(hook, target)
    dol.write(hook, instruction.to_bytes(4, "big"))
    return hook


def check_gecko_window(codehandler_end: int, codelist_end: int) -> str | None:
    """Warn when the handler + codes overflow the classic 0x80001800 window.

    Returns a human-readable warning, or None if everything fits.
    """
    highest = max(codehandler_end, codelist_end)
    if highest > GECKO_WINDOW_END:
        overflow = highest - GECKO_WINDOW_END
        return (
            f"Gecko codes extend to {highest:#010x}, {overflow} bytes past the "
            f"conventional window end {GECKO_WINDOW_END:#010x}. The game's OS "
            f"init may clobber them. Trim the code list, or relocate the list "
            f"with a codehandler that supports it (see GeckoLoader --gct-move)."
        )
    return None


def looks_like_codehandler(data: bytes) -> bool:
    """Heuristic: does this blob look like codehandler.bin rather than a GCT?

    A GCT starts with the magic 00D0C0DE 00D0C0DE; anything else at the
    codehandler address is assumed to be handler code.
    """
    return not data.startswith(b"\x00\xd0\xc0\xde\x00\xd0\xc0\xde")
