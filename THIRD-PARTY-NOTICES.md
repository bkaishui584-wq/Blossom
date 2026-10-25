# 第三方组件与许可证

本程序随包分发以下第三方组件。它们在各自的许可证下分发，
不影响本程序自身的专有许可（见 `LICENSE`）。

这一页由 `build_notices.py` 生成，可重复运行核对。

## 一、基础包里的组件

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| bottle | 0.13.4 | MIT |
| clr_loader | 0.3.1 | MIT（按包内 LICENSE 认定） |
| colorama | 0.4.6 | BSD License |
| comtypes | 1.4.17 | MIT |
| distro | 1.9.0 | Apache License, Version 2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| jiter | 0.17.0 | MIT |
| openai | 1.109.1 | Apache-2.0 |
| pillow | 12.3.0 | MIT-CMU |
| proxy_tools | 0.1.0 | MIT |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pyserial | 3.5 | BSD |
| pythonnet | 3.2.1 | MIT |
| pywebview | 6.2.1 | BSD License |
| pywin32 | 312 | PSF |
| pywinauto | 0.6.9 | BSD 3-clause |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| tkinterdnd2 | 0.6.4 | MIT License |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| typing-inspection | 0.4.4 | MIT |

## 二、能力包里的组件（用户按需安装）

| 组件 | 版本 | 许可证 | 属于哪个能力包 |
| --- | --- | --- | --- |
| beautifulsoup4 | 4.15.0 | MIT License | 文档解析 |
| certifi | 2026.7.22 | MPL-2.0 | 文档解析 |
| cffi | 2.1.1 | MIT-0 | 文档解析 |
| charset-normalizer | 3.5.1 | MIT | 文档解析 |
| click | 8.5.0 | BSD-3-Clause | 文档解析 |
| cobble | 0.1.4 | BSD License | 文档解析 |
| coloredlogs | 15.0.1 | MIT | 文档解析 |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | 文档解析 |
| defusedxml | 0.7.1 | PSFL | 文档解析 |
| et-xmlfile | 2.0.0 | MIT | 文档解析 |
| flatbuffers | 25.12.19 | Apache 2.0 | 文档解析 |
| idna | 3.19 | BSD-3-Clause | 文档解析 |
| lxml | 6.1.3 | BSD-3-Clause | 文档解析 |
| magika | 0.6.3 | Apache-2.0 | 文档解析 |
| mammoth | 1.11.0 | BSD-2-Clause | 文档解析 |
| markdownify | 1.2.3 | MIT License | 文档解析 |
| markitdown | 0.1.8 | MIT | 文档解析 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 文档解析 |
| onnxruntime | 1.20.1 | MIT License | 文档解析 |
| openpyxl | 3.1.5 | MIT | 文档解析 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | 文档解析 |
| pdfminer.six | 20260107 | MIT | 文档解析 |
| pillow | 12.3.0 | MIT-CMU | 文档解析 |
| protobuf | 7.36.2 | 3-Clause BSD License | 文档解析 |
| pycparser | 3.0 | BSD-3-Clause | 文档解析 |
| python-dotenv | 1.2.3 | BSD-3-Clause | 文档解析 |
| python-pptx | 1.0.2 | MIT | 文档解析 |
| requests | 2.32.5 | Apache-2.0 | 文档解析 |
| six | 1.17.0 | MIT | 文档解析 |
| soupsieve | 3.0.3 | MIT | 文档解析 |
| typing-extensions | 4.16.0 | PSF-2.0 | 文档解析 |
| urllib3 | 2.8.0 | MIT | 文档解析 |
| xlsxwriter | 3.2.9 | BSD-2-Clause | 文档解析 |
| sherpa-onnx | 1.13.8 | Apache licensed, as found in the LICENSE file | 离线语音 |
| sherpa-onnx-core | 1.13.8 | Apache-2.0 | 离线语音 |
| soundcard | 0.4.6 | BSD 3-clause | 离线语音 |

## 三、其它说明

- Python 运行时（PSF 许可证）随包分发，见 bundle 内的 `python313.dll`。
- 打包工具 PyInstaller 只以引导程序形式嵌入可执行文件；
  其许可证带例外条款，允许这样分发打包后的程序，PyInstaller 本体不再单独分发。
- 本程序自带一个 Live2D 角色（`resources/live2d_models/live2D0`，本项目自己的美术资源），
  第一次启动时放进用户数据目录，用户随时可以换成自己的模型。
- 除此之外的 Live2D 模型、语音模型和 OCR 模型都不随程序分发；
  这些资源由用户自行准备，或在启用可选能力时单独下载。
- 完整许可证正文可在各组件的 `*.dist-info` 目录里找到；
  本页只做清单，不替代原文。
