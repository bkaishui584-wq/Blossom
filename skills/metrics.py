"""轻量计时：UI 响应、Skill 加载、工具调用和 Token 成本都要看得见。

只留最近若干次样本，不落库，不联网。
"""

import time
from contextlib import contextmanager


class Timings:
    MAX_SAMPLES = 200

    def __init__(self):
        self._samples = {}

    def record(self, label, seconds):
        bucket = self._samples.setdefault(str(label), [])
        bucket.append(float(seconds))
        if len(bucket) > self.MAX_SAMPLES:
            del bucket[0 : len(bucket) - self.MAX_SAMPLES]
        return seconds

    @contextmanager
    def timed(self, label):
        started = time.monotonic()
        try:
            yield
        finally:
            self.record(label, time.monotonic() - started)

    def stats(self, label):
        samples = sorted(self._samples.get(str(label), []))
        if not samples:
            return {"count": 0, "avg": 0.0, "max": 0.0, "p95": 0.0}
        index = int(len(samples) * 0.95) - 1
        return {
            "count": len(samples),
            "avg": round(sum(samples) / len(samples), 4),
            "max": round(samples[-1], 4),
            "p95": round(samples[max(0, min(index, len(samples) - 1))], 4),
        }

    def snapshot(self):
        return {label: self.stats(label) for label in sorted(self._samples)}

    def clear(self):
        self._samples.clear()
