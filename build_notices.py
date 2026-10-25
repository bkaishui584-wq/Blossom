"""生成 THIRD-PARTY-NOTICES.md：把随包分发的第三方组件与它们的许可证列清楚。

口径是可重复的，不靠手写：
    1. 扫本仓库代码，找出直接导入的第三方模块；
    2. 映射到发行包名，再按包元数据算出完整依赖闭包；
    3. 逐个读出许可证，按「基础包 / 能力包」分开列。

只用于发行说明，不参与程序运行。
"""

import ast
import importlib.metadata as md
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "THIRD-PARTY-NOTICES.md"

# 标准库之外、又确实来自本机 Python 环境的模块名要在这里过滤掉。
STDLIB = set(sys.stdlib_module_names)

# 只给测试和打包用的，不进发行包。
DEV_ONLY = {"pytest", "pyinstaller", "setuptools", "wheel", "pip", "unit" + "test"}

# 既在能力包里、基础包也直接依赖的组件（液态玻璃需要 Pillow 解码背景图）。
BASE_ALSO = {"pillow"}

# 能力包里的那几套（与服务端一起分发，但不在基础包里）。
PACK_HINTS = {
    "markitdown": "docs",
    "magika": "docs",
    "mammoth": "docs",
    "openpyxl": "docs",
    "python-pptx": "docs",
    "pdfminer.six": "docs",
    "lxml": "docs",
    "onnxruntime": "docs",
    "numpy": "docs",
    "sherpa-onnx": "voice",
    "sherpa-onnx-core": "voice",
}


def imported_modules():
    """扫仓库里的一级导入名。只认自己写的代码。"""
    names = set()
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & {"build", "dist", "dist_packs", "__pycache__", "_internal", "backups", "tests"}:
            continue
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {name for name in names if name not in STDLIB}


def distributions_for(modules):
    """模块名 -> 发行包名。有的模块名和包名不一样（pptx -> python-pptx）。"""
    mapping = md.packages_distributions()
    found = {}
    for module in modules:
        for dist_name in mapping.get(module, []):
            found[dist_name.lower()] = module
    return found


def closure(dist_names):
    """按元数据把依赖闭包算全，跳过只给测试用的。

    skip 里的名字会被整片跳过：算基础包时不把能力包的东西算进来，反之亦然。
    """
    return _closure(dist_names, skip=DEV_ONLY)


def _closure(dist_names, skip):
    seen = {}
    stack = list(dist_names)
    while stack:
        name = str(stack.pop()).strip().lower()
        if not name or name in seen or name in skip:
            continue
        try:
            dist = md.distribution(name)
        except Exception:
            continue
        seen[name] = dist
        for item in dist.requires or []:
            if ";" in item and "extra" in item.split(";", 1)[1]:
                continue
            base = item.split(";")[0].split("[")[0]
            for sep in (">=", "==", "<=", "~=", "!=", ">", "<", ","):
                base = base.split(sep)[0]
            stack.append(base.strip())
    return seen


def canonical(name):
    return str(name).strip().lower().replace("_", "-")


def pack_components():
    """读 dist_packs 里每个包的真实清单，返回 (行, 全部组件名)。"""
    import json
    import zipfile

    rows = []
    names = set()
    folder = ROOT / "dist_packs"
    if not folder.is_dir():
        return rows, names
    for archive_path in sorted(folder.glob("*.zip")):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("pack.json").decode("utf-8"))
        except Exception:
            continue
        title = str(manifest.get("title") or manifest.get("name") or archive_path.stem)
        for component in sorted(manifest.get("provides") or []):
            names.add(canonical(component))
            try:
                dist = md.distribution(component)
                version = dist.version
                lic = license_text(dist)
            except Exception:
                version, lic = "", "（本机没有这个包，版本见包内元数据）"
            rows.append((component, version, lic, title))
    return rows, names


