"""P3-1：视觉检测事件层（YOLO 接入位）。

只验证：没接检测源时安静、白名单与置信度过滤、
出现/消失只发一次事件、出错不影响整体。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior.detections import (
    CLEAR_AFTER_MISSES,
    DEFAULT_LABELS,
    DEFAULT_MIN_CONFIDENCE,
    DetectionProvider,
    DetectionWatcher,
    MockDetectionProvider,
    NullDetectionProvider,
)
from events import bus as bus_module
from events.bus import EventBus


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)

    def info(self, message, *args):
        pass


class ScriptedProvider(DetectionProvider):
    """脚本化的检测源，只用于测试。"""

    name = "scripted"

    def __init__(self, detections=(), available=True, boom=False, ok=True):
        self.detections = list(detections)
        self.available = available
        self.boom = boom
        self.ok = ok
        self.calls = 0

    def is_available(self):
        return self.available

    def detect(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("camera exploded")
        return {"ok": self.ok, "detections": list(self.detections), "error": ""}


def watcher(provider, bus=None, **kwargs):
    bus = bus if bus is not None else EventBus()
    seen = []
    for name in (bus_module.VISION_DETECTED, bus_module.VISION_CLEARED):
        bus.subscribe(name, lambda event: seen.append(event))
    return DetectionWatcher(provider=provider, bus=bus, **kwargs), seen


class ProviderTests(unittest.TestCase):
    def test_null_provider_sees_nothing(self):
        provider = NullDetectionProvider()
        self.assertFalse(provider.is_available())
        self.assertFalse(provider.detect()["ok"])
        self.assertEqual(provider.describe()["provider"], "null")

    def test_mock_provider_replays_script(self):
        provider = MockDetectionProvider([{"label": "cat", "confidence": 0.9}])
        self.assertTrue(provider.is_mock)
        self.assertTrue(provider.is_available())
        self.assertEqual(provider.detect()["detections"][0]["label"], "cat")
        self.assertEqual(provider.calls, 1)

    def test_defaults_are_conservative(self):
        self.assertIn("person", DEFAULT_LABELS)
        self.assertGreaterEqual(DEFAULT_MIN_CONFIDENCE, 0.5)
        self.assertGreaterEqual(CLEAR_AFTER_MISSES, 2)


class FilterTests(unittest.TestCase):
    def test_whitelist_keeps_known_labels_only(self):
        probe, _ = watcher(ScriptedProvider([{"label": "cat", "confidence": 0.9}]))
        self.assertEqual(probe.update(), [bus_module.VISION_DETECTED])
        probe2, seen2 = watcher(
            ScriptedProvider([{"label": "unknown_thing", "confidence": 0.9}])
        )
        self.assertEqual(probe2.update(), [])
        self.assertEqual(seen2, [])
        self.assertEqual(probe2.dropped_count, 1)

    def test_low_confidence_is_dropped(self):
        probe, seen = watcher(ScriptedProvider([{"label": "person", "confidence": 0.2}]))
        self.assertEqual(probe.update(), [])
        self.assertEqual(seen, [])
        self.assertEqual(probe.dropped_count, 1)

    def test_garbage_items_are_dropped(self):
        probe, _ = watcher(
            ScriptedProvider(["cat", {"confidence": "high"}, {"label": "cup"}])
        )
        self.assertEqual(probe.update(), [])
        self.assertEqual(probe.dropped_count, 3)

    def test_confidence_is_taken_from_the_best_hit(self):
        probe, seen = watcher(
            ScriptedProvider(
                [
                    {"label": "cat", "confidence": 0.6},
                    {"label": "cat", "confidence": 0.95},
                ]
            )
        )
        probe.update()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["confidence"], 0.95)

    def test_custom_labels_and_threshold(self):
        probe, seen = watcher(
            ScriptedProvider([{"label": "cup", "confidence": 0.3}]),
            labels=("cup",),
            min_confidence=0.1,
        )
        self.assertEqual(probe.update(), [bus_module.VISION_DETECTED])
        self.assertEqual(seen[0].payload["label"], "cup")


class EventTests(unittest.TestCase):
    def test_appearing_fires_once(self):
        provider = ScriptedProvider([{"label": "person", "confidence": 0.8}])
        probe, seen = watcher(provider)
        self.assertEqual(probe.update(), [bus_module.VISION_DETECTED])
        self.assertEqual(probe.update(), [])
        self.assertEqual(probe.detected_count, 1)
        self.assertEqual(len(seen), 1)

    def test_clearing_needs_two_misses(self):
        provider = ScriptedProvider([{"label": "person", "confidence": 0.8}])
        probe, seen = watcher(provider)
        probe.update()
        provider.detections = []
        self.assertEqual(probe.update(), [])
        self.assertEqual(probe.update(), [bus_module.VISION_CLEARED])
        self.assertEqual(probe.visible, {})
        self.assertEqual(len(seen), 2)

    def test_reappearing_fires_again(self):
        provider = ScriptedProvider([{"label": "dog", "confidence": 0.7}])
        probe, _ = watcher(provider)
        probe.update()
        provider.detections = []
        probe.update()
        probe.update()
        provider.detections = [{"label": "dog", "confidence": 0.7}]
        self.assertEqual(probe.update(), [bus_module.VISION_DETECTED])
        self.assertEqual(probe.detected_count, 2)

    def test_broken_provider_stays_quiet(self):
        logger = FakeLogger()
        probe, seen = watcher(ScriptedProvider(boom=True), logger=logger)
        self.assertEqual(probe.update(), [])
        self.assertEqual(seen, [])
        self.assertEqual(len(logger.warnings), 1)

    def test_unavailable_provider_stays_quiet(self):
        probe, seen = watcher(
            ScriptedProvider([{"label": "cat", "confidence": 0.9}], ok=False)
        )
        self.assertEqual(probe.update(), [])
        self.assertEqual(seen, [])
        self.assertIsNone(probe.last_at)

    def test_no_bus_still_counts(self):
        probe = DetectionWatcher(
            provider=ScriptedProvider([{"label": "cat", "confidence": 0.9}])
        )
        self.assertEqual(probe.update(), [])
        self.assertEqual(probe.detected_count, 1)

    def test_bus_failure_is_contained(self):
        class BrokenBus:
            def emit(self, *args, **kwargs):
                raise RuntimeError("bus gone")

        logger = FakeLogger()
        probe = DetectionWatcher(
            provider=ScriptedProvider([{"label": "cat", "confidence": 0.9}]),
            bus=BrokenBus(),
            logger=logger,
        )
        self.assertEqual(probe.update(), [])
        self.assertEqual(len(logger.warnings), 1)

    def test_provider_probe_failure_is_contained(self):
        class Broken(DetectionProvider):
            name = "broken"

            def is_available(self):
                raise RuntimeError("no probe")

            def detect(self):
                return {"ok": False, "detections": []}

        logger = FakeLogger()
        probe = DetectionWatcher(provider=Broken(), logger=logger)
        self.assertFalse(probe.available())
        self.assertEqual(len(logger.warnings), 1)


class DescribeTests(unittest.TestCase):
    def test_describe_lists_what_it_saw(self):
        probe, _ = watcher(ScriptedProvider([{"label": "cat", "confidence": 0.9}]))
        probe.update()
        info = probe.describe()
        self.assertEqual(info["provider"], "scripted")
        self.assertEqual(info["visible"], ["cat"])
        self.assertEqual(info["detected_count"], 1)
        self.assertEqual(info["cleared_count"], 0)
        self.assertIn("min_confidence", info)


class BoundaryTests(unittest.TestCase):
    def test_module_uses_no_third_party_or_camera(self):
        lines = (
            ROOT / "behavior" / "detections.py"
        ).read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            self.assertTrue(
                stripped.startswith(
                    ("import time", "import abc", "from abc", "from events")
                ),
                stripped,
            )
        text = "\n".join(lines)
        for word in ("cv2", "ultralytics", "torch", "numpy", "pyserial", "socket"):
            self.assertNotIn(word, text)

    def test_agent_core_stays_out_of_vision(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8")
        for word in ("detection", "DetectionWatcher", "VISION_DETECTED", "cv2"):
            self.assertNotIn(word, text)

    def test_event_names_are_stable(self):
        self.assertEqual(bus_module.VISION_DETECTED, "vision.detected")
        self.assertEqual(bus_module.VISION_CLEARED, "vision.cleared")

    def test_desktop_wires_the_watcher(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("DetectionWatcher(provider=NullDetectionProvider()", main)
        desktop = (ROOT / "ui" / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("DETECTION_MS", desktop)
        self.assertIn("_detection_tick", desktop)
        self.assertIn("detection=detection", desktop)


if __name__ == "__main__":
    unittest.main()
