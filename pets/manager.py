"""多宠物名册：家里可以住不止一只桌宠。

当前阶段一个窗口同时显示一只，可以切换。
每只宠物有自己的名字、人格（走 PersonaProvider）、形象名（等 AvatarProvider 实接）
和绑定的外部设备名（走 DeviceManager）。

这里只做名册和切换，不做形象渲染，也不做人格训练。
"""

from dataclasses import dataclass, field

DEFAULT_PET_ID = "default"


@dataclass
class PetProfile:
    """一只桌宠的身份。缺字段都有默认值，配置文件写一半也能用。"""

    id: str
    name: str = ""
    persona_package: str = ""
    avatar: str = ""
    device: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        identifier = str(data.get("id") or data.get("name") or "").strip()
        if not identifier:
            return None
        return cls(
            id=identifier,
            name=str(data.get("name") or "").strip(),
            persona_package=str(data.get("persona_package") or data.get("persona") or "").strip(),
            avatar=str(data.get("avatar") or "").strip(),
            device=str(data.get("device") or "").strip(),
            note=str(data.get("note") or "").strip(),
        )

    def label(self):
        return self.name or self.id

    def describe(self):
        return {
            "id": self.id,
            "name": self.label(),
            "persona_package": self.persona_package or None,
            "avatar": self.avatar or None,
            "device": self.device or None,
        }


class PetManager:
    """宠物列表和当前活跃的那只。切换人格不改程序，也不改安全规则。"""

    def __init__(self, profiles=(), active=DEFAULT_PET_ID, persona=None, logger=None):
        self.logger = logger
        self.persona = persona
        self._pets = {}
        for profile in profiles:
            self.register(profile)
        if not self._pets:
            self.register(PetProfile(id=DEFAULT_PET_ID, name="默认"))
        self.active = str(active or DEFAULT_PET_ID)
        if self.active not in self._pets:
            self.active = DEFAULT_PET_ID if DEFAULT_PET_ID in self._pets else self.names()[0]
        self.switch_count = 0

    # ---------- 名册 ----------
    def register(self, profile):
        if profile is None:
            return {"ok": False, "error": "空的宠物资料"}
        identifier = str(getattr(profile, "id", "") or "").strip()
        if not identifier:
            return {"ok": False, "error": "宠物没有名字"}
        profile.id = identifier
        self._pets[identifier] = profile
        return {"ok": True, "pet": identifier}

    def get(self, pet_id):
        return self._pets.get(str(pet_id or "").strip())

    def names(self):
        return sorted(self._pets)

    def list_profiles(self):
        return [self._pets[name] for name in self.names()]

    def current(self):
        return self.get(self.active)

    def session_id(self):
        """每只宠物有自己的会话，记忆不串门。"""
        return f"pet:{self.active}"

    # ---------- 切换 ----------
    def select(self, pet_id):
        profile = self.get(pet_id)
        if profile is None:
            return {"ok": False, "error": f"没有叫 {pet_id} 的宠物"}
        self.active = profile.id
        self.switch_count += 1
        self._apply(profile)
        self._log("switched to %s", profile.id)
        return {"ok": True, "pet": profile.describe()}

    def refresh(self):
        """把当前这只的人格再同步一次（人格包是后装的也能对上）。"""
        profile = self.current()
        if profile is None:
            return None
        return self._apply(profile)

    def _apply(self, profile):
        if self.persona is None:
            return None
        getter = getattr(self.persona, "select", None)
        if getter is None:
            return None
        try:
            return getter(profile.persona_package)
        except Exception as exc:
            self._log("persona switch failed: %s", exc)
            return None

    def describe(self):
        """只给开发者面板看，不发给模型。"""
        current = self.current()
        return {
            "count": len(self._pets),
            "active": self.active,
            "pets": [profile.describe() for profile in self.list_profiles()],
            "switches": self.switch_count,
            "persona_package": current.persona_package if current else None,
        }

    def _log(self, message, *args):
        if self.logger:
            self.logger.info(message, *args)


def from_config(config, persona=None, logger=None):
    """默认宠物 + 每个角色包一只。装了什么角色包就多几只宠物。"""
    profiles = [
        PetProfile(
            id=DEFAULT_PET_ID,
            name=str(getattr(config, "persona_name", "") or "").strip(),
            note="默认人格",
        )
    ]
    for package in _packages(config):
        avatar = package.avatar or {}
        profiles.append(
            PetProfile(
                id=package.id,
                name=package.name or package.id,
                persona_package=package.id,
                avatar=str(avatar.get("model") or avatar.get("provider") or ""),
                note=package.description,
            )
        )
    return PetManager(
        profiles=profiles,
        active=str(getattr(config, "character", "") or "") or DEFAULT_PET_ID,
        persona=persona,
        logger=logger,
    )


def _packages(config):
    root = str(getattr(config, "characters_dir", "") or "")
    if not root:
        return []
    try:
        from persona.package import list_packages

        return list_packages(root)
    except Exception:
        return []
