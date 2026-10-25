"""File & Knowledge：搜本地文件、读内容、必要时记成项目笔记。默认只读。"""

from skills.library._common import mapping, payload

STOPWORDS = ("帮我", "找一下", "搜一下", "找找", "的", "了", "吗", "？", "?", "一下")


def _query(request):
    text = str(request or "").strip()
    for word in STOPWORDS:
        text = text.replace(word, " ")
    return " ".join(text.split())


def run(ctx):
    query = str(ctx.inputs.get("query") or "").strip() or _query(ctx.request)
    if not query:
        return {
            "ok": False,
            "error": "要给我一个查找内容，比如一个函数名、一段话或者文件名。",
        }
    base = str(ctx.inputs.get("path") or "")
    found = ctx.tool("search_files", pattern=query.split()[0], path=base, limit=20)
    if not found.ok:
        return {"ok": False, "error": "搜索没成功：%s" % found.error}
    data = payload(found)
    matches = mapping(data).get("matches") or []
    if not matches:
        return {
            "ok": True,
            "summary": "没找到和「%s」相关的内容。" % query,
            "detail": "换一个更具体的词（函数名、变量名、报错原文）通常更容易命中。",
            "verified": True,
        }
    files = []
    for item in matches:
        name = str(item.get("file"))
        if name not in files:
            files.append(name)
    top = files[0]
    listing = []
    read = ctx.tool("read_file", path=top, max_chars=2000)
    if read.ok:
        blob = payload(read)
        listing.append(
            "最相关文件 %s 的开头：\n%s"
            % (top, str(mapping(blob).get("content") or "")[:500])
        )
    lines = "\n".join(
        "- %s:%s %s" % (item.get("file"), item.get("line"), item.get("text"))
        for item in matches[:12]
    )
    detail = "\n".join(
        [
            "命中 %d 处，涉及 %d 个文件。" % (len(matches), len(files)),
            lines,
            "",
            *listing,
        ]
    )
    remembered = False
    if ctx.inputs.get("remember") and ctx.memory is not None:
        note = "在 %s 里，%s 出现在 %s" % (top, query, ", ".join(files[:5]))
        try:
            ctx.memory.remember(note, kind="project", importance=2)
            remembered = True
        except Exception:
            remembered = False
    if remembered:
        detail += "\n\n已把这次检索记成项目记忆。"
    return {
        "ok": True,
        "summary": "找到 %d 处，最相关的是 %s。" % (len(matches), top),
        "detail": detail,
        "verified": True,
        "artifacts": files[:5],
    }
