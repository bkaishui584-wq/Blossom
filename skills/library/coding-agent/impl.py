"""Coding Agent：先看清项目，再提最小改动，改完回读复核。

拿不到改动内容就只报告事实。工具被拒就如实说被拒。
"""

import os
import re

from skills.library._common import mapping, payload

PATH_RE = re.compile(
    r"[\w\-./\\]+\.(py|js|ts|tsx|jsx|json|md|txt|html|css|sql|yml|yaml|c|h|cpp|hpp|ino)"
)
SKIP_DIRS = {"__pycache__", ".git", "dist", "build", "node_modules", ".venv", "venv", "logs"}


def _target(request):
    match = PATH_RE.search(str(request or ""))
    return match.group(0) if match else ""


def _entries(value):
    if isinstance(value, dict):
        return list(value.get("entries") or [])
    return []


def _names(entries, kind=None):
    return [
        str(item.get("name"))
        for item in entries
        if isinstance(item, dict) and (kind is None or item.get("type") == kind)
    ]


def _has_tests(workspace):
    return bool(workspace) and os.path.isdir(os.path.join(workspace, "tests"))


def run(ctx):
    facts = []
    info = mapping(payload(ctx.tool("workspace_info")))
    workspace = ""
    if isinstance(info, dict):
        workspace = str(info.get("workspace") or "")
        facts.append(
            "工作区：%s（%s）" % (workspace, "Git 仓库" if info.get("is_git") else "非 Git 仓库")
        )

    top = mapping(payload(ctx.tool("list_dir", path="")))
    entries = _entries(top)
    if entries:
        dirs = _names(entries, "dir")
        files = _names(entries, "file")
        facts.append("顶层：%d 个目录、%d 个文件" % (len(dirs), len(files)))
        if dirs:
            facts.append("目录：" + "、".join(dirs[:10]))
        if files:
            facts.append("文件：" + "、".join(files[:10]))

    target = str(ctx.inputs.get("target") or "") or _target(ctx.request)
    excerpt = ""
    if target:
        read = ctx.tool("read_file", path=target, max_chars=4000)
        if read.ok:
            data = mapping(payload(read))
            if data:
                excerpt = str(data.get("content") or "")
                facts.append(
                    "读了 %s：%d 行%s" % (
                        data.get("path"), data.get("lines", 0),
                        "（已截断）" if data.get("truncated") else "",
                    )
                )
        else:
            facts.append("没读到 %s：%s" % (target, read.error))
        stem = os.path.splitext(os.path.basename(target))[0]
        if stem:
            found = ctx.tool("search_files", pattern=stem, limit=15)
            if found.ok:
                data = mapping(payload(found))
                matches = (data or {}).get("matches") or []
                files = sorted({str(item.get("file")) for item in matches})
                facts.append(
                    "引用 %s 的位置：%d 处%s" % (
                        stem, len(matches), ("（" + "、".join(files[:6]) + "）") if files else "",
                    )
                )
            else:
                facts.append("引用搜索没成功：%s" % found.error)

    change_path = str(ctx.inputs.get("change_path") or "")
    content = ctx.inputs.get("change_content")
    applied = False
    verified = False
    if change_path and content is not None:
        written = ctx.tool(
            "write_file",
            path=change_path,
            content=str(content),
            overwrite=bool(ctx.inputs.get("overwrite")),
        )
        if not written.ok:
            facts.append("写入没有完成：%s" % written.error)
        else:
            applied = True
            data = mapping(payload(written))
            head = str(content)[:60]
            back = ctx.tool("read_file", path=change_path, max_chars=1200)
            on_disk = ""
            if back.ok:
                disk = mapping(payload(back))
                on_disk = str(disk.get("content") or "")
            verified = bool(head) and head in on_disk
            facts.append(
                "写了 %s（%s 字），回读%s" % (
                    change_path, (data or {}).get("chars", 0),
                    "一致" if verified else "没对上，需要人工确认",
                )
            )
            diff = ctx.tool("git_read", subcommand="diff", args="--stat")
            if diff.ok:
                body = str(mapping(payload(diff)).get("output") or "")
                if body:
                    facts.append("git diff --stat：\n" + str(body)[:400])

    test_command = str(ctx.inputs.get("test_command") or "")
    if test_command or (applied and ctx.inputs.get("run_tests")):
        command = test_command or "python -m unittest discover -s tests"
        result = ctx.tool("run_tests", command=command)
        if result.ok:
            data = mapping(payload(result))
            passed = bool(data.get("passed"))
            facts.append("测试%s：%s" % ("通过" if passed else "没通过", command))
            tail = str(data.get("output") or "").strip().splitlines()[-4:]
            if tail:
                facts.append("\n".join(tail))
            verified = verified and passed
        else:
            facts.append("测试没跑成：%s" % result.error)

    if applied:
        summary = "改了 %s，%s。" % (change_path, "回读一致" if verified else "还没验证")
    elif target:
        summary = "看完了 %s，给出的是事实清单，没有改任何文件。" % target
    else:
        summary = "看了项目结构，没有指定要改的文件，所以没有动任何文件。"

    detail = "\n".join(facts)
    if excerpt and not applied:
        detail += "\n\n目标文件开头：\n" + excerpt[:800]
    if _has_tests(workspace) and not applied:
        detail += "\n\n仓库里有 tests/，要跑测试我会请求权限：python -m unittest discover -s tests"
    return {
        "ok": True,
        "summary": summary,
        "detail": detail,
        "verified": verified,
        "artifacts": [change_path] if applied else [],
    }
