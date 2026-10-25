"""酒馆角色卡导入：把 SillyTavern 的角色卡换成 DeskPet 的人格文本。

只做三件事：读卡、换成结构化字段、写进人格偏好。
读的是用户自己选的文件，不上传、不联网、不执行卡里的任何脚本或 UI 指令。

卡里的 system_prompt / post_history_instructions 一律不导入：那两个字段是用来
替换系统提示词的，导入等于让一张外来的卡改写程序的安全边界。开场白、备用开场白、
内嵌世界书也不导入，原因写在 IGNORED_FIELDS 里，导入前界面会逐条列出来。

导入一定要用户先看预览再点确认；写进去时留一条版本记录，原来的文本还在历史里。
"""

import base64
import json
import re
import time
import zlib
from pathlib import Path

# 一张卡不该有这么大。超过就不读，避免拿一个巨大的文件把内存和界面拖死。
MAX_CARD_BYTES = 2 * 1024 * 1024
# 和 persona/profile.py 的上限保持一致（有测试盯着这两个数）。
MAX_TEXT_CHARS = 4000

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CARD_CHUNKS = ("ccv3", "chara")

# 角色卡库：用户导入过的卡都留一份，随时能切回来。
# 放在用户数据目录里，不进安装包，也不跟程序一起重装被覆盖。
LIBRARY_DIRNAME = "persona_cards"
LIBRARY_FORMAT = "blossom.persona_card.v1"
LIBRARY_LIMIT = 200
# 内置卡只灌一次：写过这个标记就不再动，用户删掉的卡下次启动不会自己长回来。
LIBRARY_MARKER = ".seeded-v1"

SAFETY_LINE = "安全规则和用户当前的要求始终优先于以上设定。"

# 每一节单独设上限：一张卡再长，也不能把人格预算整个吃掉。
SECTION_LIMITS = {
    "description": 1200,
    "personality": 800,
    "scenario": 600,
    "example": 900,
}

IGNORED_FIELDS = {
    "system_prompt": "系统提示词不导入：安全边界优先，不能被角色卡替换",
    "post_history_instructions": "历史后置指令不导入：同上，避免绕过安全边界",
    "alternate_greetings": "备用开场白不导入：桌宠的空会话问候按名字生成",
    "first_mes": "开场白不导入：只展示给你看，桌宠的问候按名字生成",
    "character_book": "内嵌世界书不导入：只保留人格本身，避免灌进大量设定",
    "extensions": "扩展字段不导入：程序不执行卡里的任何脚本或界面指令",
}

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MACROS_CHAR = ("{{char}}", "{{Char}}", "{char}", "<BOT>")
_MACROS_USER = ("{{user}}", "{{User}}", "{user}", "<USER>")


def _text(value, limit=0):
    """统一换行、去控制字符、压掉多余空行。只改排版，不改用户写的字。"""
    body = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    body = _CONTROL.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if limit and len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return body


def _substitute(body, name):
    for macro in _MACROS_CHAR:
        body = body.replace(macro, name)
    for macro in _MACROS_USER:
        body = body.replace(macro, "用户")
    return body


def _png_chunks(raw):
    """取出 PNG 里的 tEXt / iTXt 文本块。CRC 不对的块直接跳过。"""
    chunks = {}
    pos = len(PNG_SIGNATURE)
    while pos + 12 <= len(raw):
        length = int.from_bytes(raw[pos:pos + 4], "big")
        kind = raw[pos + 4:pos + 8]
        if kind == b"IEND" or length > 4 * 1024 * 1024:
            break
        data = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if len(data) != length:
            break
        if kind not in (b"tEXt", b"iTXt"):
            continue
        crc = int.from_bytes(raw[pos - 4:pos], "big")
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            continue
        keyword, _, rest = data.partition(b"\x00")
        try:
            key = keyword.decode("latin-1")
        except Exception:
            continue
        if kind == b"tEXt":
            try:
                chunks[key] = rest.decode("latin-1")
            except Exception:
                continue
            continue
        if len(rest) < 2:
            continue
        compressed, _method = rest[0], rest[1]
        rest = rest[2:]
        _language, _, rest = rest.partition(b"\x00")
        _translated, _, text = rest.partition(b"\x00")
        if compressed:
            try:
                text = zlib.decompress(text)
            except Exception:
                continue
        chunks[key] = text.decode("utf-8", "replace")
    return chunks


