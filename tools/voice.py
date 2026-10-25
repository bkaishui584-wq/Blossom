"""语音状态工具：如实说这台机器上的语音现在能干什么。

只读。不开麦、不朗读、不下载、不改任何设置，用户问「语音能用吗」时给真实读数。
"""

from tools.registry import Tool

_provider = None


def set_voice_provider(provider=None):
    """挂上语音后端。没挂就如实说没挂。"""
    global _provider
    if provider is not None:
        _provider = provider
    return _provider


def provider_state():
    """当前语音后端的真实状态。"""
    if _provider is None:
        return {
            "name": "none",
            "label": "还没接语音后端",
            "available": False,
            "offline": True,
            "reason": "应用还没装配语音后端",
        }
    try:
        return dict(_provider.describe() or {})
    except Exception as exc:
        return {
            "name": str(getattr(_provider, "name", "voice")),
            "label": "语音后端",
            "available": False,
            "offline": True,
            "reason": "读不到状态：%s" % exc,
        }


def pack_rows(config=None):
    """已经装好的语音包。目录不在就返回空。"""
    root = str(getattr(config, "voices_dir", "") or "") if config is not None else ""
    if not root:
        return []
    from voice import pack as voice_pack

    rows = []
    for item in voice_pack.scan(root):
        rows.append(
            {
                "id": str(getattr(item, "id", "")),
                "name": str(getattr(item, "name", "")),
                "version": str(getattr(item, "version", "")),
                "engine": str(getattr(item, "engine", "")),
            }
        )
    return rows


def voice_status_text(config=None):
    data = provider_state()
    label = str(data.get("label") or data.get("name") or "语音后端")
    bits = []
    if data.get("available"):
        bits.append("说话后端：%s，现在能用。" % label)
    else:
        bits.append(
            "说话后端：%s，现在不能用（%s）。" % (label, data.get("reason") or "没有可用实现")
        )
    bits.append("要不要联网：" + ("不需要。" if data.get("offline") else "要联网，可能还要密钥。"))
    output = bool(getattr(config, "voice_output", False)) if config is not None else False
    bits.append("自动朗读：" + ("开着，回话会念出来。" if output else "关着。"))
    engine = ""
    if config is not None:
        engine = str(getattr(config, "stt_engine", "") or "").strip()
    bits.append("语音识别引擎：" + (engine if engine else "没配，所以暂时不能说话输入。"))
    packs = pack_rows(config)
    if packs:
        names = "、".join(item["name"] or item["id"] for item in packs)
        bits.append("语音包：装了 %d 个（%s）。" % (len(packs), names))
    else:
        bits.append("语音包：一个都没装，用系统默认声音。")
    return "".join(bits)


def voice_pack_text(config=None):
    packs = pack_rows(config)
    if not packs:
        return "一个语音包都没装，现在用系统默认声音。"
    lines = ["装了 %d 个语音包：" % len(packs)]
    for item in packs:
        lines.append(
            "- %s（%s，版本 %s，引擎 %s）"
            % (item["name"] or item["id"], item["id"], item["version"] or "未标", item["engine"] or "未标")
        )
    return "".join([lines[0]] + ["\n" + line for line in lines[1:]])


def register_voice_tools(registry, config=None):
    """装上语音状态工具。只报状态，不动麦克风。"""
    registry.register(
        Tool(
            name="voice_status",
            description="这台机器上的语音现在能不能用：后端、朗读开关、识别引擎、语音包。只读。",
            schema={"type": "object", "properties": {}},
            permission="READ",
            risk="只读语音配置状态，不开麦",
            handler=lambda: voice_status_text(config),
            keywords=("语音", "麦克风", "朗读", "说话", "听我说话", "语音包"),
            readonly=True,
        )
    )
    registry.register(
        Tool(
            name="voice_pack_list",
            description="列出已经装好的语音包，没装就说没装。",
            schema={"type": "object", "properties": {}},
            permission="READ",
            risk="只读语音包清单",
            handler=lambda: voice_pack_text(config),
            keywords=("语音包", "声音包", "换声音"),
            readonly=True,
        )
    )
    return registry
