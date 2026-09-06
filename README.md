# BareTail Clone

A real-time log file viewer for Windows, Linux and macOS — an independent
Python reimplementation of [BareTail 3.50a](https://www.baremetalsoft.com/baretail/),
including the search and filter features of BareTailPro.

Not affiliated with or endorsed by Bare Metal Software. Written from the
published feature descriptions; no original code was used or decompiled.
BareTail is their product, and worth the $25 if you want the real thing.

Pure standard library. No dependencies, no installer.

```bash
python baretail.py engine.log
```

## What it does

**Files of any size.** A 5 GB log opens in 70 ms and scrolls instantly. The
file is never loaded — only the ~50 lines on screen are ever in memory, so
memory use and repaint cost are the same for a 4 KB file and a 40 GB one.

**Follow tail.** Watches a growing file like `tail -f`, keeping up with fast
writers, and handles the file being truncated or rotated out from under it.

**Tabs.** Several files at once, with a per-tab indicator showing which ones
changed while you were looking elsewhere. The strip goes on any of the four
edges, horizontally or vertically.

**Highlighting.** Colour whole lines by the text they contain. Rules apply top
to bottom and the first match wins, so ordering resolves overlaps.

**Search and filter** (the BareTailPro half). Incremental literal or regex
search with live syntax feedback, a sortable results table with timestamps,
line numbers and capture groups, and filter-tail mode showing only the lines
that match — or only those that do not — updating live as the file grows.

Unicode, UTF-8, UTF-16 and ANSI; CRLF, LF and bare CR.

## Command line

```
baretail [options] {file(s)}

  -wp, --window-position left top width height
  -ws, --window-state 0|1|2            0 normal, 1 minimised, 2 maximised
  -tc, --tile-window-count count
  -ti, --tile-window-index index
```

```bash
python baretail.py engine.log stats.log
python baretail.py --tile-window-count 3 --tile-window-index 0 engine.log
```

## Keys

| | |
|---|---|
| `Ctrl+O` / `Ctrl+W` | Open file / close tab |
| `Ctrl+F`, `F3`, `Shift+F3` | Find, find next, find previous |
| `F12` | Follow tail |
| `Ctrl+H` | Highlighting on/off |
| `Ctrl+G` | Go to line |
| `Ctrl+Home` / `Ctrl+End` | Start / end of file |
| `Ctrl+Tab` | Next tab |

## Settings

Preferences go in a file beside the application, in the registry, or nowhere —
your choice, under the Preferences menu. The file is JSON and portable: copy
`baretail.json` alongside the application to carry your highlight rules to
another machine. Sets of open files can be saved and reloaded as sessions.

## How it handles large files

Storing a byte offset per line is not viable — a 20 GB log holds roughly 200
million lines, or 1.6 GB of offsets. Instead a checkpoint is kept every 4096
lines, which for that file is about 50,000 entries and a little over a
megabyte. Any line number is then at most 4096 steps from a known one.

Scroll position is a **byte offset**, not a line number, so jumping anywhere
never requires having counted the lines before it. The line count is filled in
by a background thread, and the file is fully usable — scrollable, tailable,
searchable — long before that finishes.

Measured on a 5 GB, 63-million-line file:

| | |
|---|---|
| Open to first paint | 69 ms |
| Random seek + repaint | 7.4 ms median |
| Memory, after 800 scroll operations | 33 MB |
| Full line index (63.3M lines) | 9.6 s (532 MB/s) |
| Line number lookup, indexed | 0.31 ms |

Of that 7.4 ms repaint, about 6 ms is Tk's own cost to paint 52 lines of text;
the file access underneath it is 0.1–0.3 ms.

## Layout

```
baretail.py              entry point
baretail/
  cli.py                 command line switches, window placement, tiling
  config.py              JSON settings; file | registry | none, plus sessions
  core/                  no tkinter imports anywhere in here
    encoding.py          BOM sniffing, incremental decoding, line splitting
    fileopen.py          share-delete open, so the writer can still rotate
    linefile.py          the sparse index and byte-addressed reads
    indexer.py           background line counting
    tailer.py            growth, truncation and rotation detection
    highlight.py         rules, resolved first-match-wins
    search.py            chunked literal and regex scanning
  ui/
    logview.py           the virtualised viewport
    filterview.py        the filter-tail view
    tabstrip.py          the four-sided tab strip
    mainwindow.py        menus, toolbar, status bar, wiring
    searchbar.py         find bar and results table
    highlightdlg.py  prefsdlg.py  dialogs.py  pump.py  tkutil.py
tests/                   135 tests, no display needed for most
tools/                   log generator, benchmark, tail verification
```

`core/` contains no tkinter imports, so the engine is testable headlessly and
the toolkit choice stays reversible.

## Development

```bash
python -m pytest tests/ -q
```

```bash
python tools/make_logfile.py big.log --size 5G
python tools/benchmark.py big.log
python tools/verify_tail.py
```

`verify_tail.py` appends from a genuinely separate process — the GIL would
serialise a writer thread against the viewer and hide the races worth
catching — then checks that no lines were lost and that the view still shows
the true end of the file.

## Notes

Three details are worth knowing about, because each is easy to get wrong and
none is obvious. The first two are Windows-specific; the third is not.

**Share mode.** Python's built-in `open()` withholds `FILE_SHARE_DELETE`, so
merely having a log open would make `os.replace` fail for whoever owns it —
the viewer would break log rotation in the applications it is meant to be
watching. `core/fileopen.py` opens through `CreateFileW` instead.

**Threads and Tk.** The indexer, tailer and search engine each run on their own
thread and none of them may touch a widget. Everything they report is posted
through `ui/pump.py` and applied on the main thread.

**Tk finalisers, which are subtler.** `tkinter.font.Font.__del__` calls into
Tcl, and a finaliser runs on whichever thread happens to collect the object —
for anything caught in a reference cycle, that is the cyclic collector, on
whatever thread allocated at the wrong moment. When that thread was a worker,
the Tcl call blocked forever. The symptom was a search delivering exactly one
batch of results and then hanging, which looks like a bug in the search engine
and is nothing of the sort. `ui/tkutil.py` creates fonts whose collection can
never touch Tcl, and releases them explicitly from the main thread instead.
