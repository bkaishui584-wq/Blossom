"""外部连接器：要独立进程、SDK、MCP 或外部账号的能力。

这一层只做三件事：说清要什么、检查本机齐不齐、把「真跑起来会执行哪条命令」写明白。
它不装依赖、不下载、不启动服务，也不默认开启。

连接器和技能不是一回事：技能是「怎么做一件事」的说明，连接器是「必须另起一个进程
或连一个外部服务」的那种可执行扩展。所以它单独立类型，不塞进技能里。
"""

from dataclasses import dataclass, field

from extensions.manifest import ExtensionManifest


@dataclass
class ConnectorSpec:
    """一个连接器的说明书。字段都是事实，不做推测。"""

    id: str
    name: str
    summary: str
    homepage: str = ""
    pinned: str = ""
    license: str = ""
    license_status: str = "review-needed"
    command: tuple = ()
    binaries: tuple = ()
    env_keys: tuple = ()
    needs_browser: bool = False
    needs_account: bool = False
    permissions: tuple = ("read", "action")
    notes: tuple = ()
    mobile: bool = False

    def __post_init__(self):
        self.command = tuple(str(item) for item in self.command)
        self.binaries = tuple(str(item) for item in self.binaries)
        self.env_keys = tuple(str(item) for item in self.env_keys)
        self.permissions = tuple(str(item) for item in self.permissions)
        self.notes = tuple(str(item) for item in self.notes)

    def requirements(self):
        """把「要什么」摊成一句一句，界面直接显示。"""
        lines = []
        if self.binaries:
            lines.append("需要命令：%s" % "、".join(self.binaries))
        if self.env_keys:
            lines.append("需要环境变量：%s" % "、".join(self.env_keys))
        if self.needs_browser:
            lines.append("需要浏览器（首次使用要单独装驱动）")
        if self.needs_account:
            lines.append("需要外部账号或密钥")
        if self.command:
            lines.append("真跑起来会执行：%s" % " ".join(self.command))
        return lines

    def plan(self):
        """描述要执行的命令，但绝不在这里执行。"""
        if not self.command:
            return "这个连接器没有登记启动命令。"
        return "本程序不会自己启动它。真要开，得由你确认后手动执行：%s" % " ".join(self.command)

    def health(self):
        return tuple(
            {"kind": "binary", "name": name, "hint": "缺 %s 就跑不起来" % name}
            for name in self.binaries
        )

    def to_manifest(self):
        return ExtensionManifest(
            id=self.id,
            name=self.name,
            kind="connector",
            source="dev",
            summary=self.summary,
            homepage=self.homepage,
            pinned=self.pinned,
            license=self.license or "未声明",
            license_status=self.license_status,
            platforms=("windows", "linux", "darwin"),
            offline=False,
            requires_network=True,
            requires_api_key=bool(self.env_keys) or self.needs_account,
            permissions=self.permissions,
            dependencies=self.requirements(),
            tags=("连接器", "开发专用", "待核验", "默认关闭"),
            health=self.health(),
            notes=self.notes,
            verdict="review",
            verdict_note="外部连接器，要另起进程或连外部服务，默认不开；先确认依赖和账号再决定。",
        )


SPECS = (
    ConnectorSpec(
        id="connector-playwright-mcp",
        name="Playwright 连接器（浏览器自动化）",
        summary="用 Playwright 的 MCP 服务做界面自动化与回归测试，主要给开发工作区用。",
        homepage="https://github.com/microsoft/playwright-mcp",
        pinned="playwright-mcp@latest",
        license="Apache-2.0（Playwright 本体）",
        license_status="review-needed",
        command=("npx", "-y", "@playwright/mcp@latest"),
        binaries=("node", "npx"),
        needs_browser=True,
        notes=(
            "版本没有锁到具体号：上游用 latest 发布，锁号会很快过期，所以标「待核验」。",
            "第一次真要用，需要自己装浏览器驱动；本程序不代装。",
            "打开之后它能在浏览器里点东西、填表单，属于有副作用的操作，必须单独授权。",
        ),
    ),
    ConnectorSpec(
        id="connector-chrome-devtools-mcp",
        name="Chrome DevTools 连接器（页面调试）",
        summary="接 Chrome DevTools 的 MCP 服务，用来调试网页式工作台和前端问题。",
        homepage="https://github.com/ChromeDevTools/chrome-devtools-mcp",
        pinned="chrome-devtools-mcp@latest",
        license="Apache-2.0（chromium 项目惯例，具体以上游为准）",
        license_status="review-needed",
        command=("npx", "-y", "chrome-devtools-mcp@latest"),
        binaries=("node", "npx"),
        needs_browser=True,
        notes=(
            "需要本机已装 Chrome。",
            "它是外部进程，不链接进主程序，也不会在启动时自动拉起。",
        ),
    ),
)


def specs():
    return list(SPECS)


def get(connector_id):
    for spec in SPECS:
        if spec.id == str(connector_id or ""):
            return spec
    return None


def catalog():
    return [spec.to_manifest() for spec in SPECS]


def requirements(connector_id):
    spec = get(connector_id)
    return spec.requirements() if spec is not None else []


def plan(connector_id):
    spec = get(connector_id)
    return spec.plan() if spec is not None else "没有这个连接器。"
