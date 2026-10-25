"""Optional native file-drop bridge.

tkinterdnd2 carries the TkDND binaries. When it is absent the rest of the
interface keeps working and callers show the file picker instead.
"""

import tkinter as tk

_loaded_roots = set()


def available(root=None):
    """Return True when the optional TkDND package can be loaded."""
    try:
        from tkinterdnd2 import TkinterDnD
    except Exception:
        return False
    if root is None:
        return True
    key = str(root)
    if key in _loaded_roots:
        return True
    try:
        TkinterDnD._require(root)
    except Exception:
        return False
    _loaded_roots.add(key)
    return True


def split_files(root, data):
    """Turn a TkDND file payload into ordinary paths."""
    text = str(data or "").strip()
    if not text:
        return []
    try:
        values = root.tk.splitlist(text)
    except (tk.TclError, ValueError):
        values = [text]
    return [str(value) for value in values if str(value).strip()]


def bind_files(widget, callback, root=None, enter=None, leave=None):
    """Register one widget as a file drop target.

    callback receives a list of paths. enter/leave are optional visual hooks.
    Returns True only when registration actually succeeded.
    """
    root = root or widget.winfo_toplevel()
    if not available(root):
        return False
    try:
        from tkinterdnd2 import COPY, DND_FILES

        widget.drop_target_register(DND_FILES)

        def on_drop(event):
            try:
                callback(split_files(root, getattr(event, "data", "")))
            except Exception:
                pass
            return COPY

        def on_enter(event):
            if enter is not None:
                enter()
            return COPY

        def on_leave(event):
            if leave is not None:
                leave()
            return COPY

        widget.dnd_bind("<<Drop>>", on_drop)
        if enter is not None:
            widget.dnd_bind("<<DropEnter>>", on_enter)
        if leave is not None:
            widget.dnd_bind("<<DropLeave>>", on_leave)
    except Exception:
        return False
    return True


def bind_tree(widget, callback, root=None, enter=None, leave=None):
    """Register a section and every child currently inside it."""
    root = root or widget.winfo_toplevel()
    ok = bind_files(widget, callback, root=root, enter=enter, leave=leave)
    for child in widget.winfo_children():
        ok = bind_tree(child, callback, root=root, enter=enter, leave=leave) or ok
    return ok
