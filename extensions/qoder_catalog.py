"""Qoder 市场技能的核验登记。

用户给的三个链接在 2026-10-14 用公开接口（免登录）拉过详情和 SKILL.md，
结论按实际取到的内容写死在这里。P1/P2 的候选只登记，不假装能用。

三件事必须说清：
1. 核验结论来自真实抓取，不是看市场介绍猜的。
2. 带脚本的一律隔离：清单里只记脚本路径，导入时不搬、任何环节都不执行。
3. 接口不提供许可证字段。凡是没有随包声明的，一律按「待审阅」处理，不进发行包。

原始抓取产物留在 %TEMP%\\qoder_stage\\<id>\\ 下，随时可复核。
"""

from extensions.manifest import ExtensionManifest

AUDITED_ON = "2026-10-14"
MARKET = "https://qoder.com/zh/marketplace/skill?id=%s"


def _skill_url(skill_id):
    return MARKET % skill_id


def _user_skills():
    """用户点名要接的三个技能。逐项有结论，不能含糊成「先放着」。"""
    return [
        ExtensionManifest(
            id="qoder-self-improving-agent-next",
            name="自我改进智能体（self-improving-agent-next）",
            kind="skill",
            source="optional",
            summary="把命令失败、用户纠正、能力缺口记成可复用的记录，形成持续改进闭环。说明文件为主的纯 Markdown 工作流。",
            homepage=_skill_url("official_37w9JSkb"),
            pinned="official_37w9JSkb@1.0.0",
            license="未声明",
            license_status="review-needed",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            requires_network=False,
            permissions=("read",),
            dependencies=("无额外依赖，说明文件即可读",),
            install_size="几十 KB",
            scripts=(
                "hooks/openclaw/handler.js",
                "hooks/openclaw/handler.ts",
            ),
            verdict="importable",
            audience="user",
            tags=("技能", "Qoder", "自我改进", "带脚本-已隔离"),
            notes=(
                "内容检查通过：SKILL.md 是纯说明，可以安全导入，已经导入到本机技能目录。",
                "整包自动检查会把这一条判成待核验，因为包里另有脚本；人工复核的结论是说明可用、脚本隔离。",
                "包里的 hooks/ 与 scripts/ 是 OpenClaw 钩子脚本，只登记不加载、不复制、不执行。",
                "作者 weiyunbyte，分类 devops，市场安装量 48。",
            ),
        ),
        ExtensionManifest(
            id="qoder-openvino-minicpm-pet",
            name="本地 MiniCPM 桌宠（OpenVINO）",
            kind="plugin",
            source="optional",
            summary="OpenBMB 的部署脚本：把 MiniCPM-Desk-Pet 这套另一个桌宠装到 Intel AIPC 上，不是本项目的技能说明。",
            homepage=_skill_url("official_YWzdk3I1"),
            pinned="official_YWzdk3I1@1.0.0",
            license="未声明（上游 OpenBMB/MiniCPM-Desk-Pet 需另行核）",
            license_status="review-needed",
            platforms=("windows",),
            offline=False,
            requires_network=True,
            requires_model=True,
            permissions=("action", "system"),
            dependencies=(
                "Intel AIPC 硬件（LNL/ARL/PTL/WCL）",
                "Python 3.11、Node 18+、npm、git",
                "首次下载约 2.4 GB",
            ),
            install_size="约 2.4 GB（含模型与 Electron 缓存）",
            scripts=(
                "scripts/run.ps1",
                "scripts/run.cmd",
                "scripts/server.py",
            ),
            verdict="unsuitable",
            verdict_note="要求 Intel AIPC 硬件、首次下载约 2.4 GB，而且它部署的是另一个桌宠应用（MiniCPM Desk Pet），不适合本机接入。",
            audience="developer",
            tags=("Qoder", "部署脚本", "不适合本机", "带脚本-已隔离"),
            notes=(
                "核验结论：不导入、不执行。",
                "理由是硬件门槛（Intel AIPC）、约 2.4 GB 下载，而且它部署的是另一个桌宠应用。",
                "说明里要求用 ExecutionPolicy Bypass 跑 PowerShell 再装 npm 依赖，本机不照做。",
            ),
        ),
        ExtensionManifest(
            id="qoder-self-improving-agent-tmp",
            name="自我改进智能体（临时版）",
            kind="skill",
            source="optional",
            summary="与 self-improving-agent-next 同源的英文版，内容更长、规则更细，同样带 OpenClaw 钩子脚本。",
            homepage=_skill_url("official_2Wz77lhr"),
            pinned="official_2Wz77lhr@1.0.0",
            license="未声明",
            license_status="review-needed",
            platforms=("windows", "linux", "darwin"),
            offline=True,
            requires_network=False,
            permissions=("read",),
            dependencies=("无额外依赖，说明文件即可读",),
            install_size="几十 KB",
            scripts=(
                "hooks/openclaw/handler.js",
                "hooks/openclaw/handler.ts",
            ),
            verdict="review",
            audience="developer",
            tags=("技能", "Qoder", "内容重复", "带脚本-已隔离"),
            notes=(
                "核验结论：与 official_37w9JSkb 功能重叠，建议只留一个。",
                "带脚本，未核验许可证；等确认过再决定要不要导入。",
            ),
            verdict_note="与 official_37w9JSkb 内容重复且带脚本，先记为待核验，不启用。",
        ),
    ]


