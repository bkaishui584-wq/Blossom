"""扩展清单：一个扩展是什么、从哪来、要什么权限、能不能进发行包。

清单只描述事实。它自己不会执行任何东西，也不会因为清单里写了什么就去装依赖。
"""

from dataclasses import dataclass, field

KINDS = ("skill", "tool", "runtime", "memory", "voice", "vision", "provider", "plugin", "connector")
KIND_LABELS = {
    "skill": "技能",
    "tool": "工具",
    "runtime": "运行引擎",
    "memory": "记忆后端",
    "voice": "语音",
    "vision": "视觉",
    "provider": "服务提供方",
    "plugin": "插件",
    "connector": "连接器",
}

# 受众：这套扩展是给最终用户用的，还是只在开发工作区里露面的。
# 开发专用的默认不进发行包，也不在普通用户的主界面里推。
AUDIENCES = ("user", "developer")
AUDIENCE_LABELS = {"user": "面向最终用户", "developer": "开发者专用"}

# 核验结论：给外面导入进来的东西一个明确说法，不能含糊成「好像能用」。
# importable 只表示内容能安全导入，不等于已经启用。
VERDICTS = ("", "importable", "review", "unsuitable")
VERDICT_LABELS = {
    "": "未评估",
    "importable": "可导入",
    "review": "待核验",
    "unsuitable": "不适合本机接入",
}

SOURCES = ("builtin", "optional", "dev")
SOURCE_LABELS = {"builtin": "内置", "optional": "可选扩展", "dev": "开发专用"}

PERMISSION_LEVELS = ("none", "read", "personal", "action", "system")
PERMISSION_LABELS = {
    "none": "不需要权限",
    "read": "读公开内容",
    "personal": "读个人数据",
    "action": "有副作用的操作",
    "system": "系统级操作",
}

LICENSE_STATUSES = ("approved", "review-needed", "blocked", "unknown")
LICENSE_LABELS = {
    "approved": "许可证已审阅",
    "review-needed": "许可证待审阅",
    "blocked": "许可证不允许随包分发",
    "unknown": "许可证不明",
}

# 这三类只在自己的进程里跑，不许链接进主程序。
ISOLATED_KINDS = ("runtime", "memory", "voice", "vision")


def _tuple(value):
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item) for item in value)


