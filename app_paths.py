"""app_paths.py - single source of truth for file locations.

Why this module exists
----------------------
Before Wave 3 / G2 every module that needed a path recomputed it inline:

    if getattr(sys, "frozen", False):
        BASE_DIR = Path(sys.executable).parent
    else:
        BASE_DIR = Path(__file__).parent

That works for a onedir build only by accident. ``__file__`` of a frozen module
points into the bundled resource root - ``_internal/`` in a onedir build, a
temporary extraction directory in onefile - so any module that used its own
``__file__`` as the application directory would have written settings, models
and checkpoints *inside* ``_internal/``, where the next update wipes them.

The rule G2 makes explicit, and that every caller now shares:

    user data   -> beside the launcher (BASE_DIR)      writable, survives updates
    resources   -> resource_root()                     read-only, ships with the app

Layouts
-------
=================  ==========================  ===============================
layout             BASE_DIR (user data)        resource_root() (read-only)
=================  ==========================  ===============================
source tree        directory of this file      same
PyInstaller onedir directory of the .exe      ``<_internal>`` (= sys._MEIPASS)
PyInstaller onefile directory of the .exe     ``sys._MEIPASS`` (temp dir)
=================  ==========================  ===============================

``sys._MEIPASS`` is deliberately not used to detect "am I frozen": it is set by
the bootloader in both modes, so the check is ``sys.frozen``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True when running from a PyInstaller build (onefile or onedir)."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Writable application directory: where settings and models live.

    In a frozen build this is the directory containing the launcher; in a
    source checkout it is the repository root.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            # onedir puts the launcher one level above _internal/; onefile has
            # no _internal/ and the launcher's own directory is the only
            # sensible place for user data in either case.
            meipass_path = Path(meipass)
            if meipass_path.name == "_internal":
                return meipass_path.parent
        return Path(sys.executable).parent
    return _HERE


def resource_root() -> Path:
    """Read-only root of files shipped with the program."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).parent
    return _HERE


def resource_path(*parts: str) -> Path:
    """Absolute path of a shipped resource, e.g. ``("ov_models", "vad.onnx")``.

    Falls back to the application directory so that pre-G2 packages (which put
    resources beside the launcher) and hand-placed files keep working.
    """
    parts = tuple(str(p) for p in parts)
    primary = resource_root().joinpath(*parts)
    if primary.exists():
        return primary
    legacy = app_dir().joinpath(*parts)
    return legacy if legacy.exists() else primary


def user_path(*parts: str) -> Path:
    """Absolute path of writable user data."""
    return app_dir().joinpath(*parts)
