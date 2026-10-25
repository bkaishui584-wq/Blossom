"""自带的扩展清单目录。

里面的版本号和许可证是按 2026-10-13 那天从上游仓库读到的真实值写死的：
锁定 commit、记录许可证、标出能不能进发行包。不追最新代码，也不猜许可证。

审阅结论说明：
- approved：许可证明确允许，可以评估接入；要不要随包分发还要看具体用法。
- review-needed：许可证缺失、写了「Other」或者一仓多证，必须逐个确认后才能进包。
- blocked：许可证与商业分发冲突，代码不得链接、复制或随包发布。
"""

from extensions.manifest import ExtensionManifest
from extensions.connectors import catalog as connector_catalog
from extensions.qoder_catalog import qoder_catalog

# 读这些仓库的日期。写进清单是为了让人能核对，不是自动更新。
AUDITED_ON = "2026-10-13"


def _builtin_adapters():
    """DeskPet 自己的适配器：这些是已经装在产品里的东西。"""
    return [
        ExtensionManifest(
            id="blossom.memory.local",
            name="本地记忆（SQLite）",
            kind="memory",
            source="builtin",
            summary="产品自带的记忆后端：分层记忆、摘要、偏好都在这套里，离线可用。",
            homepage="",
            pinned="builtin",
            license="DeskPet 自有代码",
            license_status="approved",
            offline=True,
            permissions=("personal",),
            tags=("本地", "离线", "默认启用"),
            notes=("现有记忆数据不搬不换；换后端只是多一个可选来源。",),
            bundled=True,
        ),
        ExtensionManifest(
            id="blossom.runtime.agent",
            name="内置 Agent 运行引擎",
            kind="runtime",
            source="builtin",
            summary="现有 Agent Core：路由、工具调用、权限确认都在这一层，没模型时退到本地能力。",
            homepage="",
            pinned="builtin",
            license="DeskPet 自有代码",
            license_status="approved",
            offline=True,
            permissions=("action",),
            tags=("本地", "默认启用"),
            bundled=True,
        ),
        ExtensionManifest(
            id="blossom.voice.system",
            name="系统语音（离线）",
            kind="voice",
            source="builtin",
            summary="用系统自带的朗读与本地识别，不联网、不要密钥。",
            homepage="",
            pinned="builtin",
            license="DeskPet 自有代码",
            license_status="approved",
            offline=True,
            permissions=("personal",),
            tags=("本地", "离线"),
            bundled=True,
        ),
        ExtensionManifest(
            id="blossom.vision.uia",
            name="屏幕语义（UI Automation）",
            kind="vision",
            source="builtin",
            summary="只读 Windows 公开的可访问性信息拿应用名和窗口标题，不截图、不做视觉推断。",
            homepage="",
            pinned="builtin",
            license="DeskPet 自有代码",
            license_status="approved",
            offline=True,
            platforms=("windows",),
            permissions=("read",),
            tags=("本地", "默认关闭"),
            bundled=True,
        ),
        ExtensionManifest(
            id="blossom.tools.reducer",
            name="命令输出压缩",
            kind="tool",
            source="builtin",
            summary="长命令输出先压缩再给模型看，原始输出随时能翻出来，失败和拒绝照原样保留。",
            homepage="",
            pinned="builtin",
            license="DeskPet 自有代码",
            license_status="approved",
            offline=True,
            permissions=("read",),
            tags=("本地",),
            bundled=True,
        ),
    ]


