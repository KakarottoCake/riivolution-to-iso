# Riivultimatum

Bake a Riivolution mod into a standalone Wii ISO that boots under USB Loader GX
— **including the memory/code patches** that other converters give up on.

> ## ⚠️ Status: works in Dolphin, **NOT YET TESTED ON REAL HARDWARE**
>
> This is early software. Here is exactly what has and has not been verified.
>
> **Verified.** Built ISOs boot in Dolphin 5.0-18995 and reach in-game rendering.
> The memory patching is proven byte-for-byte equivalent to what Riivolution
> does at boot (see [Memory equivalence proof](#memory-equivalence-proof)), and
> the disc image passes `DolphinTool verify` with no hash-tree errors.
>
> **NOT verified.** Nobody has yet booted an output ISO on a real Wii under USB
> Loader GX. Dolphin emulates the apploader; a console is the final word. If you
> try it, please open an issue with the result either way — that is the single
> most useful contribution right now.
>
> **Also untested against real mods:** `ocarina` (Gecko) patches, `search`
> patches, `<file>` patches using `offset`/`resize`, and macro/`${param}`
> substitution. All are unit-tested, none has run against a mod in the wild.
>
> End-to-end testing so far covers a single large Super Mario Galaxy 2 mod
> using a Kamek/Syati code loader — several hundred memory patches and a dozen
> recursive folder patches, all applied with no failures. Broader coverage
> across more games and mods is the main thing this needs.
>
> Keep your original ISO. Nothing here modifies it, but do not rely on that.

## Why this exists

Riivolution applies mods on the fly at boot. USB Loader GX can't do that, so
Riivolution mods are unplayable from a USB drive unless someone bakes them into
a real ISO. Existing tools stop short:

- [RiivolutionIsoBuilder](https://github.com/Asu-chan/RiivolutionIsoBuilder)
  handles `<file>`/`<folder>` patches but warns that "mods that use code hacks
  are known to cause issues with USB Loaders."
- [SMG-Riiv-to-WBFS](https://github.com/viktormax3/SMG-Riiv-to-WBFS) does merge
  memory patches into `main.dol`, but is hardcoded to a handful of Super Mario
  Galaxy mods.

## How the code patches work

The [Riivolution patch format spec](https://riivolution.github.io/wiki/Patch_Format/)
says `<memory>` patches "execute immediately before game boot, after disc
mounting but before executable initialization." They run **once, before the DOL
entry point** — not continuously. Dolphin agrees: `ApplyGeneralMemoryPatches`
is the last thing `CBoot::BootUp` does.

That makes the Wii apploader's own DOL section loading a *semantically exact*
substitute for Riivolution's memory patcher. Every memory patch becomes a
static DOL edit:

| Riivolution patch | What we do |
|---|---|
| `<memory offset value>`, offset **inside** a DOL section | Convert VA to file offset, patch the bytes in place |
| `<memory offset value/valuefile>`, offset **outside** every section (e.g. `0x80001800`) | **Declare a new DOL section** at that address — the apploader loads it there before the entry point runs |
| `<memory search original value align>` | Resolve the scan statically over the DOL's section contents |
| `<memory ocarina offset value>` | Find the pattern, find the next `blr` (`0x4E800020`), overwrite it with a branch |
| `<file>` / `<folder>` | Bake into the rebuilt FST |

**No PPC stub, no assembler, no devkitPPC, no cache-coherency hazards.** The
same trick is what [`wstrt --add-section`](https://szs.wiimm.de/info/add-section.html)
and [GeckoLoader](https://github.com/JoshuaMKW/GeckoLoader) use to inject the
Gecko codehandler, so it is proven on real hardware.

Gecko codes still behave "on the fly": the ocarina path places the codehandler
at `0x80001800` and the code list at `0x800028B8` as new DOL sections and hooks
a `blr`, so the handler runs every frame exactly as it does under Riivolution.

## Kamek / Syati code-loader mods (SMG2, NSMBW)

The heavyweight mods — [Syati](https://github.com/SMGCommunity/Syati) for Super
Mario Galaxy 2, Kamek for NSMBW — don't just patch bytes, they inject a *code
loader* that pulls a relocatable binary in at runtime. That sounds like the
thing a static ISO can't do. It isn't:

- The Syati loader is placed at **`0x80001800`** by an ordinary memory patch, so
  it becomes a new DOL section like any other blob.
- At runtime the loader reads its CustomCode binary through the **DVD API**
  (`DVDConvertPathToEntrynum` → `DVDFastOpen` → `DVDReadPrio`) — that is, from
  the **disc FST**. Riivolution serves that path off SD on the fly; we bake the
  same file into the FST instead, and `DVDConvertPathToEntrynum` resolves it
  identically.
- The loader resizes SystemHeap itself based on the binary's Kamek header, so
  there is nothing for the patcher to arrange.

The risk with a big DOL like SMG2's is **slot exhaustion**: only 7 text and 11
data sections exist. When they're all taken, `Dol.compact()` merges two
address-contiguous sections — identical bytes to identical addresses, one slot
freed — before giving up. It will not bridge a gap by default, since that would
write zeroes to addresses the apploader had been leaving alone.

### Two traps worth knowing about

Real code-loader mods surface these; synthetic test data does not.

1. **A loaded section outranks the declared `.bss` range.** Shipping DOLs do
   overlap the two — a game can declare a large bss span and still place
   initialised data sections *inside* it. A naive "is this address in bss?"
   check therefore refuses patches to bytes that genuinely ship in the
   executable. Section containment has to win.
2. **The loader window must be classed as text even with no ocarina patch.**
   Kamek/Syati mods carry no `ocarina="true"` element, so the only signal that
   `0x80001800` holds code is the address itself. Getting this wrong puts the
   loader in a data section, which the apploader does not icache-invalidate.

## Download (Windows)

The [Releases](https://github.com/KakarottoCake/riivolution-to-iso/releases)
tab has a prebuilt Windows zip: unpack it, keep `RiivolutionUltimatum.exe` and
the `wit/` folder together, and double-click the exe. Nothing to install — wit
is bundled. `RiivolutionUltimatum.exe --check-wit` confirms wit is found.

To build the release yourself: `pwsh packaging/build_release.ps1` (needs
`pip install pyinstaller` and wit unpacked under `tools/`).

## Install (from source)

```
pip install -e .
```

Also install **[Wiimms ISO Tools](https://wit.wiimm.de/)** and put `wit` on your
PATH (or pass `--wit-path`). `wit` handles partition decryption, FST rebuild,
the H0–H3 hash tree, and trucha signing — the parts that are easy to get subtly
wrong.

## Use

### Graphical interface

```bash
riivultimatum --gui        # or: riivultimatum-gui
```

A single window: pick the source game, add one or more mods, tick the options
you want per mod, choose an ISO or a USB-drive WBFS output, and press **Build**.
The build runs on a background thread with a live log, so the window stays
responsive through the multi-minute extract/compose. **Dry run** applies every
patch and shows the report without writing an image — the fastest way to see
whether a mod converts cleanly.

The GUI is a thin shell over the same engine the CLI uses; anything it does,
the CLI can do too.

### Stacking multiple mods

Both the GUI (add several mods to the list) and the CLI (repeat `--xml`) apply
mods in order onto one disc. A later mod's files and memory writes layer on top
of an earlier mod's, so **order is meaningful — the last mod wins on overlap**.
Before building, the tool checks every pair of mods for the same file being
replaced twice or overlapping memory writes, and lists each collision in the
report as a `WARNING` naming both mods (it does not block the build; the
overwrite is deterministic, you just get told).

```bash
riivultimatum --iso game.iso --xml base.xml --xml addon.xml \
              --all-defaults --out combined.iso
```

This is unproven territory: no two real mods have been stacked and booted yet.
The mechanism is sound (see below) but treat multi-mod output as experimental.

### Command line

```bash
# See what the mod offers
riivultimatum --iso game.iso --xml sd/riivolution/mod.xml --list-options

# Check what would happen, without building
riivultimatum --iso game.iso --xml sd/riivolution/mod.xml \
              --choice "Section/Option=Choice" --dry-run

# Build an ISO
riivultimatum --iso game.iso --xml sd/riivolution/mod.xml \
              --choice "Section/Option=Choice" --out modded.iso
```

`--sd-root` defaults to the XML's grandparent (mods ship as
`<sd>/riivolution/mod.xml`), so you normally don't need it.

Every patch is reported as `APPLIED`, `SKIPPED`, `UNSUPPORTED`, or `FAILED`.
A `FAILED` entry aborts the build rather than shipping a broken ISO.

### Writing straight to a USB drive

`--wbfs` emits a split WBFS image in USB Loader GX's own layout, so `--out` can
point at the drive's `wbfs/` folder and skip the separate install step:

```bash
riivultimatum --iso game.iso --xml sd/riivolution/mod.xml \
              --choice "Section/Option=Choice" --wbfs --out E:/wbfs
```

That writes `E:/wbfs/<Disc Title> [<GAMEID>]/<GAMEID>.wbfs`, split at 4 GB for
FAT32. Characters FAT32 rejects are stripped from the title.

Then, in USB Loader GX's per-game settings: set **Game IOS** to your cIOS slot
(usually 249) and turn **Ocarina/cheats OFF** — the mod's code is already baked
in, and the loader's cheat engine would fight it.

### Game ID

By default the *game code* is altered (e.g. `SMNE01` → `SMZE01`) so the mod gets
its own save slot; Riivolution's `<savegame>` redirection cannot be baked into
an ISO, and without this the mod would overwrite the retail game's save. Use
`--keep-game-id` to opt out, or `--game-id` to choose your own.

The **region character (position 4) is deliberately never touched** — tools
derive the disc's region setting from it, so changing `...E..` to `...R..` would
re-flag an NTSC game as PAL and hand you 50 Hz and region-check problems.

## Known limits

These are stated plainly rather than papered over:

- **`<savegame>` redirect** — impossible offline. Mitigated by the game ID change.
- **Memory patches targeting `.bss`** — the game zeroes `.bss` during startup, so
  such a patch would be wiped. **Riivolution has the same limitation**, so this is
  parity, not a regression. The report names each one.
- **Memory patches targeting REL-loaded code** — same story; patch the REL file
  itself with a `<file>` patch instead.
- **DOL section slots** — a DOL has 7 text and 11 data slots. New sections are
  coalesced by adjacency to conserve them, but a mod needing more than are free
  will fail with an explicit error rather than silently dropping patches.
- **Gecko payload size** — codes past `0x80003000` are flagged; the game's OS
  init may clobber them.
- Output ISOs are trucha-signed and need a cIOS with hash/signature checks
  patched (d2x). That is the normal USB Loader GX setup.

## Layout

```
riivultimatum/
  riivo/parser.py    Riivolution XML -> data model (mirrors Dolphin's)
  riivo/resolve.py   choices, ${param} substitution, path resolution
  riivo/fst.py       <file>/<folder> patches against the extracted image
  dol/reader.py      DOL parse/serialise, VA lookup, section allocation
  dol/memory.py      <memory> patch -> DOL edit lowering  (the core)
  dol/gecko.py       ocarina hook finding and branch encoding
  disc/wit.py        extract/compose via Wiimms ISO Tools
  pipeline.py        one build: resolve, stack mods, conflict-check, compose
  cli.py             command-line front-end over pipeline
  gui.py             Tkinter front-end over pipeline
  report.py
```

Both front-ends call `pipeline.build`; there is no second implementation of the
patching logic behind the GUI.

## Testing

```
py -3.13 -m pytest tests -q
```

The unit suite covers DOL round-tripping, address lookup, section allocation and
coalescing, the ocarina branch encoding, search alignment, XML/option parsing,
and the file-patch size rules.

<a id="memory-equivalence-proof"></a>
### Memory equivalence proof

The central claim — that lowering `<memory>` patches into DOL sections yields
the same MEM1 image Riivolution would have produced by writing RAM — is
directly checkable offline:

```
python scripts/verify_memory_equivalence.py \
    --original-dol original/sys/main.dol \
    --patched-dol  staging/image/sys/main.dol \
    --xml "mod/riivolution/mod.xml" --sd-root mod \
    --game-id RMGE01 --choice "Section/Option=Choice"
```

It lays the original DOL's sections into a flat 24 MiB image, applies every
memory patch as a plain RAM write using a deliberately naive applier that
shares no code with `dol/memory.py`, then lays the patched DOL out the same way
and diffs both the bytes and the coverage map.

Run it whenever you build something new. On the mods tested so far it reports
zero value differences and zero coverage differences — run it on yours before
trusting the output.

### Independent cross-checks with DolphinTool

`DolphinTool` ships with Dolphin and reads disc images with an entirely
different codebase from `wit`, which makes it a useful second opinion on the
image `wit` composed:

```
DolphinTool header -i out.iso                     # game ID, region, country
DolphinTool verify -i out.iso                     # hash tree + signatures
DolphinTool extract -i out.iso -g -s "path/in/fst" -o out
```

`verify` on a good build reports exactly two **Low** findings — "the update
partition is missing" (dropped deliberately by `--psel data`) and "the DATA
partition is not correctly signed" (trucha signing, which is what a cIOS is
for). Anything about the hash tree means the rebuild went wrong.

`header` is worth a glance every time: it prints the region, which is the
quickest way to catch a game ID change that accidentally re-flagged the disc.

Extracting a file back out and hashing it against the mod folder confirms the
FST rebuild and re-encryption round-tripped cleanly.

### Boot test

```
Dolphin.exe -b -e out.iso
```

Builds have been confirmed to boot in Dolphin 5.0-18995 and reach in-game
rendering, with the window title showing the patched game ID.

That is worth more than "it didn't crash". With a Kamek/Syati mod, `main.dol`
carries in-place patches that branch to `0x80001800`, and the loader there runs
early during heap creation. Had the new text section failed to load, the game
would have died on that branch long before drawing anything — so reaching a
rendered frame exercises the section-at-`0x80001800` mechanism end to end.

### Still outstanding

**Hardware.** Install to USB and boot under USB Loader GX. Dolphin emulates the
apploader; a real console is the final word, and is the gate before trusting
this for anything that matters. Results either way are very welcome in the
issue tracker.

## License

GPL-3.0-or-later. Derives from Dolphin (GPL-2.0-or-later) for the Riivolution
parser/patcher semantics and GeckoLoader (GPL-3.0) for the DOL section approach.
