"""Live2D 接入层。模型由用户自己准备，程序只负责发现、校验、映射与下发。

当前不做渲染：真正的画面需要一个渲染端口（Cubism Core + 你自己的模型文件）。
这一层保证的是：
    模型放在约定的位置就能被发现，形式错误不会报错；
    表情与动作能对到模型自己声明的名字上；
    缺模型或缺渲染器时如实说明，绝不假装在显示。

Agent Core 只认 AvatarProvider，接不接 Live2D 都不改它。

模型放法（目录可配，默认在用户数据目录的 live2d/ 下）：
    live2d/<名字>/<名字>.model3.json
"""

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from avatar.interface import EMOTION_EXPRESSIONS, AvatarProvider
from characters import actions as action_table

MODEL_SUFFIXES = (".model3.json", ".model.json")

# 情绪到动作组的默认映射。名字对不上就如实跳过，不乱播。
EMOTION_MOTIONS = {
    "neutral": "Idle",
    "calm": "Idle",
    "thinking": "Idle",
    "sad": "Idle",
    "tired": "Idle",
    "sleepy": "Idle",
    "happy": "TapBody",
    "excited": "TapBody",
    "curious": "TapBody",
    "angry": "TapBody",
    "annoyed": "TapBody",
}

# 动画名到动作组。同样只是候选，对不上就跳过。
ANIMATION_MOTIONS = {
    "blink": "Idle",
    "breathe": "Idle",
    "sleep": "Idle",
    "look_around": "Idle",
    "look_away": "Idle",
    "sway": "Idle",
    "nod": "TapBody",
    "jump": "TapBody",
    "wave": "TapBody",
    "look_at": "TapBody",
    "puff": "TapBody",
    "dangle": "TapBody",
}

NO_MODEL = "还没有放模型"
NO_HOST = "还没有安装 Live2D 渲染器"

# refresh() 的哨兵：不传模型就表示"只换动作表，模型照旧"。
_UNCHANGED = object()


def _read_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _expressions_of(refs):
    """表情名单：新格式带 Name，老格式只有文件名。"""
    raw = refs.get("Expressions")
    if raw is None:
        raw = refs.get("expressions")
    names = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            name = str(item.get("Name") or "").strip()
            if not name:
                name = _stem(item.get("File"))
        else:
            name = _stem(item)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _motions_of(refs):
    """动作组名到数量：{"Idle": 2, "TapBody": 3}。"""
    raw = refs.get("Motions")
    if raw is None:
        raw = refs.get("motions")
    groups = {}
    if not isinstance(raw, dict):
        return groups
    for group, items in raw.items():
        name = str(group or "").strip()
        if not name:
            continue
        groups[name] = len(items) if isinstance(items, list) else 1
    return groups


