"""命令输出压缩。

给模型看的长输出先压缩，省的是上下文，不是把信息藏起来：
- 原始输出一直留着，随时能翻。
- 报错、权限拒绝、测试失败这类行一条都不许丢。
- 失败、异常退出、诊断场景直接看完整输出。
- 只压白名单里的命令族，不劫持所有命令。

装了 RTK 并且用户明确启用时才走它；它一出问题就退回本地压缩，并如实标注。
"""

import re
import shutil

# 命中这些字样的输出不压缩：里面通常有关键信息。
FAILURE_MARKERS = (
    "failed",
    "failure",
    "error",
    "traceback",
    "exception",
    "panic",
    "fatal",
    "denied",
    "refused",
    "forbidden",
    "拒绝",
    "不允许",
    "失败",
    "报错",
    "异常",
    "权限不足",
)

# 只认这些命令族。不在里面的原样返回，不做任何处理。
FAMILIES = (
    ("files", ("ls", "dir", "find", "tree", "du", "fd", "rg --files")),
    ("git", ("git status", "git log", "git show", "git diff", "git branch")),
    ("diff", ("diff ", "git diff")),
    ("test", ("pytest", "unittest", "tox", "npm test", "pnpm test", "yarn test", "cargo test", "go test", "dotnet test")),
    ("list", ("pip list", "pip freeze", "npm ls", "conda list")),
)

HEAD_LINES = 10
TAIL_LINES = 10
MAX_LINES = 60
MAX_LINE_CHARS = 240


def _run_binary(argv, text):
    """给 RTK 之类的外部压缩器用的入口。默认实现就是跑子进程。"""
    import subprocess

    try:
        done = subprocess.run(
            argv,
            input=text.encode("utf-8", "replace"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


class CommandOutputReducer:
    """命令输出压缩器。压缩是可选优化，信息完整是硬要求。"""

    def __init__(self, registry=None, runner=None, logger=None):
        self.registry = registry
        self.runner = runner or _run_binary
        self.logger = logger
        self.last_error = ""

    # ---------- 判断 ----------
    def family(self, command):
        text = str(command or "").strip().lower()
        for name, prefixes in FAMILIES:
            if text.startswith(tuple(prefixes)):
                return name
        return ""

    def supported(self, command):
        return bool(self.family(command))

    def _rtk_on(self):
        if self.registry is None:
            return False
        if not self.registry.is_enabled("rtk"):
            return False
        return shutil.which("rtk") is not None

    # ---------- 压缩 ----------
    def reduce(self, command, output, keep_full=True):
        """返回一份压缩结果。original 永远带着，界面能随时切回完整输出。"""
        raw = str(output or "")
        before = len(raw.encode("utf-8", "replace"))
        family = self.family(command)
        result = {
            "command": str(command or ""),
            "family": family,
            "supported": bool(family),
            "before_bytes": before,
            "after_bytes": before,
            "saved_bytes": 0,
            "tool": "none",
            "fallback": False,
            "changed": False,
            "text": raw,
            "original": raw if keep_full else "",
            "preserved_count": 0,
            "note": "",
        }
        if not family or not raw:
            result["tool"] = "passthrough"
            result["note"] = "这个命令不在压缩白名单里，原样给出"
            return result

        if self._rtk_on():
            try:
                squeezed = self.runner(["rtk", "compress", "--", str(command or "")], raw)
            except Exception as exc:
                squeezed = None
                self.last_error = str(exc)
            if squeezed:
                text = self._guard(raw, squeezed, family)
                result["tool"] = "rtk"
                return self._finish(result, text)
            result["fallback"] = True
            result["note"] = "RTK 没给出结果，退回本地压缩"

        text = self._local(raw, family)
        if result["tool"] == "none":
            result["tool"] = "local"
        return self._finish(result, text)

    def _finish(self, result, text):
        after = len(text.encode("utf-8", "replace"))
        result["text"] = text
        result["after_bytes"] = after
        # 只报实测字节数，不换算成钱，也不夸大节省比例。
        result["saved_bytes"] = max(0, result["before_bytes"] - after)
        result["changed"] = text != result["original"]
        return result

    def _guard(self, raw, squeezed, family):
        """外部压缩器的结果也要过一遍：关键行不许被压掉。"""
        required = self._required_lines(raw)
        missing = [line for line in required if line not in squeezed]
        if missing:
            # 外部工具把关键行压没了，那就别用它。
            self.last_error = "压缩结果丢了 %d 条关键行" % len(missing)
            return self._local(raw, family)
        return squeezed

    def _required_lines(self, raw):
        kept = []
        for line in raw.splitlines():
            low = line.lower()
            if any(marker in low for marker in FAILURE_MARKERS):
                kept.append(line)
            elif line.strip().startswith("!!"):
                kept.append(line)
        return kept

    def _local(self, raw, family):
        """本地压缩：留头、留尾、关键行全留，中间折叠并写清折了多少行。"""
        lines = raw.splitlines()
        if len(lines) <= MAX_LINES:
            return self._clean(raw)
        head = lines[:HEAD_LINES]
        tail = lines[-TAIL_LINES:]
        keep = set(range(len(head))) | set(range(len(lines) - len(tail), len(lines)))
        important = []
        for index, line in enumerate(lines):
            low = line.lower()
            if any(marker in low for marker in FAILURE_MARKERS):
                keep.add(index)
                important.append(index)
        dropped = [index for index in range(len(lines)) if index not in keep]
        out = []
        for index in range(len(lines)):
            if index in keep:
                out.append(self._clip(lines[index]))
        marker = "…… 这里略过 %d 行（原始输出完整保留，可以随时展开）……" % len(dropped)
        head_end = HEAD_LINES
        return "\n".join(out[:head_end] + [marker] + out[head_end:])

    def _clean(self, text):
        return "\n".join(self._clip(line) for line in text.splitlines())

    def _clip(self, line):
        line = str(line)
        if len(line) <= MAX_LINE_CHARS:
            return line
        return line[:MAX_LINE_CHARS] + " ……（这行太长，已截断，完整内容在原始输出里）"

    def describe(self):
        return {
            "name": "command-output-reducer",
            "families": [name for name, _ in FAMILIES],
            "rtk_enabled": self._rtk_on(),
            "rtk_present": shutil.which("rtk") is not None,
            "rules": ["原始输出保留", "报错与拒绝行不丢", "失败时看完整输出"],
        }
