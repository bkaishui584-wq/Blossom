"""Office Agent：读文档、写结果。写入前查目标，禁止静默覆盖。"""

import re

from skills.library._common import mapping, payload

PATH_RE = re.compile(
    r"[\w\-./\\]+\.(md|txt|csv|json|docx|xlsx|pptx|pdf|html|rst)"
)


def run(ctx):
    path = str(ctx.inputs.get("path") or "").strip()
    if not path:
        found = PATH_RE.search(str(ctx.request or ""))
        path = found.group(0) if found else ""
    if not path:
        return {"ok": False, "error": "要读哪个文件？把路径给我。"}
    read = ctx.tool("read_document", path=path, max_chars=4000)
    facts = []
    if not read.ok:
        lines = [
            "读不了 %s：%s" % (path, read.error),
            "现在支持：md、txt、csv、json、docx、xlsx、pptx、pdf。",
            "pdf 只做最朴素的文本流尝试，扫描件抽不出正文。",
        ]
        out_path = str(ctx.inputs.get("out_path") or "")
        if not out_path:
            return {"ok": False, "error": "\n".join(lines)}
        facts.append(lines[0])
    else:
        data = mapping(payload(read))
        body = str(data.get("content") or "")
        facts.append("读了 %s（%s，%d 字）" % (data.get("path"), data.get("format"), len(body)))
        facts.append("开头：\n" + body[:600])

    out_path = str(ctx.inputs.get("out_path") or "")
    content = ctx.inputs.get("content")
    if out_path and content is not None:
        written = ctx.tool(
            "write_file",
            path=out_path,
            content=str(content),
            overwrite=bool(ctx.inputs.get("overwrite")),
        )
        if not written.ok:
            return {
                "ok": False,
                "error": "写不了 %s：%s\n目标已存在时必须显式允许覆盖。" % (out_path, written.error),
            }
        verified = bool(written.verified)
        back = ctx.tool("read_document", path=out_path, max_chars=400)
        if back.ok:
            blob = mapping(payload(back))
            verified = bool(str(content)[:40]) and str(content)[:40] in str(blob.get("content") or "")
        facts.append("写了 %s，回读%s" % (out_path, "一致" if verified else "没对上"))
        return {
            "ok": True,
            "summary": "写了 %s。" % out_path,
            "detail": "\n".join(facts),
            "verified": verified,
            "artifacts": [out_path],
        }
    if out_path and content is None:
        return {
            "ok": False,
            "error": "给了输出路径但没给内容，我先不写。",
            "summary": "\n".join(facts),
        }
    return {
        "ok": True,
        "summary": "读了 %s，没有写任何文件。" % path,
        "detail": "\n".join(facts),
        "verified": True,
    }
