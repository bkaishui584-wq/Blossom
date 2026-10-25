"""设备清单：用户以后把自己的外设写进 devices/devices.json，程序就认得出、管得住。

这一层只做发现、校验、注册：真串口已经有了实现，但默认不连，
真实硬件仍然被 DeviceManager 的 allow_real 挡住，没写端口就用内存回环。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from devices.esp32 import Esp32Adapter, build_transport
from devices.interface import MockDevice

MANIFEST_NAME = "devices.json"
KINDS = ("mock", "esp32", "wifi")
MAX_DEVICES = 16
MAX_NAME = 40


@dataclass
class DeviceSpec:
    """清单里的一条设备。只描述它是什么，不负责连接。"""

    name: str
    kind: str
    capabilities: tuple = ()
    auto_connect: bool = False
    options: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "auto_connect": self.auto_connect,
            "options": dict(self.options),
        }


def find_manifest(root):
    """在给定目录里找设备清单。没有就返回空串，这不是错误。"""
    if not root:
        return ""
    path = Path(str(root)) / MANIFEST_NAME
    return str(path) if path.is_file() else ""


def parse_manifest(data):
    """把清单内容解析成设备。返回 (设备列表, 错误列表)。某一条坏了只跳过它。"""
    raw = data.get("devices") if isinstance(data, dict) else data
    if raw is None:
        return [], ["清单里没有 devices 列表"]
    if not isinstance(raw, list):
        return [], ["devices 必须是列表"]

    specs = []
    errors = []
    seen = set()
    for index, entry in enumerate(raw):
        slot = "第 %d 个设备" % (index + 1)
        if not isinstance(entry, dict):
            errors.append("%s不是对象，已跳过" % slot)
            continue
        name = str(entry.get("name") or "").strip()[:MAX_NAME]
        if not name:
            errors.append("%s没有名字，已跳过" % slot)
            continue
        if name in seen:
            errors.append("%s名字重复（%s），已跳过" % (slot, name))
            continue
        kind = str(entry.get("kind") or "mock").strip().lower()
        if kind not in KINDS:
            errors.append("%s的类型不认识：%s，已跳过" % (slot, kind))
            continue
        caps = entry.get("capabilities")
        if not isinstance(caps, list):
            caps = []
        capabilities = tuple(
            str(item).strip() for item in caps if str(item or "").strip()
        )
        options = entry.get("options")
        if not isinstance(options, dict):
            options = {}
        seen.add(name)
        specs.append(
            DeviceSpec(
                name=name,
                kind=kind,
                capabilities=capabilities,
                auto_connect=bool(entry.get("auto_connect")),
                options=options,
            )
        )
        if len(specs) >= MAX_DEVICES:
            errors.append("设备超过 %d 个，后面的先不看" % MAX_DEVICES)
            break
    return specs, errors


def load_manifest(path):
    """读清单文件。返回 (设备列表, 错误列表)。文件不在不是错误，读不动才是。"""
    if not path:
        return [], []
    target = Path(str(path))
    if not target.is_file():
        return [], []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return [], ["设备清单读不动：%s" % exc]
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [], ["设备清单不是合法 JSON：%s" % exc]
    return parse_manifest(data)


def build_adapter(spec, logger=None):
    """按一条描述造出设备。造不出来就抛异常，由调用方如实回报。"""
    if spec.kind == "mock":
        return MockDevice(name=spec.name, capabilities=spec.capabilities)
    if spec.kind == "esp32":
        # options 里写了 port 才走真串口；没写就还是内存回环，不偷偷连硬件。
        return Esp32Adapter(
            name=spec.name,
            transport=build_transport(spec.options, logger=logger),
            logger=logger,
        )
    raise ValueError("不认识的设备类型：%s" % spec.kind)


def install(manager, specs, logger=None):
    """把清单里的设备注册到设备管理器。返回每一条的结果，不抛异常。"""
    results = []
    for spec in specs:
        try:
            adapter = build_adapter(spec, logger=logger)
        except Exception as exc:
            results.append({"name": spec.name, "kind": spec.kind, "ok": False, "error": str(exc)})
            continue
        outcome = manager.register(adapter)
        results.append(
            {
                "name": spec.name,
                "kind": spec.kind,
                "ok": bool(outcome.get("ok")),
                "error": outcome.get("error", ""),
                "auto_connect": spec.auto_connect,
            }
        )
    return results


def load_into(manager, root, logger=None):
    """发现清单、注册设备、连上标了 auto_connect 的。返回一份如实的结果。"""
    path = find_manifest(root)
    specs, errors = load_manifest(path)
    results = install(manager, specs, logger=logger)
    accepted = {item["name"] for item in results if item.get("ok")}
    wanted = [spec.name for spec in specs if spec.auto_connect and spec.name in accepted]
    return {
        "path": path,
        "specs": [spec.to_dict() for spec in specs],
        "errors": list(errors),
        "results": results,
        "connected": manager.connect_all(wanted) if wanted else {},
    }
