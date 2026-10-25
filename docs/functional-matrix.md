# DeskPet 功能矩阵（V5 升级）

最后更新：DeskPet V2 升级（阶段 A–L）之后。

> **这一页是历史记录。** 下表停在 V5 那一轮，只作参考；
> 当前状态看本文档末尾的「第一阶段收尾后的实际状态」，以及 `docs/acceptance.md`
> 的「第一阶段收尾验收（A–H）」一节。

审计依据：当前工作区实际代码、实际运行结果、测试证据。旧文档只作参考。
本轮改动过的文件见「V5 变动」一节。

状态口径：IMPLEMENTED 已接入运行流程；PARTIAL 有实现但缺口明显；INTERFACE_ONLY 只有接口或模拟；NOT_IMPLEMENTED 未实现；BLOCKED 缺外部依赖或授权。

## 速览

| 状态 | 条目数 |
| --- | --- |
| IMPLEMENTED | 68 |
| PARTIAL | 7 |
| INTERFACE_ONLY | 4 |
| NOT_IMPLEMENTED | 3 |
| BLOCKED | 3 |

口径：条目数按本文档表格里实际的行数统计（每行一条，按第 2 列的状态归类）。

V5（阶段 A–L）变动：
- 语音：音频采集从「没有采集链路」变成真的能录（本机设备拒绝录音，如实报原因）；语音识别从只有接口变成引擎探测 + 本地适配器，本机没引擎所以记 BLOCKED；按住说话打通到聊天通道。
- 感知：截图从只有接口变成真实截一次并落本地，OCR 明确划在不假装的范围里。
- 设备：补上真实串口传输，真机动作仍按授权范围拦着。
- 记忆：新增结构化项目记忆（目标 / 进度 / 待办 / 阻塞）。
- 界面：新增流式回复与真正生效的停止生成。
- 人格：新增性格冲突提示，以及版本号、历史与一键撤销。
- 多角色：新增角色包的新建、删除、备份与恢复，全程不碰 live2d/ 里的模型源文件。
- 互动：补上长按与放下的触发（行为层本来就有长按反应，缺的是宿主不发这个事件），并让画面里两种新反应真的写出参数。
- 人格：说话示例（角色包 `persona.examples`）会进人格说明，安全边界仍排在最后。
- 仍未做：换系统发音人（需要 SAPI voice token 枚举）。
- 文档：本矩阵按本轮实际代码重写，旧版里过期的状态不再沿用。

## Live2D 与角色

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 真实 Live2D 渲染 | IMPLEMENTED | `avatar/web2d.py`、`avatar/web2d_renderer.py`、`resources/live2d/` | 独立子进程 + 无边框透明窗口 + WebView2 + 官方 Cubism Core + 自写 WebGL 渲染器；实测出图 |
| 透明背景与命中区域 | IMPLEMENTED | `avatar/web2d_renderer.py` | 分层窗口 + 颜色键；透明像素不拦鼠标 |
| 模型发现与依赖校验 | IMPLEMENTED | `avatar/live2d.py` | 扫 `live2d/`，按 `.model3.json` 发现，报缺失文件 |
| 手动重载与卸载 | IMPLEMENTED | `ui/desktop.py` `live2d_reload` / `live2d_unload` | 设置里可重新加载、卸载、切显示 |
| 自动热重载 | NOT_IMPLEMENTED | - | 按文档要求只在渲染器确证支持时才做，当前不做 |
| 动作接口 | PARTIAL | `avatar/live2d.py` `play_action`、`play_motion`、`Live2DHost` | 14 个逻辑动作齐、四种状态如实返回；草稿模型仍无动作资源，所以可用数为 0/14 |
| 表情接口 | PARTIAL | `avatar/live2d.py` `set_expression`、`match_expression` | 同上，草稿模型无表情资源 |
| 逻辑动作注册与映射 | IMPLEMENTED | `characters/actions.py` | 14 个固定动作名、两种写法解析、认不出的名字只报告；配了但模型没有就报缺失，不拿别的动作顶替 |
| 互动触发（点击 / 长按 / 拖动 / 放下） | IMPLEMENTED | `ui/pet_host.py`、`ui/desktop.py`、`resources/live2d/pet_page.js` | 按住不动 600ms 才算长按，拖起来或松手就取消；拖动后松手会额外发一次放下反应，位置照旧记住；五种反应在页面里各自写出可观测的参数，没有死分支 |
| 角色资源管理 | IMPLEMENTED | `characters/manager.py`、`app/main.py` | 扫描角色包与真实模型、切换失败保留原角色、重载比对资源指纹、界面只读状态 |
| 物理查询 | INTERFACE_ONLY | `avatar/live2d.py` `_physics_of` | 只识别声明，无写入入口 |
| 参数调试入口 | INTERFACE_ONLY | `avatar/live2d.py` | 只读展示参数，未做可撤销写入 |
| 角色包清单 `character.json` | IMPLEMENTED | `persona/package.py`、`characters/` | `id` / `name` / `version` / `persona` / `avatar` / `actions` / `voice` 皆可选，旧包不加新字段照样能用 |

