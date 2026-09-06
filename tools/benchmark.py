"""Measure the viewport against a large file.

    python tools/benchmark.py path/to/big.log

The claim the whole design rests on is that repaint cost and memory use are
independent of file size.  This exercises a real Tk window -- not a mock -- and
reports the numbers, so the claim is checked rather than asserted.
"""

from __future__ import annotations

import ctypes
import os
import random
import statistics
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baretail.core.highlight import DEFAULT_RULES, RuleSet          # noqa: E402
from baretail.core.indexer import Indexer                           # noqa: E402
from baretail.core.linefile import LineFile                         # noqa: E402
from baretail.ui.logview import VirtualTextView                     # noqa: E402


def rss_mb() -> float:
    """Resident set size of this process, in MB."""
    if os.name == "nt":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32
        # The pseudo-handle is (HANDLE)-1; without an explicit restype ctypes
        # hands back a 32-bit int and the call fails on 64-bit Windows.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel32.GetCurrentProcess()
        # Modern Windows exports this from kernel32 as K32GetProcessMemoryInfo;
        # the psapi.dll name is a stub that is not always resolvable.
        for dll, name in ((kernel32, "K32GetProcessMemoryInfo"),
                          (ctypes.windll.psapi, "GetProcessMemoryInfo")):
            try:
                func = getattr(dll, name)
            except AttributeError:
                continue
            func.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
            if func(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / (1 << 20)
        return float("nan")
    with open(f"/proc/{os.getpid()}/statm") as fh:
        return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / (1 << 20)


def report(label: str, samples: list[float]) -> None:
    ms = [s * 1000 for s in samples]
    print(f"  {label:<28} median {statistics.median(ms):7.2f} ms   "
          f"p95 {sorted(ms)[int(len(ms) * 0.95) - 1]:7.2f} ms   "
          f"max {max(ms):7.2f} ms   (n={len(ms)})")


def main(path: str) -> int:
    size = os.path.getsize(path)
    print(f"file: {path}")
    print(f"size: {size / (1 << 30):.2f} GB\n")

    base_rss = rss_mb()

    root = tk.Tk()
    root.title("benchmark")
    root.geometry("1200x800")

    t0 = time.perf_counter()
    lf = LineFile(path)
    view = VirtualTextView(root, lf, RuleSet(list(DEFAULT_RULES)))
    view.pack(fill="both", expand=True)
    root.update()
    view.goto_start()
    root.update()
    open_ms = (time.perf_counter() - t0) * 1000

    print(f"open to first paint:  {open_ms:.1f} ms")
    print(f"encoding detected:    {lf.codec.label}")
    print(f"visible lines:        {view.visible_lines}")
    print(f"RSS after open:       {rss_mb():.1f} MB (baseline {base_rss:.1f} MB)\n")

    rng = random.Random(7)

    # 1. Random seeks: the scrollbar-drag case, and the one that would expose
    #    any hidden dependence on file size.
    samples = []
    for _ in range(200):
        target = rng.randint(0, size - 1)
        start = time.perf_counter()
        view.set_top(target)
        root.update_idletasks()
        samples.append(time.perf_counter() - start)
    report("random seek + repaint", samples)

    # 2. Sequential scrolling, as with the wheel.
    view.goto_start()
    root.update_idletasks()
    samples = []
    for _ in range(300):
        start = time.perf_counter()
        view.scroll_lines(3)
        root.update_idletasks()
        samples.append(time.perf_counter() - start)
    report("wheel scroll (3 lines)", samples)

    # 3. Paging, which moves a whole screen at a time.
    samples = []
    for _ in range(100):
        start = time.perf_counter()
        view.scroll_lines(view.visible_lines)
        root.update_idletasks()
        samples.append(time.perf_counter() - start)
    report("page down", samples)

    # 4. Backwards scrolling reads behind the current offset, which is the
    #    more awkward direction and worth measuring separately.
    samples = []
    for _ in range(200):
        start = time.perf_counter()
        view.scroll_lines(-3)
        root.update_idletasks()
        samples.append(time.perf_counter() - start)
    report("wheel scroll back", samples)

    # 5. Jump to the end, which is what follow-tail does on every update.
    samples = []
    for _ in range(100):
        start = time.perf_counter()
        view.goto_end()
        root.update_idletasks()
        samples.append(time.perf_counter() - start)
    report("goto end (tail repaint)", samples)

    peak_rss = rss_mb()
    print(f"\nRSS after scrolling:  {peak_rss:.1f} MB")

    # Indexing runs in the background; time a full pass so the cost of the
    # line count is known, separately from anything the user waits on.
    print("\nindexing the whole file...")
    t0 = time.perf_counter()
    indexer = Indexer(lf)
    indexer.start()
    while not indexer.wait_complete(1.0):
        done = lf.indexed_to / max(1, size)
        sys.stdout.write(f"\r  {done * 100:5.1f}%  ")
        sys.stdout.flush()
    indexer.stop()
    index_s = time.perf_counter() - t0
    print(f"\r  complete in {index_s:.1f} s "
          f"({size / (1 << 20) / index_s:.0f} MB/s), {lf.line_count:,} lines")
    print(f"  checkpoints held:   {len(lf._checkpoints):,}")
    print(f"  RSS after indexing: {rss_mb():.1f} MB")

    # With the index built, line-number lookups become the interesting cost.
    samples = []
    for _ in range(100):
        target = rng.randint(0, size - 1)
        start = time.perf_counter()
        lf.line_number_at(target)
        samples.append(time.perf_counter() - start)
    report("line_number_at (indexed)", samples)

    print(f"\nfinal RSS: {rss_mb():.1f} MB for a {size / (1 << 30):.2f} GB file")
    root.destroy()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
