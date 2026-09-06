"""Opening a log file without getting in the writer's way.

Python's built-in ``open()`` on Windows requests a share mode that permits
other processes to read and write the file but **not to rename or delete it**.
For a log viewer that is unacceptable: merely having a file open would make
``os.replace`` fail for whoever owns it, so the viewer would break log rotation
in the applications it is meant to be watching.  The writer's rotation fails
with a sharing violation, and the cause -- a passive viewer in another window
-- is not remotely obvious.

Passing ``FILE_SHARE_DELETE`` to ``CreateFileW`` fixes it.  A rename or delete
then succeeds while our handle stays valid, still reading the original file,
which is exactly the behaviour the tailer's rotation detection expects.

On POSIX this is not an issue -- an open file descriptor never prevents
unlinking -- so the built-in is used unchanged.
"""

from __future__ import annotations

import os
import sys

__all__ = ["open_shared"]

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    #: (HANDLE)-1, widened to the platform's pointer size.
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _CreateFileW = ctypes.windll.kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,   # lpFileName
        wintypes.DWORD,     # dwDesiredAccess
        wintypes.DWORD,     # dwShareMode
        ctypes.c_void_p,    # lpSecurityAttributes
        wintypes.DWORD,     # dwCreationDisposition
        wintypes.DWORD,     # dwFlagsAndAttributes
        wintypes.HANDLE,    # hTemplateFile
    ]
    _CreateFileW.restype = wintypes.HANDLE

    def open_shared(path: str):
        """Open ``path`` for binary reading, allowing rename and delete.

        The Win32 handle is adopted by a file object, so closing that object
        closes the handle; there is no separate cleanup to remember.
        """
        handle = _CreateFileW(
            path,
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == _INVALID_HANDLE:
            error = ctypes.get_last_error() or ctypes.GetLastError()
            raise OSError(0, os.strerror(22) if not error else
                          ctypes.FormatError(error), path, error)

        # open_osfhandle takes ownership of the handle; if wrapping it fails
        # the handle would otherwise leak.
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except OSError:
            ctypes.windll.kernel32.CloseHandle(handle)
            raise
        try:
            return os.fdopen(fd, "rb", buffering=0)
        except OSError:
            os.close(fd)
            raise

else:

    def open_shared(path: str):
        """Open ``path`` for binary reading.

        An open descriptor never blocks unlink or rename on POSIX, so no
        special handling is needed.
        """
        return open(path, "rb", buffering=0)