## 界面

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 聊天界面 | IMPLEMENTED | `ui/chat_view.py` | 气泡、状态、复制、重试、清空前确认、回车发送；五类消息分色；任务状态单独成行；浮层大小与开合可恢复 |
| 设置中心 | IMPLEMENTED | `ui/settings_view.py` | 基础 / 模型 / 角色 / 人格 / 记忆 / 语音 / 主动行为 / 权限 / 开发者 九个分区 |
| 记忆管理界面 | IMPLEMENTED | `ui/memory_view.py` | 统计、分类、搜索、编辑、锁定、来源、导出、备份 |
| 人格界面 | IMPLEMENTED | `ui/persona_view.py` | 特征与反馈 |
| 桌宠宿主与交互 | IMPLEMENTED | `ui/pet_host.py` | 拖动、双击、右键菜单、隐藏、位置恢复、屏幕边界 |
| 系统托盘与快捷键 | IMPLEMENTED | `ui/tray.py` | 显示、隐藏、设置、退出；P46 修掉两处真 bug（`wintypes` 没有 `WNDCLASSW`、64 位句柄被截断成 32 位），托盘现在真的会出现，命令切回主线程执行 |
| 统一视觉规范 | IMPLEMENTED | `ui/theme.py` | 字体、按钮、输入、消息样式集中定义 |
| 开发者面板 | IMPLEMENTED | 设置 → 开发者页 | 运行模式、意图、任务状态、模型槽位、角色后端、记忆条数、Token、隐私、设备集中一处，默认收起 |
| 流式回复与停止生成 | IMPLEMENTED | `agent/core.py` `respond(on_delta=)`、`ui/chat_view.py`、`ui/desktop.py` | 有流式就不等整句；工具调用与无流式后端自动退回整句；点停止真的取消，迟到的回复按轮次号丢弃；出错保留已出现的内容并标记不完整 |
| 聊天界面按职责拆分 | PARTIAL | `ui/desktop.py` 2347 行 | 宿主、Live2D 接线、聊天、语音中心、行为 tick 仍在一个文件，拆分留到后续阶段 |

## 记忆

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 分层记忆与容量 | IMPLEMENTED | `memory/manager.py` | 短期、长期、偏好、反馈分开；容量与检索条数分离 |
| 检索与排序 | IMPLEMENTED | `memory/manager.py` `search` / `_score` | 相关度、重要度、时效 |
| 冲突与替代 | IMPLEMENTED | `_find_conflicts`、`archive_memory`、`confirm_memory` | 新信息替旧信息，旧的标记不生效 |
| 摘要压缩 | IMPLEMENTED | `_extract_summary`、`_compress_if_needed` | 本地可解释摘要，不依赖模型 |
| 结构化管理与导出 | IMPLEMENTED | `memory/manager.py` `browse` / `edit` / `stats` | 统计、来源、锁定 |
| 备份、校验与恢复 | IMPLEMENTED | `memory/backup.py` | 导出 / 导入 / 库备份 / 恢复前校验与确认 |
| 时间线、日记、关系、习惯 | IMPLEMENTED | `memory/timeline.py`、`diary.py`、`relationship.py`、`habit.py` | 均接入记忆库 |
| 结构化项目记忆 | IMPLEMENTED | `memory/project.py` | 目标 / 进度 / 已完成 / 待办 / 阻塞 / 最后更新时间存在偏好表，无新表；任务跑完或报错时由 Agent Core 更新；`recall()` 每轮带上项目状态 |
| 重启恢复 | IMPLEMENTED | 实测 + 全量测试 | 消息、记忆、偏好、人格、角色与位置均持久化 |

## 人格与情绪

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 性格特征与强度 | IMPLEMENTED | `persona/traits.py` | 特征组合与强度 |
| 人格影响回复 | IMPLEMENTED | `agent/core.py` `build_persona_note` | 每次组装都注入当前人格要点 |
| 反馈与样本收集 | PARTIAL | `persona/feedback.py`、`training.py` | 收集与提示词调整，不是模型微调，界面口径要说明 |
| 性格组合冲突检查 | IMPLEMENTED | `persona/traits.py` `conflicts` / `conflict_note` | 三种会互相打架的组合（活泼↔安静、幽默↔认真、温柔↔独立）只提示不代改，人格面板与保存提示都会显示 |
| 说话示例与人格规则 | IMPLEMENTED | `persona/interface.py` `clean_examples`、`persona/package.py` | 用户自己写的说话示例（角色包 `persona.examples`）会进人格说明；最多 4 条、每条 60 字，安全边界始终排在最后 |
| 人格版本、历史与撤销 | IMPLEMENTED | `persona/history.py` | 每次改动记一笔并推进版本号，历史最多留 50 条；面板可一键撤销上一次并立刻重新生效；存储坏了如实报错 |
| 人格包 | PARTIAL | `persona/package.py` | 有包结构，与角色包、界面入口未完全打通 |
| 情绪状态 | IMPLEMENTED | `persona/state.py` | 程序管理，模型不能直接控制高风险行为 |
| 情绪到形象 | IMPLEMENTED | `Agent → AvatarManager → AvatarProvider` | 缺资源时只记状态 |

