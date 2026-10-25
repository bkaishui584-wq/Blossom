"""设备动作注册表：模型只能请求登记过的动作，参数范围由配置声明。

不让模型直接拼串口字节，也不让模型自己编一个动作名。每个动作都要说清楚：
哪些设备能做、参数什么类型什么范围、有没有副作用、要不要用户确认、超时怎么办、
断连时怎么办。

角度这类范围必须由机械结构声明（设备清单里的 limits）；没声明就没有这个动作，
绝不默认「所有舵机都能转 0-180」。
"""

from dataclasses import dataclass, field

# 参数类型。故意只支持这几种，够用且好校验。
KINDS = ("int", "number", "bool", "str")


def copy_limits(limits):
    """范围有两种写法：{min,max} 或者一个名单（比如能放哪几段提示音）。

    拷贝时两种都要留住，不然设备声明的事到电脑端就丢了。
    """
    out = {}
    for key, value in (limits or {}).items():
        if isinstance(value, dict):
            out[str(key)] = dict(value)
        elif isinstance(value, (list, tuple)):
            out[str(key)] = [str(item) for item in value]
    return out


@dataclass
class ParamSpec:
    """一个参数。from_device 表示范围要设备声明，不接受程序猜。"""

    name: str
    kind: str = "int"
    required: bool = False
    default: object = None
    minimum: object = None
    maximum: object = None
    choices: tuple = ()
    from_device: str = ""
    label: str = ""

    def to_dict(self, limits=None):
        row = {
            "name": self.name,
            "kind": self.kind,
            "required": bool(self.required),
            "default": self.default,
            "label": self.label or self.name,
        }
        if self.choices:
            row["choices"] = list(self.choices)
        # declared 的意思是「这个范围必须由设备声明」，不是「现在已经有值了」。
        declared = None
        if self.from_device and isinstance(limits, dict):
            declared = limits.get(self.from_device)
        row["declared"] = bool(self.from_device)
        if isinstance(declared, dict):
            row["min"] = declared.get("min")
            row["max"] = declared.get("max")
        elif isinstance(declared, (list, tuple)):
            # 设备声明的是一个名单（比如能放哪几段提示音），那就当可选值用。
            row["choices"] = [str(item) for item in declared]
        elif not self.from_device:
            row["min"] = self.minimum
            row["max"] = self.maximum
        return row


@dataclass
class ActionSpec:
    """一个已登记的设备动作。"""

    name: str
    description: str = ""
    requires: tuple = ()
    params: tuple = ()
    permission: str = "READ"
    needs_confirmation: bool = False
    side_effect: bool = False
    timeout: float = 3.0
    on_failure: str = "不自动重试，把失败原因原样报给用户"
    on_disconnect: str = "停在上一状态并在界面标成离线，等设备回来再说"
    note: str = ""

    def to_dict(self, limits=None):
        return {
            "name": self.name,
            "description": self.description,
            "requires": list(self.requires),
            "permission": self.permission,
            "needs_confirmation": bool(self.needs_confirmation),
            "side_effect": bool(self.side_effect),
            "timeout": float(self.timeout),
            "on_failure": self.on_failure,
            "on_disconnect": self.on_disconnect,
            "note": self.note,
            "params": [param.to_dict(limits) for param in self.params],
        }


def default_actions():
    """首批动作。只是「登记」了这些动作，设备没声明能力就等于没有。"""
    return (
        ActionSpec(
            name="device.get_status",
            description="问设备要一份运行状态：固件版本、支持的能力、当前输出",
            requires=("status",),
            permission="READ",
            timeout=2.0,
        ),
        ActionSpec(
            name="device.get_capabilities",
            description="问设备会做什么：协议版本、固件版本、能力列表和参数范围",
            requires=("status",),
            permission="READ",
            timeout=2.0,
            note="能力以设备自己报的为准，电脑端不替它猜",
        ),
        ActionSpec(
            name="sensor.read",
            description="读一次传感器数值",
            requires=("sensor",),
            permission="READ",
            timeout=3.0,
            note="读数带有效性标记；设备说这次没测到就不能当成 0",
            params=(
                ParamSpec("channel", "int", required=True, from_device="sensor_channels", label="通道"),
            ),
        ),
        ActionSpec(
            name="led.set",
            description="开关一盏指示灯",
            requires=("led",),
            permission="LOW",
            timeout=2.0,
            params=(
                ParamSpec("channel", "int", required=True, from_device="led_channels", label="通道"),
                ParamSpec("on", "bool", required=True, label="开还是关"),
            ),
        ),
        ActionSpec(
            name="audio.play",
            description="播放一段已经登记在设备上的提示音",
            requires=("audio",),
            permission="LOW",
            side_effect=True,
            timeout=3.0,
            note="只能点已登记的曲目名，不能传文件路径，也不做运行时解码",
            params=(
                ParamSpec("track_id", "str", required=True, from_device="audio_tracks", label="曲目"),
                ParamSpec("volume", "int", required=False, from_device="audio_volume", label="音量"),
            ),
        ),
        ActionSpec(
            name="servo.set_angle",
            description="把舵机转到指定角度",
            requires=("servo",),
            permission="MEDIUM",
            needs_confirmation=True,
            side_effect=True,
            timeout=5.0,
            note="角度、速度、持续时长都由机械结构声明；没声明就不让动",
            params=(
                ParamSpec("channel", "int", required=True, from_device="servo_channels", label="通道"),
                ParamSpec("angle_deg", "int", required=True, from_device="servo_angle", label="角度"),
                ParamSpec("speed_deg_s", "int", required=False, from_device="servo_speed", label="速度"),
                ParamSpec(
                    "duration_ms", "int", required=False,
                    from_device="servo_duration_ms", label="持续时长",
                ),
            ),
        ),
        ActionSpec(
            name="device.stop",
            description="让设备停下当前动作（固件真的实现了才有效）",
            requires=("stop",),
            permission="LOW",
            timeout=2.0,
            on_failure="停下失败要立刻说明，不能当它已经停了",
        ),
    )