def _upstream():
    """要评估的五个上游项目。锁定到 2026-10-13 读到的 commit。"""
    return [
        ExtensionManifest(
            id="cognee",
            name="Cognee（长期记忆后端）",
            kind="memory",
            source="optional",
            summary="给你的 Agent 一套持久长期记忆：保存、检索、改进、删除，带来源追踪。",
            homepage="https://github.com/topoteretes/cognee",
            pinned="0ec7a9fa61c9",
            license="Apache-2.0",
            license_status="approved",
            platforms=("windows", "linux", "darwin"),
            offline=False,
            requires_network=True,
            permissions=("personal", "action"),
            dependencies=("Python 3.10+", "按需的向量与图数据库依赖"),
            install_size="按数据量和后端不同，百 MB 起",
            health=({"kind": "module", "name": "cognee", "hint": "装了 cognee 才能接"},),
            tags=("记忆", "联网", "待接入"),
            notes=(
                "只做可选后端：不覆盖现有记忆数据库，也不自动导入聊天记录。",
                "导入必须由用户选范围，先建独立测试数据集，切换默认后端要等检索质量和删除正确性验证通过。",
                "本地模式首次使用可能下载模型，必须先把下载内容、大小和存放位置讲清楚。",
            ),
        ),
        ExtensionManifest(
            id="pi",
            name="Pi（可选 Agent 运行引擎）",
            kind="runtime",
            source="optional",
            summary="另一套 Agent 运行时：流式事件、工具调用、会话与任务上下文。",
            homepage="https://github.com/earendil-works/pi",
            pinned="a4d040d30f2c",
            license="MIT",
            license_status="approved",
            platforms=("windows", "linux", "darwin"),
            offline=False,
            requires_network=True,
            requires_model=True,
            requires_api_key=True,
            permissions=("action",),
            dependencies=("Node.js 运行时",),
            install_size="几十到上百 MB（不含模型）",
            health=(
                {"kind": "binary", "name": "node", "hint": "没有 Node.js 就用不了"},
                {"kind": "module", "name": "pi", "hint": "还没装 Pi 本体"},
            ),
            tags=("运行引擎", "需模型", "待接入"),
            notes=(
                "和现有 Agent 并列，不替换核心 Agent。",
                "默认不启用；先拿模拟运行时和虚拟工具验证接口。",
                "装 Pi 不等于有了本地推理能力，没有可用模型时退回现有 Agent。",
            ),
        ),
        ExtensionManifest(
            id="rtk",
            name="RTK（命令输出压缩）",
            kind="tool",
            source="optional",
            summary="压缩常见开发命令的输出，少烧一点 token。单文件 Rust 程序。",
            homepage="https://github.com/rtk-ai/rtk",
            pinned="ffa565134129",
            license="Apache-2.0",
            license_status="approved",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("read",),
            dependencies=("rtk 可执行文件",),
            install_size="单文件，几 MB",
            health=({"kind": "binary", "name": "rtk", "hint": "没装 rtk 就走本地压缩，功能不受影响"},),
            tags=("开发", "离线", "可选"),
            notes=(
                "只对白名单里的命令族用，不劫持所有命令，也不改用户的 shell 配置。",
                "原始输出始终保留；失败、异常退出和诊断场景直接看完整输出。",
                "不靠截断隐藏权限拒绝、安全错误和测试失败。",
            ),
        ),
        ExtensionManifest(
            id="ten-framework",
            name="TEN Framework（实时语音）",
            kind="voice",
            source="optional",
            summary="实时对话语音框架：VAD、轮次检测、低延迟语音事件。",
            homepage="https://github.com/TEN-framework/ten-framework",
            pinned="c6cbeebbf9a3",
            license="上游写的是 Other（需逐项确认）",
            license_status="review-needed",
            platforms=("linux", "darwin", "windows"),
            offline=False,
            requires_network=True,
            requires_api_key=True,
            permissions=("personal", "action"),
            dependencies=("示例依赖里含容器与外部语音服务",),
            install_size="示例环境很大，必须单独装",
            tags=("语音", "联网", "许可证待审", "待接入"),
            notes=(
                "示例里的 API Key、外部账户、语音服务都不作为 DeskPet 的必需依赖。",
                "不能靠语音扩展绕开麦克风权限和隐私开关。",
                "许可证逐项确认之前不随包分发。",
            ),
        ),
        ExtensionManifest(
            id="whisper-cpp",
            name="whisper.cpp（本地语音识别）",
            kind="voice",
            source="optional",
            summary="本地跑语音识别，不联网、不要密钥。",
            homepage="https://github.com/ggml-org/whisper.cpp",
            pinned="0c7be6963710",
            license="MIT",
            license_status="approved",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("personal",),
            dependencies=("识别模型文件（自己下）",),
            install_size="程序小，模型按档位几百 MB 到 1.5 GB",
            health=({"kind": "binary", "name": "whisper-cli", "hint": "没装可执行文件就用不上"},),
            tags=("语音", "离线", "可选"),
            notes=("模型权重单独授权，不进发行包；下载由用户自己决定。",),
        ),
        ExtensionManifest(
            id="lite-ai-toolkit",
            name="Lite.AI.ToolKit（本地视觉推理）",
            kind="vision",
            source="optional",
            summary="C++ 视觉模型工具箱：检测、分割等 100 多个模型。",
            homepage="https://github.com/xlite-dev/lite.ai.toolkit",
            pinned="22ebd42daaaf",
            license="GPL-3.0",
            license_status="blocked",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("personal",),
            dependencies=("MNN / ONNX Runtime / TensorRT",),
            install_size="编译产物与模型合计较大",
            tags=("视觉", "GPL", "不得随包分发"),
            notes=(
                "GPL-3.0 与商业分发冲突：审查通过之前不得链接进主程序、不得复制进发行包。",
                "单独进程或可选下载并不自动消除许可义务。",
                "屏幕感知优先走 Windows UI Automation 拿语义，视觉模型只当按需补充。",
            ),
        ),
        ExtensionManifest(
            id="flaui-mcp",
            name="FlaUI-MCP（桌面自动化辅助）",
            kind="tool",
            source="optional",
            summary="通过 UI Automation 读 Windows 桌面控件，开发与测试辅助用。",
            homepage="https://github.com/shanselman/FlaUI-MCP",
            pinned="da1d2a24eae6",
            license="MIT",
            license_status="approved",
            platforms=("windows",),
            offline=True,
            permissions=("read", "action", "system"),
            dependencies=(".NET 运行时", "MCP 客户端"),
            install_size="几十 MB",
            health=({"kind": "binary", "name": "dotnet", "hint": "没有 .NET 就跑不起来"},),
            tags=("屏幕", "自动化", "仅 Windows", "待接入"),
            notes=("只读语义优先；能改界面的操作必须走权限确认。",),
        ),
    ]