## 语音

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 语音输出 | IMPLEMENTED | `voice/speaker.py` | 直接调用系统 SAPI，语速音量可控、可中断 |
| 声音包导入 | IMPLEMENTED | `voice/pack.py` | 元数据校验、不执行包内脚本 |
| 语音识别 | BLOCKED | `voice/stt.py` | 引擎探测 + 本地 vosk 适配器（模型目录由用户自己准备）已接好；本机没装任何识别引擎，所以实测不可用，界面如实显示「不可用（缺 vosk 包或模型目录）」 |
| 唤醒词 | INTERFACE_ONLY | `voice/wake.py` | 文本匹配是真的，音频检测只有 Null |
| 音频采集 | BLOCKED | `voice/capture.py` | 用 Windows 自带 winmm/MCI 录音，无第三方依赖；本机探测到 1 个输入设备，但该设备拒绝 MCI 文件录音，系统原话会显示在语音设置里，不假装录到 |
| 麦克风隐私状态机 | IMPLEMENTED | `voice/wake.py` `MicPrivacy` | 关闭、等待唤醒、录音、识别、错误可显示 |
| 按住说话 / 停止朗读 | IMPLEMENTED | `voice/manager.py`、设置 → 语音页「录一句话」 | 录音要同时满足三件事：权限开着、有麦克风、有识别引擎；缺哪一样就说明是哪一样。识别结果走和打字同一条聊天通道 |
| 读音选择（换发音人） | NOT_IMPLEMENTED | `voice/speaker.py` | 语速、音量、试听、语音包都已接；换系统里已装的另一个发音人还没做，需要 SAPI 的 voice token 枚举，留到后续 |

## 主动陪伴与行为

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 调度与规则 | IMPLEMENTED | `scheduler/manager.py`、`rules.py` | 冷却、免打扰、频率上限、优先级 |
| 事件驱动 | IMPLEMENTED | `events/bus.py` | 事件触发，不轮询模型 |
| 安静、睡眠、专注 | IMPLEMENTED | `behavior/quiet.py`、`focus.py` | 全屏与专注检测走 Win32 |
| 待机动作规则 | IMPLEMENTED | `behavior/actions.py` | 按时段算频率，纯规则不调模型 |

## 感知

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 多显示器与布局变化 | IMPLEMENTED | `behavior/screen.py`、`ui/pet_host.py` | 布局变化后校正位置 |
| 环境信息 | IMPLEMENTED | `behavior/environment.py` | 电量、内存 |
| 窗口感知 | PARTIAL | `behavior/focus.py`、`behavior/manager.py` | 只看前台是否全屏，不读窗口内容 |
| 截图 / OCR | PARTIAL | `behavior/vision.py` | 截图已实现：Windows GDI 抓一张、自写 PNG 落本地、不联网；授权默认关，只有用户在右键菜单点「看一眼屏幕」才截一次。OCR 仍是接口，缺引擎时明确说没有，不猜图里有什么 |
| 检测事件与 YOLO | INTERFACE_ONLY | `behavior/detections.py` | 有 Mock 提供者与事件管线 |

## 设备

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 设备层与模拟设备 | IMPLEMENTED | `devices/manager.py`、`interface.py` | 发现、注册、状态、命令 |
| ESP32 适配与命令校验 | IMPLEMENTED | `devices/esp32.py` | 传输抽象 + 校验 + 超时 |
| 真实串口传输 | IMPLEMENTED | `devices/esp32.py` `SerialEsp32Transport` | 能按配置打开串口、非阻塞读、报健康状态，空端口直接拒绝；默认不连，`DeviceManager.allow_real` 未开时适配器只走 loopback |
| 真实硬件动作 | BLOCKED | - | 未授权且无硬件 |
| 身体与设备联动 | IMPLEMENTED | `behavior/body.py` | 事件驱动，无设备时静默 |

## 多角色

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 角色配置与切换 | IMPLEMENTED | `pets/manager.py` | 注册、选择、刷新、会话 ID |
| 跨角色会话与记忆隔离 | IMPLEMENTED | `pets/manager.py`、`memory/repository.py` `pet_id` | 会话按 `pet:<id>` 分开；长期记忆带归属，检索只看自己的与公共的；迁移 10 只加列，老记忆默认公共 |
| 角色包新建 / 删除 / 备份 / 恢复 | IMPLEMENTED | `characters/store.py`、设置 → 角色页 | 新建只写一份清单，引用的模型必须真实存在；删除要二次确认，角色包里放着模型源文件时程序拒绝删；备份只收清单类小文本，模型、音频、数据库、密钥都不进去；恢复只解小文本，脚本与上层路径一律跳过 |
| 多角色同屏 | NOT_IMPLEMENTED | - | 按需求推迟，不重写单角色架构 |

