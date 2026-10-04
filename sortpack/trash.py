# -*- coding: utf-8 -*-
"""
Send a file or folder to the Windows Recycle Bin (recoverable), using the shell's
SHFileOperationW — no third-party dependency, and NOT a permanent delete, so a set
folder cleaned up after processing can always be restored from the bin.

We deliberately never fall back to a hard delete: if the shell call fails, the caller
gets an exception and leaves the folder in place rather than destroying data.
"""

import os
import ctypes
from ctypes import wintypes


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # <-- the flag that routes to the Recycle Bin
_FOF_NOERRORUI = 0x0400


def send_to_recycle_bin(path):
    """Move `path` (file or folder) to the Recycle Bin. Raises on failure — never a hard
    delete. Windows only."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int

    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = path + "\x00\x00"          # pFrom is a double-null-terminated list
    op.pTo = None
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI)
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError(f"SHFileOperation a échoué (code {rc}) pour {path!r}")
    if op.fAnyOperationsAborted:
        raise OSError(f"suppression annulée pour {path!r}")
    return path
