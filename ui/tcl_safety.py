"""别让后台线程替回收站里的 Tk 对象收尾。

Tk 的变量和图片在析构时会伸手进 Tcl，而 Tcl 只认创建它的那个线程。
回复、语音、定时器这些后台线程只要触发一次垃圾回收，就可能在这里撞上
主线程正握着的 Tcl 锁，界面就此卡住不回来。这里把这类析构限回主线程。
"""

import threading
import tkinter

_MAIN = threading.main_thread()


def _guard(cls):
    original = cls.__dict__.get("__del__")
    if original is None or getattr(original, "_blossom_thread_safe", False):
        return

    def __del__(self):
        if threading.current_thread() is not _MAIN:
            # 不是主线程：宁可漏掉一次 Tcl 清理，也不能跨线程伸手。
            return
        try:
            original(self)
        except Exception:
            pass

    __del__._blossom_thread_safe = True
    cls.__del__ = __del__


def install():
    for cls in (tkinter.Variable, tkinter.Image):
        _guard(cls)


install()