## 稳定性、备份与打包

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| EXE 与桌面快捷方式 | IMPLEMENTED | `Blossom.spec`、`build.ps1`、`dist/` | 冻结运行时不带用户数据；P46 修掉 `build.ps1` 缺 BOM 导致 Windows PowerShell 解析失败的问题，并用当前源码重新打包、刷新快捷方式 |
| 程序目录与数据目录分离 | IMPLEMENTED | `security/config.py` | 认程序自身位置与 `DESKPET_ROOT`，不认当前目录 |
| 数据库迁移 | IMPLEMENTED | `database/db.py` | 版本化、可重复执行，实测版本 9 |
| 启动失败处理 | IMPLEMENTED | `security/startup.py` | 一句人话 + 脱敏 traceback 进日志 |
| 启动卡住兜底 | IMPLEMENTED | `app/main.py` `start_startup_watchdog` | 窗口 45 秒不出现时写日志并弹一句人话；已经报过错就不重复弹 |
| 跨线程界面安全 | IMPLEMENTED | `ui/desktop.py` `post` / `_drain` | 工作线程只投递，不直接碰控件 |
| 退出释放 | IMPLEMENTED | `app/main.py` | 托盘、语音、识别、唤醒、麦克风、调度器逐个关 |

## 最高风险（按影响排序）

1. `ui/desktop.py` 2347 行单文件。宿主、Live2D 接线、聊天、语音中心、行为 tick 全在这里，是后续所有界面阶段的改动热点，回归风险最高。阶段 P44 已经先做了分区与分类，文件拆分仍未做，要做就单独一步做，别和功能改动混在一起。
2. 语音输入仍是接口态。输出真的能用，输入只有接口。P46 已把界面口径改成「只能打字」，状态不再误导；真实识别没接之前，界面上都不能写成已完成。
3. 当前模型没有动作、表情、物理资源。动作系统只能降级。阶段 C 已经做到「有资源才播、没资源明确报告」，可用动作为 0/14；阶段 H 要接着做动作感与桌面互动，同样不许用伪造动画凑数。
4. 全屏、专注、多显示器判定靠 Win32 调用，只在当前单屏 150% 缩放下实测过。多屏与缩放变化下的边界未验证。
5. 跨角色私有记忆隔离只靠会话 ID，没有越权读取校验。阶段 I 之前要补。

## Agent V3

| 项目 | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| 结构化意图路由 | IMPLEMENTED | `agent/router.py` `IntentRouter.decide` / `RouteDecision` | 输出意图、置信度、目标、要求的动作、是否要工具、是否要确认、缺什么信息、是否接续任务；停/取消/确认 → 本地能力 → 闲聊/咨询/查信息/任务 → 模糊先问；老接口一字未改 |
| 任务规划与预算 | IMPLEMENTED | `agent/task.py` `TaskLimits` / `TaskPlan` / `TaskStep`、`agent/core.py` `_out_of_budget` | 单轮限轮数、工具次数、时间、重试；到顶就停下问用户，绝不无限循环；状态覆盖 PLANNING / WAITING_CONFIRMATION / VERIFYING 等 |
| 任务持久化与恢复 | IMPLEMENTED | 迁移 11（`tasks` + `task_steps`）、`agent/task_store.py` | 目标、状态、步骤、待答问题、待确认、失败原因、项目与会话关联都落库；重启后列待续任务但不自动重跑；中断态标记为等用户 |
| 工具元数据与结果校验 | IMPLEMENTED | `tools/registry.py` `Tool` / `ToolOutcome`、`tools/builtin.py` `_calendar_verify` | 只读、可取消、可重试、幂等、超时、执行后校验方法；`for_intent` 只挑当前相关工具；日程写入写完后回库复核，读不到就不算校验通过；耗时超阈值的慢工具单独记审计 |
| 任务取消 | IMPLEMENTED | `agent/core.py` `cancel_task` / `_run_tools` / `_finish_task_round` | 用户喊停后当前任务不再执行任何后续工具，状态落成 CANCELLED 并写库 |
| 记忆与上下文 V3 | IMPLEMENTED | `memory/manager.py`、`agent/context.py`、`memory/review.py` | 上下文按真实 token 预算裁剪，记忆按相关度取少量，旧对话压摘要；来源区分用户明说 / 程序观察 / 模型推断；改口后新的取代旧的 |
| 主动陪伴 V2 | IMPLEMENTED | `scheduler/manager.py` `tick` / `trace_rows`、`scheduler/rules.py` `topic_rule` | 事件触发；门禁顺序为偏好 → 免打扰 → 冷却 → 每日上限 → 条件；一轮最多开口一条；自主话题只从项目进展、日程、完成的任务取材，取不到就沉默 |
| 人格与独立性 | IMPLEMENTED | `persona/interface.py` `build_persona_note` / `SAFETY_BOUNDARY`、`persona/traits.py` | 性格强度真实进提示词；安全边界固定排在最后且挤不掉；允许温和反对；反馈只收说话方式类；人格改不了权限 |
| 长期陪伴能力 | IMPLEMENTED | `agent/capabilities.py` `_review` / `_reminders` / `_project`、`memory/review.py`、`memory/diary.py` | 日报、周报、提醒列表与取消、项目跟踪、日记全部本地完成，只读真实记录，读不到就说读不到，一次模型都不调 |
| Agent 诊断视图 | IMPLEMENTED | `agent/diagnostics.py`、`ui/desktop.py` 开发者页 | 环形缓冲记录本轮意图、路由、模式、本地能力、工具、记忆条数、token、任务状态与主动行为跳过原因；只记结论，不含模型私密推理 |
| 场景化安全验收 | IMPLEMENTED | `tests/test_p50.py` | 13 个场景逐条跑真实行为：误触发、误执行、越权、注入、伪造成功、取消、重启恢复、记忆污染、免打扰、无模型降级、未授权设备 |

