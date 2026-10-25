"""Agent 运行时适配。

现有 Agent Core 是默认引擎。Pi 之类的运行时是并列的可选实现，不替换核心。

一条硬规则：外部运行时调用工具，必须经过 DeskPet 的权限层。
适配器自己不做特权决定；拿不到权限闸门就直接拒绝执行工具。
"""


class RuntimeAdapter:
    name = "runtime"
    label = "运行引擎"

    def available(self):
        return False

    def describe(self):
        return {"name": self.name, "label": self.label, "available": self.available()}

    def stream(self, prompt, session_id=""):
        raise NotImplementedError

    def cancel(self):
        return False


class BuiltinRuntime(RuntimeAdapter):
    """现有 Agent Core 的包装。默认引擎，离线也能跑到哪算哪。"""

    name = "builtin"
    label = "内置 Agent 运行引擎"

    def __init__(self, agent=None):
        self.agent = agent

    def available(self):
        return self.agent is not None

    def describe(self):
        data = super().describe()
        data["offline"] = True
        data["tools_via"] = "permission"
        return data

    def stream(self, prompt, session_id=""):
        if self.agent is None:
            yield {"type": "error", "text": "没有可用的 Agent"}
            return
        try:
            reply = self.agent.respond(session_id or "cli", str(prompt or ""))
        except Exception as exc:
            yield {"type": "error", "text": "对话失败：%s" % exc}
            return
        text = getattr(reply, "text", None)
        if text is None and isinstance(reply, dict):
            text = reply.get("text") or reply.get("content")
        yield {"type": "text", "text": str(text or "")}
        yield {"type": "done", "ok": bool(getattr(reply, "ok", True))}

    def cancel(self):
        return False


class PiRuntimeAdapter(RuntimeAdapter):
    """Pi 的可选适配。

    它必须靠 Node.js 和一个可用模型才跑得起来；没有就把话说清楚。
    工具调用一律交给 DeskPet 的权限闸门，闸门不在就拒绝执行。
    """

    name = "pi"
    label = "Pi（可选运行引擎）"

    def __init__(self, node_bin="", sdk=None, permission_gate=None, logger=None):
        self.node_bin = node_bin
        self.sdk = sdk
        self.permission_gate = permission_gate
        self.logger = logger
        self.last_error = ""
        self._cancelled = False

    def available(self):
        import shutil

        if self.sdk is not None:
            return True
        if not self.node_bin or shutil.which(self.node_bin) is None:
            self.last_error = "没有 Node.js 运行时"
            return False
        self.last_error = "还没装 Pi 本体"
        return False

    def describe(self):
        data = super().describe()
        data["offline"] = False
        data["requires_model"] = True
        data["tools_via"] = "permission"
        data["error"] = self.last_error
        return data

    def can_run_tools(self):
        return callable(self.permission_gate)

    def check_tool(self, name, level="", params=None):
        """工具能不能跑，由 DeskPet 的权限中心说了算。"""
        if not self.can_run_tools():
            return False, "没有接入权限中心，外部运行时不准调工具"
        try:
            allowed = self.permission_gate(name, level, params)
        except Exception as exc:
            return False, "权限检查失败：%s" % exc
        if isinstance(allowed, tuple):
            return bool(allowed[0]), str(allowed[1] if len(allowed) > 1 else "")
        return bool(allowed), "" if allowed else "权限中心拒绝了这次调用"

    def stream(self, prompt, session_id=""):
        if not self.available():
            yield {"type": "unavailable", "text": "Pi 现在用不了：" + (self.last_error or "环境不齐")}
            return
        yield {"type": "error", "text": "Pi 适配器只搭了接口，还没做端到端验证"}

    def cancel(self):
        self._cancelled = True
        return True


def build_runtime(registry=None, agent=None):
    """挑一个运行引擎：Pi 能用就用它，否则用内置的。"""
    if registry is not None:
        adapter = PiRuntimeAdapter(node_bin="node")
        if registry.is_enabled("pi") and adapter.available():
            return adapter
    return BuiltinRuntime(agent)