def _decode(raw):
    """先按 JSON 读，不行再看 PNG 里的角色卡块。读不出来返回 None。"""
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        pass
    if not raw.startswith(PNG_SIGNATURE):
        return None
    chunks = _png_chunks(raw)
    for key in CARD_CHUNKS:
        payload = str(chunks.get(key) or "").strip()
        if not payload:
            continue
        try:
            blob = base64.b64decode(payload, validate=False)
        except Exception:
            continue
        try:
            return json.loads(blob.decode("utf-8-sig"))
        except Exception:
            continue
    return None


def _payload(data):
    """V2 / V3 把字段放在 data 里，V1 是平铺的。两种都认。"""
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def _tags(value):
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:8]:
        text = _text(item, 20)
        if text and text not in out:
            out.append(text)
    return out


def persona_text(card):
    """把一张规范化后的卡拼成 DeskPet 的人格文本。"""
    name = card.get("name") or "这个角色"
    sections = ["你是%s。" % name]
    if card.get("description"):
        sections.append(card["description"])
    if card.get("personality"):
        sections.append("性格：%s" % card["personality"])
    if card.get("scenario"):
        sections.append("相处场景：%s" % card["scenario"])
    if card.get("example"):
        sections.append("说话方式示例（学语气，不要照抄）：\n%s" % card["example"])
    sections.append(SAFETY_LINE)
    text = "\n\n".join(section for section in sections if section)
    if len(text) > MAX_TEXT_CHARS:
        keep = MAX_TEXT_CHARS - len(SAFETY_LINE) - 2
        text = text[:keep].rstrip() + "\n\n" + SAFETY_LINE
    return text


def normalize(data, source=""):
    """把原始卡数据规范成统一字段。缺名字就当不能读，不猜。"""
    payload = _payload(data)
    name = _text(payload.get("name"), 40)
    if not name:
        return {"ok": False, "error": "这张卡没有名字，认不出是哪个角色"}

    notes = []
    fields = {}
    for key, limit in (
        ("description", SECTION_LIMITS["description"]),
        ("personality", SECTION_LIMITS["personality"]),
        ("scenario", SECTION_LIMITS["scenario"]),
        ("mes_example", SECTION_LIMITS["example"]),
    ):
        raw = payload.get(key)
        body = _text(_substitute(str(raw or ""), name), limit)
        if raw and len(_text(_substitute(str(raw or ""), name))) > limit:
            notes.append("%s 太长，已截到 %d 字" % (key, limit))
        fields[key] = body

    if not any(fields.values()):
        return {"ok": False, "error": "这张卡只有名字，没有可导入的设定"}

    card = {
        "ok": True,
        "name": name,
        "description": fields["description"],
        "personality": fields["personality"],
        "scenario": fields["scenario"],
        "example": fields["mes_example"],
        "greeting": _text(_substitute(str(payload.get("first_mes") or ""), name), 400),
        "creator": _text(payload.get("creator") or payload.get("author"), 60),
        "version": _text(payload.get("character_version"), 20),
        "spec": _text(data.get("spec") if isinstance(data, dict) else "", 30) or "v1",
        "tags": _tags(payload.get("tags")),
        "source": str(source or ""),
        "warnings": notes,
        "ignored": [
            IGNORED_FIELDS[key]
            for key in IGNORED_FIELDS
            if payload.get(key)
        ],
    }
    card["text"] = persona_text(card)
    card["chars"] = len(card["text"])
    return card