## 已完成阶段

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| A | 全项目审计与功能清单 | 完成，就是本文档 |
| B | 聊天界面与设置中心 | 完成，见 `docs/acceptance.md` 阶段 P44 |
| C | 角色资源与动作系统 V2 | 完成，见 `docs/acceptance.md` 阶段 P45 |
| D | 持久记忆 V2.1 | 完成 | 逐条核对后确认已满足（分层、来源/置信度/替代元数据、冲突处理、容量可配、检索条数与存储分离、默认窗口 24 条）；P46 另补归属隔离 |
| E | 人格、情绪、关系与主动陪伴 | 完成 | 特征强度、反馈撤销、样本导出、专注/安静/睡眠/免打扰/频率上限都走既有 Provider 与 Scheduler；界面已写明「只收集样本，不做微调」 |
| F | 真实语音输入、唤醒词与声音包 | 部分完成 | 声音包导入与语音输出可用；真实麦克风采集与识别引擎仍未接，P46 把界面口径改成「只能打字」，不再误导 |
| G | 桌面环境感知与行为 | 完成 | 电量、内存、屏幕布局、全屏专注、多显示器校正都有实现与测试；窗口内容不读，是有意为之 |
| H | 动作感与桌面互动 | 部分完成 | 交互触发（悬停、单击、双击、右键、拖动）与待机动作规则已接；模型没有动作资源，可用动作 0/14，界面如实报缺 |
| I | 多角色与独立记忆 | 完成 | 名册、切换、会话隔离、记忆归属隔离齐；多角色同屏仍不做 |
| J | 设备与实体扩展 | 完成 | DeviceManager + MockDevice + ESP32 适配层 + 身体联动，只跑模拟，不接真机 |
| K | 稳定性、备份、打包与安全 | 完成 | 见 `docs/acceptance.md` 阶段 E 与 P46；托盘两个真 bug 已修，打包脚本已能跑，发布包不含密钥 |
| L | 测试与最终验收 | 完成 | 全量 1446 项通过（跳过 4 项），本阶段新增见 `tests/test_p46.py` |
| V3 A–J | 智能体核心二次进阶 | 完成 | 全量 1536 项通过（跳过 4 项）；新增 `tests/test_p47.py`–`test_p50.py` 共 90 项；详见 `docs/acceptance.md` 的 Agent V3 章节 |
| V4 A | 聊天 UI 精致化 | 完成 | 统一视觉规范、消息气泡、Markdown 与代码块、复制、历史定位、错误重试、深色/浅色/系统主题、设置分区与窗口状态持久化；`tests/test_p51.py` |
| V4 B | 统一 Skill 系统 | 完成 | `SkillRegistry` / `SkillLoader` / `SkillValidator` / `SkillRouter` / `SkillExecutor` + 管理界面；`SKILL.md` 说明文件，参考资料与工具 schema 按需加载；`tests/test_p52.py` |
| V4 C | 首批高价值 Skill | 完成 | `coding-agent`、`research-agent`、`file-knowledge`、`office-agent`、`study-agent`、`personal-workflow`、`git-review`，均在 `skills/library/` 下有真实执行路径 |
| V4 D | Skill 自动匹配与组合 | 完成 | 按意图与任务状态选技能、展示推荐原因与所需权限、手动切换与禁用自动匹配、步骤记录与取消后停止；`tests/test_p52.py` |
| V4 E | 技能学习与个性化 | 完成 | 记住认可的工作流与输出格式、成功率与耗时统计、临时授权不转永久权限；`skills/learning.py`、`skills/metrics.py` |
| V4 F | 性能与安全 | 完成 | 技能文档不常驻、定向检索与差异摘要、步骤与模型请求设限、模型不可用时本地能力仍可用 |
| V4 桌宠本体 | 碰撞体积、点击交互与自愈 | 完成 | 命中体积按透明像素生成；单击/双击/悬停/拖动转成事件；渲染进程崩掉自动重起；启动慢不再误报失败；`tests/test_p54.py` |
| V4 打包 | EXE 与桌面快捷方式 | 完成 | `build.ps1` 重新打包成功，数据与程序分离，快捷方式指向当前 EXE；实测单实例、无模型启动、数据库被占用降级、关闭无残留 |
| V4 L | 测试与最终验收 | 完成 | 全量 1684 项通过（跳过 4 项）；V4 新增 `tests/test_p51.py`–`test_p54.py` 共 147 项；详见 `docs/acceptance.md` 的 V4 章节 |

