"""液态玻璃背景层。

用 Pillow 把渐变（或用户图片）预渲染成一张模糊过的底图，铺在窗口最底层。
外层窗口的真实圆角和半透明由 ui/glass_native 处理；卡片四角
从这张底图取像素做遮罩，因此角外是背景，角内才是卡片。

渲染有缓存和防抖，动画只做低频重绘，不用粒子、不占满 CPU。
所有失败都回退到渐变，不会让界面打不开。
"""

import math
import time
import tkinter as tk
from pathlib import Path

from ui import theme

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
except Exception:  # 没装 Pillow 时只画纯色，不影响启动
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageOps = None
    ImageTk = None

MAX_SIDE = 2600
MAX_SOURCE = 4200
MAX_IMAGE_BYTES = 40 * 1024 * 1024
_IMAGE_CACHE = {}
_IMAGE_CACHE_LIMIT = 4
_DEBOUNCE_MS = 180


def available():
    return Image is not None


def inspect_image(path):
    """选图前先验证：格式、大小、尺寸都要过，别等渲染时才炸。"""
    if not available():
        return {"ok": False, "error": "这台机器没有图片组件，暂时不能设背景"}
    try:
        target = Path(str(path))
        if not target.is_file():
            return {"ok": False, "error": "找不到这个文件"}
        if target.stat().st_size > MAX_IMAGE_BYTES:
            return {"ok": False, "error": "图片超过 40 MB，换一张小一点的"}
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
        if width < 200 or height < 200:
            return {"ok": False, "error": "图片太小（%dx%d），当背景会糊" % (width, height)}
        return {"ok": True, "width": width, "height": height, "path": str(target)}
    except Exception as exc:
        return {"ok": False, "error": "读不了这张图片：%s" % exc}


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def _hex(color, fallback="#101018"):
    """只接受 #rrggbb；别的写法退回默认，避免坏配置把渲染打断。"""
    text = str(color or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text.lower()
        except ValueError:
            return fallback
    return fallback


def _size(value):
    try:
        width = max(64, min(MAX_SIDE, int(value[0])))
        height = max(64, min(MAX_SIDE, int(value[1])))
    except Exception:
        return 960, 640
    return width, height


def _gradient(size, start, end, glow, phase=0.0):
    """小图做渐变再放大：快，而且放大后自带柔和过渡。"""
    width, height = size
    small = (96, 96)
    base = Image.new("RGB", small, start)
    pixels = base.load()
    for y in range(small[1]):
        for x in range(small[0]):
            # 斜向渐变 + 一点点相位，动态模式下光带会缓慢移动
            mix = (x + y) / float(small[0] + small[1] - 2)
            mix = _clamp(mix + phase * 0.08, 0.0, 1.0)
            pixels[x, y] = tuple(
                int(round(start[index] * (1 - mix) + end[index] * mix))
                for index in range(3)
            )
    canvas = base.resize((width, height), Image.BICUBIC)

    # 光晕：画在小图上模糊后放大，成本低，效果接近环境光
    glow_size = (160, 160)
    halo = Image.new("L", glow_size, 0)
    draw = ImageDraw.Draw(halo)
    cx = glow_size[0] * (0.32 + 0.36 * (0.5 + 0.5 * math.sin(phase)))
    cy = glow_size[1] * (0.30 + 0.30 * (0.5 + 0.5 * math.cos(phase * 0.8)))
    draw.ellipse(
        (cx - 52, cy - 52, cx + 52, cy + 52),
        fill=140,
    )
    halo = halo.filter(ImageFilter.GaussianBlur(30)).resize((width, height), Image.BICUBIC)
    tint = Image.new("RGB", (width, height), glow)
    canvas = Image.composite(tint, canvas, halo.point(lambda value: int(value * 0.55)))
    return canvas


def _to_rgb(hex_color):
    text = _hex(hex_color)
    return tuple(int(text[index:index + 2], 16) for index in (1, 3, 5))


def _user_image(path, size, zoom=1.0, offset=(0.5, 0.5), blur=10):
    """读用户图片：EXIF 摆正、按 cover 裁切、支持缩放和偏移。"""
    target = Path(str(path))
    try:
        stat = target.stat()
    except Exception:
        return None
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    source = _IMAGE_CACHE.get(key)
    if source is None:
        try:
            with Image.open(target) as raw:
                raw.draft("RGB", (MAX_SOURCE, MAX_SOURCE))
                source = ImageOps.exif_transpose(raw).convert("RGB")
        except Exception:
            return None
        if max(source.size) > MAX_SOURCE:
            source = source.copy()
            source.thumbnail((MAX_SOURCE, MAX_SOURCE), Image.LANCZOS)
        if len(_IMAGE_CACHE) >= _IMAGE_CACHE_LIMIT:
            _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
        _IMAGE_CACHE[key] = source

    width, height = size
    scale = max(width / source.width, height / source.height) * max(1.0, float(zoom or 1.0))
    draw_size = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
    try:
        picture = source.resize(draw_size, Image.LANCZOS)
    except Exception:
        return None
    left = int((draw_size[0] - width) * _clamp(float(offset[0]), 0.0, 1.0))
    top = int((draw_size[1] - height) * _clamp(float(offset[1]), 0.0, 1.0))
    picture = picture.crop((left, top, left + width, top + height))
    if blur:
        # 先缩小再模糊再放大：同样的观感，成本低一个数量级
        tiny = picture.resize((max(64, width // 4), max(64, height // 4)), Image.BILINEAR)
        tiny = tiny.filter(ImageFilter.GaussianBlur(max(1, blur // 4)))
        picture = tiny.resize((width, height), Image.BICUBIC)
    return picture


def render(
    size,
    preset=None,
    mode=None,
    image_path="",
    zoom=1.0,
    offset=(0.5, 0.5),
    phase=0.0,
    blur=10,
    dim=None,
):
    """生成一张背景图。返回 PIL.Image，失败返回 None（调用方退回纯色）。"""
    if not available():
        return None
    width, height = _size(size)
    preset_name = str(preset or theme.preset())
    item = theme.PRESETS.get(preset_name) or theme.PRESETS[theme.DEFAULT_PRESET]
    dark = str(mode or theme.active()) == "dark"
    start_hex, end_hex, glow_hex = item["night" if dark else "day"]
    start, end, glow = _to_rgb(start_hex), _to_rgb(end_hex), _to_rgb(glow_hex)

    picture = None
    if image_path:
        picture = _user_image(
            image_path, (width, height), zoom=zoom, offset=offset, blur=int(blur or 0)
        )
    if picture is None:
        picture = _gradient((width, height), start, end, glow, phase=phase)
    else:
        # 用户图片上叠一层主题色调：白天更亮、夜里更沉，文字始终有底
        veil = Image.new("RGB", (width, height), end if dark else start)
        picture = Image.blend(picture, veil, 0.18 if dark else 0.12)
        amount = (0.25 if dark else 0.08) if dim is None else max(0.0, min(0.7, float(dim)))
        if amount:
            picture = Image.blend(picture, Image.new("RGB", (width, height), "#000000"), amount)

    # 顶部到中部加一点纵向明暗，让"玻璃"有环境光
    shade = Image.new("L", (1, height), 0)
    for y in range(height):
        depth = int(70 * (1 - y / float(max(1, height - 1))))
        shade.putpixel((0, y), max(0, min(255, depth)))
    shade = shade.resize((width, height))
    white = Image.new("RGB", (width, height), _to_rgb("#ffffff"))
    picture = Image.composite(white, picture, shade.point(lambda value: int(value * 0.22)))
    return picture


def photo(size, **kwargs):
    """渲染并转成 Tk 可用的 PhotoImage；调用方负责保持引用。"""
    picture = render(size, **kwargs)
    if picture is None:
        return None
    try:
        return ImageTk.PhotoImage(picture)
    except Exception:
        return None


def thumbnail(path, size=(240, 140)):
    """设置页的预览小图。读不出来就返回 None。"""
    if not available():
        return None
    picture = _user_image(path, _size(size), blur=0)
    if picture is None:
        return None
    try:
        return ImageTk.PhotoImage(picture)
    except Exception:
        return None


class BackgroundLayer:
    """铺在窗口最底层的背景。窗口变化时防抖重绘，动效低频重绘。"""

    def __init__(self, master, options=None, logger=None):
        self.master = master
        self.options = options or (lambda: {})
        self.logger = logger
        self.label = tk.Label(master, bd=0, highlightthickness=0, bg=theme.BG)
        self.label.place(x=0, y=0, relwidth=1, relheight=1)
        try:
            self.label.lower()
        except Exception:
            pass
        self._photo = None
        self._picture = None
        self._after = None
        self._motion_after = None
        self._phase = 0.0
        self._last_size = (0, 0)
        self._last_error = ""
        self._destroyed = False
        try:
            master.bind("<Configure>", self._on_configure, add="+")
        except Exception:
            pass
        self.refresh()

    # ---------- 外部接口 ----------
    def refresh(self, immediate=True):
        if self._destroyed:
            return None
        if self._after is not None:
            try:
                self.master.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if immediate:
            return self._render()
        try:
            self._after = self.master.after(_DEBOUNCE_MS, self._render)
        except Exception:
            return None
        return None

    def set_motion(self, level):
        self._schedule_motion()
        return str(level or "off")

    def stop(self):
        self._destroyed = True
        for attr in ("_after", "_motion_after"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    self.master.after_cancel(handle)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self.label.destroy()
        except Exception:
            pass

    def last_error(self):
        return self._last_error

    def picture(self):
        """给卡片四角取真实背景像素；没有渲染结果时返回 None。"""
        return self._picture

    # ---------- 内部 ----------
    def _on_configure(self, event=None):
        if self._destroyed:
            return None
        if event is not None and getattr(event, "widget", None) is not self.master:
            return None
        self.refresh(immediate=False)
        return None

    def _options(self):
        try:
            data = self.options() or {}
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _render(self):
        self._after = None
        if self._destroyed:
            return None
        try:
            width = max(64, self.master.winfo_width())
            height = max(64, self.master.winfo_height())
        except Exception:
            return None
        if width < 120 or height < 120:
            return None
        options = self._options()
        motion = str(options.get("motion") or "off")
        phase = self._phase if motion != "off" else 0.0
        self._last_error = ""
        picture = render(
            (width, height),
            preset=options.get("preset") or theme.preset(),
            mode=options.get("mode") or theme.active(),
            image_path=options.get("image") or "",
            zoom=options.get("zoom") or 1.0,
            offset=(options.get("x", 0.5), options.get("y", 0.5)),
            phase=phase,
            blur=options.get("blur", 10),
            dim=options.get("dim"),
        )
        if picture is None:
            self._last_error = "背景渲染失败，已退回纯色"
            self._picture = None
            try:
                self.label.configure(image="", bg=theme.BG)
            except Exception:
                pass
            self._photo = None
        else:
            try:
                self._picture = picture
                self._photo = ImageTk.PhotoImage(picture)
                self.label.configure(image=self._photo, bg=theme.BG)
            except Exception as exc:
                self._last_error = "背景显示失败：%s" % exc
        self._last_size = (width, height)
        try:
            from ui import glass_native

            glass_native.refresh_rounding()
        except Exception:
            pass
        self._schedule_motion()
        return bool(self._photo)

    def _schedule_motion(self):
        if self._motion_after is not None:
            try:
                self.master.after_cancel(self._motion_after)
            except Exception:
                pass
            self._motion_after = None
        if self._destroyed:
            return None
        level = str(self._options().get("motion") or "off")
        if level == "off":
            return None
        # 任务书要求背景动画周期 20–40 秒：慢，才不抢注意力也不吃 CPU。
        interval = 30000 if level == "low" else 20000
        try:
            self._motion_after = self.master.after(interval, self._tick)
        except Exception:
            self._motion_after = None
        return None

    def _tick(self):
        self._motion_after = None
        if self._destroyed:
            return None
        # 后台/最小化时不画：省电，也不和 Live2D 抢帧
        try:
            if self.master.state() == "iconic" or not self.master.winfo_viewable():
                self._schedule_motion()
                return None
        except Exception:
            pass
        self._phase += 0.9
        self._render()
        return None
