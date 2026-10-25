"""原生圆角与半透明：窗口裁切、窗口透明度与卡片四角遮罩。"""

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui import glass_native, theme


class TkCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def settle(self, rounds=10):
        for _ in range(rounds):
            try:
                self.root.update_idletasks()
                self.root.update()
            except tk.TclError:
                return
            time.sleep(0.02)


class CornerImageTests(unittest.TestCase):
    def test_outside_wedge_is_opaque_and_inside_stays_clear(self):
        image = glass_native._corner_image(16, "nw", theme.CARD)
        self.assertEqual(image.size, (16, 16))
        self.assertEqual(image.mode, "RGBA")
        self.assertGreater(image.getpixel((0, 0))[3], 240)
        self.assertEqual(image.getpixel((15, 15))[3], 0)

    def test_every_corner_uses_its_own_quarter(self):
        for corner, solid, clear in (
            ("nw", (0, 0), (15, 15)),
            ("ne", (15, 0), (0, 15)),
            ("sw", (0, 15), (15, 0)),
            ("se", (15, 15), (0, 0)),
        ):
            image = glass_native._corner_image(16, corner, theme.CARD)
            self.assertGreater(image.getpixel(solid)[3], 240, corner)
            self.assertEqual(image.getpixel(clear)[3], 0, corner)


class WindowGlassTests(TkCase):
    def test_window_gets_round_region_and_live_alpha(self):
        win = tk.Toplevel(self.root)
        win.geometry("320x220+80+80")
        win.overrideredirect(True)
        win.configure(bg=theme.BG)
        win.deiconify()
        self.settle()
        glass_native.apply_window(win, radius=18, alpha=0.93)
        self.settle()
        self.assertAlmostEqual(float(win.wm_attributes("-alpha")), 0.93, places=2)
        self.assertEqual(getattr(win, "_glass_region_size", None)[2], 18)
        win.destroy()

    def test_round_widget_installs_four_corner_masks(self):
        win = tk.Toplevel(self.root)
        win.geometry("320x220+80+80")
        win.configure(bg=theme.BG)
        card = theme.card(win)
        card.pack(fill="both", expand=True, padx=20, pady=20)
        win.deiconify()
        self.settle()
        self.assertEqual(len(card._glass_corner_labels), 4)
        self.assertEqual(len(getattr(card, "_glass_corner_photos", {})), 4)
        win.destroy()

    def test_card_radius_is_taken_from_the_card_option(self):
        frame = theme.card(self.root, radius=8)
        self.assertEqual(frame._glass_corner_radius, 8)


if __name__ == "__main__":
    unittest.main()
