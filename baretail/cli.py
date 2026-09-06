"""Command line handling.

Mirrors BareTail's documented usage::

    baretail [options] {file(s)}

      -wp, --window-position left top width height
      -ws, --window-state 0|1|2            0 normal, 1 minimised, 2 maximised
      -tc, --tile-window-count count
      -ti, --tile-window-index index

Tiling exists so a batch file can launch several instances that lay themselves
out without overlapping -- one per log, arranged by the caller rather than
dragged into place by hand.
"""

from __future__ import annotations

import argparse
import os
import sys

__all__ = ["parse_args", "apply_geometry", "Options"]

#: Window states, as documented for ``-ws``.
STATE_NORMAL = 0
STATE_MINIMISED = 1
STATE_MAXIMISED = 2


class Options(argparse.Namespace):
    files: list[str]
    window_position: list[int] | None
    window_state: int
    tile_window_count: int | None
    tile_window_index: int | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baretail",
        description="Real-time log file viewer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  baretail engine.log\n"
               "  baretail engine.log stats.log\n"
               "  baretail --tile-window-count 3 --tile-window-index 0 engine.log\n")
    parser.add_argument("files", nargs="*", help="log files to open, one tab each")
    parser.add_argument("-wp", "--window-position", nargs=4, type=int,
                        metavar=("LEFT", "TOP", "WIDTH", "HEIGHT"),
                        help="place the window explicitly")
    parser.add_argument("-ws", "--window-state", type=int, choices=(0, 1, 2), default=0,
                        help="0 normal, 1 minimised, 2 maximised")
    parser.add_argument("-tc", "--tile-window-count", type=int, metavar="COUNT",
                        help="number of windows being tiled")
    parser.add_argument("-ti", "--tile-window-index", type=int, metavar="INDEX",
                        help="which of them this one is, counting from 0")
    return parser


def parse_args(argv: list[str] | None = None) -> Options:
    parser = build_parser()
    options = parser.parse_args(argv, namespace=Options())

    # Tiling needs both halves to mean anything, and an index outside the
    # count would place the window off-screen.
    count, index = options.tile_window_count, options.tile_window_index
    if (count is None) != (index is None):
        parser.error("--tile-window-count and --tile-window-index must be used together")
    if count is not None:
        if count < 1:
            parser.error("--tile-window-count must be at least 1")
        if not 0 <= index < count:
            parser.error(f"--tile-window-index must be between 0 and {count - 1}")

    missing = [f for f in options.files if not os.path.exists(f)]
    if missing:
        # A warning rather than an error: the remaining files still open, and
        # a log that does not exist yet may appear moments later.
        for name in missing:
            print(f"baretail: {name}: no such file", file=sys.stderr)

    return options


def apply_geometry(window, options: Options) -> None:
    """Position and size the window according to the options.

    Explicit placement wins over tiling; asking for both is contradictory, and
    the specific request is the one the caller meant.
    """
    if options.window_position:
        left, top, width, height = options.window_position
        window.geometry(f"{max(100, width)}x{max(100, height)}+{left}+{top}")
    elif options.tile_window_count:
        left, top, width, height = _tile_rect(
            window, options.tile_window_count, options.tile_window_index)
        window.geometry(f"{width}x{height}+{left}+{top}")

    if options.window_state == STATE_MAXIMISED:
        window.state("zoomed")
    elif options.window_state == STATE_MINIMISED:
        window.iconify()


def _tile_rect(window, count: int, index: int) -> tuple[int, int, int, int]:
    """The rectangle for one window of a horizontal tiling.

    Tiled as full-width bands stacked down the screen rather than as columns:
    log lines are long, so height is the dimension that can be given up
    without making the content unreadable.
    """
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    # Leave room for the taskbar rather than running underneath it.
    usable_height = int(screen_height * 0.95)

    band = usable_height // count
    return 0, index * band, screen_width, band