## 旧文档与现状的差异

- 旧验收文档按阶段推进写，未反映新增的 `docs/functional-matrix.md`。以本条矩阵为准。
- `README.md` 的 Live2D 章节与设置分区已按最新实现更新过，口径与代码一致。

V4 到此结束：桌宠本体、聊天工作台与 Skill 系统都在这一版里打通，Live2D 动作与实体设备仍是下一阶段的事。

| V5-1 | Local First 第一阶段 | 完成 | 能力注册表 + 依赖标注、本地时间/系统状态/计时器/待办工具、待办表迁移 13、权限中心的范围授权与四种模式；`tests/test_p55.py` 64 项；天气缓存、文件工具、生命状态、开发工具与能力中心界面留给后续阶段 |
| V5-2 | Local First 第二阶段 | 完成 | 天气/新闻缓存带来源与新鲜度、取不到就给带时间戳的旧记录；授权目录的文件工具（读写/搜索/复制，默认只在工作区，禁止永久删除）；应用启动（网址/文件/已登记应用，HIGH 权限）；审计按类别/对象/结论过滤；`tests/test_p63.py` 31 项 |
| V5-3 | Local First 第三阶段 | 完成 | 生命状态机（待机/活跃/工作/休息/睡觉 + 合法转换 + `life.state` 事件）；待办到点提醒（新增即挂、重启补挂、做完不响）；晚间收尾与早间天气规则；安静时段/频率/冷却沿用既有 Scheduler 门禁；`tests/test_p64.py` 28 项 |
| V5-4 | Local First 第四阶段 | 完成 | 白名单命令 `run_command`（cwd 与命令都过权限，`git` 写操作仍归 `git_action`）；Skill 接入与技能清单展示；能力与权限中心（能力/工具/技能/授权/最近执行、撤销授权、授权目录、暂停主动行为、离线可用清单）；`tests/test_p65.py` 16 项 |
| V9.1-1 | 本地工具补充 | 完成 | 文本整理 `text_process`、任务待续 `task_unfinished`、自检 `run_diagnostics`、备份 `backup_list`/`backup_create`、恢复 `restore_backup`（HIGH，先校验后留副本）；五个新技能，技能库 11 → 16；无模型时只读工具由程序直接跑；`tests/test_p88.py` 43 项 |
| V9.1-2 | 本地运行时探测 | 完成（仅探测） | `adapters/local_runtimes.py` 报 Ollama、llama.cpp、sherpa-onnx、MarkItDown、Docling、pywinauto、ripgrep、FTS5 的装没装、还差什么、许可；不下载、不安装、不起服务；`tests/test_p89.py` 23 项 |
| V9.1-3 | 本地模型接入 | 完成 | 陪伴模型可直接指向本机 Ollama（`http://127.0.0.1:11434/v1`）。程序已支持 OpenAI 兼容接口，因此没有新增 Provider 类；Ollama 与模型由用户自装自拉，程序不代劳。`local_ability_status` 现在把已装上的 Ollama 与模型名报成「装好了」 | 实机自检 `companion_model: connected`；经程序自身适配器发消息拿到回复 |
| V9.1-4 | 文档解析 / 离线语音 / 界面语义 | 完成 | 文档解析：`adapters/documents.py` 用 MarkItDown 读 docx/xlsx/pptx/pdf/html/epub/rtf/ipynb，读不出退回自带解析（表格 openpyxl、PDF pdfminer）。离线语音：`voice/stt.py` 的 `SherpaSttProvider` 认 whisper / sense_voice / paraformer 三种目录，模型文件用户自备。界面语义：`behavior/awareness.py` 的 `UiAutomationProvider` 装 pywinauto 后能读前台窗口的可访问性树，密码框与密钥类控件一律过滤，前台窗口先按系统句柄定位。屏幕感知默认关闭，开关在「屏幕感知」页。`tests/test_p90.py` 35 项、`tests/test_p91.py` 37 项 |
| V9.1-5 | 记忆全文检索（FTS5） | 完成 | `memory/fts.py` 给记忆建 FTS5 索引（优先 trigram 分词，中文能按子串命中），插入/修改/删除都有触发器同步；检索先用索引缩范围，再按原有的重要度、可信度、新鲜度打分，索引不可用时整层退回关键词扫描。删除记忆会同时从索引移除。「本地能力」页报索引建没建、多少条。`tests/test_p91.py` 37 项 |