def _skills_sources():
    """技能来源。开发用的不进普通用户的发行包。"""
    return [
        ExtensionManifest(
            id="agent-skills-format",
            name="Agent Skills 格式（说明文件约定）",
            kind="skill",
            source="dev",
            summary="技能说明文件的写法约定：SKILL.md、必需元数据、按需加载。",
            homepage="https://agentskills.io/home",
            pinned="",
            license="格式说明，不是代码依赖",
            license_status="review-needed",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("none",),
            tags=("技能", "格式"),
            notes=("只当格式参考用；不整仓复制，也不当代码引入。",),
        ),
        ExtensionManifest(
            id="frontend-agent-skills",
            name="Frontend Agent Skills（界面与体验）",
            kind="skill",
            source="dev",
            summary="前端体验与视觉质量的技能集合。",
            homepage="https://github.com/hueyexe/frontend-agent-skills",
            pinned="2841c079dd8a",
            license="MIT",
            license_status="approved",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("none",),
            tags=("技能", "界面", "开发专用"),
            notes=("逐个技能过一遍再引用，不整仓复制。",),
        ),
        ExtensionManifest(
            id="superpowers",
            name="Superpowers（开发方法论技能）",
            kind="skill",
            source="dev",
            summary="成体系的开发流程技能：排错、测试优先、代码审查。",
            homepage="https://github.com/obra/superpowers",
            pinned="bb92a7774141",
            license="MIT",
            license_status="approved",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("none",),
            tags=("技能", "开发专用"),
            notes=("开发工作区用，不进普通用户安装包。",),
        ),
        ExtensionManifest(
            id="anthropic-skills",
            name="Anthropic Skills（技能样例）",
            kind="skill",
            source="dev",
            summary="公开的 Agent Skills 样例集合。",
            homepage="https://github.com/anthropics/skills",
            pinned="dbd4588f9e10",
            license="仓库未声明统一许可证",
            license_status="review-needed",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            permissions=("none",),
            tags=("技能", "许可证待审", "开发专用"),
            notes=("一仓多证，必须逐个技能和资源确认，不得整仓无差别复制。",),
        ),
        ExtensionManifest(
            id="mcp-servers",
            name="MCP Servers（参考服务端集合）",
            kind="provider",
            source="optional",
            summary="Model Context Protocol 的官方服务端集合。",
            homepage="https://github.com/modelcontextprotocol/servers",
            pinned="bbb701e7ac1e",
            license="仓库写着 Other，各服务端许可证不同",
            license_status="review-needed",
            platforms=("windows", "linux", "darwin"),
            offline=False,
            requires_network=True,
            permissions=("read", "action"),
            tags=("协议", "许可证待审", "待接入"),
            notes=("按需挑单个服务端，逐个查许可证和权限，不整仓引入。",),
        ),
    ]


def builtin_catalog():
    """返回本轮锁定的完整清单：自带适配器 + 待评估上游 + 技能来源 + Qoder 核验登记。"""
    return (
        _builtin_adapters()
        + _upstream()
        + _skills_sources()
        + qoder_catalog()
        + connector_catalog()
    )


def catalog_by_kind():
    result = {}
    for manifest in builtin_catalog():
        result.setdefault(manifest.kind, []).append(manifest)
    return result
