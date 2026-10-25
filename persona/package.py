"""角色包：一个文件夹装一套角色资料。

用户以后训练好的人格，或者自己准备的角色，按这个格式放进来就能用：

    characters/<任意名字>/character.json
    {
      "id": "xiaodie",
      "name": "小蝶",
      "version": "1.0",
      "description": "一句话介绍",
      "persona": {
        "name": "小蝶",
        "style": "说话短、爱吐槽",
        "traits": ["嘴硬", "心软"],
        "rules": ["不撒娇"]
      },
      "avatar": {
        "provider": "live2d",
        "model": "xiaodie.model3.json",
        "scale": 1.0,
        "visible": true
      },
      "actions": {
        "Idle": "Idle",
        "Blink": "Blink",
        "Happy": {"motion": "Happy", "index": 0, "expression": "smile"}
      },
      "voice": {"pack": "xiaozhuo"}
    }

各段都可以缺。清单只写真实存在的资源，不用为了凑格式编文件。

这里只负责发现、读取、选择。不联网下载、不做角色市场。
avatar 与 actions 段原样读出来交给 characters.manager 去核对真实文件，
本模块不加载模型、不改用户文件。

人格不能因为来自角色包就绕过 Permission，也不能改系统安全规则。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from persona import traits as traits_module
from persona.interface import PersonaProvider

CHARACTER_FILE = "character.json"


def _read_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _traits_desc(raw):
    if not raw:
        return ""
    if isinstance(raw, (list, tuple)):
        raw = "，".join(str(item) for item in raw)
    try:
        return traits_module.describe(traits_module.parse(str(raw)))
    except Exception:
        return ""


@dataclass
class CharacterPackage:
    """一个角色包。缺字段就用默认值，坏文件在读进来之前就被跳过了。"""

    id: str
    name: str = ""
    version: str = ""
    description: str = ""
    path: str = ""
    persona: dict = field(default_factory=dict)
    avatar: dict = field(default_factory=dict)
    actions: dict = field(default_factory=dict)
    voice: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data, path=""):
        data = dict(data or {})
        identifier = str(data.get("id") or Path(path).name or "").strip()
        if not identifier:
            return None
        persona = data.get("persona")
        avatar = data.get("avatar")
        actions = data.get("actions")
        voice = data.get("voice")
        return cls(
            id=identifier,
            name=str(data.get("name") or "").strip(),
            version=str(data.get("version") or "").strip(),
            description=str(data.get("description") or "").strip(),
            path=str(path),
            persona=dict(persona) if isinstance(persona, dict) else {},
            avatar=dict(avatar) if isinstance(avatar, dict) else {},
            actions=dict(actions) if isinstance(actions, dict) else {},
            voice=dict(voice) if isinstance(voice, dict) else {},
        )

    def describe(self):
        return {
            "id": self.id,
            "name": self.name or self.id,
            "version": self.version or None,
            "path": self.path,
            "has_avatar": bool(self.avatar),
            "actions": len(self.actions),
            "has_voice": bool(self.voice),
        }


def list_packages(root):
    """扫描角色包目录。目录不在或某个包坏了就跳过，不抛异常。"""
    if not root:
        return []
    base = Path(root)
    try:
        folders = sorted(item for item in base.iterdir() if item.is_dir())
    except Exception:
        return []
    packages = []
    for folder in folders:
        data = _read_json(folder / CHARACTER_FILE)
        package = CharacterPackage.from_dict(data, path=str(folder)) if data else None
        if package is not None:
            packages.append(package)
    return packages


class PackagePersonaProvider(PersonaProvider):
    """把选中的角色包套在默认人格外面；没装包时行为与默认人格完全一样。"""

    def __init__(self, default, packages=(), selected=""):
        self.default = default
        self.packages = list(packages)
        self.selected = str(selected or "").strip()

    # ---------- 选择 ----------
    def select(self, package_id):
        self.selected = str(package_id or "").strip()
        return self.active()

    def active(self):
        if not self.selected:
            return None
        for package in self.packages:
            if package.id == self.selected:
                return package
        return None

    # ---------- PersonaProvider ----------
    def get_profile(self):
        profile = dict(self.default.get_profile() or {})
        package = self.active()
        if package is None:
            return profile
        persona = package.persona or {}
        if persona.get("name"):
            profile["name"] = str(persona["name"])
        if persona.get("style"):
            profile["style"] = str(persona["style"])
        traits_desc = _traits_desc(persona.get("traits"))
        if traits_desc:
            profile["traits_desc"] = traits_desc
        examples = persona.get("examples")
        if examples:
            profile["examples"] = list(examples) if isinstance(examples, (list, tuple)) else [examples]
        profile["package"] = package.id
        return profile

    def get_behavior_rules(self):
        package = self.active()
        rules = (package.persona or {}).get("rules") if package else None
        if rules:
            return [str(rule) for rule in rules]
        return self.default.get_behavior_rules()

    def get_current_state(self):
        return self.default.get_current_state()

    def set_traits(self, traits):
        """调整默认人格的性格强度。角色包自带性格时，角色包的写法优先。"""
        setter = getattr(self.default, "set_traits", None)
        return setter(traits) if setter else {}

    def feedback_buckets(self):
        """用户纠正过的说法还要继续生效，所以跟着默认人格走。"""
        getter = getattr(self.default, "feedback_buckets", None)
        return getter() if getter else {}

    def describe(self):
        """只给开发者面板看，绝不发给模型。"""
        package = self.active()
        return {
            "selected": self.selected or None,
            "installed": [item.id for item in self.packages],
            "active": package.id if package else None,
            "has_avatar": bool(package.avatar) if package else False,
        }
