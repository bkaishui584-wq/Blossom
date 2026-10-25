# 第二阶段扩展接口

这一页写给要往下加东西的人：每一类扩展点在哪、要满足什么约定、现在到哪一步了。
今天能用的接口都在这里，没写进来的就是还没有。

状态口径：**已接入**（在跑，有测试）、**只有接口**（位置留好了，没有实现）、
**还没做**（别按名字去用）。

## 一、本地模型 Provider

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `model/base.py:ModelProvider` | 已接入 | 模型统一接口：`is_available` / `chat` / `stream`，Agent 只认它 |
| `model/base.py:ModelSlot` | 已接入 | 一个角色的独立状态：陪伴与任务各一份，缺一个不影响另一个 |
| `model/adapter.py:OpenAICompatibleProvider` | 已接入 | 唯一碰厂商 SDK 的地方，支持自定义 `base_url`（本机模型走这条也行） |
| `model/adapter.py:OllamaProvider` | 已接入 | 预设 `http://127.0.0.1:11434/v1`，不需要云 Key；模型要用户自己拉取 |
| `model/logged.py:LoggedProvider` | 已接入 | 记录角色、状态、耗时、Token 和脱敏错误，不保存提示词与回复正文 |
| `adapters/local_runtimes.py` | 只有接口 | 只做探测：Ollama / llama.cpp / sherpa 装没装、有哪些模型，不替用户下载 |

要加一个本地模型：写一个 `ModelProvider` 子类，`is_available()` 老实回答装没装，
在 `model/router.py:build_task_provider`（或陪伴那一路）里挑一个塞进 `ModelSlot` 即可。
不需要动 Agent Core。

## 二、Skill 导入与版本管理

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `skills/loader.py` | 已接入 | 发现与加载 `SKILL.md`（内置在 `skills/library/`，用户在数据目录的 `skills/`） |
| `skills/schema.py` | 已接入 | 解析前置字段与正文段落，步骤、允许工具、验收标准都在这里定 |
| `skills/validator.py` | 已接入 | 结构、依赖、权限、路径、不可信内容校验 |
| `skills/registry.py` / `manager.py` | 已接入 | 注册、启停、版本、运行记录 |
| `skills/router.py` | 已接入 | 按意图与关键词挑候选，挑不中不硬凑 |
| `extensions/` | 已接入 | 扩展与技能的导入、登记、许可证与风险标注 |

约定：**Skill 只描述流程，不带来权限**。它声明的每个工具调用仍然单独过
`ToolExecutor` → `PermissionManager`。外部内容一律当不可信输入。

## 三、屏幕感知 Provider

分级开关在 `security/privacy.py:CONTROLS`，默认全关：

| 开关 | 管什么 | 状态 |
| --- | --- | --- |
| `read_screen` | 屏幕数量与分辨率 | 已接入（`behavior/screen.py`） |
| `awareness` | 前台应用名与窗口标题 | 已接入（`behavior/awareness.py`） |
| `vision` | 用户手动触发的一张截图 | 已接入，截图只存本地（`behavior/vision.py`） |
| `screen_to_model` | 屏幕内容交给模型 | 已接入：云端模型默认拿不到屏幕工具，本机模型不受限 |

加一类新感知：实现一个 Provider 交给 `behavior/awareness.py`，并在 `CONTROLS` 里
加一个自己的开关；**不要复用别人的开关**。

OCR 位置在 `behavior/vision.py:OcrProvider`，**只有接口**：没有引擎时如实说读不了字。

## 四、设备 Provider

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `devices/provider.py:DeviceProvider` | 已接入 | 连、断、读、写四件事，确认逻辑在基类统一走 |
| `devices/actions.py` | 已接入 | 动作白名单与参数范围；范围必须由设备声明，不许程序猜 |
| `devices/simulator.py` | 已接入 | 假固件，独立验协议与参数，没有硬件时全靠它 |
| `devices/permissions.py` | 已接入 | 授权按 `设备+动作` 一条条给，可撤销 |
| `devices/tasks.py` | 已接入 | 任务队列、超时、取消、重试上限 |
| `devices/bridge.py` | 已接入 | 设备事件 → EventBus |

加一种设备：实现 `DeviceProvider` 的四个方法 + 声明能力与范围，写进设备清单即可，
电脑端的动作、权限、任务、界面都不用改。

## 五、传输层

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `devices/ports.py` | 已接入 | 串口枚举与打开（有 pyserial 就用它设波特率，没有就退回标准库并如实标注） |
| `devices/esp32.py:SerialEsp32Transport` | 已接入 | 真实串口，缓冲分行、非阻塞 |
| `devices/wifi.py:WifiProvider` | 已接入 | TCP，同一套协议；连上先发 `auth`；**没加密**，只适合可信局域网 |
| 协议本体 | 已接入 | `devices/protocol.py`：一行一帧，UTF-8，版本化，八种状态，统一错误码 |

再加一条传输（比如 MQTT）：照着 `WifiProvider` 写读写两个方法就行，
业务协议和动作处理不要复制第二份。

## 六、语音输入输出

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `voice/interface.py:VoiceProvider` | 已接入 | 合成与识别的统一接口，没装引擎时是 `NullVoiceProvider` |
| `voice/capture.py:AudioCaptureProvider` | 已接入 | 录音，默认 `NullCapture`；采集前过隐私开关 `listen` |
| `voice/manager.py` | 已接入 | 朗读、转写、唤醒三条流水线 |
| `voice/pack.py` | 已接入 | 音色包（可选，装在本机） |
| `adapters/voice_provider.py` | 已接入 | 探测本机的离线语音组件，不自动下载 |

前提：**默认不录音**，要用户点一下或显式打开才采；没有引擎时按钮明确不可用。

## 七、数据备份与恢复

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `memory/backup.py:export_payload` / `import_payload` | 已接入 | 记忆与偏好的导出导入，按白名单的层 |
| `memory/backup.py:backup_database` | 已接入 | 数据库文件级备份 |
| `tools/local_extra.py`（`backup_create` / `backup_list` / `restore_backup`） | 已接入 | 界面与技能用的入口；恢复要显式确认，覆盖前先留一份 |
| 数据库迁移 | 已接入 | `database/db.py:MIGRATIONS` 只追加，靠 `schema_migrations` 记账，失败不写半个 |

## 八、诊断与安装打包

| 位置 | 状态 | 说明 |
| --- | --- | --- |
| `agent/diagnostics.py` | 已接入 | 运行期诊断缓冲 |
| `tools/runtimes.py`（`run_diagnostics` / `local_ability_status`） | 已接入 | 依赖、路径、权限、模型连接的自检 |
| `Blossom.spec` + `build.ps1` | 已接入 | 打包、快捷方式、`.env.example` 随包 |
| `docs/distribution.md` | 已接入 | 装、卸、排障 |

打包边界：只带程序与只读资源。用户数据、密钥、数据库、日志不进包；
固件源码随包带一份，装完在 `_internal/firmware/` 下。

## 九、还没做的

- MQTT / 多设备组网。
- Wi-Fi 的加密与设备认证（现在是明文 + 共享令牌）。
- OCR 引擎（位置留好了，接口在）。
- 社区技能库、签名校验、第三方分发。

这些按顺序排在第二阶段，接口位置已经在本页写明，不需要回头改架构。