def license_text(dist):
    meta = dist.metadata
    for key in ("License-Expression", "License"):
        value = (meta.get(key) or "").strip()
        if value and "\n" not in value and len(value) < 80:
            return value
    classifiers = meta.get_all("Classifier") or []
    for item in classifiers:
        if item.startswith("License ::"):
            return item.split("::")[-1].strip()
    # 元数据里没写：翻包里的 LICENSE 文件，按常见措辞认一下。
    for entry in dist.files or []:
        name = str(entry).lower()
        if "license" not in name and "copying" not in name:
            continue
        try:
            text = Path(dist.locate_file(entry)).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        low = text.lower()
        if "apache license" in low:
            return "Apache-2.0（按包内 LICENSE 认定）"
        if "mit license" in low or "permission is hereby granted, free of charge" in low:
            return "MIT（按包内 LICENSE 认定）"
        if "redistribution and use in source and binary forms" in low:
            return "BSD（按包内 LICENSE 认定）"
    return "未声明（包内有 LICENSE 文件，以原文为准）"


def main():
    modules = imported_modules()
    direct = distributions_for(modules)
    # 能力包那份清单以实际打好的包为准（包里有什么就写什么），
    # 不按开发环境的依赖闭包猜——猜出来的和真发出去的会不一样。
    pack_rows, pack_names = pack_components()
    base_direct = [
        name
        for name in direct
        if canonical(name) not in pack_names or canonical(name) in BASE_ALSO
    ]
    base_packages = {
        name: dist
        for name, dist in closure(base_direct).items()
        if canonical(name) not in pack_names or canonical(name) in BASE_ALSO
    }
    base_rows = [(name, dist.version, license_text(dist)) for name, dist in sorted(base_packages.items())]
    # 同一个包在环境里可能有两份元数据（下划线/连字符写法），去重。
    seen = set()
    packed = set()
    deduped = []
    for name, version, lic in base_rows:
        key = canonical(name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((name, version, lic))
    base_rows = deduped
    deduped = []
    for name, version, lic, pack in pack_rows:
        key = canonical(name)
        if key in packed:
            continue
        packed.add(key)
        deduped.append((name, version, lic, pack))
    pack_rows = deduped

    lines = [
        "# 第三方组件与许可证",
        "",
        "本程序随包分发以下第三方组件。它们在各自的许可证下分发，",
        "不影响本程序自身的专有许可（见 `LICENSE`）。",
        "",
        "这一页由 `build_notices.py` 生成，可重复运行核对。",
        "",
        "## 一、基础包里的组件",
        "",
        "| 组件 | 版本 | 许可证 |",
        "| --- | --- | --- |",
    ]
    for name, version, lic in base_rows:
        lines.append("| %s | %s | %s |" % (name, version, lic))
    lines += [
        "",
        "## 二、能力包里的组件（用户按需安装）",
        "",
        "| 组件 | 版本 | 许可证 | 属于哪个能力包 |",
        "| --- | --- | --- | --- |",
    ]
    for name, version, lic, pack in pack_rows:
        lines.append("| %s | %s | %s | %s |" % (name, version, lic, pack))
    lines += [
        "",
        "## 三、其它说明",
        "",
        "- Python 运行时（PSF 许可证）随包分发，见 bundle 内的 `python313.dll`。",
        "- 打包工具 PyInstaller 只以引导程序形式嵌入可执行文件；",
        "  其许可证带例外条款，允许这样分发打包后的程序，PyInstaller 本体不再单独分发。",
        "- 本程序自带一个 Live2D 角色（`resources/live2d_models/live2D0`，本项目自己的美术资源），",
        "  第一次启动时放进用户数据目录，用户随时可以换成自己的模型。",
        "- 除此之外的 Live2D 模型、语音模型和 OCR 模型都不随程序分发；",
        "  这些资源由用户自行准备，或在启用可选能力时单独下载。",
        "- 完整许可证正文可在各组件的 `*.dist-info` 目录里找到；",
        "  本页只做清单，不替代原文。",
        "",
    ]
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print("写好：%s（基础包 %d 个、能力包 %d 个）" % (OUTPUT.name, len(base_rows), len(pack_rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
