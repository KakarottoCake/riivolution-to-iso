Riivolution Ultimatum
=====================

Bake a Riivolution mod into a standalone Wii ISO (or WBFS) that boots under
USB Loader GX -- including the memory/code patches other converters skip.

WHAT'S IN THIS FOLDER
  RiivolutionUltimatum.exe   the program (double-click to open the GUI)
  wit\                       Wiimms ISO Tools, used to read/rebuild the disc
  LICENSE                    GNU GPL v3 (this program)
  NOTICE                     attribution for wit, Dolphin, GeckoLoader
  README.txt                 this file

Keep RiivolutionUltimatum.exe and the wit\ folder together -- the program
looks for wit right next to itself. If you move the exe, move wit\ with it.

HOW TO USE
  1. Double-click RiivolutionUltimatum.exe.
  2. Pick your original game image (.iso or .wbfs).
  3. Add one or more Riivolution mods (the mod's .xml file), and tick the
     options you want.
  4. Choose the output: an .iso file, or a USB drive's wbfs\ folder.
  5. Press "Dry run" to preview the patch report, or "Build" to make it.

For USB Loader GX: after building, set the game's IOS to your cIOS slot
(usually 249) and turn Ocarina/cheats OFF -- the mod's code is already baked in.

COMMAND LINE
  The same exe works from a terminal when given arguments, e.g.
    RiivolutionUltimatum.exe --iso game.iso --xml mod.xml --all-defaults --out out.iso
  (Console output only appears when run from a terminal.)

STATUS
  Confirmed working in the Dolphin emulator. NOT yet confirmed on a real Wii
  console. Keep your original game image. Report results either way at:
  https://github.com/KakarottoCake/riivolution-to-iso

wit (Wiimms ISO Tools) is (C) Dirk Clemens, GPL v2, from https://wit.wiimm.de/
and is included unmodified; its license and source pointer are in the wit\
folder. This program does not modify wit and merely runs it as a separate tool.
