"""Research Agent：有来源才下结论。没有来源就说没有。"""

from skills.library._common import payload

FACT_LIMIT = 5


def run(ctx):
    topic = str(ctx.inputs.get("topic") or ctx.inputs.get("question") or ctx.request or "").strip()
    out = ctx.tool("get_news", topic=topic[:40], limit=FACT_LIMIT)
    if not out.ok:
        return {
            "ok": True,
            "summary": "没有可验证的来源，我不给结论。",
            "detail": "\n".join(
                [
                    "来源服务没成功：%s" % out.error,
                    "事实：暂无",
                    "推断：暂无",
                    "未知：没有来源就无法核验，也就不能给结论。",
                    "下一步：把资料贴给我，或者配好新闻服务的 Key 再试。",
                ]
            ),
            "verified": False,
        }
    data = mapping(payload(out))
    items = data.get("items") or []
    titles = [str(item.get("title") or "").strip() for item in items]
    titles = [title for title in titles if title]
    if not titles:
        return {
            "ok": True,
            "summary": "服务通了，但没返回任何条目，所以给不出结论。",
            "detail": "事实：暂无\n推断：暂无\n未知：这一轮没有可核验的内容。",
            "verified": False,
        }
    facts = "\n".join("- %s" % title for title in titles)
    detail = "\n".join(
        [
            "事实（来自检索到的标题）：",
            facts,
            "",
            "推断：以上标题只能说明「有人在这样报道」，不能证明内容本身正确。",
            "",
            "未知：没有读到原文，也没有第二来源交叉验证。",
        ]
    )
    return {
        "ok": True,
        "summary": "拿到 %d 条来源标题，只报事实，不下结论。" % len(titles),
        "detail": detail,
        "verified": True,
    }
