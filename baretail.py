"""BareTail -- a real-time log file viewer.

    python baretail.py [options] {file(s)}

Run with --help for the full option list.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from baretail.cli import apply_geometry, parse_args
    from baretail.config import Config
    from baretail.ui.mainwindow import MainWindow

    options = parse_args(argv)
    window = MainWindow(Config.load())

    for path in options.files:
        window.open_file(path, activate=path == options.files[0])

    # After the files, so an explicit -wp is not overridden by anything the
    # views do while opening.
    apply_geometry(window, options)

    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