def _stem(value):
    """文件名变名字：模型去掉 .model3.json，表情去掉 .exp.json。"""
    name = Path(str(value or "")).name
    for suffix in MODEL_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    for suffix in (".json", ".exp"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _physics_of(refs):
    """物理配置文件名。没有就空着，不做任何假设。"""
    raw = refs.get("Physics")
    if raw is None:
        raw = refs.get("physics")
    return str(raw).strip() if isinstance(raw, str) else ""


def _pose_of(refs):
    """姿态配置文件名。没有就空着。"""
    raw = refs.get("Pose")
    if raw is None:
        raw = refs.get("pose")
    return str(raw).strip() if isinstance(raw, str) else ""


def _textures_of(refs):
    raw = refs.get("Textures")
    if raw is None:
        raw = refs.get("textures")
    return tuple(str(item) for item in raw) if isinstance(raw, list) else ()

def _files_of(value):
    """一段引用里可能是一条路径，也可能是带 File 的字典列表。只认识这两种。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            name = item.get("File") or item.get("file")
        else:
            name = item
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def referenced_files(refs):
    """模型自己声明的依赖文件（相对模型目录）。只读声明，不猜别的。"""
    if not isinstance(refs, dict):
        return ()
    names = []
    for key in ("Moc", "moc"):
        value = refs.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for key in ("Textures", "textures", "Expressions", "expressions"):
        names.extend(_files_of(refs.get(key)))
    motions = refs.get("Motions")
    if motions is None:
        motions = refs.get("motions")
    if isinstance(motions, dict):
        for items in motions.values():
            names.extend(_files_of(items))
    for key in ("Physics", "physics", "Pose", "pose"):
        value = refs.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return tuple(unique)


def find_models(root):
    """扫一遍目录里的 Live2D 模型。目录不在、文件坏了都不抛异常。"""
    if not root:
        return []
    base = Path(root)
    if not base.is_dir():
        return []
    found = []
    try:
        files = sorted(base.rglob("*"))
    except Exception:
        return []
    for item in files:
        if not item.is_file():
            continue
        if not item.name.endswith(MODEL_SUFFIXES):
            continue
        model = Live2DModel.from_file(item)
        if model is not None:
            found.append(model)
    found.sort(key=lambda model: model.id.lower())
    return found


@dataclass
class Live2DModel:
    """一个用户自己准备的 Live2D 模型资料。只读数据，不加载贴图。"""

    id: str
    path: str = ""
    name: str = ""
    expressions: tuple = ()
    motions: dict = field(default_factory=dict)
    textures: tuple = ()
    refs: dict = field(default_factory=dict)
    physics: str = ""
    pose: str = ""

    @classmethod
    def from_file(cls, path):
        data = _read_json(path)
        if data is None:
            return None
        refs = data.get("FileReferences")
        if not isinstance(refs, dict):
            refs = {}
        identifier = _stem(path)
        if not identifier:
            return None
        return cls(
            id=identifier,
            path=str(path),
            name=str(data.get("Name") or "").strip(),
            expressions=_expressions_of(refs),
            motions=_motions_of(refs),
            textures=_textures_of(refs),
            physics=_physics_of(refs),
            pose=_pose_of(refs),
            refs=dict(refs),
        )

    def label(self):
        return self.name or self.id

    def match_expression(self, expression):
        """把情绪对到模型声明的表情名上，对不上返回 None。"""
        wanted = str(expression or "").strip()
        if not wanted or not self.expressions:
            return None
        lowered = wanted.lower()
        for name in self.expressions:
            if name.lower() == lowered:
                return name
        for name in self.expressions:
            if lowered in name.lower() or name.lower() in lowered:
                return name
        return None

    def match_motion(self, group):
        """把动作组对到模型声明的名字上，对不上返回 None。"""
        wanted = str(group or "").strip()
        if not wanted or not self.motions:
            return None
        lowered = wanted.lower()
        for name in self.motions:
            if name.lower() == lowered:
                return name
        for name in self.motions:
            if lowered in name.lower() or name.lower() in lowered:
                return name
        return None

    def missing_files(self):
        """声明了却找不到的文件。空元组表示引用都有效。只报告，不改模型。"""
        if not self.path:
            return ()
        base = Path(self.path).parent
        missing = []
        for name in referenced_files(self.refs):
            if not (base / name).is_file():
                missing.append(name)
        return tuple(missing)

    def complete(self):
        return not self.missing_files()

    def describe(self):
        return {
            "id": self.id,
            "name": self.label(),
            "path": self.path,
            "expressions": list(self.expressions),
            "motions": dict(self.motions),
            "textures": len(self.textures),
            "physics": self.physics,
            "pose": self.pose,
            "missing": list(self.missing_files()),
        }


class Live2DHost(ABC):
    """渲染端口。等以后有 Live2D 运行时再实现，这里只定接口。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """能不能真的把画面显示出来。"""

    @abstractmethod
    def load(self, model):
        """装载一个模型。"""

    @abstractmethod
    def set_expression(self, expression):
        """切表情。"""

    @abstractmethod
    def play_motion(self, group, index=0):
        """播动作。"""

    @abstractmethod
    def snapshot(self):
        """当前状态。"""


class NullLive2DHost(Live2DHost):
    """没有渲染器时的默认实现：如实说没装，不假装在画。"""

    name = "none"

    def is_available(self):
        return False

    def load(self, model):
        return {"ok": False, "host": self.name, "error": NO_HOST}

    def set_expression(self, expression):
        return {"ok": False, "host": self.name, "error": NO_HOST}

    def play_motion(self, group, index=0):
        return {"ok": False, "host": self.name, "error": NO_HOST}

    def snapshot(self):
        return {"host": self.name, "available": False, "reason": NO_HOST}

    def describe(self):
        return {"host": self.name, "available": False, "note": NO_HOST}


class Live2DProvider(AvatarProvider):
    """AvatarProvider 的 Live2D 实现。

    没模型或没渲染器时只记录状态，界面不再绘制旧的 Canvas 占位脸。
    """

    name = "live2d"

    def __init__(
        self,
        model=None,
        host=None,
        fallback=None,
        logger=None,
        emotions=None,
        motions=None,
        animations=None,
        action_map=None,
    ):
        self.model = model
        self.host = host or NullLive2DHost()
        self.fallback = fallback
        self.logger = logger
        self.emotions = dict(emotions or EMOTION_EXPRESSIONS)
        self.motions = dict(motions or EMOTION_MOTIONS)
        self.animations = dict(animations or ANIMATION_MOTIONS)
        # 角色包自己写的动作映射。空的时候沿用上面的默认候选，行为与以前完全一致。
        self.action_map = dict(action_map or {})
        self.expression = "normal"
        self.animation = None
        self.state = {}
        self.loaded = False
        self.reason = NO_MODEL if model is None else ""
        self.expressions_sent = 0
        self.motions_sent = 0
        self.missed = 0
        self.last_miss = None
        self.last_command = None
        self.last_action = None
        self.actions_played = 0
        self._missed_names = set()

    # ---------- 装配 ----------
    def attach_fallback(self, provider):
        """保留兼容接口：旧界面可挂一个状态接收器，生产界面不画占位脸。"""
        self.fallback = provider
        return provider

    # ---------- AvatarProvider ----------
    def set_expression(self, expression):
        self.expression = str(expression or "normal")
        self._send_expression(self.expression)
        self._forward_to_fallback("set_expression", self.expression)
        return self.expression

    def play_animation(self, animation):
        self.animation = animation
        name = str(animation or "")
        plan = self._try_action(action_table.ANIMATION_ACTIONS.get(name.lower()))
        if plan is None:
            self._send_motion(self.animations.get(name, name))
        self._forward_to_fallback("play_animation", animation)
        return self.animation

    def set_state(self, state):
        self.state = dict(state or {})
        emotion = str(self.state.get("emotion") or "").strip()
        if emotion:
            plan = self._try_action(action_table.EMOTION_ACTIONS.get(emotion))
            if plan is None:
                self.expression = self.emotions.get(emotion, emotion)
                self._send_expression(self.expression)
                self._send_motion(self.motions.get(emotion, ""))
        self._forward_to_fallback("set_state", self.state)
        return self.state

    # ---------- 角色包动作 ----------
    def play_action(self, action):
        """按角色包的动作表播一个逻辑动作。

        角色包没配、模型里没有，都如实返回状态，绝不拿别的动作冒充成功。
        """
        plan = action_table.resolve(self.action_map, action, self.model)
        self.last_action = dict(plan)
        if plan["status"] == action_table.OK:
            self.actions_played += 1
            if plan["motion"]:
                self._send_motion(plan["motion"])
            if plan["expression"]:
                self.expression = plan["expression"]
                self._send_expression(plan["expression"])
        else:
            self._miss(plan["label"] or "未知动作", plan["reason"])
        return plan

    def action_report(self):
        """逐个逻辑动作报可用情况。界面和开发者面板读这一份。"""
        return action_table.report(self.action_map, self.model)

    def _try_action(self, action):
        """角色包为这个动作配了资源才走动作表；没配就交回默认候选。"""
        if not action or action not in self.action_map:
            return None
        return self.play_action(action)

    def refresh(self, model=_UNCHANGED, action_map=None):
        """换模型或换动作表之后重新装载。渲染器不支持就如实返回失败。"""
        if model is not _UNCHANGED:
            self.model = model
            self.loaded = False
        if action_map is not None:
            self.action_map = dict(action_map)
        return self.load()

    def snapshot(self):
        return {
            "expression": self.expression,
            "animation": self.animation,
            "state": dict(self.state),
            "model": self.model.id if self.model else None,
            "rendering": self.rendering(),
        }

    def describe(self):
        """只给开发者面板看，不发给模型。"""
        return {
            "provider": self.name,
            "model": self.model.describe() if self.model else None,
            "host": getattr(self.host, "name", "?"),
            "host_available": self.host_available(),
            "rendering": self.rendering(),
            "reason": self.reason or None,
            "fallback": getattr(self.fallback, "name", None),
            "expressions_sent": self.expressions_sent,
            "motions_sent": self.motions_sent,
            "missed": self.missed,
            "last_miss": self.last_miss,
            "last_command": self.last_command,
            "actions": action_table.summary(self.action_map, self.model),
            "last_action": self.last_action,
            "actions_played": self.actions_played,
        }

    # ---------- 角色窗口 ----------
    def _host_call(self, method, *args):
        """转发给渲染宿主。宿主没有这个能力就如实说不支持。"""
        getter = getattr(self.host, method, None)
        if getter is None:
            return {"ok": False, "host": getattr(self.host, "name", "?"), "error": NO_HOST}
        try:
            return getter(*args)
        except Exception as exc:
            self._warn("live2d host %s failed: %s", method, exc)
            return {"ok": False, "error": f"渲染器报错：{exc}"}

    def load_model(self, model=None):
        if model is not None:
            self.model = model
        return self.load()

    def unload_model(self):
        self.loaded = False
        return self._host_call("unload_model")

    def set_visible(self, visible):
        return self._host_call("set_visible", bool(visible))

    def set_scale(self, value):
        return self._host_call("set_scale", value)

    def set_emotion(self, emotion):
        """情绪进到这里就分两路：能对上的走表情/动作，对不上的只记状态。"""
        self.set_state({"emotion": str(emotion or ""), "intensity": 0.5})
        return self._host_call("set_emotion", emotion)

    def host_status(self):
        getter = getattr(self.host, "get_status", None)
        if getter is None:
            return getattr(self.host, "snapshot", lambda: {})()
        try:
            return getter()
        except Exception as exc:
            self._warn("live2d host status failed: %s", exc)
            return {}

    def get_status(self):
        """给界面看的合并视图，只含真实检测结果，不发给模型。"""
        return {
            "provider": self.name,
            "model": self.model.describe() if self.model else None,
            "host": getattr(self.host, "name", "?"),
            "host_available": self.host_available(),
            "rendering": self.rendering(),
            "loaded": bool(self.loaded),
            "reason": self.reason or None,
            "expressions_sent": self.expressions_sent,
            "motions_sent": self.motions_sent,
            "missed": self.missed,
            "last_miss": self.last_miss,
            "host_status": self.host_status(),
        }

    # ---------- 状态 ----------
    def host_available(self):
        try:
            return bool(self.host.is_available())
        except Exception as exc:
            self._warn("live2d host probe failed: %s", exc)
            return False

    def rendering(self):
        """真的在画吗：有模型、有渲染器、且模型装上了。"""
        return bool(self.model and self.host_available() and self.loaded)

    def load(self):
        """把模型交给渲染端口。缺东西时如实说缺什么。"""
        if self.model is None:
            self.loaded = False
            self.reason = NO_MODEL
            return {"ok": False, "error": NO_MODEL}
        if not self.host_available():
            self.loaded = False
            self.reason = NO_HOST
            return {"ok": False, "error": NO_HOST}
        try:
            result = self.host.load(self.model) or {}
        except Exception as exc:
            self.loaded = False
            self.reason = f"模型装载失败：{exc}"
            self._warn("live2d load failed: %s", exc)
            return {"ok": False, "error": self.reason}
        self.loaded = bool(result.get("ok"))
        self.reason = "" if self.loaded else str(result.get("error") or "模型装不上")
        self.last_command = {"load": self.model.id}
        return dict(result)

    # ---------- 内部 ----------
    def _send_expression(self, expression):
        if not self._renderable():
            return None
        declared = self.model.match_expression(expression)
        if declared is None:
            self._miss(expression, "模型里没有这个表情")
            return None
        result = self._call_host("set_expression", declared)
        if result.get("ok"):
            self.expressions_sent += 1
        return result

    def _send_motion(self, group):
        wanted = str(group or "").strip()
        if not wanted or not self._renderable():
            return None
        declared = self.model.match_motion(wanted)
        if declared is None:
            self._miss(wanted, "模型里没有这组动作")
            return None
        result = self._call_host("play_motion", declared)
        if result.get("ok"):
            self.motions_sent += 1
        return result

    def _renderable(self):
        if self.model is None:
            self.reason = NO_MODEL
            return False
        if not self.host_available():
            self.reason = NO_HOST
            return False
        if not self.loaded:
            self.reason = "模型还没装上"
            return False
        self.reason = ""
        return True

    def _call_host(self, method, value):
        try:
            result = getattr(self.host, method)(value) or {}
        except Exception as exc:
            self._warn("live2d %s failed: %s", method, exc)
            return {"ok": False, "error": f"渲染器报错：{exc}"}
        self.last_command = {method: value}
        return dict(result)

    def _miss(self, value, why):
        """模型没有这份资源时如实记一笔；同一个名字只提醒一次，别把日志刷爆。"""
        self.missed += 1
        self.last_miss = {"value": value, "why": why}
        name = str(value)
        first = name not in self._missed_names
        self._missed_names.add(name)
        if self.logger is None:
            return
        if first:
            self.logger.warning("live2d miss: %s (%s)", value, why)
        else:
            self.logger.debug("live2d miss again: %s (%s)", value, why)

    def _forward_to_fallback(self, method, value):
        if self.fallback is None:
            return None
        getter = getattr(self.fallback, method, None)
        if getter is None:
            return None
        try:
            return getter(value)
        except Exception as exc:
            self._warn("placeholder avatar %s failed: %s", method, exc)
            return None

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def pick_model(models, wanted):
    """按配置选模型：指定名字就找那个，没指定就用找到的第一个。"""
    if not models:
        return None
    name = str(wanted or "").strip()
    if not name:
        return models[0]
    lowered = name.lower()
    for model in models:
        if lowered in {model.id.lower(), model.name.lower(), Path(model.path).name.lower()}:
            return model
    # 点名的模型找不到就返回 None：选角色这种明确要求不猜。
    # 启动时"宁可显示一个角色"的兜底在 build_provider 里做。
    return None


BUNDLED_DIRNAME = "live2d_models"


def bundled_models_dir():
    """随程序分发的角色模型目录。打包后在解包目录里，开发时在项目 resources/ 下。"""
    try:
        from security.config import resource_path

        return Path(resource_path(BUNDLED_DIRNAME))
    except Exception:
        return None


def seed_bundled_models(config, logger=None):
    """第一次运行时，把随包带的角色模型放进用户的数据目录。

    只补不覆盖：同名目录已经在了（用户自己放过一个，或者之前放过）就一个字都不动。
    返回这次放进去的目录列表。
    """
    source = bundled_models_dir()
    if source is None or not source.is_dir():
        return []
    target_root = Path(str(getattr(config, "live2d_dir", "") or ""))
    if not str(target_root):
        return []
    copied = []
    for folder in sorted(source.iterdir()):
        if not folder.is_dir():
            continue
        target = target_root / folder.name
        if target.exists():
            continue
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(folder, target)
        except Exception as exc:
            if logger:
                logger.warning("随包模型 %s 没放进去：%s", folder.name, exc)
            continue
        copied.append(str(target))
    if copied and logger:
        logger.info("随包角色模型已放入用户目录：%s", "、".join(Path(item).name for item in copied))
    return copied


def pick_host(config, logger=None):
    """有渲染环境就用真画面；没有就退回"不假装在画"的空实现。"""
    if not getattr(config, "live2d_render", True):
        return NullLive2DHost()
    try:
        from avatar.web2d import build_host

        host = build_host(config, logger=logger)
    except Exception as exc:
        if logger:
            logger.warning("live2d render host unavailable: %s", exc)
        host = None
    return host or NullLive2DHost()


def build_provider(config, logger=None, host=None, characters=None):
    """按配置装一个 Live2D 形象接口。没模型也照常返回，只是它不会画。

    给了角色管理器就由它决定用哪个模型、配了哪些动作；没给就按配置找，行为与以前一致。
    """
    if characters is not None:
        model = characters.model()
        action_map = characters.action_map()
    else:
        root = str(getattr(config, "live2d_dir", "") or "")
        wanted = str(getattr(config, "live2d_model", "") or "")
        models = find_models(root)
        model = pick_model(models, wanted)
        if model is None and models:
            # 配置里点名的模型在这台机器上找不到（换了电脑、删了模型、拼错了）：
            # 退回扫到的第一个。宁可显示一个角色，也不要因为配置对不上就整个不显示。
            model = models[0]
            if wanted and logger:
                logger.warning("配置里的角色模型 %s 没找到，改用 %s", wanted, model.id)
        action_map = {}
    if host is None:
        # 没有模型就不用去找渲染环境，省掉一次进程外依赖探测。
        host = pick_host(config, logger=logger) if model is not None else NullLive2DHost()
    return Live2DProvider(model=model, host=host, logger=logger, action_map=action_map)
