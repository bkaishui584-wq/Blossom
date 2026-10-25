"""把可选能力打成独立的能力包（不放进基础包）。

用法：
    python build_packs.py            # 打全部
    python build_packs.py docs       # 只打文档解析

产物放在 dist_packs/ 下：<名字>-<版本>.zip 与同名 .sha256。
包里是一个 site/ 目录加一份 pack.json，装到数据目录的 packs/<名字>/ 后，
程序启动时会把 site/ 接进 sys.path（见 extensions/packs.py）。

为什么这么切：文档解析与离线语音合计一百多 MB，多数用户用不到。
基础包只留桌宠本体、Live2D、屏幕语义、串口和模型适配。
"""

import hashlib
import importlib.metadata as md
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "dist_packs"
STAGE_DIR = ROOT / "build" / "packs"

# 每个包自带完整依赖闭包，不去猜基础包里恰好带了什么。
#
# 早先想按"基础包里已经有的就不重复装"来省体积，但那是猜：PyInstaller 把纯 Python
# 模块收进 exe 里的 PYZ，从目录上看不出到底有哪些。猜错的后果是用户装上包之后
# import 失败，而且报错位置很偏（缺的是别人的依赖）。多带几 MB 换"装了就一定能用"，
# 这个取舍更划算。
BASE_PACKAGES = set()

PACKS = {
    "docs": {
        "title": "文档解析",
        "version": "1.0.0",
        "note": "读取 docx / xlsx / pptx / pdf / html 这类容器格式。不装时这些格式读不了，"
                "纯文本和 Markdown 照常能读。",
        # 注意用发行名：python-pptx 装进来的模块叫 pptx。
        # pandas 不在里面：markitdown 的 xlsx 转换器硬依赖它（约 59 MB），
        # 而本项目读 xlsx 直接走 openpyxl（见 adapters/documents.py），用不着 pandas。
        "seeds": ["markitdown", "magika", "mammoth", "openpyxl", "python-pptx",
                  "pdfminer.six", "lxml", "pillow", "onnxruntime"],
        # 这两个只有 onnxruntime 的训练/转换工具区才用，我们只做推理。
        # 打完会用真实解析跑一遍验证，跑得通才留着这个剔除。
        "drop": ["sympy", "mpmath"],
    },
    "voice": {
        "title": "离线语音",
        "version": "1.1.0",
        "note": "离线语音识别、合成与实时音频采集。不装时语音按钮不可用，界面会写明原因。",
        "seeds": ["sherpa-onnx", "soundcard"],
        "drop": [],
    },
}


def closure(seeds, drop=()):
    """按包元数据算出依赖闭包。基础包里有的、明确要剔的，都不算进来。"""
    seen = {}
    unknown = []
    stack = list(seeds)
    dropped = set(item.lower() for item in drop) | BASE_PACKAGES
    seeds = [str(item).strip().lower() for item in seeds]
    while stack:
        name = str(stack.pop()).strip().lower()
        if not name or name in seen or name in dropped:
            continue
        try:
            dist = md.distribution(name)
        except Exception:
            if name in seeds:
                # 种子写错名字会让包悄悄少东西，必须当场喊出来。
                unknown.append(name)
            seen[name] = None
            continue
        seen[name] = dist
        for item in dist.requires or []:
            if ";" in item and "extra" in item.split(";", 1)[1]:
                continue
            base = item.split(";")[0].split("[")[0]
            for sep in (">=", "==", "<=", "~=", "!=", ">", "<", ","):
                base = base.split(sep)[0]
            stack.append(base.strip())
    if unknown:
        raise SystemExit("这些种子名在本机找不到，检查一下是不是写成了模块名：%s" % unknown)
    return {name: dist for name, dist in seen.items() if dist is not None}


def copy_package(dist, target):
    """按 pip 记录的清单把包的文件原样拷过去，包含数据文件与 dist-info。"""
    copied = 0
    for entry in dist.files or []:
        source = Path(dist.locate_file(entry))
        if not source.is_file():
            continue
        relative = Path(str(entry))
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, destination)
            copied += 1
        except OSError as exc:
            print("    跳过 %s：%s" % (relative, exc))
    return copied


def build(name):
    spec = PACKS[name]
    packages = closure(spec["seeds"], spec.get("drop") or ())
    stage = STAGE_DIR / name
    if stage.exists():
        shutil.rmtree(stage)
    site = stage / "site"
    site.mkdir(parents=True)
    print("[%s] %s，共 %d 个包" % (name, spec["title"], len(packages)))
    for package_name, dist in sorted(packages.items()):
        copied = copy_package(dist, site)
        print("    %-24s %-12s %d 个文件" % (package_name, dist.version, copied))
    manifest = {
        "name": name,
        "title": spec["title"],
        "version": spec["version"],
        "note": spec["note"],
        "provides": sorted(packages),
    }
    (stage / "pack.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = OUT_DIR / ("%s-%s.zip" % (name, spec["version"]))
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (OUT_DIR / (archive_path.name + ".sha256")).write_text(digest + "\n", encoding="utf-8")
    print("[%s] 打好：%s（%.1f MB）" % (name, archive_path.name, archive_path.stat().st_size / 1e6))
    print("[%s] sha256：%s" % (name, digest))
    return archive_path


def main(argv=None):
    wanted = [item for item in (argv or sys.argv[1:]) if item in PACKS]
    if not wanted:
        wanted = list(PACKS)
    for name in wanted:
        build(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
