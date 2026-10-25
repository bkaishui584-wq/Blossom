"""Git & Code Review：只读状态与差异，找出值得看一眼的地方。

本 Skill 的工具白名单里没有写操作，提交和推送必须走单独的授权流程。
"""

import re

from skills.library._common import mapping, payload

RISK_PATTERNS = (
    (re.compile(r"^\+\s*print\(", re.MULTILINE), "新增了调试用的 print"),
    (re.compile(r"(password|secret|api_key|token)\s*=\s*[\"'][^\"']{6,}", re.IGNORECASE), "疑似写死了凭据"),
    (re.compile(r"except\s*:\s*$", re.MULTILINE), "裸 except 会吞掉错误"),
    (re.compile(r"TODO|FIXME", re.IGNORECASE), "留下了 TODO/FIXME"),
    (re.compile(r"^\+\s*import pdb|breakpoint\(", re.MULTILINE), "留下了调试断点"),
)


def _text(value):
    if isinstance(value, dict):
        return str(value.get("output") or "")
    return str(value or "")


STATUS_CODES = ("M", "A", "D", "R", "C", "U", "?", "!")


def _looks_like_status(line):
    if len(line) < 2:
        return False
    return line[0] in STATUS_CODES + " " and line[1] in STATUS_CODES + " "


def run(ctx):
    facts = []
    status = ctx.tool("git_read", subcommand="status", args="--short")
    if not status.ok:
        return {"ok": False, "error": "读 Git 状态失败：%s" % status.error}
    data = mapping(payload(status))
    if not isinstance(data, dict):
        return {"ok": False, "error": "Git 返回了看不懂的内容，先不动手。"}
    code = int(data.get("returncode") or 0)
    status_text = str(data.get("output") or "")
    lowered = status_text.lower()
    if code != 0 or "not a git repository" in lowered or "不是 git 仓库" in lowered:
        first = (status_text.strip().splitlines() or ["没有输出"])[0]
        return {
            "ok": False,
            "error": "这里不是 Git 仓库，或者 Git 没装上：%s" % first,
        }
    changed = [line.strip() for line in status_text.splitlines() if _looks_like_status(line)]
    if not changed:
        return {
            "ok": True,
            "summary": "工作区是干净的，没有未提交的改动。",
            "detail": "git status --short 没有输出。",
            "verified": True,
        }
    facts.append("改了 %d 个文件：" % len(changed))
    facts.extend("  " + line for line in changed[:15])

    stat = ctx.tool("git_read", subcommand="diff", args="--stat")
    stat_text = _text(mapping(payload(stat))) if stat.ok else ""
    if stat_text:
        facts.append("\n改动规模：\n" + stat_text[:800])

    diff = ctx.tool("git_read", subcommand="diff")
    diff_text = _text(mapping(payload(diff))) if diff.ok else ""
    risks = []
    for pattern, label in RISK_PATTERNS:
        if pattern.search(diff_text):
            risks.append(label)
    if risks:
        facts.append("\n值得看一眼的地方：" + "、".join(risks))

    log = ctx.tool("git_read", subcommand="log", args="--oneline -5")
    log_text = _text(mapping(payload(log))) if log.ok else ""
    if log_text:
        facts.append("\n最近的提交：\n" + log_text[:400])

    files = [line.split()[-1] for line in changed if line.split()]
    suggestion = "、".join(files[:3]) or "若干文件"
    facts.append("\n建议的提交说明：更新 %s" % suggestion)
    return {
        "ok": True,
        "summary": "工作区有 %d 个文件改动，%s。" % (
            len(changed), "发现 %d 个风险点" % len(risks) if risks else "没发现明显风险"
        ),
        "detail": "\n".join(facts),
        "verified": True,
        "artifacts": files[:10],
    }
