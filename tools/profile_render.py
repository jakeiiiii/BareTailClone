"""Break a repaint down into its parts, to find where the time actually goes.

    python tools/profile_render.py path/to/big.log
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baretail.core.highlight import DEFAULT_RULES, RuleSet          # noqa: E402
from baretail.core.linefile import LineFile                         # noqa: E402
from baretail.ui.logview import VirtualTextView                     # noqa: E402


def timeit(label, fn, n=200):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    print(f"  {label:<34} median {statistics.median(samples):7.3f} ms   "
          f"max {max(samples):7.3f} ms")
    return statistics.median(samples)


def main(path):
    size = os.path.getsize(path)
    root = tk.Tk()
    root.geometry("1200x800")
    lf = LineFile(path)
    view = VirtualTextView(root, lf, RuleSet(list(DEFAULT_RULES)))
    view.pack(fill="both", expand=True)
    root.update()

    rng = random.Random(7)
    visible = view.visible_lines
    print(f"visible lines: {visible}\n")

    print("core file operations:")
    timeit("os.path.getsize (one stat)", lambda: os.path.getsize(path), 2000)
    timeit("size property", lambda: lf.size, 2000)
    timeit("line_start_at_or_before(random)",
           lambda: lf.line_start_at_or_before(rng.randint(0, size)))
    timeit("read_lines_at(random, visible)",
           lambda: lf.read_lines_at(rng.randint(0, size), visible))
    timeit("offset_of_last_page(visible)", lambda: lf.offset_of_last_page(visible))
    timeit("step_lines(random, 3)",
           lambda: lf.step_lines(rng.randint(0, size), 3))
    timeit("step_lines(random, -3)",
           lambda: lf.step_lines(rng.randint(0, size), -3))

    print("\nrender path:")
    offsets = [rng.randint(0, size) for _ in range(200)]
    it = iter(offsets * 10)

    def set_top_only():
        view.set_top(next(it))

    timeit("view.set_top (render, no flush)", set_top_only)

    def set_top_and_flush():
        view.set_top(next(it))
        root.update_idletasks()

    timeit("view.set_top + update_idletasks", set_top_and_flush)

    print("\ntk widget cost alone:")
    lines = lf.read_lines_at(size // 2, visible)
    body = "\n".join(l.text for l in lines)

    def refill():
        view.text.configure(state="normal")
        view.text.delete("1.0", "end")
        view.text.insert("1.0", body)
        view.text.configure(state="disabled")

    timeit("delete + insert 52 lines", refill)

    def refill_and_flush():
        refill()
        root.update_idletasks()

    timeit("delete + insert + update_idletasks", refill_and_flush)

    def tag_pass():
        rules = view._rules
        for line in lines:
            rules.match_index(line.text)

    timeit("highlight match 52 lines", tag_pass, 1000)

    root.destroy()


if __name__ == "__main__":
    main(sys.argv[1])
