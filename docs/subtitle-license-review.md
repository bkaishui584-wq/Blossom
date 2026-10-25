# 实时字幕参考项目许可证核验

核验日期：2026-10-24。结果来自 GitHub API 返回的仓库元数据、根目录文件列表和
README。仓库内容以后仍可能变化，复用前要重新核验当时对应的提交。

| 项目 | 核验结果 | 技术路线 | 处理 |
| --- | --- | --- | --- |
| `lyming99/livetool` | Apache-2.0，Python，公开仓库，未归档 | Electron + TypeScript + React；funasr / whisper 识别，独立字幕层 | 可按 Apache-2.0 条件复用，但当前没有复制代码；复用时要保留许可证和变更说明 |
| `umlx5h/LLPlayer` | GPL-3.0，C# / WPF，公开仓库，未归档 | Whisper.cpp / faster-whisper、双语字幕、OCR、实时翻译 | 只作产品和架构参考；代码不进入 DeskPet，避免把发行版带入 GPL 约束 |
| `xu-0306/live-subtitle` | 没有 LICENSE 或其他授权文件，公开预览，Python | Chrome 标签页音频、WebSocket、FastAPI、WhisperLiveKit、页内字幕层 | 默认保留全部权利；不复制、不改造、不再分发其代码或资源 |
| `NBS282/LiveTranslate` | MIT，Rust / Python，公开仓库，未归档 | 本地 Parakeet、MarianMT、字幕浮层、TTS 与虚拟音频线 | 可按 MIT 条件复用，但当前没有复制代码；复用时要保留版权与许可声明 |

DeskPet 当前的字幕实现由项目内代码独立完成，没有复制上述仓库的源码、资源或
构建产物。寻找方案时只参考了公开的模块划分和用户流程。

以后若要复用代码，先固定到具体提交，再检查该提交的 LICENSE、依赖许可证、模型
许可证和再分发条件。GPL/AGPL 项目默认不进入 DeskPet 的发行包。没有许可证的
项目不复制，也不把“公开可见”当成授权。
