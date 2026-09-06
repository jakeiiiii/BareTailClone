"""Moving work from background threads onto the Tk main thread.

Tk is not thread-safe.  The indexer, tailer and search engine all run on their
own threads, and none of them may touch a widget -- doing so produces crashes
and hangs that appear at random and are close to impossible to reproduce.

Every callback from those threads is therefore posted here and drained by a
periodic ``after`` on the main thread.  ``Tk.after`` is itself only reliable
when called from the main thread, so a plain queue plus a polling drain is the
mechanism rather than ``after`` from the worker.
"""

from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

__all__ = ["UiPump"]

#: Drain interval.  Fast enough that tail updates look immediate, slow enough
#: that an idle application is not waking up constantly.
INTERVAL_MS = 40

#: Callbacks drained per tick.  A burst -- a search over a large file matching
#: nearly every line -- must not freeze the window by draining without limit.
MAX_PER_TICK = 200


class UiPump:
    """A thread-safe channel onto the Tk main loop."""

    def __init__(self, widget: tk.Misc, interval_ms: int = INTERVAL_MS) -> None:
        self._widget = widget
        self._interval = interval_ms
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._running = False
        self._after_id: str | None = None

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._schedule()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def post(self, func: Callable[[], None], *args, **kwargs) -> None:
        """Queue ``func`` to run on the main thread. Safe from any thread."""
        if args or kwargs:
            self._queue.put(lambda: func(*args, **kwargs))
        else:
            self._queue.put(func)

    def _schedule(self) -> None:
        if self._running:
            self._after_id = self._widget.after(self._interval, self._drain)

    def _drain(self) -> None:
        for _ in range(MAX_PER_TICK):
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except tk.TclError:
                # The widget was destroyed between posting and draining, which
                # is normal during shutdown.
                pass
            except Exception:
                # One bad callback must not stop the pump for everything else.
                pass
        self._schedule()
