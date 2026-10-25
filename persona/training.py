"""人格训练端口。

当前只收集样本，不做任何训练：没有训练算法、没有微调、不会自动改人格。
以后用户训练好的人格通过 PersonaProvider 接入（见 persona/interface.py）。
"""

import json
from abc import ABC, abstractmethod

KINDS = ("dialogue", "feedback", "correction", "preference", "behavior")

KIND_LABELS = {
    "dialogue": "对话样本",
    "feedback": "用户反馈",
    "correction": "用户纠正",
    "preference": "偏好",
    "behavior": "行为案例",
}


class PersonalityTrainingInterface(ABC):
    """未来训练人格的接入点。训练内容见 KINDS。"""

    @abstractmethod
    def collect(self, kind, payload, source="USER_EXPLICIT"):
        """收集一条样本。"""

    @abstractmethod
    def export(self, limit=200):
        """导出样本，交给用户的训练流程。"""

    @abstractmethod
    def status(self):
        """样本统计。"""


class LocalTrainingCollector(PersonalityTrainingInterface):
    """本地样本收集器：只写进 SQLite，不训练、不改人格。"""

    version = 1

    def __init__(self, database, logger=None):
        self.database = database
        self.logger = logger

    def collect(self, kind, payload, source="USER_EXPLICIT"):
        name = str(kind or "").strip()
        if name not in KINDS:
            return None
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        if not text.strip():
            return None
        return self.database.insert(
            "INSERT INTO persona_samples (kind, content, source) VALUES (?, ?, ?)",
            (name, text, source),
        )

    def export(self, limit=200):
        rows = self.database.query(
            "SELECT kind, content, source, created_at FROM persona_samples"
            " ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return {"version": self.version, "samples": rows}

    def status(self):
        rows = self.database.query(
            "SELECT kind, COUNT(*) AS n FROM persona_samples GROUP BY kind"
        )
        counts = {row["kind"]: row["n"] for row in rows}
        return {"total": sum(counts.values()), "kinds": counts, "training": False}
