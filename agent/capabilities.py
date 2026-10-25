"""本地能力层与 Local First Router。

顺序固定：本地能力 → 安全工具 → 陪伴模型 → 任务模型。
原则：能不用模型，就不用模型。

每个能力只读本机真实状态，算不出来就返回 None，由上层如实说明做不到，
绝不编造数据。这里的结果是程序写出来的，不经过模型，也不会把调试信息发出去。
"""

import ast
import ctypes
import operator
import random
import re
import time
from dataclasses import dataclass, field

from agent.router import MODE_LABELS
from persona.traits import active as active_traits

# V5：能力依赖谁。本地能算的排第一，联网和模型都要能如实说做不了。
REQUIRES_LOCAL = "none"
REQUIRES_NETWORK = "network"
REQUIRES_MODEL = "model"

REQUIRES_LABELS = {
    REQUIRES_LOCAL: "本地就能用",
    REQUIRES_NETWORK: "需要网络",
    REQUIRES_MODEL: "需要模型",
}

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

EMOTION_WORDS = {
    "neutral": "平静",
    "happy": "开心",
    "sad": "低落",
    "angry": "生气",
    "tired": "累了",
    "excited": "兴奋",
    "thinking": "思考中",
    "curious": "好奇",
    "annoyed": "有点烦",
    "calm": "放松",
    "sleepy": "困了",
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_DURATION = re.compile(r"(\d+)\s*(个?小时|分钟|分|秒)")


@dataclass
class LocalAnswer:
    """本地能力的回答：文字是程序写的，不是模型编的。"""

    text: str
    capability: str
    data: dict = field(default_factory=dict)


class LocalCapability:
    """一个本地能力：命中词 + 回答函数（拿不到答案就返回 None）。"""

    def __init__(
        self, name, label, handler, triggers=(), description="", requires=REQUIRES_LOCAL
    ):
        self.name = name
        self.label = label
        self.handler = handler
        self.triggers = tuple(triggers)
        self.description = description or label
        self.requires = requires or REQUIRES_LOCAL

    def matches(self, text):
        return any(word in text for word in self.triggers)

    def run(self, text, context):
        try:
            return self.handler(text, context)
        except Exception:
            # 本地能力出错时保持沉默，交给上层如实说明，不编造结果。
            return None


class CapabilityRouter:
    """本地优先路由器：不调用模型，不上网。"""

    def __init__(
        self,
        memory=None,
        config=None,
        scheduler=None,
        status=None,
        clock=time.localtime,
        skills=None,
        logger=None,
        persona=None,
        tasks=None,
        calendar=None,
        database=None,
        review=None,
        devices=None,
        policy=None,
        worklog=None,
    ):
        self.memory = memory
        # V11：设备管理器。只读它来回答「接了什么设备」，不从这里下发动作。
        self.devices = devices
        self.policy = policy
        self.worklog = worklog
        self.config = config
        self.scheduler = scheduler  # 一次性提醒的入口，没接上就只是不能计时
        self.status = status  # 返回桌宠状态 dict 的函数
        self.persona = persona  # 只读人格，用来回答「你是什么性格」
        # V3：回顾、提醒与项目跟踪。没接上就如实说读不到，绝不编。
        self.tasks = tasks
        self.calendar = calendar
        self.database = database
        self.review = review
        self.clock = clock
        self.logger = logger
        self.skills = list(skills) if skills is not None else default_capabilities()
        self.last = None  # 上一次命中的能力名，只给开发者面板看

    def add(self, capability):
        self.skills.append(capability)
        return capability

    def resolve(self, text):
        """按注册顺序找第一个能回答的本地能力；都不行就返回 None。"""
        lowered = str(text or "").strip().lower()
        if not lowered:
            return None
        for skill in self.skills:
            if not skill.matches(lowered):
                continue
            answer = skill.run(lowered, self)
            if answer:
                self.last = skill.name
                return LocalAnswer(answer, skill.name)
        self.last = None
        return None

    def boundary(self, companion=False, task=False, short=False):
        """诚实的能力边界：说清不联网能做什么，什么需要模型。

        short=True 只回第一句，聊天里报一句就够，不用把整段说明铺进去。
        """
        labels = [skill.label for skill in self.skills if skill.label]
        names = "、".join(labels)
        lines = [f"不用模型我也能做：{names}。"]
        if short:
            # 聊天里每次失败都跟一句，太长会刷屏：只报前几个，其余的指路。
            head = "、".join(labels[:6])
            if len(labels) > 6:
                return f"不用模型我也能做：{head}……问「你能做什么」看全部。"
            return f"不用模型我也能做：{head}。"
        if companion:
            lines.append("日常聊天已经接上模型。")
        else:
            lines.append("开放聊天、推理、写代码需要先配好聊天模型，这些我现在做不到。")
        if task:
            lines.append("动手改文件、执行命令这类事会先问你确认，再交给任务模型和工具。")
        else:
            lines.append("动手改文件、执行命令需要先配好任务模型，而且每次都会先问你。")
        return "".join(lines)

    # ---------- 供各能力读取的只读状态 ----------
    def status_view(self):
        if self.status is None:
            return {}
        try:
            return dict(self.status() or {})
        except Exception:
            return {}

    def preference(self, key):
        if self.memory is None:
            return None
        try:
            return self.memory.get_preference(key)
        except Exception:
            return None


class LocalCapabilityRegistry(CapabilityRouter):
    """能力注册表：启动时就能列出「哪些不用模型、哪些要联网、现在能不能用」。

    probes 是 name -> 返回 (ready, reason) 的函数。没给探针就按依赖如实标注，
    不做假检测，也不在启动时偷偷联网。
    """

    def __init__(self, *args, probes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.probes = dict(probes or {})

    def add(self, capability, requires=None):
        if requires:
            capability.requires = requires
        return super().add(capability)

    def requires_of(self, capability):
        return str(getattr(capability, "requires", REQUIRES_LOCAL) or REQUIRES_LOCAL)

    def readiness(self, capability):
        """返回 (能不能用, 人话原因)。检测本身出错按不可用处理。"""
        probe = self.probes.get(capability.name)
        if probe is not None:
            try:
                ready, reason = probe()
                return bool(ready), str(reason)
            except Exception:
                return False, "能力检测没跑起来"
        requires = self.requires_of(capability)
        if requires == REQUIRES_NETWORK:
            return True, "要联网，取不到就如实说取不到"
        if requires == REQUIRES_MODEL:
            return True, "要模型，模型没配好就说做不了"
        return True, "本机就能算"

    def catalog(self):
        """给能力中心用的一张表，顺序就是路由优先级。"""
        rows = []
        for skill in self.skills:
            requires = self.requires_of(skill)
            ready, reason = self.readiness(skill)
            rows.append(
                {
                    "name": skill.name,
                    "label": skill.label,
                    "requires": requires,
                    "requires_label": REQUIRES_LABELS.get(requires, requires),
                    "ready": ready,
                    "reason": reason,
                }
            )
        return rows

    def offline_names(self):
        """断网、没模型也不受影响的那些能力。"""
        return [
            row["name"]
            for row in self.catalog()
            if row["requires"] == REQUIRES_LOCAL and row["ready"]
        ]


# ---------------------------------------------------------------- 计算与换算

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_CALC_NOISE = (
    "算一下", "算算", "计算", "等于多少", "是多少", "得多少", "多少", "请问",
    "帮我", "？", "?", "＝", "=", "，", ",",
)
_CALC_WORDS = {
    "加": "+",
    "减": "-",
    "乘": "*",
    "除以": "/",
    "除": "/",
    "×": "*",
    "÷": "/",
    "的": "",
}


def _format_number(value):
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return str(value)


def _calc_value(expr):
    """只允许数字和四则运算。不用 eval，避免执行任意代码。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    try:
        return _calc_node(tree.body)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _calc_node(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise TypeError("only numbers")
        return node.value
    if isinstance(node, ast.BinOp):
        func = _OPS.get(type(node.op))
        if func is None:
            raise TypeError("unsupported operator")
        left = _calc_node(node.left)
        right = _calc_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("exponent too large")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ZeroDivisionError("divide by zero")
        return func(left, right)
    if isinstance(node, ast.UnaryOp):
        func = _OPS.get(type(node.op))
        if func is None:
            raise TypeError("unsupported operator")
        return func(_calc_node(node.operand))
    raise TypeError("unsupported expression")


def _calculator(text, ctx):
    body = str(text)
    for word in _CALC_NOISE:
        body = body.replace(word, " ")
    for word, symbol in _CALC_WORDS.items():
        body = body.replace(word, symbol)
    for candidate in re.findall(r"[-+]?\d[\d\s().+\-*/]*", body):
        expr = candidate.replace(" ", "").strip("+-*/.")
        if not re.search(r"\d", expr):
            continue
        if not re.search(r"[+\-*/]", expr):
            continue
        value = _calc_value(expr)
        if value is None:
            continue
        return f"{expr} = {_format_number(value)}"
    return None


_UNIT_WORDS = (
    ("公里", "km"),
    ("千米", "km"),
    ("英里", "mile"),
    ("公斤", "kg"),
    ("千克", "kg"),
    ("斤", "jin"),
    ("磅", "lb"),
    ("厘米", "cm"),
    ("公分", "cm"),
    ("英寸", "inch"),
    ("米", "m"),
    ("摄氏度", "c"),
    ("摄氏", "c"),
    ("华氏度", "f"),
    ("华氏", "f"),
    ("度", "c"),
)

_UNIT_LABELS = {
    "km": "公里",
    "mile": "英里",
    "kg": "公斤",
    "jin": "斤",
    "lb": "磅",
    "cm": "厘米",
    "m": "米",
    "inch": "英寸",
    "c": "摄氏度",
    "f": "华氏度",
}

_RATIOS = {
    ("km", "mile"): 0.621371,
    ("mile", "km"): 1.609344,
    ("kg", "jin"): 2.0,
    ("jin", "kg"): 0.5,
    ("kg", "lb"): 2.204623,
    ("lb", "kg"): 0.453592,
    ("cm", "inch"): 0.393701,
    ("inch", "cm"): 2.54,
    ("m", "cm"): 100.0,
    ("cm", "m"): 0.01,
    ("m", "km"): 0.001,
    ("km", "m"): 1000.0,
    ("m", "inch"): 39.370079,
    ("inch", "m"): 0.0254,
    ("m", "mile"): 0.000621371,
    ("mile", "m"): 1609.344,
}

_CONVERT_TRIGGERS = ("换算", "多少", "等于", "是几", "换成", "转为")


def _scan_units(text):
    """按出现顺序取单位，长的优先（"公斤" 不会被当成 "斤"）。"""
    words = sorted(_UNIT_WORDS, key=lambda item: -len(item[0]))
    found = []
    index = 0
    while index < len(text):
        for word, unit in words:
            if text.startswith(word, index):
                found.append(unit)
                index += len(word)
                break
        else:
            index += 1
    return found


def _convert(text, ctx):
    lowered = str(text)
    if not any(word in lowered for word in _CONVERT_TRIGGERS):
        return None
    number = _NUMBER.search(lowered)
    units = _scan_units(lowered)
    if number is None or len(units) < 2:
        return None
    value = float(number.group())
    source, target = units[0], units[1]
    if source == target:
        return None
    if {source, target} == {"c", "f"}:
        result = value * 9 / 5 + 32 if target == "f" else (value - 32) * 5 / 9
    else:
        ratio = _RATIOS.get((source, target))
        if ratio is None:
            return None
        result = value * ratio
    return (
        f"{_format_number(value)}{_UNIT_LABELS[source]}"
        f" ≈ {_format_number(result)}{_UNIT_LABELS[target]}。"
    )


# ---------------------------------------------------------------- 时间与日期


def _time_now(text, ctx):
    return f"现在是 {time.strftime('%H:%M', ctx.clock())}。"


def _date_today(text, ctx):
    stamp = ctx.clock()
    return f"今天是 {time.strftime('%Y-%m-%d', stamp)}，{WEEKDAY_LABELS[stamp.tm_wday]}。"


# ---------------------------------------------------------------- 倒计时与提醒

_TIMER_TRIGGERS = ("提醒", "叫我", "倒计时", "计时", "闹钟")
_TIMER_NOISE = (
    "提醒我", "提醒一下", "提醒", "叫我", "倒计时", "计时", "闹钟", "帮我", "请",
    "记得", "一下", "之后", "后",
)


def _parse_duration(text):
    total = 0
    for value, unit in _DURATION.findall(str(text)):
        amount = int(value)
        if "小时" in unit:
            total += amount * 3600
        elif "分" in unit:
            total += amount * 60
        else:
            total += amount
    return total or None


def _duration_label(seconds):
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _timer_subject(text):
    body = _DURATION.sub(" ", str(text))
    for word in _TIMER_NOISE:
        body = body.replace(word, " ")
    return body.strip(" ，,。.、") or "时间到了"


def _timer(text, ctx):
    if not any(word in str(text) for word in _TIMER_TRIGGERS):
        return None
    seconds = _parse_duration(text)
    if not seconds:
        return None
    subject = _timer_subject(text)
    if ctx.scheduler is None:
        return "倒计时需要调度器在运行，现在没法计时。"
    ctx.scheduler.schedule_once(
        seconds,
        situation=f"提醒用户：{subject}",
        local_text=f"时间到了：{subject}",
        emotion="happy",
        capability="timer",
    )
    return f"好，{_duration_label(seconds)}后提醒你{subject}。"


# ---------------------------------------------------------------- 状态与系统

_STATUS_TRIGGERS = ("你现在", "你的状态", "桌宠状态", "什么模式", "在做什么")


def _status(text, ctx):
    info = ctx.status_view()
    if not info:
        return None
    mode = MODE_LABELS.get(info.get("mode"), info.get("mode") or "未知")
    emotion = EMOTION_WORDS.get(info.get("emotion"), info.get("emotion") or "未知")
    lines = [f"我现在是{mode}，情绪{emotion}"]
    if not info.get("companion_ready"):
        lines.append("聊天模型未配置")
    if not info.get("task_ready"):
        lines.append("任务模型未配置")
    return "，".join(lines) + "。"


class _PowerStatus(ctypes.Structure):
    _fields_ = (
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    )


class _MemoryStatus(ctypes.Structure):
    _fields_ = (
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def _battery_text():
    try:
        status = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return "读不到电池状态。"
    except Exception:
        return "这台机器读不到电池状态。"
    percent = int(status.BatteryLifePercent)
    if percent < 0 or percent > 100:
        return "这台电脑没有电池，接着电源在用。"
    return f"电量 {percent}%，{'正在充电' if status.ACLineStatus == 1 else '使用电池'}。"


def _memory_text():
    try:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return "读不到系统内存状态。"
    except Exception:
        return "读不到系统内存状态。"
    total = status.ullTotalPhys / 1024**3
    free = status.ullAvailPhys / 1024**3
    return (
        f"内存占用 {int(status.dwMemoryLoad)}%，"
        f"可用 {free:.1f} GB（共 {total:.1f} GB）。"
    )


def _system(text, ctx):
    lowered = str(text)
    if any(word in lowered for word in ("电量", "电池", "电源")):
        return _battery_text()
    if any(word in lowered for word in ("内存", "系统状态", "电脑状态")):
        return _memory_text()
    return None


def battery_text():
    """给本地工具用：读不到就说读不到。"""
    return _battery_text()


def memory_text():
    return _memory_text()


# ---------------------------------------------------------------- 记忆与设置

_MEMORY_LIST_TRIGGERS = ("记住了什么", "记得什么", "我的记忆", "记住了啥", "记了什么")

# 问「我」的事：只答真实记住的，答不上来就直说没记住，不许猜。
_MEMORY_ASK_TRIGGERS = ("记得我", "还记得", "我是谁", "了解我", "我的偏好", "我的习惯")
_MEMORY_ASK_HEADS = (
    "我最喜欢", "我喜欢", "我讨厌", "我爱吃", "我习惯", "我叫", "我的名字", "我的生日",
)
_MEMORY_TRIGGERS = _MEMORY_LIST_TRIGGERS + _MEMORY_ASK_TRIGGERS + _MEMORY_ASK_HEADS
_MEMORY_QUERY_NOISE = ("你还记得", "记得我", "我的", "什么", "啥", "吗", "？", "?", "来着")
_MEMORY_TAIL = ("什么", "啥", "吗", "?", "？")
_REMEMBER_WORDS = ("记住", "记一下", "记下来", "别忘了", "别忘", "记着")
_MEMORY_NONE = "我还没有记住这个。"
_CLIP = 40


def _clip(content):
    content = str(content).strip()
    return content[:_CLIP] + ("…" if len(content) > _CLIP else "")


def _memory_question(text):
    """「我喜欢什么」「你还记得吗」才算问记忆，普通聊天不抢答。"""
    if any(word in text for word in _MEMORY_ASK_TRIGGERS):
        return True
    asked = any(word in text for word in _MEMORY_ASK_HEADS)
    return asked and any(word in text for word in _MEMORY_TAIL)


_RPS_TRIGGERS = ("猜拳", "石头剪刀布", "剪刀石头布", "比试手气")
_RPS_MOVES = ("石头", "剪刀", "布")
_RPS_BEATS = {"石头": "剪刀", "剪刀": "布", "布": "石头"}


def _rps_move(text):
    found = [move for move in _RPS_MOVES if move in text]
    return found[0] if len(found) == 1 else ""


def _rps(text, ctx):
    """猜拳小游戏：纯程序规则，不经过模型，也不记分。"""
    move = _rps_move(text)
    if not move:
        return "来，你先出拳：石头、剪刀、还是布？"
    mine = random.choice(_RPS_MOVES)
    if mine == move:
        return f"我出{mine}，你出{move}，平局，再来一次？"
    if _RPS_BEATS[mine] == move:
        return f"我出{mine}，你出{move}，这局我赢啦。"
    return f"我出{mine}，你出{move}，这局你赢了，再来？"


_DIARY_TRIGGERS = ("日记", "每日小结", "昨天过得", "写下什么")


def _diary(text, ctx):
    """日记是程序写的，本地读出来就行，不经过模型。"""
    service = getattr(ctx.memory, "diary", None) if ctx.memory is not None else None
    if service is None:
        return None
    try:
        service.ensure_recent()
        rows = service.read(3)
    except Exception:
        return None
    if not rows:
        return "我还没写过日记，等今天过完就写一条。"
    lines = "\n".join(f"{row['day']}：{row['content']}" for row in rows)
    return "我记下的日子：\n" + lines


_RELATIONSHIP_TRIGGERS = ("认识多久", "认识几天", "我们认识", "什么关系", "关系怎么样")


def _relationship(text, ctx):
    """关系状态是程序记的，本地回答，不经过模型。"""
    state = getattr(ctx.memory, "relationship", None) if ctx.memory is not None else None
    if state is None:
        return None
    try:
        note = state.note()
    except Exception:
        return None
    if not note:
        return "我们还没怎么聊过，多聊几句我就记住了。"
    return note


def _memories_text(rows, prefix):
    return prefix + "；".join(_clip(row["content"]) for row in rows)


def _memory(text, ctx):
    if ctx.memory is None:
        return None
    # 「记住…」是写入，由记忆写入路径回答，这里不抢答。
    if any(word in text for word in _REMEMBER_WORDS) and not text.rstrip().endswith(_MEMORY_TAIL):
        return None
    try:
        if any(word in text for word in _MEMORY_LIST_TRIGGERS):
            rows = ctx.memory.recent_memories(limit=5)
            if not rows:
                return "我还没记住什么。你说「记住……」我就记下来。"
            return _memories_text(rows, f"我记着 {len(rows)} 条：")
        if not _memory_question(text):
            return None  # 「我喜欢你」这类不是问记忆，交给模型
        query = text
        for word in _MEMORY_QUERY_NOISE:
            query = query.replace(word, " ")
        rows = ctx.memory.search(query.strip() or text, limit=3)
        if not rows:
            return _MEMORY_NONE
        return _memories_text(rows, "我记着：")
    except Exception:
        return None


_SETTINGS_TRIGGERS = ("免打扰", "静音时段", "主动提醒", "桌宠设置", "你的设置")


def _devices(text, ctx):
    """看设备：读设备管理器里的真实状态。读不到就返回 None，交给别的能力或模型。"""
    manager = getattr(ctx, "devices", None)
    if manager is None:
        return None
    try:
        states = list(manager.list_devices())
    except Exception:
        return None
    if not states:
        return "现在没有接入任何设备。想试的话，可以在设备中心连一台模拟设备。"
    parts = []
    for state in states:
        name = state.get("device") or state.get("name") or "?"
        kind = "模拟设备" if state.get("simulated") else "真机"
        where = "已连接" if state.get("connected") else "未连接"
        extra = "，固件 %s" % state["firmware"] if state.get("firmware") else ""
        parts.append("%s（%s，%s%s）" % (name, kind, where, extra))
    return "设备：" + "；".join(parts) + "。"


def _settings(text, ctx):
    raw = ctx.preference("proactive_enabled")
    if raw is None and ctx.memory is None:
        return None
    enabled = str(raw).strip().lower() not in {"0", "false", "no", "off"} if raw else True
    quiet = ctx.preference("quiet_hours") or "23-7"
    model = ""
    if ctx.config is not None:
        model = getattr(ctx.config, "companion_model", "") or ""
    return (
        f"主动提醒{'开着' if enabled else '关着'}，免打扰时段 {quiet}。"
        f"聊天模型：{model or '未配置'}。"
    )


# ---------------------------------------------------------------- 宠物名册

_PET_TRIGGERS = (
    "有哪些宠物", "宠物列表", "换个宠物",
    "换宠物", "换一个宠物", "换只宠物",
)


def _pets(text, ctx):
    """宠物名册是程序管的，不用模型。"""
    roster = getattr(ctx, "pets", None)
    if roster is None:
        return None
    try:
        profiles = roster.list_profiles()
    except Exception:
        return None
    if len(profiles) <= 1:
        return "现在就我一个。"
    for profile in profiles:
        name = profile.name
        if name and name in text:
            result = roster.select(profile.id)
            if result.get("ok"):
                # 记忆归属跟着换，免得换过去还能读到上一只的私有记忆。
                if ctx.memory is not None:
                    try:
                        ctx.memory.use_pet(profile.id)
                    except Exception:
                        pass
                return f"好，现在让{name}来。"
            return None
    labels = "、".join(profile.label() for profile in profiles)
    return f"现在有：{labels}。想换谁？"


# ---------------------------------------------------------------- 人格

_PERSONA_TRIGGERS = ("你是什么性格", "你的性格", "你的人格", "你是谁", "你叫什么", "你的名字")


def _persona(text, ctx):
    """报自己的名字和性格。性格只影响表达，不影响权限判断。"""
    persona = getattr(ctx, "persona", None)
    if persona is None:
        return None
    try:
        profile = persona.get_profile() or {}
    except Exception:
        return None
    name = str(profile.get("name") or "").strip() or "桌宠"
    lines = [f"你好，我叫{name}，是一个正在开发中的陪伴式桌宠。"]
    traits = active_traits(profile.get("traits"))
    if traits:
        lines.append("我的性格：" + "、".join(f"{trait} {level}" for trait, level in traits) + "。")
    else:
        lines.append("我还没有特别的性格设定。")
    return "".join(lines)


_CONTEXT_MODES = (
    ("dnd", ("勿扰模式", "开启勿扰", "进入勿扰", "别打扰我")),
    ("rest", ("休息模式", "休息情境", "进入休息")),
    ("work", ("工作模式", "工作情境", "进入工作")),
    ("companion", ("陪伴模式", "正常聊天", "退出勿扰", "回到陪伴")),
)
_CONTEXT_TRIGGERS = ("当前情境", "什么情境") + tuple(
    word for _mode, words in _CONTEXT_MODES for word in words
)


def _context(text, ctx):
    """Switch or report the explicit context mode. No model involved."""
    body = str(text or "")
    if ctx.policy is None:
        return None
    for mode, words in _CONTEXT_MODES:
        if not any(word in body for word in words):
            continue
        if not ctx.policy.set_mode(mode):
            return "情境没切换成功：偏好存不下来。"
        info = ctx.policy.summary()
        return "情境切到%s。%s" % (info["label"], info["description"])
    if any(word in body for word in _CONTEXT_TRIGGERS):
        info = ctx.policy.summary()
        return "现在是%s情境。%s" % (info["label"], info["description"])
    return None


# ---------------------------------------------------------------- 边界

_BOUNDARY_TRIGGERS = (
    "你能做什么", "你会什么", "有什么功能", "能干什么", "你的能力",
    "能帮我做什么", "会做什么",
)


def _boundary(text, ctx):
    info = ctx.status_view()
    return ctx.boundary(
        companion=bool(info.get("companion_ready")), task=bool(info.get("task_ready"))
    )


# ------------------------------------------------- 回顾、提醒与项目跟踪（V3）

_REVIEW_TRIGGERS = (
    "今天做了什么", "今天干了什么", "今天都做了什么", "日报", "今日总结",
    "周报", "本周总结", "这周做了什么", "这一周做了什么", "回顾一下",
)
_REMINDER_TRIGGERS = (
    "我的提醒", "有什么提醒", "提醒列表", "日程列表", "查看提醒", "有哪些日程",
    "取消提醒", "取消日程", "删掉提醒",
)
_PROJECT_TRIGGERS = ("项目状态", "项目进展", "项目到哪", "进行到哪", "项目怎么样了")


def _review_service(ctx):
    if ctx.review is not None:
        return ctx.review
    if ctx.database is None:
        return None
    try:
        from memory.review import ReviewService

        ctx.review = ReviewService(ctx.database, memory=ctx.memory, tasks=ctx.tasks)
    except Exception:
        return None
    return ctx.review


def _review(text, ctx):
    if not any(word in str(text) for word in _REVIEW_TRIGGERS):
        return None
    service = _review_service(ctx)
    if service is None:
        return "回顾需要读到本地记录，现在读不到。"
    weekly = any(word in str(text) for word in ("周报", "本周", "这周", "这一周", "一周"))
    report = service.weekly() if weekly else service.daily()
    if not report.get("ok"):
        return "回顾暂时读不到记录。"
    return report["text"]


def _number_in(text):
    match = re.search(r"\d+", str(text))
    return int(match.group(0)) if match else None


def _reminders(text, ctx):
    if not any(word in str(text) for word in _REMINDER_TRIGGERS):
        return None
    if ctx.calendar is None:
        return "日程服务没有接上，现在看不到提醒。"
    wants_cancel = any(word in str(text) for word in ("取消", "删掉", "删除"))
    try:
        result = ctx.calendar.list_events(limit=20)
    except Exception:
        return "日程读不出来，稍后再试。"
    if not result or not result.get("ok"):
        return "日程读不出来，稍后再试。"
    events = result["data"]["events"]
    if wants_cancel:
        key = _number_in(text)
        if key is None:
            return "要取消哪条？把编号告诉我，编号用「我的提醒」就能看到。"
        try:
            done = ctx.calendar.cancel_event(key)
        except Exception as exc:
            return "取消失败：" + str(exc)
        if not done.get("ok"):
            return done.get("error", "没取消成。")
        return "取消了：%s。" % done["data"]["title"]
    if not events:
        return "现在没有记着的提醒。"
    lines = ["现在记着这些提醒："]
    for row in events[:5]:
        lines.append("- #%s %s（%s）" % (row["id"], row["title"], row["when_at"]))
    lines.append("要取消就说「取消提醒 编号」。")
    return " ".join(lines)


def _project(text, ctx):
    if not any(word in str(text) for word in _PROJECT_TRIGGERS):
        return None
    lines = []
    if ctx.memory is not None:
        try:
            rows = ctx.memory.browse(kind="project", limit=5)
        except Exception:
            rows = []
        for row in rows or []:
            content = row.get("content") if isinstance(row, dict) else str(row)
            if content:
                lines.append("- " + str(content)[:80])
    if ctx.tasks is not None:
        try:
            records = ctx.tasks.recent(limit=3)
        except Exception:
            records = []
        for record in records or []:
            lines.append("- 任务「%s」%s" % (record.goal[:30], record.state_label))
    if not lines:
        return "我这边还没有项目的记录。多做几个任务，我就记住了。"
    return "我记得的项目情况：" + " ".join(lines)


_TASKS_TRIGGERS = (
    "任务状态", "任务列表", "任务进度", "有哪些任务", "有什么任务", "什么任务",
    "任务做到哪", "上次做到哪", "任务失败", "失败原因", "为什么失败", "怎么失败了",
    "没做完的任务", "未完成的任务",
)
_CONTINUE_TRIGGERS = ("继续昨天", "继续上次", "继续之前", "接着上次", "接着做上次")
_TASK_DONE_STATES = ("done", "completed", "skipped", "ok", "success")


def _row_cell(row, name):
    """任务步骤是库里的一行，取不到就给空串。"""
    try:
        if name in row.keys():
            return str(row[name] or "")
    except Exception:
        pass
    try:
        return str(row.get(name) or "")
    except Exception:
        return ""


def _task_line(record):
    goal = str(getattr(record, "goal", "") or "")[:36]
    label = str(getattr(record, "state_label", "") or "")
    return "%s（%s）" % (goal or "（没写目标）", label)


def _task_left_steps(ctx, task_id, limit=3):
    """这条任务还剩哪几步。读不到就返回空列表，绝不编。"""
    store = ctx.tasks
    if store is None:
        return []
    try:
        steps = list(store.steps(str(task_id or "")))
    except Exception:
        return []
    rows = []
    for step in steps:
        status = _row_cell(step, "status")
        title = _row_cell(step, "title")
        if not title or status in _TASK_DONE_STATES:
            continue
        rows.append(title)
        if len(rows) >= limit:
            break
    return rows


def _tasks(text, ctx):
    """任务做到哪了、上次为什么没成。只读本地任务库，读不到就说读不到。"""
    body = str(text or "")
    asked = any(word in body for word in _TASKS_TRIGGERS)
    wants_continue = any(word in body for word in _CONTINUE_TRIGGERS)
    if not (asked or wants_continue):
        return None
    store = ctx.tasks
    if store is None:
        return "任务记录在本地任务库里，现在读不到。"
    if wants_continue:
        status = ctx.status_view() or {}
        if status.get("task_ready"):
            # 有任务模型就交给它接着干，本地不抢。
            return None
    try:
        unfinished = list(store.unfinished(limit=5))
        recent = list(store.recent(limit=5))
    except Exception:
        return "任务库读不出来，稍后再试。"
    if not unfinished and not recent:
        return "我这边还没有任务记录。要做的事说清楚，我会先给计划再动手。"
    lines = []
    asks_failure = any(
        word in body for word in ("失败", "为什么", "怎么没", "没成", "错误")
    )
    if asks_failure:
        stuck = [
            row for row in recent
            if str(getattr(row, "state", "") or "") in ("failed", "waiting_user")
        ]
        if not stuck:
            lines.append("最近没有卡住的任务。")
        for record in stuck[:3]:
            reason = str(getattr(record, "error", "") or "（没记下原因）")[:60]
            goal = str(getattr(record, "goal", "") or "")[:24]
            lines.append("「%s」没成：%s。" % (goal, reason))
    if unfinished:
        names = "、".join(_task_line(record) for record in unfinished[:3])
        lines.append("还没做完的有 %d 件：%s。" % (len(unfinished), names))
        left = _task_left_steps(ctx, getattr(unfinished[0], "id", ""))
        if left:
            lines.append("下一步：%s。" % "、".join(left))
    elif not lines:
        lines.append("现在没有没做完的任务。")
    if wants_continue:
        lines.append("要接着做需要任务模型；配好以后我就照上面的步骤继续。")
    return " ".join(lines)


_PLANS_TRIGGERS = (
    "有哪些计划", "计划列表", "待执行的计划", "排了哪些", "等着跑的事",
    "你安排了什么", "主动提醒有哪些", "什么时候会提醒我",
)


def _plans(text, ctx):
    """在等的提醒和主动规则。只列真装上的，并说清关了程序就不跑。"""
    if not any(word in str(text or "") for word in _PLANS_TRIGGERS):
        return None
    lines = []
    if ctx.calendar is not None:
        try:
            result = ctx.calendar.list_events(limit=10)
        except Exception:
            result = None
        events = ((result or {}).get("data") or {}).get("events") or []
        if events:
            lines.append("记着的提醒 %d 条：" % len(events))
            for row in events[:5]:
                lines.append("- %s（%s）" % (row.get("title"), row.get("when_at")))
    rules = list(getattr(ctx.scheduler, "rules", []) or []) if ctx.scheduler is not None else []
    if rules:
        names = [str(getattr(rule, "name", "") or "") for rule in rules]
        shown = [name for name in names if name][:6]
        if shown:
            lines.append("主动规则 %d 条：%s。" % (len(rules), "、".join(shown)))
    if not lines:
        return "现在没有排着的提醒或主动规则。"
    lines.append("这些只在桌宠开着的时候跑；关掉程序就不会自己动。")
    return " ".join(lines)


_WORK_TRIGGERS = (
    "工作多久", "工作记录", "工作日报", "工作周报", "今天用了多久",
    "这周用了多久", "时间花在哪", "应用时长",
)


def _work(text, ctx):
    """Local work report. It never turns disabled state into fake data."""
    if not any(word in str(text or "") for word in _WORK_TRIGGERS):
        return None
    service = getattr(ctx, "worklog", None)
    if service is None:
        return "工作记录没有接入。"
    try:
        weekly = any(word in str(text) for word in ("周报", "这周", "本周"))
        result = service.report(weekly=weekly)
        state = service.state()
    except Exception:
        return "工作记录暂时读不出来。"
    lines = []
    if not state.get("active"):
        lines.append("工作记录当前没有开启，下面是已经存下来的记录。")
    lines.append(str(result.get("text") or "没有工作记录。"))
    return "\n".join(lines)


def default_capabilities():
    """注册顺序就是匹配优先级：越具体的越靠前。"""
    return [
        LocalCapability("boundary", "说明我的能力边界", _boundary, _BOUNDARY_TRIGGERS),
        LocalCapability("timer", "倒计时和提醒", _timer, _TIMER_TRIGGERS),
        LocalCapability("settings", "念设置", _settings, _SETTINGS_TRIGGERS),
        LocalCapability("calculator", "算数", _calculator, ("算一下", "算算", "计算", "等于多少", "得多少")),
        LocalCapability("convert", "单位换算", _convert, _CONVERT_TRIGGERS),
        LocalCapability("time", "报时间", _time_now, ("几点", "报时间", "现在时间", "当前时间")),
        LocalCapability("date", "报日期", _date_today, ("几号", "日期", "星期", "礼拜")),
        LocalCapability("status", "说自己的状态", _status, _STATUS_TRIGGERS),
        LocalCapability("system", "系统状态和电量", _system, ("电量", "电池", "电源", "内存", "系统状态", "电脑状态")),
        LocalCapability("persona", "说自己的名字和性格", _persona, _PERSONA_TRIGGERS),
        LocalCapability("context", "切换陪伴情境", _context, _CONTEXT_TRIGGERS),
        LocalCapability(
            "devices",
            "看设备状态",
            _devices,
            ("接了什么设备", "有什么设备", "设备状态", "设备列表", "外接设备", "连了什么设备"),
        ),
        LocalCapability("pets", "多只宠物切换", _pets, _PET_TRIGGERS),
        LocalCapability("memory", "念出记住的事", _memory, _MEMORY_TRIGGERS),
        LocalCapability("relationship", "关系", _relationship, _RELATIONSHIP_TRIGGERS),
        LocalCapability("diary", "读日记", _diary, _DIARY_TRIGGERS),
        LocalCapability("review", "日报与周报", _review, _REVIEW_TRIGGERS),
        LocalCapability("work", "工作记录与时长", _work, _WORK_TRIGGERS),
        LocalCapability("reminders", "管理提醒", _reminders, _REMINDER_TRIGGERS),
        LocalCapability("project", "项目跟踪", _project, _PROJECT_TRIGGERS),
        LocalCapability(
            "tasks", "看任务做到哪了", _tasks, _TASKS_TRIGGERS + _CONTINUE_TRIGGERS
        ),
        LocalCapability("plans", "看在等着的提醒和计划", _plans, _PLANS_TRIGGERS),
        LocalCapability("game", "猜拳小游戏", _rps, _RPS_TRIGGERS),
    ]
