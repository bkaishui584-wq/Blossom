"""本地运行时状态工具：装了什么、还差什么，只读报出来。

探测本身不装东西、不下载、不启动服务。没有模型时这条工具照样能用，
它就是回答「我现在能不能用本地模型／离线语音」的那一份真实答案。
"""

from adapters import local_runtimes
from tools.registry import Tool


def register_runtime_tools(registry, config=None, detect=None):
    """装上本地能力查询。detect 可注入，测试用得上。"""
    probe = detect or local_runtimes.detect

    def abilities_text(what="all"):
        kind = str(what or "all").strip().lower()
        base = getattr(config, "data_root", "") or ""
        payload = (
            probe(base)
            if detect is not None
            else local_runtimes.detect(base, config=config)
        )
        entries = payload.get("entries") or []
        if kind not in ("all", "", "全部"):
            entries = [item for item in entries if item.get("kind") == kind]
            if not entries:
                return "这个分类下没有可探测的组件。"
        return local_runtimes.text({"entries": entries})

    registry.register(
        Tool(
            name="local_ability_status",
            description="查本机装了哪些本地能力组件：本地模型、文档解析、离线语音、屏幕理解、全文检索。只读，不下载也不安装。",
            schema={
                "type": "object",
                "properties": {
                    "what": {
                        "type": "string",
                        "enum": [
                            "all",
                            "local-model",
                            "document",
                            "voice",
                            "vision",
                            "search",
                            "index",
                            "render",
                        ],
                        "description": "只看哪一类，默认全部",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读本机组件状态",
            readonly=True,
            handler=abilities_text,
            summarize=lambda payload: str(payload or "").strip() or None,
            keywords=(
                "本地模型",
                "离线语音",
                "装了哪些",
                "本地能力",
                "ollama",
                "能不能离线",
                "文档解析",
                "全文检索",
            ),
            timeout=8.0,
        )
    )
