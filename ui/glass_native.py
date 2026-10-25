"""Native glass helpers for the Tk workbench.

Windows can clip a top-level window, so the outer workbench uses SetWindowRgn
plus real window transparency. Individual cards get four tiny corner overlays sampled
from the real background layer; the outside wedge hides the square Tk corner
and the inside stays transparent, which gives the same visual result without
replacing every widget.
"""

import ctypes
import sys
import tkinter as tk

from ui import theme

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:
    Image = None
    ImageDraw = None
    ImageTk = None

_WINDOWS = sys.platform == "win32" and Image is not None
_user32 = ctypes.windll.user32 if _WINDOWS else None
_gdi32 = ctypes.windll.gdi32 if _WINDOWS else None
_dwmapi = ctypes.windll.dwmapi if _WINDOWS else None

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

_BG_LAYER = None
_CARDS = []
_REFRESH_JOB = None


def available():
    return bool(_WINDOWS)


def _hex_rgb(color):
    text = str(color or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            return tuple(int(text[index:index + 2], 16) for index in (1, 3, 5))
        except ValueError:
            pass
    return (25, 23, 39)


def _mix(first, second, amount=0.2):
    a = _hex_rgb(first)
    b = _hex_rgb(second)
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(int(round(a[index] * (1 - amount) + b[index] * amount)) for index in range(3))


def _top_handle(window):
    try:
        handle = int(window.winfo_id())
        parent = int(_user32.GetParent(handle) or 0)
        return parent or handle
    except Exception:
        return 0


def _apply_region(window, radius, size=None):
    """Clip the whole top-level client area to a rounded rectangle."""
    if not _WINDOWS:
        return False
    try:
        if size is None:
            window.update_idletasks()
            width = max(1, int(window.winfo_width()))
            height = max(1, int(window.winfo_height()))
        else:
            width, height = (max(1, int(value)) for value in size[:2])
        handle = _top_handle(window)
        if not handle or width < 2 or height < 2:
            return False
        radius = max(0, min(int(radius), width // 2, height // 2))
        if radius <= 0:
            return False
        region = _gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius * 2, radius * 2
        )
        if not region:
            return False
        if not _user32.SetWindowRgn(handle, region, True):
            _gdi32.DeleteObject(region)
            return False
        window._glass_region_size = (width, height, radius)
        return True
    except Exception:
        return False


def _apply_dwm_round(window):
    if not _WINDOWS or _dwmapi is None:
        return False
    try:
        handle = _top_handle(window)
        value = ctypes.c_int(DWMWCP_ROUND)
        result = _dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(handle),
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except Exception:
        return False


def apply_window(window, radius=18, alpha=0.94, tint=None):
    """Apply real window clipping and live transparency.

    alpha below 1 makes the whole workbench genuinely see-through. The region
    clips the outer corners at the OS level. No extra composition layer is
    drawn over the client area, because that washes out the text.
    """
    if not _WINDOWS:
        return False
    try:
        window.wm_attributes("-alpha", max(0.72, min(1.0, float(alpha))))
    except Exception:
        pass
    tint = tint or theme.CARD
    _apply_dwm_round(window)
    applied = _apply_region(window, radius)
    window._glass_native_radius = int(radius)
    window._glass_native_tint = str(tint)

    if not getattr(window, "_glass_native_bound", False):
        window._glass_native_bound = True

        def apply_region_now():
            window._glass_region_job = None
            try:
                if not window.winfo_exists() or not window.winfo_ismapped():
                    return None
                size = (int(window.winfo_width()), int(window.winfo_height()))
            except Exception:
                return None
            if min(size) < 2:
                return None
            if (size[0], size[1], int(window._glass_native_radius)) == getattr(
                window, "_glass_region_size", None
            ):
                return None
            _apply_region(window, window._glass_native_radius, size)
            return None

        def queue_region(_event=None):
            if _event is not None and getattr(_event, "widget", None) is not window:
                return None
            try:
                if not window.winfo_exists():
                    return None
            except Exception:
                return None
            old = getattr(window, "_glass_region_job", None)
            if old is not None:
                try:
                    window.after_cancel(old)
                except Exception:
                    pass
            try:
                # Coalesce the burst of Configure events produced while resizing.
                window._glass_region_job = window.after(12, apply_region_now)
            except Exception:
                window._glass_region_job = None
            return None

        def on_configure(event=None):
            return queue_region(event)

        def on_map(event=None):
            queue_region(event)
            try:
                _apply_dwm_round(window)
            except Exception:
                pass
            return None

        def on_destroy(event=None):
            if event is not None and getattr(event, "widget", None) is not window:
                return None
            old = getattr(window, "_glass_region_job", None)
            if old is not None:
                try:
                    window.after_cancel(old)
                except Exception:
                    pass
                window._glass_region_job = None
            return None

        window.bind("<Configure>", on_configure, add="+")
        window.bind("<Map>", on_map, add="+")
        window.bind("<Destroy>", on_destroy, add="+")
    return bool(applied)


def set_background_layer(layer):
    """Tell the card corner masks where the real background pixels live."""
    global _BG_LAYER
    _BG_LAYER = layer
    return refresh_rounding()


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except Exception:
        return False


def _corner_boxes(radius):
    r = int(radius)
    edge = 2 * r - 1
    return {
        "nw": (0, 0, edge, edge),
        "ne": (-r, 0, r - 1, edge),
        "sw": (0, -r, edge, r - 1),
        "se": (-r, -r, r - 1, r - 1),
    }


def _crop_rgb(picture, x, y, size, fallback):
    if picture is None:
        return Image.new("RGB", (size, size), fallback)
    try:
        width, height = picture.size
        if x >= 0 and y >= 0 and x + size <= width and y + size <= height:
            return picture.crop((x, y, x + size, y + size)).convert("RGB")
        sample = picture.getpixel(
            (max(0, min(width - 1, x + size // 2)), max(0, min(height - 1, y + size // 2)))
        )
        canvas = Image.new("RGB", (size, size), tuple(sample[:3]))
        if x < width and y < height:
            left = max(0, x)
            top = max(0, y)
            right = min(width, x + size)
            bottom = min(height, y + size)
            if right > left and bottom > top:
                canvas.paste(
                    picture.crop((left, top, right, bottom)).convert("RGB"),
                    (left - x, top - y),
                )
        return canvas
    except Exception:
        return Image.new("RGB", (size, size), fallback)


def _corner_image(radius, corner, parent_bg, picture=None, x=0, y=0):
    r = max(4, int(radius))
    scale = 4
    big = r * scale
    bg = _crop_rgb(picture, int(x), int(y), big, _hex_rgb(parent_bg))
    mask = Image.new("L", (big, big), 255)
    draw = ImageDraw.Draw(mask)
    boxes = _corner_boxes(big)
    draw.ellipse(boxes[corner], fill=0)
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    image.paste(bg, (0, 0), mask)

    border = _mix(theme.BORDER, theme.TEXT, 0.28)
    arc_draw = ImageDraw.Draw(image)
    arcs = {
        "nw": (180, 270),
        "ne": (270, 360),
        "sw": (90, 180),
        "se": (0, 90),
    }
    start, end = arcs[corner]
    arc_draw.arc(
        boxes[corner],
        start=start,
        end=end,
        fill=border + (180,),
        width=scale,
    )
    return image.resize((r, r), Image.LANCZOS)


def _card_parent_bg(widget):
    try:
        return str(widget.master.cget("bg"))
    except Exception:
        return theme.BG


def _update_corners(widget):
    if not _alive(widget):
        return False
    labels = getattr(widget, "_glass_corner_labels", None)
    if not labels:
        return False
    try:
        width = int(widget.winfo_width())
        height = int(widget.winfo_height())
    except Exception:
        return False
    radius = int(getattr(widget, "_glass_corner_radius", theme.RADIUS))
    radius = max(4, min(radius, width // 2, height // 2))
    if width < 2 or height < 2:
        return False

    parent_bg = _card_parent_bg(widget)
    picture = None
    left = top = 0
    if (
        _BG_LAYER is not None
        and parent_bg.lower() == str(theme.BG).lower()
        and _alive(getattr(_BG_LAYER, "master", None))
    ):
        try:
            picture = _BG_LAYER.picture()
            left = int(widget.winfo_rootx()) - int(_BG_LAYER.master.winfo_rootx())
            top = int(widget.winfo_rooty()) - int(_BG_LAYER.master.winfo_rooty())
        except Exception:
            picture = None
            left = top = 0

    positions = {
        "nw": (left, top),
        "ne": (left + width - radius, top),
        "sw": (left, top + height - radius),
        "se": (left + width - radius, top + height - radius),
    }
    photos = {}
    for corner, label in labels.items():
        image = _corner_image(
            radius,
            corner,
            parent_bg,
            picture=picture,
            x=positions[corner][0],
            y=positions[corner][1],
        )
        photo = ImageTk.PhotoImage(image)
        photos[corner] = photo
        label.configure(image=photo, bg=widget.cget("bg"))
        label.lift()
    widget._glass_corner_photos = photos
    return True


def _schedule(widget, delay=24):
    if not _alive(widget):
        return None
    old = getattr(widget, "_glass_corner_job", None)
    if old is not None:
        try:
            widget.after_cancel(old)
        except Exception:
            pass
    try:
        widget._glass_corner_job = widget.after(delay, lambda: _update_corners(widget))
    except Exception:
        widget._glass_corner_job = None
    return widget._glass_corner_job


def _cancel_scheduled(widget):
    job = getattr(widget, "_glass_corner_job", None)
    if job is not None:
        try:
            widget.after_cancel(job)
        except Exception:
            pass
        widget._glass_corner_job = None


def round_widget(widget, radius=None):
    """Mask a card's four square corners with the pixels behind it."""
    if not available() or widget is None:
        return widget
    if getattr(widget, "_glass_corner_labels", None):
        return widget
    radius = int(radius or theme.RADIUS)
    widget._glass_corner_radius = radius
    widget._glass_corner_labels = {}
    for corner, place in (
        ("nw", {"x": 0, "y": 0}),
        ("ne", {"relx": 1.0, "x": -radius, "y": 0}),
        ("sw", {"x": 0, "rely": 1.0, "y": -radius}),
        ("se", {"relx": 1.0, "x": -radius, "rely": 1.0, "y": -radius}),
    ):
        label = tk.Label(
            widget,
            bd=0,
            highlightthickness=0,
            bg=widget.cget("bg"),
            takefocus=0,
        )
        label.place(width=radius, height=radius, **place)
        label.bind("<Button-1>", lambda event: None)
        widget._glass_corner_labels[corner] = label
    _CARDS.append(widget)
    widget.bind("<Configure>", lambda event, target=widget: _schedule(target), add="+")
    widget.bind("<Map>", lambda event, target=widget: _schedule(target), add="+")
    widget.bind(
        "<Destroy>",
        lambda event, target=widget: (
            _cancel_scheduled(target)
            if getattr(event, "widget", None) is target
            else None
        ),
        add="+",
    )
    _schedule(widget, 10)
    return widget


def refresh_rounding():
    global _CARDS
    _CARDS = [widget for widget in _CARDS if _alive(widget)]
    for widget in list(_CARDS):
        _update_corners(widget)
    return len(_CARDS)


def refresh_rounding_later(delay=35):
    """Debounce corner refreshes while a page is scrolling."""
    global _REFRESH_JOB
    root = None
    if _BG_LAYER is not None and _alive(getattr(_BG_LAYER, "master", None)):
        root = _BG_LAYER.master
    if root is None:
        live = [widget for widget in _CARDS if _alive(widget)]
        root = live[0] if live else None
    if root is None:
        return None
    if _REFRESH_JOB is not None:
        try:
            root.after_cancel(_REFRESH_JOB)
        except Exception:
            pass
    try:
        _REFRESH_JOB = root.after(int(delay), refresh_rounding)
    except Exception:
        _REFRESH_JOB = None
    return _REFRESH_JOB