| V9.1-6 | 本地知识库（文件索引与检索） | 完成 | @@knowledge/index.py@@ 把正文按段落切块（位置说明记到「第 N-M 段」），索引建在 FTS5 虚表 @@doc_chunks_fts@@ 上（优先 trigram，插删改有触发器同步，建不出来退 LIKE）；@@knowledge/service.py@@ 管目录、扫文件、读正文，解析复用 @@tools/workspace.py@@ 的 @@extract_document@@；迁移 16 新增 @@documents@@ / @@doc_chunks@@ / @@doc_sources@@ 三张表；工具 @@knowledge_search@@ / @@knowledge_status@@ 都是只读，扩目录只能由用户在界面里加；技能 @@knowledge-base@@，技能库 16 → 17；界面新增「文件与知识」页（目录 / 搜索 / 状态三栏）。`tests/test_p92.py` 37 项 |
| V9.1 收尾 | 测试与验收 | 完成 | 全量逐文件跑完，基线 `TOTAL` 项通过（跳过 4 项）；新增 `tests/test_p92.py` 37 项，`tests/test_p51.py` / `test_p88.py` 按技能库变化更新 |


## V10：Qoder 技能接入与扩展生态（第十四轮）

| 阶段 | 项目 | 状态 | 说明 | 测试 |
| --- | --- | --- | --- | --- |
| P0 | 扩展类型系统 | 完成 | 类型补齐插件与连接器（共九种）；新增「受众」（面向最终用户 / 开发者专用）与「核验结论」（未评估 / 可导入 / 待核验 / 不适用）；开发专用与不适用的一律不进发行包；`extensions/manifest.py` | `test_p93.ManifestTypeTests` |
| P0 | Qoder 只读客户端 | 完成 | 列清单、关键词搜索、查详情、取 SKILL.md；只用公开接口免登录；超时、非 JSON、连不上都如实报错；`extensions/qoder.py` | `test_p93.QoderClientTests` |
| P0 | 安全技能导入器 | 完成 | 十步检查：来源、临时目录、路径穿越、符号链接、SKILL.md 解析、引用核对、脚本与外部地址扫描、许可证、权限与联网、锁定版本；结论三种；`extensions/importer.py` | `test_p93.ImporterStructureTests`、`test_p93.ZipSafetyTests` |
| P0 | 脚本隔离 | 完成 | 落地只搬说明和文本参考；脚本留在原地，只登记路径，导入、更新、启动任何环节都不执行 | `test_p93.ImporterApplyTests` |
| P0 | 三个指定技能逐项核验 | 完成 | 可导入 1 个、不适用 1 个、待核验 1 个；都标了真实结论与原因，没有一个显示成已安装或已启用 | `test_p93.QoderCatalogTests`、`test_p93.VerdictGateTests` |
| P0 | 开发者 / 最终用户隔离 | 完成 | 两个集合互不重叠；开发专用的默认不进普通用户安装包 | `test_p93.AudienceIsolationTests` |
| P0 | 技能与扩展中心 | 完成 | 扩展表新增核验结论与受众两列；筛选加到「推荐技能 / 待核验 / 需要配置 / 已禁用 / 开发者专用 / 面向最终用户 / 更新与兼容性」；「查看说明」摊开来源与结论；`ui/capability_view.py`、`ui/skills_view.py` | `test_p93.FilterAlignmentTests`、`test_p87.CapabilityViewTests` |
| P1 | UI/UX Pro Max、Superpowers、Code Simplifier、Deep Research | 完成 | 四个条目、内容与上游许可证都查过。UI/UX Pro Max 与 Deep Research 内容检查通过；Superpowers 上游 AGPL-3.0，按 GPL 同规则挡住；Code Simplifier 上游仓库取不到，按待审阅 | `test_p94.CatalogueAuditTests` |
| P2 | Context7、Playwright、Chrome DevTools、Design Review、Architecture Visualization | 完成 | 五个都定位到条目并核过。Playwright 与 Design Review 上游 MIT；Playwright 与 Chrome DevTools 另建连接器适配层，默认关闭 | `test_p94.ConnectorTests`、`test_p94.CatalogueAuditTests` |
| P2 | 外部连接器适配 | 完成 | `extensions/connectors.py`：登记启动命令、所需命令、浏览器与账号要求，并写清本程序不会自己启动；环境检查沿用扩展健康检查；默认关闭、不进包 | `test_p94.ConnectorTests` |
| P2 | 更新检查 | 完成 | `extensions/updates.py`：拿锁定版本比市场当前版本，只报差异不下载；界面按钮走后台线程；连接器等非市场条目不被误查 | `test_p94.UpdateCheckTests` |
| P1 | 许可证深查 | 完成 | 三个「未声明」的上游仓库逐个试了十个常见许可证路径，并查 package.json 与 README；全都没有，才按「未声明」记 | `test_p94.CatalogueAuditTests` |
| P2 | 不适用判定 | 完成 | Architecture Visualization 是腾讯云智能顾问接入包，带 6 个脚本与签名密钥，核验结论改为「不适用」，注册表拒绝启用 | `test_p94.CatalogueAuditTests.test_the_cloud_governance_pack_is_marked_unsuitable` |
| P2 | 真机导入两条 | 完成 | `self-improving-agent` 与 `ui-ux-pro-max` 两条说明文件导入本机技能目录，能加载、能启停；随包脚本一个都没落地 | 实机：技能列表可见、校验通过 |
| P2 | 新技能可被推荐 | 完成 | `skills/router.py` 增加 `NAME_HINTS` / `hint_names`：用户技能没有触发词段时，按技能名片段兜底匹配；只认注册表里真实存在的技能，无关闲聊仍返回空 | `test_p94.SkillRecommendationTests` |
| P0 | 数据安全 | 守卫 | 导入器只写临时目录；测试全程用临时目录和临时压缩包，不碰用户库与 Live2D 资源 | `test_p93.NoRealDataTests` |