@dataclass
class ExtensionManifest:
    """一个扩展的清单。默认值偏保守：不联网、不要密钥、不进发行包。"""

    id: str
    name: str
    kind: str = "provider"
    source: str = "optional"
    summary: str = ""
    homepage: str = ""
    pinned: str = ""
    license: str = ""
    license_status: str = "unknown"
    platforms: tuple = ("windows",)
    offline: bool = True
    requires_network: bool = False
    requires_model: bool = False
    requires_api_key: bool = False
    requires_admin: bool = False
    permissions: tuple = ()
    dependencies: tuple = ()
    install_size: str = ""
    scripts: tuple = ()
    health: tuple = ()
    tags: tuple = ()
    notes: tuple = ()
    bundled: bool = False
    audience: str = ""
    verdict: str = ""
    verdict_note: str = ""

    def __post_init__(self):
        self.platforms = _tuple(self.platforms)
        self.permissions = _tuple(self.permissions)
        self.dependencies = _tuple(self.dependencies)
        self.scripts = _tuple(self.scripts)
        self.tags = _tuple(self.tags)
        self.notes = _tuple(self.notes)
        # health 是字典列表，这里不做字符串化。
        self.health = tuple(dict(item) for item in (self.health or ()))
        # 没写受众就跟来源走：开发来源一律算开发者专用。
        self.audience = str(self.audience or ("developer" if self.source == "dev" else "user"))

    # ---------- 校验 ----------
    def validate(self):
        """返回这份清单的问题清单。空列表表示没问题。"""
        errors = []
        if not str(self.id or "").strip():
            errors.append("缺少 id")
        if not str(self.name or "").strip():
            errors.append("缺少 name")
        if self.kind not in KINDS:
            errors.append("kind 不在允许范围：%s" % self.kind)
        if self.source not in SOURCES:
            errors.append("source 不在允许范围：%s" % self.source)
        if self.license_status not in LICENSE_STATUSES:
            errors.append("license_status 不在允许范围：%s" % self.license_status)
        if self.audience not in AUDIENCES:
            errors.append("audience 不在允许范围：%s" % self.audience)
        if self.verdict not in VERDICTS:
            errors.append("verdict 不在允许范围：%s" % self.verdict)
        if self.verdict in ("review", "unsuitable") and not self.verdict_note:
            errors.append("标了待核验或不适用的扩展要写清原因")
        for level in self.permissions:
            if level not in PERMISSION_LEVELS:
                errors.append("权限等级不认识：%s" % level)
        if self.source == "builtin" and not self.bundled:
            errors.append("内置扩展必须声明 bundled")
        if self.source in ("optional", "dev") and self.bundled:
            errors.append("只有内置扩展才能进发行包")
        if self.requires_admin and "system" not in self.permissions:
            errors.append("要管理员权限就得声明 system 权限")
        return errors

    # ---------- 能不能用 ----------
    def license_blocked(self):
        return self.license_status == "blocked"

    def needs_review(self):
        return self.license_status in ("review-needed", "unknown")

    def bundle_blocked(self):
        """能不能随安装包分发。没审过的一律不进包。"""
        if self.license_blocked():
            return "许可证不允许随包分发"
        if self.verdict == "unsuitable":
            return "核验结论是不适用，不进发行包"
        if self.source == "dev" or self.audience == "developer":
            return "开发专用，不进发行包"
        if self.needs_review():
            return "许可证还没审阅"
        return ""

    def highest_permission(self):
        order = {name: index for index, name in enumerate(PERMISSION_LEVELS)}
        if not self.permissions:
            return "none"
        return max(self.permissions, key=lambda item: order.get(item, 0))

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "summary": self.summary,
            "homepage": self.homepage,
            "pinned": self.pinned,
            "license": self.license,
            "license_status": self.license_status,
            "license_label": LICENSE_LABELS.get(self.license_status, self.license_status),
            "platforms": list(self.platforms),
            "offline": bool(self.offline),
            "requires_network": bool(self.requires_network),
            "requires_model": bool(self.requires_model),
            "requires_api_key": bool(self.requires_api_key),
            "requires_admin": bool(self.requires_admin),
            "permissions": list(self.permissions),
            "dependencies": list(self.dependencies),
            "install_size": self.install_size,
            "scripts": list(self.scripts),
            "health": [dict(item) for item in self.health],
            "tags": list(self.tags),
            "notes": list(self.notes),
            "bundled": bool(self.bundled),
            "audience": self.audience,
            "audience_label": AUDIENCE_LABELS.get(self.audience, self.audience),
            "verdict": self.verdict,
            "verdict_label": VERDICT_LABELS.get(self.verdict, self.verdict),
            "verdict_note": self.verdict_note,
        }

    def to_row(self):
        """给界面卡片用的紧凑视图。"""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "summary": self.summary,
            "pinned": self.pinned,
            "license": self.license,
            "license_status": self.license_status,
            "license_label": LICENSE_LABELS.get(self.license_status, self.license_status),
            "permission": self.highest_permission(),
            "permission_label": PERMISSION_LABELS.get(self.highest_permission(), self.highest_permission()),
            "requires_network": bool(self.requires_network),
            "requires_model": bool(self.requires_model),
            "requires_api_key": bool(self.requires_api_key),
            "offline": bool(self.offline),
            "platforms": list(self.platforms),
            "install_size": self.install_size,
            "has_scripts": bool(self.scripts),
            "bundled": bool(self.bundled),
            "tags": list(self.tags),
            "audience": self.audience,
            "audience_label": AUDIENCE_LABELS.get(self.audience, self.audience),
            "verdict": self.verdict,
            "verdict_label": VERDICT_LABELS.get(self.verdict, self.verdict),
            "verdict_note": self.verdict_note,
        }
