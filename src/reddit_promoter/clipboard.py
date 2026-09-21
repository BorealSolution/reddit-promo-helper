"""Putting text on the system clipboard, on whatever OS this is running on.

Manual mode depends on this: the tool drafts the message, puts it on the
clipboard, and the operator pastes it into Reddit themselves. If the clipboard
is unavailable the caller prints the text instead - never silently skips it,
because the whole point is getting the exact text out intact.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


class ClipboardUnavailable(Exception):
    """No working clipboard backend on this machine."""


def _windows(text: str) -> str:
    # clip.exe reads UTF-16LE; anything else mangles non-ASCII.
    subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
    return "clip.exe"


def _macos(text: str) -> str:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    return "pbcopy"


def _linux(text: str) -> str:
    data = text.encode("utf-8")
    # Wayland first, then the two common X11 tools.
    candidates = [
        (["wl-copy"], "wl-copy"),
        (["xclip", "-selection", "clipboard"], "xclip"),
        (["xsel", "--clipboard", "--input"], "xsel"),
    ]
    for argv, name in candidates:
        if shutil.which(argv[0]):
            subprocess.run(argv, input=data, check=True)
            return name
    raise ClipboardUnavailable(
        "no clipboard tool found - install wl-clipboard, xclip or xsel"
    )


def _tkinter(text: str) -> str:
    """Last resort. Works anywhere tkinter has a display."""
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()      # required, or the clipboard is dropped on destroy
    finally:
        root.destroy()
    return "tkinter"


def copy(text: str) -> str:
    """Put `text` on the clipboard. Returns the backend used.

    Raises ClipboardUnavailable if nothing worked.
    """
    if not text:
        raise ClipboardUnavailable("nothing to copy")

    if sys.platform == "win32":
        order = [_windows, _tkinter]
    elif sys.platform == "darwin":
        order = [_macos, _tkinter]
    else:
        order = [_linux, _tkinter]

    problems = []
    for backend in order:
        try:
            return backend(text)
        except ClipboardUnavailable as exc:
            problems.append(str(exc))
        except Exception as exc:            # missing binary, no display, ...
            problems.append(f"{backend.__name__.lstrip('_')}: {exc}")

    raise ClipboardUnavailable("; ".join(problems) or "no backend available")


def describe() -> str:
    """Which backend this machine will use, for the README and diagnostics."""
    if sys.platform == "win32":
        return "clip.exe (built into Windows), falling back to tkinter"
    if sys.platform == "darwin":
        return "pbcopy (built into macOS), falling back to tkinter"
    found = [n for n in ("wl-copy", "xclip", "xsel") if shutil.which(n)]
    if found:
        return f"{found[0]}, falling back to tkinter"
    return ("no clipboard tool found - install wl-clipboard, xclip or xsel; "
            "tkinter may still work")
