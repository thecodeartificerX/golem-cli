"""Native Windows file/folder picker dialogs using ctypes.

Provides blocking functions that open OS-native dialogs via win32 APIs.
Intended to be called from FastAPI endpoints via ``asyncio.to_thread()``.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Struct definitions (Win64 — verified: OPENFILENAMEW=152, BROWSEINFOW=64)
# ---------------------------------------------------------------------------
# All pointer fields use c_void_p (not c_wchar_p) so the OS can write back
# into mutable buffers. We pass addresses via ctypes.addressof().


class OPENFILENAMEW(ctypes.Structure):
    """Win32 OPENFILENAMEW — used by GetOpenFileNameW."""

    _fields_ = [
        ("lStructSize", wt.DWORD),
        ("hwndOwner", wt.HWND),
        ("hInstance", ctypes.c_void_p),
        ("lpstrFilter", ctypes.c_void_p),
        ("lpstrCustomFilter", ctypes.c_void_p),
        ("nMaxCustFilter", wt.DWORD),
        ("nFilterIndex", wt.DWORD),
        ("lpstrFile", ctypes.c_void_p),
        ("nMaxFile", wt.DWORD),
        ("lpstrFileTitle", ctypes.c_void_p),
        ("nMaxFileTitle", wt.DWORD),
        ("lpstrInitialDir", ctypes.c_void_p),
        ("lpstrTitle", ctypes.c_void_p),
        ("Flags", wt.DWORD),
        ("nFileOffset", ctypes.c_uint16),
        ("nFileExtension", ctypes.c_uint16),
        ("lpstrDefExt", ctypes.c_void_p),
        ("lCustData", wt.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_void_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wt.DWORD),
        ("FlagsEx", wt.DWORD),
    ]


class BROWSEINFOW(ctypes.Structure):
    """Win32 BROWSEINFOW — used by SHBrowseForFolderW."""

    _fields_ = [
        ("hwndOwner", wt.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", ctypes.c_void_p),
        ("lpszTitle", ctypes.c_void_p),
        ("ulFlags", wt.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wt.LPARAM),
        ("iImage", ctypes.c_int),
    ]


# ---------------------------------------------------------------------------
# DLL handles and function bindings (module-level, initialized once)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _comdlg32 = ctypes.windll.comdlg32  # type: ignore[attr-defined]
    _shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    _ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]

    _comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    _comdlg32.GetOpenFileNameW.restype = wt.BOOL

    _comdlg32.CommDlgExtendedError.argtypes = []
    _comdlg32.CommDlgExtendedError.restype = ctypes.c_uint32

    _shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    _shell32.SHBrowseForFolderW.restype = ctypes.c_void_p  # PIDLIST_ABSOLUTE

    _shell32.SHGetPathFromIDListEx.argtypes = [
        ctypes.c_void_p,  # PCIDLIST_ABSOLUTE pidl
        ctypes.c_wchar_p,  # PWSTR pszPath
        ctypes.c_int,  # int cchPath
        ctypes.c_uint32,  # GPFIDL_FLAGS uOpts (0 = GPFIDL_DEFAULT)
    ]
    _shell32.SHGetPathFromIDListEx.restype = wt.BOOL

    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.restype = None

# ---------------------------------------------------------------------------
# Flag constants
# ---------------------------------------------------------------------------

# GetOpenFileNameW flags
_OFN_EXPLORER = 0x00080000
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008

# SHBrowseForFolderW ulFlags
_BIF_RETURNONLYFSDIRS = 0x00000001
_BIF_NEWDIALOGSTYLE = 0x00000040

# Buffer sizes
_MAX_PATH_BUF: int = 32768  # Extended path limit (2^15 wchars)
_MAX_PATH_SHORT: int = 260  # Display name buffer for BROWSEINFOW


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def open_file_dialog(initial_dir: str | None = None) -> str | None:
    """Open a native Windows file picker filtered to .md files.

    Returns the selected file path (forward slashes) or None if cancelled.
    Blocks until the user picks or cancels — call via ``asyncio.to_thread()``.
    """
    if sys.platform != "win32":
        raise NotImplementedError("File dialogs require Windows")

    # Build buffers — all must stay alive as locals for the duration of the call
    filter_str = "Markdown Files (*.md)\x00*.md\x00All Files (*.*)\x00*.*\x00\x00"
    filter_buf = ctypes.create_unicode_buffer(filter_str, len(filter_str))

    file_buf = ctypes.create_unicode_buffer(_MAX_PATH_BUF)

    title_buf = ctypes.create_unicode_buffer("Select Markdown Spec File")
    defext_buf = ctypes.create_unicode_buffer("md\x00")

    # Optional initial directory
    init_buf = ctypes.create_unicode_buffer(initial_dir) if initial_dir else None

    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = None
    ofn.lpstrFilter = ctypes.addressof(filter_buf)
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.addressof(file_buf)
    ofn.nMaxFile = _MAX_PATH_BUF
    ofn.lpstrTitle = ctypes.addressof(title_buf)
    ofn.lpstrDefExt = ctypes.addressof(defext_buf)
    ofn.Flags = _OFN_EXPLORER | _OFN_FILEMUSTEXIST | _OFN_PATHMUSTEXIST | _OFN_HIDEREADONLY | _OFN_NOCHANGEDIR

    if init_buf is not None:
        ofn.lpstrInitialDir = ctypes.addressof(init_buf)

    result = _comdlg32.GetOpenFileNameW(ctypes.byref(ofn))

    if result != 0:
        return Path(file_buf.value).as_posix()

    # result == 0: check if cancelled or actual error
    err = _comdlg32.CommDlgExtendedError()
    if err != 0:
        raise OSError(f"GetOpenFileNameW failed with CommDlgExtendedError={err:#x}")

    return None  # User cancelled


def open_folder_dialog(initial_dir: str | None = None) -> str | None:
    """Open a native Windows folder picker dialog.

    Returns the selected directory path (forward slashes) or None if cancelled.
    Blocks until the user picks or cancels — call via ``asyncio.to_thread()``.

    Note: ``initial_dir`` is accepted for API symmetry but the legacy
    SHBrowseForFolderW API does not support setting an initial directory
    without a callback. The parameter is reserved for future use.
    """
    if sys.platform != "win32":
        raise NotImplementedError("Folder dialogs require Windows")

    disp_buf = ctypes.create_unicode_buffer(_MAX_PATH_SHORT)
    title_buf = ctypes.create_unicode_buffer("Select Project Root Directory")

    bi = BROWSEINFOW()
    bi.hwndOwner = None
    bi.pidlRoot = None
    bi.pszDisplayName = ctypes.addressof(disp_buf)
    bi.lpszTitle = ctypes.addressof(title_buf)
    bi.ulFlags = _BIF_RETURNONLYFSDIRS | _BIF_NEWDIALOGSTYLE
    bi.lpfn = None
    bi.lParam = 0
    bi.iImage = 0

    pidl = _shell32.SHBrowseForFolderW(ctypes.byref(bi))

    if pidl is None or pidl == 0:
        return None

    path_buf = ctypes.create_unicode_buffer(_MAX_PATH_BUF)
    try:
        ok = _shell32.SHGetPathFromIDListEx(pidl, path_buf, _MAX_PATH_BUF, 0)
    finally:
        _ole32.CoTaskMemFree(pidl)

    if ok != 0:
        return Path(path_buf.value).as_posix()

    return None
