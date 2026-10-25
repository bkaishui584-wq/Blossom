"""记忆后端适配。

现有 SQLite 记忆是默认后端，不动它。Cognee 之类的外部记忆平台只作为可选来源：
装了、开了、环境齐了才接；没装就如实说没装。

导入既有聊天记录必须由用户选范围，先给预览，再给撤销。不默认全量导入。
"""


class MemoryBackend:
    """记忆后端的接口。谁来当后端都得守这个形状。"""

    name = "memory"
    label = "记忆后端"

    def available(self):
        return False

    def describe(self):
        return {"name": self.name, "label": self.label, "available": self.available()}

    def remember(self, text, **meta):
        raise NotImplementedError

    def recall(self, query, limit=8, **meta):
        raise NotImplementedError

    def improve(self, memory_id, note=""):
        raise NotImplementedError

    def forget(self, memory_id=""):
        raise NotImplementedError


class LocalMemoryBackend(MemoryBackend):
    """现有记忆系统的包装。默认就是它，离线可用。"""

    name = "local"
    label = "本地记忆（SQLite）"

    def __init__(self, memory=None):
        self.memory = memory
        self.last_error = ""

    def available(self):
        return self.memory is not None

    def describe(self):
        data = super().describe()
        data["store"] = "sqlite"
        data["offline"] = True
        return data

    def remember(self, text, **meta):
        if self.memory is None:
            return {"ok": False, "reason": "没有可用的记忆系统"}
        try:
            result = self.memory.remember(text, **meta) if hasattr(self.memory, "remember") else None
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "reason": "记忆写入失败：%s" % exc}
        return {"ok": True, "result": result, "source": "local"}

    def recall(self, query, limit=8, **meta):
        if self.memory is None:
            return {"ok": False, "reason": "没有可用的记忆系统", "items": []}
        try:
            rows = self.memory.search(query, limit=limit) if hasattr(self.memory, "search") else []
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "reason": "记忆检索失败：%s" % exc, "items": []}
        items = []
        for row in rows or []:
            if isinstance(row, dict):
                items.append(dict(row))
            else:
                items.append({"text": str(row)})
        return {"ok": True, "items": items, "source": "local"}

    def improve(self, memory_id, note=""):
        if self.memory is None or not hasattr(self.memory, "update"):
            return {"ok": False, "reason": "本地记忆不支持就地改进"}
        try:
            self.memory.update(memory_id, note)
        except Exception as exc:
            return {"ok": False, "reason": "改进失败：%s" % exc}
        return {"ok": True, "source": "local"}

    def forget(self, memory_id=""):
        if self.memory is None or not hasattr(self.memory, "forget"):
            return {"ok": False, "reason": "本地记忆不支持删除接口"}
        try:
            self.memory.forget(memory_id)
        except Exception as exc:
            return {"ok": False, "reason": "删除失败：%s" % exc}
        return {"ok": True, "source": "local"}


class CogneeAdapter(MemoryBackend):
    """Cognee 的可选适配。

    这个类不会为了「能用」就去装依赖、下载模型或导入聊天记录。
    没装就是没装，调用一律返回明确的失败原因。
    """

    name = "cognee"
    label = "Cognee（外部长期记忆）"

    def __init__(self, module=None, settings=None, logger=None):
        self.module = module
        self.settings = dict(settings or {})
        self.logger = logger
        self.last_error = ""
        self._dataset = ""

    def _cognee(self):
        if self.module is not None:
            return self.module
        try:
            import cognee  # noqa: F401  只有装了才走得到这里

            self.module = cognee
        except Exception as exc:
            self.last_error = "没装 cognee：%s" % exc
            self.module = None
        return self.module

    def available(self):
        return self._cognee() is not None

    def describe(self):
        data = super().describe()
        data["offline"] = False
        data["dataset"] = self._dataset
        data["note"] = "可选后端，不覆盖现有记忆；导入要用户选范围"
        return data

    def _refuse(self, action):
        return {"ok": False, "reason": "Cognee 不可用（%s 需要它）：%s" % (action, self.last_error or "没装")}

    def remember(self, text, dataset="", **meta):
        cognee = self._cognee()
        if cognee is None:
            return self._refuse("remember")
        if not str(text or "").strip():
            return {"ok": False, "reason": "没有要记的内容"}
        try:
            cognee.add(str(text), dataset_name=dataset or self._dataset or "blossom")
            return {"ok": True, "source": "cognee", "dataset": dataset or self._dataset}
        except Exception as exc:
            return {"ok": False, "reason": "Cognee 写入失败：%s" % exc}

    def recall(self, query, limit=8, dataset="", **meta):
        cognee = self._cognee()
        if cognee is None:
            return self._refuse("recall")
        try:
            found = cognee.search(str(query), datasets=[dataset or self._dataset] if (dataset or self._dataset) else None)
        except Exception as exc:
            return {"ok": False, "reason": "Cognee 检索失败：%s" % exc, "items": []}
        items = []
        for row in found or []:
            items.append({"text": str(row), "source": "cognee"})
        return {"ok": True, "items": items[: max(1, int(limit or 8))], "source": "cognee"}

    def improve(self, memory_id, note=""):
        cognee = self._cognee()
        if cognee is None:
            return self._refuse("improve")
        return {"ok": False, "reason": "Cognee 的改进要先有用户反馈，这里只留接口"}

    def forget(self, memory_id=""):
        cognee = self._cognee()
        if cognee is None:
            return self._refuse("forget")
        try:
            cognee.prune.prune_data()
        except Exception as exc:
            return {"ok": False, "reason": "Cognee 删除失败：%s" % exc}
        return {"ok": True, "source": "cognee"}

    def plan_import(self, session_count, scope=""):
        """导入既有聊天记录前先给一份计划，让用户看清楚要动什么。"""
        return {
            "ok": True,
            "scope": scope or "未选",
            "session_count": int(session_count or 0),
            "dataset": "blossom-import",
            "steps": ["建独立数据集", "先导 20 条做预览", "用户确认", "批量导入", "保留撤销入口"],
            "warning": "现有 SQLite 记忆不会被替换或清空。",
        }


def build_memory_backend(registry=None, memory=None):
    """挑一个记忆后端：Cognee 能用就用它，否则用本地。"""
    if registry is not None:
        adapter = CogneeAdapter()
        if registry.is_enabled("cognee") and adapter.available():
            return adapter
    return LocalMemoryBackend(memory)
