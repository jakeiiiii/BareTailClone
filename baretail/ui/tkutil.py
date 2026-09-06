"""Tk objects that are safe to garbage-collect.

``tkinter.font.Font.__del__`` calls into Tcl to delete the underlying font.
So does ``tkinter.Variable.__del__``.  A finaliser runs on whichever thread
happens to collect the object, and if that object sits in a reference cycle it
is the *cyclic* collector that frees it -- which runs on whatever thread was
unlucky enough to allocate at the wrong moment.

When that thread is one of ours, the Tcl call blocks forever.  Observed
symptom: a search over a small file delivering exactly one batch of results and
then hanging, with the worker's stack ending in ``font.py``, ``__del__``.  It
looks like a bug in the search engine and is nothing of the sort.

The fix is to make the finaliser harmless.  Fonts created here never issue a
Tcl call when collected; the underlying font is instead released explicitly by
:func:`release_font`, from the main thread, when the widget owning it goes
away.
"""

from __future__ import annotations

import tkinter.font as tkfont

__all__ = ["view_font", "release_font"]


def view_font(**kwargs) -> tkfont.Font:
    """Create a font whose garbage collection cannot touch Tcl."""
    font = tkfont.Font(**kwargs)
    # tkinter checks this flag in __del__ before calling "font delete".
    # Clearing it turns finalisation into a no-op, wherever it happens.
    font.delete_font = False
    return font


def release_font(font: tkfont.Font | None) -> None:
    """Delete a font's Tcl object. Must be called from the main thread.

    Safe to call more than once, and safe once the interpreter is already
    being torn down -- both are ordinary during application shutdown.
    """
    if font is None:
        return
    try:
        font._call("font", "delete", font.name)
    except Exception:
        # The font may already be gone, or Tk may be shutting down; neither
        # is worth reporting, and neither leaves anything to clean up.
        pass
