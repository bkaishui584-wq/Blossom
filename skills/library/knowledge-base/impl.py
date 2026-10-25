"""Knowledge Base：在本机索引好的资料里找片段。

只调用只读的 knowledge_search，自己不经手任何文件。
技能本身不获得权限：能不能搜、搜哪些目录，都由知识库服务和权限中心决定。
"""


def run(ctx):
    query = str(ctx.inputs.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "要搜什么？把关键词给我。"}

    outcome = ctx.tool("knowledge_search", query=query)
    if not getattr(outcome, "ok", False):
        return {"ok": False, "error": str(getattr(outcome, "error", "") or "没搜成")}

    body = str(getattr(outcome, "result", "") or "").strip()
    return {
        "ok": True,
        "summary": body,
        "detail": "结果来自本地索引，每条都带文件名和位置，能回原文。",
        "verified": True,
    }
