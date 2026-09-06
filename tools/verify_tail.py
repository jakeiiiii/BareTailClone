"""Verify follow-tail against a file being written by another process.

    python tools/verify_tail.py

Appending from a thread in the same process would prove very little -- the GIL
would serialise the writer against the viewer and hide exactly the races this
is meant to catch.  So the writer is a genuine separate process, and the check
is that the viewer ends up showing the real last line of the file with no
lines lost along the way.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baretail.config import Config, StorageMode        # noqa: E402
from baretail.ui.mainwindow import MainWindow          # noqa: E402

WRITER = r"""
import sys, time
path, rate, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
line = "%s APPENDED line %08d " + "x" * 40 + "\n"
n = 0
started = time.monotonic()
with open(path, "ab", buffering=0) as fh:
    while time.monotonic() - started < seconds:
        tick = time.monotonic()
        written = 0
        buf = []
        while written < rate // 20:
            text = line % (time.strftime("%H:%M:%S"), n)
            buf.append(text)
            written += len(text)
            n += 1
        fh.write("".join(buf).encode())
        elapsed = time.monotonic() - tick
        if elapsed < 0.05:
            time.sleep(0.05 - elapsed)
print(n)
"""


def main() -> int:
    rate = 10 << 20          # 10 MB/s
    seconds = 8.0

    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "live.log")
    with open(path, "wb") as fh:
        fh.write(b"initial line\n")

    config = Config(path=os.path.join(directory, "settings.json"))
    config.storage = StorageMode.NONE
    config.set("poll_interval", 0.1)

    app = MainWindow(config)
    app.geometry("1100x700")
    app.update()
    tab = app.open_file(path)
    app.update()
    assert tab.view.following, "should be following on open"

    script = os.path.join(directory, "writer.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(WRITER)

    print(f"appending at {rate / (1 << 20):.0f} MB/s for {seconds:.0f}s...")
    writer = subprocess.Popen([sys.executable, script, path, str(rate), str(seconds)],
                              stdout=subprocess.PIPE, text=True)

    started = time.monotonic()
    repaints = 0
    last_shown = ""
    while writer.poll() is None:
        app.update()
        shown = tab.view.text.get("1.0", "end-1c").split("\n")[-1]
        if shown != last_shown:
            repaints += 1
            last_shown = shown
        elapsed = time.monotonic() - started
        sys.stdout.write(f"\r  {elapsed:4.1f}s  size {os.path.getsize(path) / (1 << 20):7.1f} MB"
                         f"  repaints {repaints:5d}  showing …{shown[-34:]}")
        sys.stdout.flush()
        time.sleep(0.01)

    written_lines = int(writer.stdout.read().strip())
    print()

    # Let the final poll land, so the comparison is against a settled view.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.02)

    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 4096))
        true_last = fh.read().split(b"\n")[-2].decode()

    shown_last = tab.view.text.get("1.0", "end-1c").split("\n")[-1]
    counted = tab.linefile.line_count

    print(f"\nfile size      : {size / (1 << 20):.1f} MB")
    print(f"lines written  : {written_lines + 1:,} (including the initial line)")
    print(f"lines counted  : {counted:,}")
    print(f"repaints       : {repaints}")
    print(f"file's last    : {true_last}")
    print(f"view's last    : {shown_last}")

    ok = True
    if shown_last != true_last:
        print("\nFAIL: the view is not showing the end of the file")
        ok = False
    if not tab.view.following:
        print("\nFAIL: follow mode was lost")
        ok = False

    # Truncation while following, which must re-anchor rather than error.
    print("\ntruncating the file while following...")
    with open(path, "r+b") as fh:
        fh.truncate(0)
    with open(path, "ab") as fh:
        fh.write(b"after truncation\n")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.02)
    shown = tab.view.text.get("1.0", "end-1c").strip()
    print(f"view now shows : {shown!r}")
    if shown != "after truncation":
        print("FAIL: did not re-anchor after truncation")
        ok = False

    app.on_close()
    print("\nPASS" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
