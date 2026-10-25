# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

只打包程序与只读资源：不打包 .env、database、logs，也不打包任何密钥。
数据与日志在 EXE 旁边生成，用户数据不会因为重新打包而丢。
"""

from pathlib import Path

ROOT = Path(SPECPATH)
ICON = ROOT / "resources" / "deskpet.ico"
LIVE2D_ASSETS = ROOT / "resources" / "live2d"
SKILL_LIBRARY = ROOT / "skills" / "library"
# 随程序分发的角色模型。第一次运行时会被放进用户数据目录，
# 用户自己放的模型一个都不动（见 avatar/live2d.py:seed_bundled_models）。
LIVE2D_MODELS = ROOT / "resources" / "live2d_models"
# 两份固件都带上：ESP-IDF 那份是正式用的，Arduino 那份留着对照。
# 装完在 _internal/firmware 下能找到，编译前记得先复制到纯英文路径。
FIRMWARE = ROOT / "firmware"

# 可选组件：本机装了才跟着打包，没装程序照旧能跑，对应功能各自退回自带实现。
#   webview       Live2D 角色画面
#   markitdown    文档解析（docx / xlsx / pptx / pdf / html / epub ...）
#   sherpa_onnx   离线语音识别
# 这几个都靠 collect_all 收：只写 hiddenimports 会漏掉它们的数据文件和原生库
# （magika 的模型、onnxruntime 与 sherpa 的 dll）。
OPTIONAL_IMPORTS = []
OPTIONAL_DATAS = []
OPTIONAL_BINARIES = []
OPTIONAL_HIDDEN = []

# 这些子模块是训练、调优、格式转换的工具区，程序一个都用不到，
# 但它们内部 import torch（分析器会顺着把 torch、torchvision 整包拖进来，
# 光 torch 就 365 MB）。按前缀整片跳过，别让体积失控。
SKIP_SUBTREES = (
    "onnxruntime.tools",
    "onnxruntime.transformers",
    "onnxruntime.training",
    "onnxruntime.quantization",
    "onnxruntime.datasets",
)


def _skipped(name):
    """模块名和路径两种写法都要挡得住。"""
    text = str(name).replace("\\", "/")
    for prefix in SKIP_SUBTREES:
        if text == prefix or text.startswith(prefix + "."):
            return True
        slashed = prefix.replace(".", "/")
        if ("/" + slashed + "/") in text:
            return True
        if text.strip("/") == slashed or text.strip("/").startswith(slashed + "/"):
            return True
    return False


def collect_optional(name):
    """装了就把包连同数据一起收进来；没装就当不存在。"""
    try:
        __import__(name)
    except Exception:
        return False
    OPTIONAL_IMPORTS.append(name)
    try:
        from PyInstaller.utils.hooks import collect_all

        datas, binaries, hidden = collect_all(name)
        OPTIONAL_DATAS.extend(item for item in datas if not _skipped(item[0]))
        OPTIONAL_BINARIES.extend(item for item in binaries if not _skipped(item[0]))
        OPTIONAL_HIDDEN.extend(item for item in hidden if not _skipped(item))
    except Exception:
        pass
    return True


collect_optional("webview")

# 界面语义（第二层）：pywinauto 读 Windows 可访问性树，comtypes 是它的 COM 绑定。
# 这两个都没装时，屏幕感知第一层（前台应用名与标题）照旧能用。
collect_optional("pywinauto")
collect_optional("comtypes")

# 可选：设置页的背景图片拖放。包里带 TkDND 的 Tcl/Tk 脚本与 DLL。
collect_optional("tkinterdnd2")

# 文档解析（markitdown/magika/onnxruntime/PIL/lxml/pdfminer…）和离线语音（sherpa_onnx）
# 不再打进基础包：它们合计约 115 MB，多数用户用不到。
# 这两套走「能力包」：build_packs.py 单独打成 zip，用户要用时在能力中心装入，
# 运行时从数据目录的 packs/<名字>/site 加进 sys.path（见 packs.py）。
# 基础包里一条都不用留，用户没装时对应功能会如实说"没装能力包"。

# 只读资源：图标 + Live2D 渲染器自带的资源（不含任何模型，模型在用户数据目录）
# + 自带的技能库（SKILL.md 与实现都按路径现读，不打进去就找不到）。
DATAS = [(str(ICON), "resources")]
DATAS += OPTIONAL_DATAS
if LIVE2D_ASSETS.is_dir():
    DATAS.append((str(LIVE2D_ASSETS), "resources/live2d"))
if LIVE2D_MODELS.is_dir():
    DATAS.append((str(LIVE2D_MODELS), "resources/live2d_models"))
if SKILL_LIBRARY.is_dir():
    for _path in sorted(SKILL_LIBRARY.rglob("*")):
        if _path.is_dir() or "__pycache__" in _path.parts or _path.suffix == ".pyc":
            continue
        DATAS.append((str(_path), str(_path.parent.relative_to(ROOT))))
# ESP32 固件：随包带一份源码，装完照 README 就能编译，不用回头找工程。
# 逐文件收，避开 build/ 这类编译产物（里面是缓存和日志，不属于发行包）。
if FIRMWARE.is_dir():
    for _path in sorted(FIRMWARE.rglob("*")):
        if _path.is_dir() or "build" in _path.parts or "__pycache__" in _path.parts:
            continue
        DATAS.append((str(_path), str(_path.parent.relative_to(ROOT))))

# 模型 SDK 的可选依赖（数据分析、绘图、笔记本、云存储、格式化工具）用不到，
# 不排除的话会被静态分析一起拖进来，体积从几十 MB 涨到两百 MB。
# 后半段是本机装着的无关大包（训练框架、Web 框架、数据库驱动、PDF 渲染器），
# 一律排除；排不掉的那部分靠上面的 SKIP_SUBTREES 从来源处掐断。
EXCLUDES = [
    "pandas",
    "pyarrow",
    "matplotlib",
    "mpl_toolkits",
    "matplotlib_inline",
    "IPython",
    "ipykernel",
    "jupyter_client",
    "jupyter_core",
    "ipywidgets",
    "notebook",
    "nbformat",
    "comm",
    "zmq",
    "tornado",
    "boto3",
    "botocore",
    "black",
    "jedi",
    "fsspec",
    "lxml",
    "lxml_html_clean",
    "rich",
    "psutil",
    "sphinx",
    "pytest",
    "unittest",
    "setuptools",
    "pip",
    "wheel",
    "torch",
    "torchvision",
    "torchaudio",
    "torchgen",
    "sympy",
    "transformers",
    "diffusers",
    "django",
    "psycopg",
    "psycopg2",
    "psycopg_binary",
    "pypdfium2",
    "pypdfium2_raw",
    "label_studio",
    "rest_framework",
    "sqlalchemy",
    "cv2",
    # 下面这些是被别的包顺手拖进来的：本程序一行都没导入过。
    # 剔掉它们既省体积，也让第三方许可清单干净。cryptography 不能剔——
    # Live2D 的 pywebview 依赖它；click 也不能剔——httpx 与 magika 要用。
    "flask",
    "werkzeug",
    "markupsafe",
    "itsdangerous",
    "sentry_sdk",
    "opentelemetry",
    "redis",
    "yaml",
    "email_validator",
    "pyreadline3",
    # 可选能力包的那几套，基础包一律不收。
    "markitdown",
    "magika",
    "onnxruntime",
    "mammoth",
    "openpyxl",
    "pptx",
    "pdfminer",
    "numpy",
    "sherpa_onnx",
    "soundcard",
]

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=OPTIONAL_BINARIES,
    datas=DATAS,
    hiddenimports=OPTIONAL_IMPORTS + OPTIONAL_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Blossom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
    version=str(ROOT / "resources" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Blossom",
)
