"""Frozen-app entry point.

Bare launch (double-click) opens the GUI; any arguments route to the CLI, so
`RiivolutionUltimatum.exe --iso ... --xml ... --out ...` still works for
scripting. The console is suppressed in the packaged build, so CLI output is
only visible when run from a terminal.
"""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from riivultimatum.cli import main as cli_main

        return cli_main()
    from riivultimatum.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