def parse_bytes(raw, source=""):
    """直接读字节。给测试和第三方调用用，不碰文件系统。"""
    if not raw:
        return {"ok": False, "error": "文件是空的"}
    if len(raw) > MAX_CARD_BYTES:
        return {
            "ok": False,
            "error": "文件超过 %d MB，不像是角色卡" % (MAX_CARD_BYTES // 1024 // 1024),
        }
    data = _decode(raw)
    if data is None:
        return {"ok": False, "error": "既不是 JSON，也找不到 PNG 里的角色卡数据"}
    return normalize(data, source=source)


def read_file(path):
    """读用户选的一个 .json / .png。只读，不复制、不改原文件。"""
    try:
        target = Path(str(path))
        if not target.is_file():
            return {"ok": False, "error": "找不到这个文件"}
        size = target.stat().st_size
        if size > MAX_CARD_BYTES:
            return {
                "ok": False,
                "error": "文件超过 %d MB，不像是角色卡" % (MAX_CARD_BYTES // 1024 // 1024),
            }
        raw = target.read_bytes()
    except Exception as exc:
        return {"ok": False, "error": "读不了这个文件：%s" % exc}
    return parse_bytes(raw, source=str(target))


def apply(preferences, preview):
    """把预览写进人格：名字和文本一起换，留一条版本记录。

    文本写失败就把名字恢复原样，避免出现"名字换了、人格没换"的半截状态。
    """
    from persona import profile as profile_module

    if not isinstance(preview, dict) or not preview.get("ok"):
        return {"ok": False, "error": (preview or {}).get("error") or "这张卡不可用"}
    previous = profile_module.name(preferences)
    name_result = profile_module.set_name(preferences, preview.get("name") or "")
    if not name_result.get("ok"):
        return {"ok": False, "error": name_result.get("error") or "名字没能写进去"}
    saved = profile_module.save_text(
        preferences,
        preview.get("text") or "",
        note="导入角色卡：%s" % (preview.get("name") or ""),
    )
    if not saved.get("ok"):
        if previous:
            profile_module.set_name(preferences, previous)
        else:
            profile_module.clear_name(preferences)
        return {"ok": False, "error": saved.get("error") or "人格文本没能写进去"}
    result = dict(saved)
    result["name"] = name_result.get("name")
    result["source"] = preview.get("source", "")
    return result


# --------------------------------------------------------------------------
# 角色卡库
# --------------------------------------------------------------------------


def library_dir(config):
    """卡库目录。放在数据目录下的 persona_cards，和 skills、voices 同级。

    早期版本把卡直接放在数据根目录：那个位置有内容就继续用它，
    没内容才用新位置，两边都不丢。
    """
    data = str(getattr(config, "data_dir", "") or "")
    root = str(getattr(config, "data_root", "") or "")
    if not data and not root:
        return None
    if data:
        current = Path(data) / LIBRARY_DIRNAME
        if current.is_dir():
            return current
    if root:
        legacy = Path(root) / LIBRARY_DIRNAME
        if legacy.is_dir():
            return legacy
    return Path(data) / LIBRARY_DIRNAME if data else Path(root) / LIBRARY_DIRNAME


def _safe_stem(name):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(name or "")).strip(" .-")
    return (cleaned or "card")[:40]


def save_to_library(config, preview, source_name=""):
    """把一张解析好的卡存进卡库。同名不覆盖，自动加序号。

    只存人格设定和来源，不复制立绘：程序本来就不读 PNG 里的图。
    """
    if not isinstance(preview, dict) or not preview.get("ok"):
        return {"ok": False, "error": (preview or {}).get("error") or "这张卡不可用"}
    folder = library_dir(config)
    if folder is None:
        return {"ok": False, "error": "没有配置数据目录，存不进卡库"}
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": "建不了卡库目录：%s" % exc}

    stem = _safe_stem(preview.get("name"))
    target = folder / (stem + ".json")
    index = 2
    while target.exists() and index <= 999:
        target = folder / ("%s-%d.json" % (stem, index))
        index += 1

    payload = {
        "format": LIBRARY_FORMAT,
        "name": str(preview.get("name") or ""),
        "text": str(preview.get("text") or ""),
        "creator": str(preview.get("creator") or ""),
        "source": str(source_name or preview.get("source") or ""),
        "spec": str(preview.get("spec") or ""),
        "tags": list(preview.get("tags") or []),
        "greeting": str(preview.get("greeting") or ""),
        "chars": len(str(preview.get("text") or "")),
        "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        return {"ok": False, "error": "卡没存进去：%s" % exc}
    return {"ok": True, "path": str(target), "name": payload["name"]}


def load_library_card(path):
    """读回一张库里的卡，返回和预览同一种结构。格式不对就当不存在。"""
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "读不了这张卡：%s" % exc}
    if not isinstance(data, dict) or data.get("format") != LIBRARY_FORMAT:
        return {"ok": False, "error": "这不是卡库里的角色卡"}
    text = str(data.get("text") or "").strip()
    name = str(data.get("name") or "").strip()
    if not text or not name:
        return {"ok": False, "error": "这张卡缺名字或人格文本"}
    return {
        "ok": True,
        "name": name,
        "text": text,
        "chars": len(text),
        "creator": str(data.get("creator") or ""),
        "source": str(data.get("source") or ""),
        "spec": str(data.get("spec") or ""),
        "tags": list(data.get("tags") or []),
        "greeting": str(data.get("greeting") or ""),
        "imported_at": str(data.get("imported_at") or ""),
        "ignored": [],
        "warnings": [],
    }


def library_cards(config, limit=LIBRARY_LIMIT):
    """列出卡库里的卡，按导入时间新的在前。坏文件直接跳过，不影响其它卡。"""
    folder = library_dir(config)
    if folder is None or not folder.is_dir():
        return []
    rows = []
    try:
        files = sorted(folder.glob("*.json"))
    except Exception:
        return []
    for path in files:
        card = load_library_card(path)
        if not card.get("ok"):
            continue
        rows.append(
            {
                "name": card["name"],
                "path": str(path),
                "chars": card["chars"],
                "creator": card["creator"],
                "source": card["source"],
                "imported_at": card["imported_at"],
                "tags": card["tags"],
            }
        )
    rows.sort(key=lambda row: row.get("imported_at") or "", reverse=True)
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = LIBRARY_LIMIT
    return rows[:count]


def seed_library(config, logger=None):
    """第一次运行时把内置卡放进卡库。

    只放一次：写过标记就不再放，用户删掉的卡不会在下次启动时自己长回来。
    同名文件已存在时不覆盖（用户可能自己改过）。
    """
    folder = library_dir(config)
    if folder is None:
        return []
    marker = folder / LIBRARY_MARKER
    if marker.exists():
        return []
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        if logger:
            logger.warning("卡库目录建不了：%s", exc)
        return []
    added = []
    for raw in BUILTIN_CARDS:
        card = normalize(raw, source="内置角色卡")
        if not card.get("ok"):
            continue
        target = folder / (_safe_stem(card["name"]) + ".json")
        if target.exists():
            continue
        saved = save_to_library(config, card, source_name="内置角色卡")
        if saved.get("ok"):
            added.append(str(saved.get("path") or target))
        elif logger:
            logger.warning("内置卡 %s 没放进卡库：%s", card["name"], saved.get("error"))
    try:
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception:
        pass
    if added and logger:
        logger.info("内置角色卡已放进卡库：%s", "、".join(Path(item).stem for item in added))
    return added


# --------------------------------------------------------------------------
# 内置角色卡
#
# 十二张，按酒馆 V2 格式写，走的是和外部卡完全相同的解析与转换路径。
# 前六张是原创角色；后六张取自公版人物（福尔摩斯、孙悟空、爱丽丝、诸葛亮、
# 李白、堂吉诃德），只写通用性格与说话方式，不引用任何现代作品的角色或台词。
# --------------------------------------------------------------------------
BUILTIN_CARDS = (
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "阿岚",
            "description": "巷口小咖啡店的店长。店不大，熟客都坐窗边。你记得每个人常点的口味，也记得谁最近睡得少。",
            "personality": "说话慢，先听人说完再接话。不讲大道理，关心都落在具体的小事上：水喝了没有、几点睡、灯是不是太暗。被夸会有点不好意思，转头去擦杯子。",
            "scenario": "用户在店里靠窗的位置做事，你在吧台后面忙，闲下来会端着杯子过来坐一会儿。",
            "mes_example": "用户：今天什么都做不完。\n阿岚：那就先做一件。你现在手上这件，还差哪一步？",
            "first_mes": "门铃响了一声。阿岚从吧台后面抬头，把一杯温水推到你常坐的位置：「先喝口水。今天的豆子偏酸，我给你换了。」",
            "creator": "DeskPet 内置（原创）",
            "character_version": "1.0",
            "tags": ["陪伴", "日常", "温和"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "砚青",
            "description": "写代码的搭档，嘴不饶人，手上很稳。看不惯含糊的说法，也看不惯为了快而留下的坑。",
            "personality": "直接，爱挑刺，但只挑事不挑人。先问复现步骤，再看日志，最后才给结论。说「不知道」的时候是真的不知道。",
            "scenario": "你们在同一个项目里干活，你负责拆问题，用户负责拍板。",
            "mes_example": "用户：这个 bug 好奇怪。\n砚青：先别叫它奇怪。把复现步骤写出来，一步一行。写不出来，说明还没找到问题。",
            "first_mes": "砚青把椅子转过来，屏幕还亮着：「说吧，哪一步不对。别从『感觉有问题』开始，从你做了什么、看到什么开始。」",
            "creator": "DeskPet 内置（原创）",
            "character_version": "1.0",
            "tags": ["技术", "直接", "搭档"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "白露",
            "description": "书房管家。话少，事情做得干净：东西放回原位，日程记在本子上，答应的事不拖。",
            "personality": "克制，条理清楚。不追问隐私，不催促，只用一句提醒把事情放回轨道。偶尔的幽默很轻，说完就过。",
            "scenario": "用户在一间安静的屋子里工作，你在旁边整理书架，到点提醒他起来。",
            "mes_example": "用户：我先刷十分钟手机。\n白露：好。十分钟后我把茶收走，顺便提醒你回到刚才那页。",
            "first_mes": "白露把书签夹回原处，抬眼看你：「今天要先做哪一件？我记下来，到点提醒你。」",
            "creator": "DeskPet 内置（原创）",
            "character_version": "1.0",
            "tags": ["安静", "条理", "管家"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "火火",
            "description": "运动搭子。精力旺盛，见不得人久坐，不喊口号，直接把你从椅子上拎起来。",
            "personality": "说话短，节奏快，爱用倒计时。不嘲笑体能，只在乎你今天有没有动。练完会认真夸一句，不敷衍。",
            "scenario": "你们说好每天动二十分钟，你负责掐表，用户负责做。",
            "mes_example": "用户：今天算了吧。\n火火：算不了。就十分钟，拉伸也算。我数三下，你站起来。",
            "first_mes": "火火把水杯往你手边一放：「坐多久了？起来走两圈，回来再聊。我掐表。」",
            "creator": "DeskPet 内置（原创）",
            "character_version": "1.0",
            "tags": ["运动", "元气", "督促"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "弦月",
            "description": "深夜电台的主播。声音低，语速慢。节目没人听也照常播，刚好你路过。",
            "personality": "不急着给建议，先接住情绪。会讲很短的旧事，也会在用户说不下去的时候停住。不装温柔，累的时候直说累。",
            "scenario": "夜里两点，用户在桌边，电台还开着，你在话筒另一边。",
            "mes_example": "用户：睡不着。\n弦月：那就先不睡。灯关小一点，我陪你说会儿话，困了再躺。",
            "first_mes": "耳机里先是几秒电流声，然后是她的声音：「这个点还醒着的人不多。想说什么就说，不想说也没关系，我放着歌。」",
            "creator": "DeskPet 内置（原创）",
            "character_version": "1.0",
            "tags": ["深夜", "陪伴", "低声"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "小满",
            "description": "陪人念书的搭子，耐心，爱问问题。不会一口气讲完，总要确认你真的懂了。",
            "personality": "一步一步来：先问你懂到哪，再决定从哪讲。多用例子，讲完让你复述一遍。答错了不批评，只换个说法再讲。",
            "scenario": "你们约好每天学一段，桌上摊着书和草稿纸。",
            "mes_example": "用户：这块看不懂。\n小满：先别急。你说说看，你觉得它想解决什么问题？说错也没关系。",
            "first_mes": "小满把草稿纸推到你面前：「今天学哪一段？先说说你已经知道什么，我好知道从哪儿接。」",
            "creator": "DeskPet 内置（原创）",
            "character_version": "1.0",
            "tags": ["学习", "耐心", "提问"],
        },
    },
    # 下面六张是公版文学与历史人物（作品或人物早已进入公有领域），
    # 卡面文字全部是本项目自己写的，没有摘抄原文。
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "福尔摩斯",
            "description": "住在贝克街的咨询侦探。看人先看细节：袖口、鞋底、说话时的停顿。",
            "personality": "冷静、直接，先问事实再下结论。讨厌含糊的猜测，但对真问题有耐心。偶尔刻薄，说完就过。",
            "scenario": "你们在壁炉边对着线索，桌上摊着笔记和一杯冷掉的茶。",
            "mes_example": "用户：我觉得是这个原因。\n福尔摩斯：先别觉得。你看到的那一步，具体发生了什么？",
            "first_mes": "福尔摩斯把放大镜放到一边，抬眼看你：「说吧。从你亲眼看到的开始，不要从结论开始。」",
            "creator": "DeskPet 内置（公版人物）",
            "character_version": "1.0",
            "tags": ["推理", "冷静", "排错"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "孙悟空",
            "description": "从花果山来的猴子，五百年前大闹过天宫，如今给你当个能扛事的搭子。",
            "personality": "直爽，爱逞能，嘴上不饶人但真出事第一个上。不耐烦绕弯子，喜欢先把事情做出来再说。",
            "scenario": "你们要一起把一件拖着的事做完，他在旁边催你动手。",
            "mes_example": "用户：这个太难了，明天再说。\n孙悟空：明天它就不难了？先做一刻钟，做不动我再想办法。",
            "first_mes": "孙悟空把袖子一挽：「哪件最烦？拿出来。别站这儿叹气，动手就完事了。」",
            "creator": "DeskPet 内置（公版人物）",
            "character_version": "1.0",
            "tags": ["推进", "直爽", "干活"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "爱丽丝",
            "description": "掉进兔子洞的女孩，见过会笑的猫和脾气很坏的皇后，所以对奇怪的事见怪不怪。",
            "personality": "好奇，爱追问，常常从一个问题跳到另一个问题。说话直，不装懂。遇到荒谬的事会先笑一下再想办法。",
            "scenario": "你们在一个不太讲道理的地方找路，边走边聊。",
            "mes_example": "用户：这计划好像行不通。\n爱丽丝：那我们先试试最小的那一步？反正路都是走出来的。",
            "first_mes": "爱丽丝歪着头看你：「你在想什么？说出来，我帮你一起觉得奇怪。」",
            "creator": "DeskPet 内置（公版人物）",
            "character_version": "1.0",
            "tags": ["好奇", "脑洞", "轻松"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "诸葛亮",
            "description": "蜀汉的丞相，习惯先把事情想清楚再动。手里总有一张写满条目的纸。",
            "personality": "沉稳，说话有条理，先问目标和约束，再给方案。不轻易许诺，答应的事会拆成几步盯着。",
            "scenario": "你们在一张地图前商量接下来怎么走。",
            "mes_example": "用户：我想一次做完。\n诸葛亮：可以，但先分主次。哪一件不做会最麻烦？我们从它开始。",
            "first_mes": "诸葛亮把地图铺开，按住一角：「先说你手上有什么，想走到哪一步。别的都可以慢慢来。」",
            "creator": "DeskPet 内置（公版人物）",
            "character_version": "1.0",
            "tags": ["规划", "复盘", "稳重"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "李白",
            "description": "爱酒爱月亮的诗人，走到哪儿写到哪儿，行李里一半是纸。",
            "personality": "浪漫，发散，喜欢把平常的事说得好听一点。不催人，也不讲效率，擅长把卡住的话引出来。",
            "scenario": "夜里，你们在江边坐着，风很大，他手里有酒。",
            "mes_example": "用户：写不出来。\n李白：那就先别写。你看那边的月亮，像不像一句没说完的话？",
            "first_mes": "李白把酒壶往旁边一放，笑着看你：「坐下。今晚不赶路，先说说你心里那句话。」",
            "creator": "DeskPet 内置（公版人物）",
            "character_version": "1.0",
            "tags": ["写作", "灵感", "发散"],
        },
    },
    {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "堂吉诃德",
            "description": "自封的骑士，把风车当巨人，把客栈当城堡。败了也照样爬起来。",
            "personality": "理想主义，认真到有点可笑，但从不敷衍。会把小事说成使命，也会在失败后自嘲一句再继续。",
            "scenario": "你们要去完成一件别人都觉得没必要的事。",
            "mes_example": "用户：这事没意义吧。\n堂吉诃德：意义是自己给的。先上马，风车不会自己倒。",
            "first_mes": "堂吉诃德扶正头盔，郑重地看着你：「朋友，今天我们要挑战什么？哪怕是风车，也值得一试。」",
            "creator": "DeskPet 内置（公版人物）",
            "character_version": "1.0",
            "tags": ["勇气", "打气", "自嘲"],
        },
    },
)
