"""Generate large, realistic log files for testing and benchmarking.

    python tools/make_logfile.py big.log --size 5G
    python tools/make_logfile.py utf16.log --size 10M --encoding utf-16-le
    python tools/make_logfile.py grow.log --append --rate 10M --seconds 60

The last form appends continuously, which is how follow-tail gets exercised
against a file that is genuinely being written by another process.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

LEVELS = ["DEBUG", "INFO", "INFO", "INFO", "WARN", "ERROR", "FATAL"]
COMPONENTS = ["engine", "net.pool", "db.session", "cache", "auth", "scheduler", "io.writer"]
MESSAGES = [
    "request completed in %dms",
    "connection established to peer %d",
    "cache miss for key user:%d",
    "retrying operation, attempt %d",
    "flushed %d records to disk",
    "timeout waiting for lock after %dms",
    "unexpected response code %d from upstream",
    "checkpoint written, sequence %d",
]


def parse_size(text: str) -> int:
    """Accept plain bytes or a K/M/G suffix."""
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    text = text.strip().upper().rstrip("B")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def line(number: int, rng: random.Random) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1700000000 + number // 50))
    return "%s [%-5s] %-11s #%08d %s" % (
        stamp,
        rng.choice(LEVELS),
        rng.choice(COMPONENTS),
        number,
        rng.choice(MESSAGES) % rng.randint(1, 99999),
    )


def write_file(path: str, target: int, encoding: str, newline: str, seed: int) -> None:
    rng = random.Random(seed)
    terminator = newline.encode(encoding)
    written = 0
    number = 0
    started = time.monotonic()

    with open(path, "wb") as fh:
        if encoding == "utf-8-sig":
            pass  # codec emits the BOM itself on first write
        elif encoding == "utf-16-le":
            fh.write(b"\xff\xfe")
            written += 2
        elif encoding == "utf-16-be":
            fh.write(b"\xfe\xff")
            written += 2

        batch = []
        batch_bytes = 0
        while written < target:
            batch.append(line(number, rng))
            number += 1
            batch_bytes += 80
            if batch_bytes >= (1 << 20):
                data = terminator.join(
                    s.encode(encoding, "replace") for s in batch) + terminator
                fh.write(data)
                written += len(data)
                batch.clear()
                batch_bytes = 0
                _progress(written, target, started)
        if batch:
            data = terminator.join(s.encode(encoding, "replace") for s in batch) + terminator
            fh.write(data)
            written += len(data)

    _progress(written, target, started, final=True)
    print("\n%s: %d lines, %s" % (path, number, human(written)))


def append_forever(path: str, rate: int, seconds: float, encoding: str, newline: str) -> None:
    """Append at roughly ``rate`` bytes per second, for exercising follow-tail."""
    rng = random.Random()
    terminator = newline.encode(encoding)
    number = 0
    started = time.monotonic()
    written = 0

    with open(path, "ab") as fh:
        while time.monotonic() - started < seconds:
            tick = time.monotonic()
            chunk = []
            size = 0
            while size < rate // 10:
                text = line(number, rng)
                number += 1
                chunk.append(text)
                size += len(text) + len(newline)
            data = terminator.join(s.encode(encoding, "replace") for s in chunk) + terminator
            fh.write(data)
            fh.flush()
            written += len(data)
            sys.stdout.write("\rappended %s (%s/s target)   " % (human(written), human(rate)))
            sys.stdout.flush()
            elapsed = time.monotonic() - tick
            if elapsed < 0.1:
                time.sleep(0.1 - elapsed)
    print("\ndone: %d lines appended" % number)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024
    return str(n)


def _progress(written: int, target: int, started: float, final: bool = False) -> None:
    elapsed = max(time.monotonic() - started, 1e-6)
    sys.stdout.write("\r  %s / %s  (%s/s)   " % (
        human(written), human(target), human(written / elapsed)))
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument("--size", default="1G", help="target size, e.g. 500M or 5G")
    parser.add_argument("--encoding", default="utf-8",
                        choices=["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "cp1252"])
    parser.add_argument("--newline", default="lf", choices=["lf", "crlf", "cr"])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--append", action="store_true",
                        help="append continuously instead of creating a file")
    parser.add_argument("--rate", default="10M", help="bytes per second when appending")
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args(argv)

    newline = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}[args.newline]

    if args.append:
        append_forever(args.path, parse_size(args.rate), args.seconds,
                       args.encoding, newline)
    else:
        target = parse_size(args.size)
        free = None
        try:
            free = os.statvfs(os.path.dirname(os.path.abspath(args.path))).f_bavail
        except (AttributeError, OSError):
            pass
        if free is not None and target > free:
            print("not enough free space", file=sys.stderr)
            return 1
        write_file(args.path, target, args.encoding, newline, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