---

## 第一阶段收尾后的实际状态

这一节取代上面的 V5 表格。口径：**已实现且验证通过** / **已实现但未充分验证** /
**部分实现** / **未实现** / **存在缺陷**。依据是当前代码与最后一次全量回归（3263 条通过）。

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| 核心聊天与双模型槽 | 已实现且验证通过 | 陪伴/任务各自独立配置，缺一个不影响另一个 |
| 无模型降级（本地直答 + 本地工具） | 已实现且验证通过 | 报时间、待办、自检等在没有模型时可用 |
| 模型错误分类与有限重试 | 已实现且验证通过 | 超时/限流/断网按预算重试；超时归超时，不叫失败 |
| 人格配置进入运行时 | 已实现且验证通过 | 改名下一次对话就生效 |
| 人格版本、测试台、习惯学习 | 已实现，未充分验证 | 有 profile/history/lab/learner；缺端到端场景验收 |
| 记忆分层（偏好/习惯/推断/临时） | 已实现且验证通过 | 四层各有落点与测试；"临时要求"本轮才补上 |
| 记忆持久化、FTS 检索、冲突、删除 | 已实现且验证通过 | 冲突重合不够时不猜，两条都留着 |
| 工具权限、撤销、审计 | 已实现且验证通过 | 5 级权限；授权按工具/设备+动作逐条给 |
| 参数校验（含类型与多余键） | 已实现且验证通过 | 执行前拦下，handler 不跑 |
| 任务状态机、取消、超时 | 已实现且验证通过 | 11 态含 TIMEOUT；取消与失败不伪装成完成 |
| 主工作台（导航、消息、输入、面板） | 已实现且验证通过 | 授权请求进右侧面板；窄窗口自动收起 |
| 主题与对比度 | 已实现且验证通过 | 46 项配对全部达标，有常驻测试 |
| 抽象生命状态（八态） | 已实现且验证通过 | 由真实事件驱动，不轮询模型 |
| 屏幕感知分级授权 | 已实现且验证通过 | 窗口元数据 / 截图 / 交给模型各自独立开关 |
| Live2D 接口 | 已实现且验证通过 | 不碰用户资源；模型里没有的动作会如实报缺 |
| 本地基础工具六类 | 已实现且验证通过 | 能力中心可手动跑；碰文件与系统的要单独授权 |
| 技能系统与导入 | 已实现，未充分验证 | 注册/校验/路由/执行/启停齐；导入缺端到端验收 |
| ESP32 设备接入 | 已实现，未充分验证 | 协议/权限/任务/界面/固件齐；未上真机 |
| Wi-Fi 传输 | 已实现，未充分验证 | 本机回环测过；未加密，未对真设备 |
| 语音输入输出 | 部分实现 | 需自行安装离线组件；未装时按钮不可用 |
| 本地模型 Provider | 部分实现 | 只有运行时探测，没有接入 |
| OCR | 部分实现 | 只有接口，没有引擎 |
| `ui/desktop.py` 拆分 | 未实现 | 208 KB 单文件；留作单独一轮 |
| 记忆冲突的语义判定 | 存在缺陷 | 按词重合判定，语义冲突识别不出来（保守留两条） |
| `Tool.timeout` 硬打断 | 存在缺陷 | 只标慢，不打断执行 |