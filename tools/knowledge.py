"""知识库工具：查片段、看索引状态。

两件都只读。真正把目录读进索引由用户在界面上加目录触发，
模型不能自己扩范围，也拿不到没索引过的文件。
"""

from tools.registry import Tool, ToolError


def register_knowledge_tools(registry, service=None):
    """装上知识库工具。service 为空时按「没接入」如实说，不假装能用。"""

    def need():
        if service is None:
            raise ToolError("这一版没有接入知识库。")
        return service

    def search_handler(query="", limit=6):
        svc = need()
        text = str(query or "").strip()
        if not text:
            raise ToolError("要搜什么？把关键词给我。")
        try:
            count = int(float(limit or 6))
        except (TypeError, ValueError):
            count = 6
        rows = svc.search(text, limit=max(1, min(count, 12)))
        if not rows:
            state = svc.describe()
            if not state.get("documents"):
                return "知识库里还没有文档。先在「文件与知识」里加一个目录，扫一遍再来搜。"
            return "在已索引的 %d 份文档里没找到「%s」。" % (state.get("documents"), text)
        lines = ["找到 %d 段：" % len(rows)]
        for row in rows:
            lines.append(
                "- %s（%s）\n  %s\n  %s"
                % (
                    row.get("relative") or row.get("name"),
                    row.get("locator") or "",
                    row.get("path"),
                    row.get("snippet"),
                )
            )
        return "\n".join(lines)

    registry.register(
        Tool(
            name="knowledge_search",
            description="在本地知识库里按关键词找片段，返回文件名、位置和摘录。只读，不联网。",
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜的关键词"},
                    "limit": {"type": "integer", "description": "最多几段，默认 6"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读已索引的文档片段",
            readonly=True,
            handler=search_handler,
            keywords=("知识库", "搜资料", "查文档", "在我文件里找", "检索", "资料"),
            timeout=10.0,
        )
    )

    def status_handler():
        svc = need()
        try:
            state = svc.describe()
        except Exception as exc:
            raise ToolError("读知识库状态时出错：%s" % exc)
        if not state.get("documents"):
            return "知识库还空着：还没加目录。加个目录、扫一遍就能搜了。"
        roots = "、".join(state.get("roots") or []) or "（没有目录）"
        return "知识库：%d 份文档、%d 个片段；索引%s（%s 分词）。目录：%s" % (
            state.get("documents"),
            state.get("chunks"),
            "可用" if state.get("ready") else "不可用",
            state.get("tokenizer") or "默认",
            roots,
        )

    registry.register(
        Tool(
            name="knowledge_status",
            description="看本地知识库的索引状态：文档数、片段数、分词方式、已加的目录。只读。",
            schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            permission="READ",
            risk="只读索引状态",
            readonly=True,
            handler=status_handler,
            keywords=("知识库状态", "索引了多少", "知识库里有啥"),
            timeout=10.0,
        )
    )

    return 2
