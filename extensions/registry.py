"""统一扩展注册表。

把清单和用户状态合到一起，回答界面和 Agent 真正要问的问题：
有哪些扩展、哪几个能用、为什么不能用、启用了什么、装了什么。

三条硬规则：
1. 许可证没过审阅的，不能启用，也不能随包分发。
2. 可选扩展没装就是没装，不假装能跑。
3. 带脚本的扩展启用前要单独确认，读清单一律不执行脚本。
"""

from extensions import health as health_module
from extensions.catalog import builtin_catalog
from extensions.store import ExtensionStore

FILTERS = (
    ("installed", "已安装"),
    ("recommended", "推荐技能"),
    ("review", "待核验"),
    ("needs_setup", "需要配置"),
    ("disabled", "已禁用"),
    ("developer", "开发者专用"),
    ("end_user", "面向最终用户"),
    ("updates", "更新与兼容性"),
    ("optional", "可选扩展"),
    ("builtin", "内置"),
    ("offline", "本地运行"),
    ("network", "需要网络"),
    ("model", "需要模型"),
    ("permission", "需要授权"),
    ("blocked", "不可用"),
)


class ExtensionRegistry:
    def __init__(self, catalog=None, store=None, checker=None):
        self.store = store if store is not None else ExtensionStore()
        self.checker = checker or health_module.check
        self._manifests = []
        self._by_id = {}
        self.last_error = ""
        for manifest in catalog if catalog is not None else builtin_catalog():
            self.register(manifest)

    # ---------- 登记 ----------
    def register(self, manifest):
        problems = manifest.validate()
        if problems:
            # 自己的清单写错了要立刻知道，不能悄悄带进去。
            raise ValueError("%s 清单有问题：%s" % (getattr(manifest, "id", "?"), "；".join(problems)))
        if manifest.id in self._by_id:
            raise ValueError("扩展 id 重复：%s" % manifest.id)
        self._manifests.append(manifest)
        self._by_id[manifest.id] = manifest
        return manifest

    # ---------- 查询 ----------
    def all(self):
        return list(self._manifests)

    def get(self, ext_id):
        return self._by_id.get(str(ext_id or ""))

    def by_kind(self, kind):
        return [item for item in self._manifests if item.kind == str(kind or "")]

    def by_source(self, source):
        return [item for item in self._manifests if item.source == str(source or "")]

    def kinds(self):
        seen = []
        for item in self._manifests:
            if item.kind not in seen:
                seen.append(item.kind)
        return seen

    # ---------- 状态 ----------
    def state(self, ext_id):
        return self.store.state(ext_id)

    def installed(self, ext_id):
        if self.get(ext_id) is None:
            return False
        if self.get(ext_id).source == "builtin":
            return True
        return bool(self.state(ext_id).get("installed"))

    def is_enabled(self, ext_id):
        """内置扩展默认开着；装上的可选扩展要用户明确打开过。"""
        manifest = self.get(ext_id)
        if manifest is None:
            return False
        if manifest.license_blocked():
            return False
        saved = self.state(ext_id)
        if manifest.source == "builtin":
            return bool(saved.get("enabled", True))
        return bool(saved.get("enabled")) and bool(saved.get("installed"))

    def enabled(self):
        return [item for item in self._manifests if self.is_enabled(item.id)]

    def usable(self, ext_id):
        """能不能真的用：开着、环境齐了、许可证也过了。"""
        manifest = self.get(ext_id)
        if manifest is None or not self.is_enabled(ext_id):
            return False
        return self.health(ext_id).get("status") == health_module.OK

    # ---------- 开关 ----------
    def _verdict_gate(self, manifest):
        """核验结论没过的扩展，一律不给启用。"""
        if manifest.verdict == "review":
            return manifest.verdict_note or "内容或依赖还没核验完，先别开"
        if manifest.verdict == "unsuitable":
            return manifest.verdict_note or "核验结论是不适用，不开"
        return ""

    def can_enable(self, ext_id):
        """能不能启用，不能就给出人话原因。"""
        manifest = self.get(ext_id)
        if manifest is None:
            return False, "没有这个扩展"
        if manifest.license_blocked():
            return False, "许可证不允许随包分发，启用前先过审阅"
        gate = self._verdict_gate(manifest)
        if gate:
            return False, gate
        if manifest.source != "builtin" and not self.installed(ext_id):
            return False, "还没安装，装好才能启用"
        if manifest.scripts:
            return False, "带脚本，需要单独确认后才能启用"
        return True, ""

    def enable(self, ext_id, confirmed=False):
        """启用。带脚本的扩展必须显式确认，默认拒绝。"""
        manifest = self.get(ext_id)
        if manifest is None:
            return False, "没有这个扩展"
        if manifest.license_blocked():
            return False, "许可证不允许随包分发，先过审阅"
        gate = self._verdict_gate(manifest)
        if gate:
            return False, gate
        if manifest.source != "builtin" and not self.installed(ext_id):
            return False, "还没安装，装好才能启用"
        if manifest.scripts and not confirmed:
            return False, "这个扩展带脚本，要单独确认才能启用"
        if not self.store.set_enabled(ext_id, True):
            return False, self.store.last_error or "状态写不进去"
        return True, "已启用"

    def disable(self, ext_id):
        if self.get(ext_id) is None:
            return False, "没有这个扩展"
        if not self.store.set_enabled(ext_id, False):
            return False, self.store.last_error or "状态写不进去"
        return True, "已停用"

    def install(self, ext_id, pinned="", source=""):
        """登记「装好了」。真正的下载安装由用户自己决定，这里只记状态。"""
        manifest = self.get(ext_id)
        if manifest is None:
            return False, "没有这个扩展"
        if not self.store.mark_installed(ext_id, pinned=pinned or manifest.pinned, source=source or manifest.source):
            return False, self.store.last_error or "状态写不进去"
        return True, "已登记"

    def uninstall(self, ext_id):
        manifest = self.get(ext_id)
        if manifest is None:
            return False, "没有这个扩展"
        if manifest.source == "builtin":
            return False, "内置扩展不能卸载"
        if not self.store.uninstall(ext_id):
            return False, "没装过"
        return True, "已卸载"

    # ---------- 健康与视图 ----------
    def health(self, ext_id):
        manifest = self.get(ext_id)
        if manifest is None:
            return {"status": health_module.UNKNOWN, "detail": "没有这个扩展", "checks": []}
        try:
            return self.checker(manifest)
        except Exception as exc:
            return {"status": health_module.UNKNOWN, "detail": "检查失败：%s" % exc, "checks": []}

    def rows(self, flt="", kind=""):
        out = []
        for manifest in self._manifests:
            if kind and manifest.kind != kind:
                continue
            if flt and not self._matches(manifest, flt):
                continue
            row = manifest.to_row()
            check = self.health(manifest.id)
            row["installed"] = self.installed(manifest.id)
            row["enabled"] = self.is_enabled(manifest.id)
            row["usable"] = self.usable(manifest.id)
            row["health"] = check.get("status", "")
            row["health_label"] = health_module.STATUS_LABELS.get(check.get("status", ""), check.get("status", ""))
            row["health_detail"] = check.get("detail", "")
            row["can_enable"], row["can_enable_reason"] = self.can_enable(manifest.id)
            row["bundle_blocked"] = manifest.bundle_blocked()
            out.append(row)
        out.sort(key=lambda item: (item["kind"], item["source"] != "builtin", item["name"]))
        return out

    def _matches(self, manifest, flt):
        if flt == "installed":
            return self.installed(manifest.id)
        if flt == "recommended":
            # 推荐＝内容核验过、现在就能用上的候选，不是「装好了的」。
            return manifest.verdict == "importable" and not self.is_enabled(manifest.id)
        if flt == "review":
            return manifest.verdict == "review" or manifest.license_status in ("review-needed", "unknown")
        if flt == "needs_setup":
            # 装得上但还没装、也没被许可证挡住的，就是要先配置的那种。
            return manifest.source != "builtin" and not manifest.license_blocked() and not self.installed(manifest.id)
        if flt == "disabled":
            return self.installed(manifest.id) and not self.is_enabled(manifest.id)
        if flt == "developer":
            return manifest.audience == "developer"
        if flt == "end_user":
            return manifest.audience == "user"
        if flt == "updates":
            return self.installed(manifest.id) and self.health(manifest.id).get("status") != health_module.OK
        if flt in ("optional", "builtin", "dev"):
            return manifest.source == flt
        if flt == "offline":
            return bool(manifest.offline) and not manifest.requires_network
        if flt == "network":
            return bool(manifest.requires_network)
        if flt == "model":
            return bool(manifest.requires_model) or bool(manifest.requires_api_key)
        if flt == "permission":
            return bool(manifest.permissions) and "none" not in manifest.permissions
        if flt == "blocked":
            return bool(manifest.bundle_blocked()) or not self.can_enable(manifest.id)[0]
        return True

    def summary(self):
        """给界面顶部看的计数。"""
        rows = self.rows()
        return {
            "total": len(rows),
            "enabled": sum(1 for item in rows if item["enabled"]),
            "use_now": sum(1 for item in rows if item["usable"]),
            "installed": sum(1 for item in rows if item["installed"]),
            "optional": sum(1 for item in rows if item["source"] == "optional"),
            "need_review": sum(1 for item in rows if item["license_status"] in ("review-needed", "unknown")),
            "blocked": sum(1 for item in rows if item["license_status"] == "blocked"),
            "review": sum(1 for item in rows if item["verdict"] == "review"),
            "importable": sum(1 for item in rows if item["verdict"] == "importable"),
            "developer": sum(1 for item in rows if item["audience"] == "developer"),
        }

    def filters(self):
        return [{"key": key, "label": label, "count": len(self.rows(key))} for key, label in FILTERS]
