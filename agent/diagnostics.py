"""Agent 诊断记录：只留可审计的判断摘要，不留模型的私密推理。

给开发者模式用。普通用户界面不显示这些内容，它们也永远不会发给模型。
"""

import time
from collections import deque


class Diagnostics:
    """环形缓冲。记录本轮走了哪条路、用了什么、为什么跳过。"""

    def __init__(self, limit=60, logger=None):
        self.limit = max(5, int(limit))
        self.entries = deque(maxlen=self.limit)
        self.logger = logger

    def record(self, kind, **fields):
        entry = {"at": time.time(), "kind": str(kind)}
        entry.update(fields)
        self.entries.append(entry)
        return entry

    def recent(self, limit=10):
        rows = list(self.entries)[-max(1, int(limit)):]
        return list(reversed(rows))

    def last(self):
        return self.entries[-1] if self.entries else {}

    def last_of(self, kind):
        for entry in reversed(self.entries):
            if entry.get("kind") == kind:
                return entry
        return {}

    def clear(self):
        self.entries.clear()

    def rows(self):
        """开发者面板用的短行：一行一件事。"""
        last = self.last()
        if not last:
            return [("本轮诊断", "还没有记录")]
        route = last.get("route") or {}
        rows = [
            ("本轮意图", f"{route.get('intent_label', route.get('intent', '-'))}"),
            ("路由置信度", str(route.get("confidence", "-"))),
            ("本轮模式", str(last.get("mode", "-"))),
            ("本地能力", str(last.get("capability") or "未使用")),
            ("工具调用", "、".join(last.get("tools") or []) or "无"),
            ("选取记忆", str(last.get("memory_count", 0)) + " 条"),
            ("上下文估算", str(last.get("estimated_tokens", "-")) + " tokens"),
            ("任务状态", str(last.get("task_state", "-"))),
            ("权限判断", str(last.get("permission") or "无")),
            ("主动行为", str(last.get("proactive") or "无")),
        ]
        return rows