def _coerce(kind, value):
    """按类型收一次。收不动就返回 (None, 错误)。bool 不算 int，免得 True 变成 1。"""
    if kind == "bool":
        if isinstance(value, bool):
            return value, ""
        return None, "需要 true 或 false"
    if kind == "str":
        if isinstance(value, str):
            return value, ""
        return None, "需要字符串"
    if isinstance(value, bool):
        return None, "需要数字，不是布尔值"
    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, "需要整数"
        if isinstance(value, float) and value != number:
            return None, "需要整数"
        return number, ""
    try:
        return float(value), ""
    except (TypeError, ValueError):
        return None, "需要数字"


class DeviceCommandRegistry:
    """已登记动作的白名单。模型只能从这里挑，挑不到就是做不到。"""

    def __init__(self, specs=None):
        self._specs = {}
        self._order = []
        for spec in specs if specs is not None else default_actions():
            self.register(spec)

    # ---------- 装配与读 ----------
    def register(self, spec):
        name = str(getattr(spec, "name", "") or "")
        if not name:
            raise ValueError("动作必须有名字")
        if name not in self._specs:
            self._order.append(name)
        self._specs[name] = spec
        return spec

    def get(self, name):
        return self._specs.get(str(name or "").strip())

    def names(self):
        return list(self._order)

    def all(self):
        return [self._specs[name] for name in self._order]

    # ---------- 能力判断 ----------
    def supports(self, action, capabilities):
        spec = self.get(action)
        if spec is None:
            return False, "没有登记这个动作：%s" % (action or "(空)")
        have = {str(item) for item in (capabilities or ())}
        missing = [tag for tag in spec.requires if tag not in have]
        if missing:
            return False, "设备没有这个能力：%s" % "、".join(missing)
        return True, ""

    def available_for(self, capabilities, limits=None):
        """这台设备真正能跑的动作。不能跑的也列出来，但带上原因。"""
        rows = []
        for spec in self.all():
            ok, reason = self.supports(spec.name, capabilities)
            row = spec.to_dict(limits)
            row["available"] = bool(ok)
            row["reason"] = reason
            if ok:
                blocked = [
                    param.label or param.name
                    for param in spec.params
                    if param.from_device
                    and param.required
                    and not isinstance(
                        (limits or {}).get(param.from_device), (dict, list, tuple)
                    )
                ]
                if blocked:
                    row["available"] = False
                    row["reason"] = "设备没有声明 %s 的范围" % "、".join(blocked)
            rows.append(row)
        return rows

    # ---------- 校验 ----------
    def validate(self, action, params=None, limits=None, capabilities=None):
        """校验一次请求。返回 (清洗过的参数, 错误)。错误非空就是不许发。"""
        spec = self.get(action)
        if spec is None:
            return None, "没有登记这个动作：%s" % (action or "(空)")
        if capabilities is not None:
            ok, reason = self.supports(spec.name, capabilities)
            if not ok:
                return None, reason
        raw = dict(params or {})
        allowed = {param.name for param in spec.params}
        unknown = sorted(key for key in raw if key not in allowed)
        if unknown:
            return None, "动作 %s 不认识这些参数：%s" % (spec.name, "、".join(unknown))

        clean = {}
        for param in spec.params:
            if param.name not in raw or raw[param.name] is None:
                if param.required:
                    return None, "缺少参数 %s（%s）" % (param.name, param.label or param.name)
                if param.default is not None:
                    clean[param.name] = param.default
                continue
            declared = (limits or {}).get(param.from_device) if param.from_device else None
            if param.from_device and declared is None:
                # 可选参数：设备没声明，就只是这个参数用不了，不连累整个动作。
                if param.required:
                    return None, "设备没有声明 %s 的范围，动作被拒绝" % (param.label or param.name)
                return None, "设备没有声明 %s，这个参数用不了" % (param.label or param.name)
            value, error = _coerce(param.kind, raw[param.name])
            if error:
                return None, "参数 %s %s" % (param.name, error)
            choices = tuple(param.choices)
            if isinstance(declared, (list, tuple)):
                choices = tuple(str(item) for item in declared)
            if choices and value not in choices:
                return None, "参数 %s 只能是 %s" % (param.name, "、".join(map(str, choices)))
            low, high = param.minimum, param.maximum
            if isinstance(declared, dict):
                low, high = declared.get("min"), declared.get("max")
            if low is not None and value < low:
                return None, "参数 %s 不能小于 %s" % (param.name, low)
            if high is not None and value > high:
                return None, "参数 %s 不能大于 %s" % (param.name, high)
            clean[param.name] = value
        return clean, ""

    def catalog(self):
        return [spec.to_dict() for spec in self.all()]