def _candidates():
    """P1/P2 的候选。2026-10-14 逐个查过详情、内容与上游许可证。

    结论口径：
    - 可导入：内容检查通过（单个 SKILL.md，无脚本），且不依赖外部服务。
    - 待核验：依赖外部服务或账号、带脚本、或许可证查不到。
    - 许可证另列一条：查得到就写清楚，查不到就按待审阅，绝不猜。
    """
    entries = [
        dict(
            ident="qoder-ui-ux-pro-max",
            name="UI/UX Pro Max",
            summary="界面与交互规则集合：样式、配色、字体搭配和可访问性检查清单。",
            skill_id="official_drDKPmtw",
            author="nextlevelbuilder",
            repo="nextlevelbuilder/ui-ux-pro-max-skill",
            license="MIT",
            license_status="approved",
            verdict="importable",
            offline=True,
            network=False,
            notes=(
                "内容检查通过：单个 SKILL.md，没有脚本，没有外部地址。",
                "许可证 MIT，来自上游仓库的 LICENSE 文件。",
            ),
        ),
        dict(
            ident="qoder-superpowers-dev",
            name="Superpowers（技能用法约定）",
            summary="约定「开始任务前先找技能」的元技能，用来统一开发工作区的流程。",
            skill_id="official_R9CquhmS",
            author="aRustyDev",
            repo="arustydev/ai",
            license="AGPL-3.0",
            license_status="blocked",
            verdict="review",
            verdict_note="内容本身干净，但上游是 AGPL-3.0，先过许可证审查再谈启用。",
            offline=True,
            network=False,
            notes=(
                "内容检查通过：单个 SKILL.md，没有脚本。",
                "上游仓库是 AGPL-3.0，和现有的 GPL-3.0 那条同样处理：不链接、不复制、不进包。",
                "要接入得先确认授权，所以现在连启用都拦住。",
            ),
        ),
        dict(
            ident="qoder-deep-research",
            name="Deep Research",
            summary="系统性技术研究：来源核验、交叉印证、带引用的报告。",
            skill_id="official03866510",
            author="Jose-Luis-Nunez",
            repo="Jose-Luis-Nunez/FitnessApp",
            license="未声明",
            license_status="review-needed",
            verdict="importable",
            offline=True,
            network=False,
            notes=(
                "内容检查通过：单个 SKILL.md，没有脚本。",
                "许可证查法：接口无此字段，另查了上游仓库的 LICENSE、LICENSE.md、LICENSE.txt、COPYING 等十个路径和 package.json，都没有。按待审阅处理，导入前要人工确认。",
                "它自己要联网查资料，本程序不代搜、不代下载。",
            ),
        ),
        dict(
            ident="qoder-code-simplifier",
            name="Code Simplifier",
            summary="保持行为不变，把代码整理得更清楚、更一致。",
            skill_id="official_180JmBZt",
            author="tomevault-io",
            repo="tomevault-io/tomes",
            license="未声明",
            license_status="review-needed",
            verdict="review",
            offline=True,
            network=True,
            notes=(
                "说明里带了外部地址，要联网取资料，用之前先确认范围。",
                "上游仓库地址取不到（404），许可证查不到，先按待审阅处理。",
            ),
        ),
        dict(
            ident="qoder-context7",
            name="Context7",
            summary="按实际依赖版本查技术文档。",
            skill_id="official_A6HI4Dnq",
            author="takeokunn",
            repo="takeokunn/nixos-configuration",
            license="未声明",
            license_status="review-needed",
            verdict="review",
            offline=False,
            network=True,
            notes=(
                "技能本身是说明文件，但它描述的是外部文档服务：要用得另配 MCP 和账号。",
                "上游仓库里没有许可证文件，先按待审阅处理。",
            ),
        ),
        dict(
            ident="qoder-playwright",
            name="Playwright（界面测试写法）",
            summary="用 Page Object、登录夹具和可访问性检查写端到端测试。",
            skill_id="official_IYrIo8ju",
            author="navikt",
            repo="navikt/k9-sak-web",
            license="MIT",
            license_status="approved",
            verdict="review",
            offline=False,
            network=True,
            notes=(
                "内容检查提示要联网，先记为待核验。",
                "许可证 MIT，来自上游仓库的 LICENSE.md。",
                "真正的浏览器要另装，配在连接器里（见「Playwright 连接器」）。",
            ),
        ),
        dict(
            ident="qoder-chrome-devtools",
            name="Chrome DevTools（页面调试）",
            summary="管理 chrome-devtools-mcp 与浏览器的生命周期，用 MCP 做浏览器自动化。",
            skill_id="official_tqXsMvy2",
            author="-",
            repo="",
            license="未声明",
            license_status="review-needed",
            verdict="review",
            offline=False,
            network=True,
            notes=(
                "属于 MCP/连接器类能力，要独立进程和浏览器，默认不开。",
                "没有上游仓库可查许可证，先按待审阅处理。",
            ),
        ),
        dict(
            ident="qoder-design-review",
            name="Design Review",
            summary="视觉与交互审查：间距、层级、不一致和「机器味」痕迹。",
            skill_id="official_OMHhx67s",
            author="LaPaGaYo",
            repo="LaPaGaYo/nexus",
            license="MIT",
            license_status="approved",
            verdict="review",
            offline=True,
            network=True,
            notes=(
                "说明里带了外部地址，先记为待核验。",
                "许可证 MIT，来自上游仓库的 LICENSE 文件。",
            ),
        ),
        dict(
            ident="qoder-architecture-visualization",
            name="Architecture Visualization",
            summary="云架构评估、风险热力图与合规仪表盘。",
            skill_id="official_tVvbZa3y",
            author="-",
            repo="",
            license="未声明",
            license_status="review-needed",
            verdict="unsuitable",
            verdict_note="整包是腾讯云智能顾问的接口文档加 6 个调用云 API 的脚本，要云账号和密钥，不适合本机接入。",
            offline=False,
            network=True,
            notes=(
                "包里带 6 个脚本（check_env.py、tcloud_api.py 等），只登记不执行。",
                "内容里出现 secretId、token、signature 之类的签名参数，属于云凭据流程。",
                "面向云账号治理，本机不接这类账号，所以判为不适用。",
            ),
        ),
    ]
    out = []
    for item in entries:
        not_found = "关键词没有命中" if not item["skill_id"] else ""
        out.append(
            ExtensionManifest(
                id=item["ident"],
                name=item["name"],
                kind="skill",
                source="dev",
                summary=item["summary"],
                homepage=_skill_url(item["skill_id"]) if item["skill_id"] else "https://qoder.com/zh/marketplace",
                pinned=("%s@1.0.0" % item["skill_id"]) if item["skill_id"] else "",
                license=item["license"],
                license_status=item["license_status"],
                platforms=("windows", "linux", "darwin"),
                offline=item["offline"],
                requires_network=item["network"],
                permissions=("read",),
                verdict=item["verdict"],
                audience="developer",
                tags=("技能", "Qoder", "开发专用", "待核验"),
                notes=tuple(item["notes"]) + ((not_found,) if not_found else ()),
                verdict_note=item.get("verdict_note") or (
                    "已核验：内容可安全导入，等人工确认许可证后再决定是否启用。"
                    if item["verdict"] == "importable"
                    else "内容或依赖还没核完，先记为待核验，不启用。"
                ),
            )
        )
    return out


def qoder_catalog():
    """用户指定的三个，加上 P1/P2 的候选。"""
    return _user_skills() + _candidates()
