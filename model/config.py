"""模型配置仓库：只存"怎么连"，不存密钥本身。

地址、模型 ID、用途、启用状态存在 data/models.json；密钥由
security/credentials.py 用 DPAPI 加密后单独保存，这里只记一个 secret_id。
两边分开的好处：备份/导出配置不会顺手把密钥带走。
"""

import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

CONFIG_VERSION = 1
FILE_NAME = "models.json"

ROLES = ("companion", "task")
ROLE_LABELS = {"companion": "日常聊天", "task": "复杂任务"}

# 官方接口地址只作默认值，用户随时可以改；模型 ID 同理，不靠硬编码列表。
PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "docs": "https://platform.openai.com/docs/api-reference",
        "note": "填你自己的 OpenAI API Key，不是 ChatGPT 账号密码。",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "docs": "https://api-docs.deepseek.com/",
        "note": "DeepSeek 官方接口，兼容 OpenAI 的 Chat Completions 格式。",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "docs": "https://help.aliyun.com/zh/model-studio/",
        "note": "阿里云百炼的 OpenAI 兼容模式地址，Key 在百炼控制台创建。",
    },
    "custom": {
        "label": "自定义兼容接口",
        "base_url": "",
        "model": "",
        "docs": "",
        "note": "只填你信任的地址。程序不会把 Key 发到别的域名。",
    },
    "ollama": {
        "label": "Ollama 本地模型",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "",
        "docs": "https://ollama.com/",
        "note": "先自己安装 Ollama 并拉取模型。这里不下载模型，也不需要云 API Key。",
    },
}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def provider_presets():
    return {key: dict(value) for key, value in PROVIDERS.items()}


def provider_label(name):
    return (PROVIDERS.get(str(name or "")) or {}).get("label") or str(name or "")


def clean_text(value, limit=80):
    text = _CONTROL.sub("", str(value or "")).strip()
    return text[:limit]


def check_base_url(url):
    """接口地址校验：只认 https，http 仅限本机；不许带账号密码或查询参数。"""
    text = clean_text(url, 300)
    if not text:
        return "", "接口地址不能为空"
    if "://" not in text:
        return "", "接口地址要带 https:// 前缀"
    try:
        parsed = urlparse(text)
    except Exception:
        return "", "接口地址格式不对"
    if parsed.scheme not in ("https", "http"):
        return "", "接口地址只支持 https（本机可用 http）"
    host = (parsed.hostname or "").lower()
    if not host:
        return "", "接口地址缺少域名"
    if parsed.scheme == "http" and host not in _LOCAL_HOSTS:
        return "", "非本机地址必须用 https，避免密钥以明文经过网络"
    if parsed.username or parsed.password:
        return "", "接口地址里不能带账号或密码"
    if parsed.query or parsed.fragment:
        return "", "接口地址不要带查询参数，密钥只放在请求头里"
    return text.rstrip("/"), ""


def validate(payload):
    """校验一份配置。返回 (清洗后的配置, 错误)。"""
    data = dict(payload or {})
    provider = clean_text(data.get("provider") or "custom", 20).lower()
    if provider not in PROVIDERS:
        return {}, "不认识的服务商"
    model = clean_text(data.get("model"), 120)
    if not model:
        return {}, "模型 ID 不能为空"
    base_url, error = check_base_url(data.get("base_url"))
    if error:
        return {}, error
    requested = data.get("roles")
    if requested is None:
        requested = ["companion"]
    roles = []
    for role in requested:
        name = str(role or "").strip().lower()
        if name in ROLES and name not in roles:
            roles.append(name)
    if not roles:
        return {}, "至少要勾选一个用途"
    name = clean_text(data.get("name"), 40) or "%s · %s" % (
        provider_label(provider),
        model,
    )
    return {
        "name": name,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "roles": roles,
        "enabled": bool(data.get("enabled", True)),
        "note": clean_text(data.get("note"), 120),
    }, ""


def provider_for_url(url):
    """按地址猜一个服务商，只用于从旧配置迁移；猜不出就用自定义。"""
    host = ""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        host = ""
        port = None
    if "api.openai.com" in host:
        return "openai"
    if "deepseek.com" in host:
        return "deepseek"
    if "dashscope.aliyuncs.com" in host or "aliyuncs.com" in host:
        return "qwen"
    if host in _LOCAL_HOSTS and port == 11434:
        return "ollama"
    return "custom"


class ModelConfigRepository:
    """data/models.json 的读写。坏文件当空配置，不阻塞启动。"""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / FILE_NAME

    def _empty(self):
        return {
            "version": CONFIG_VERSION,
            "migrated_env": False,
            "defaults": {role: "" for role in ROLES},
            "configs": [],
        }

    def read(self):
        if not self.path.is_file():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        configs = [row for row in (data.get("configs") or []) if isinstance(row, dict)]
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
        return {
            "version": CONFIG_VERSION,
            "migrated_env": bool(data.get("migrated_env")),
            "defaults": {role: str(defaults.get(role) or "") for role in ROLES},
            "configs": configs,
        }

    def write(self, data):
        """原子写：先写临时文件再替换，写一半断电也不会留下半截 JSON。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, self.path)
            return True
        except Exception:
            return False

    def list(self):
        return list(self.read()["configs"])

    def get(self, config_id):
        for row in self.list():
            if str(row.get("id")) == str(config_id):
                return row
        return None

    def enabled(self, role=""):
        rows = [row for row in self.list() if row.get("enabled", True)]
        if role:
            rows = [row for row in rows if role in (row.get("roles") or [])]
        return rows

    def defaults(self):
        return dict(self.read()["defaults"])

    def default(self, role):
        config_id = self.defaults().get(role) or ""
        row = self.get(config_id) if config_id else None
        if row and row.get("enabled", True) and role in (row.get("roles") or []):
            return row
        # 默认项失效时，退回第一个勾了这个用途的启用配置，别让聊天直接断掉。
        rows = self.enabled(role)
        return rows[0] if rows else None

    def upsert(self, payload):
        cleaned, error = validate(payload)
        if error:
            return {"ok": False, "error": error}
        data = self.read()
        config_id = str((payload or {}).get("id") or "").strip()
        existing = None
        if config_id:
            existing = next(
                (row for row in data["configs"] if str(row.get("id")) == config_id),
                None,
            )
            if existing is None:
                return {"ok": False, "error": "找不到这条配置"}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if existing is None:
            row = dict(cleaned)
            row["id"] = uuid.uuid4().hex[:12]
            row["secret_id"] = uuid.uuid4().hex
            row["created_at"] = now
            row["updated_at"] = now
            row["imported_from_env"] = bool((payload or {}).get("imported_from_env"))
            data["configs"].append(row)
        else:
            row = dict(existing)
            row.update(cleaned)
            row["updated_at"] = now
            index = data["configs"].index(existing)
            data["configs"][index] = row
        for role in row.get("roles") or []:
            if not data["defaults"].get(role):
                data["defaults"][role] = row["id"]
        if not self.write(data):
            return {"ok": False, "error": "配置文件写不进去"}
        return {"ok": True, "config": row}

    def delete(self, config_id):
        """删配置并清掉指向它的默认项。返回原密钥标识，交给界面问用户要不要删。"""
        data = self.read()
        row = next(
            (item for item in data["configs"] if str(item.get("id")) == str(config_id)),
            None,
        )
        if row is None:
            return {"ok": False, "error": "找不到这条配置"}
        data["configs"] = [
            item for item in data["configs"] if str(item.get("id")) != str(config_id)
        ]
        for role, value in list(data["defaults"].items()):
            if str(value) == str(config_id):
                data["defaults"][role] = ""
        if not self.write(data):
            return {"ok": False, "error": "配置文件写不进去"}
        return {"ok": True, "secret_id": row.get("secret_id") or "", "config": row}

    def set_enabled(self, config_id, flag):
        data = self.read()
        for row in data["configs"]:
            if str(row.get("id")) == str(config_id):
                row["enabled"] = bool(flag)
                row["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if not self.write(data):
                    return {"ok": False, "error": "配置文件写不进去"}
                return {"ok": True, "config": row}
        return {"ok": False, "error": "找不到这条配置"}

    def set_default(self, role, config_id):
        role = str(role or "").strip().lower()
        if role not in ROLES:
            return {"ok": False, "error": "不认识的用途"}
        row = self.get(config_id) if config_id else None
        if config_id and row is None:
            return {"ok": False, "error": "找不到这条配置"}
        if row is not None:
            if not row.get("enabled", True):
                return {"ok": False, "error": "先启用这条配置"}
            if role not in (row.get("roles") or []):
                return {"ok": False, "error": "这条配置没有勾选这个用途"}
        data = self.read()
        data["defaults"][role] = str(config_id or "")
        if not self.write(data):
            return {"ok": False, "error": "配置文件写不进去"}
        return {"ok": True, "defaults": data["defaults"]}

    def mark_migrated(self):
        data = self.read()
        data["migrated_env"] = True
        return self.write(data)


def import_env_configs(config, logger=None):
    """把旧的 .env 配置一次性搬进新仓库。只读 .env，不改它。

    伴侣和任务用同一组地址+模型时合成一条，勾两个用途，避免重复条目。
    """
    repo = getattr(config, "model_configs", None)
    secrets = getattr(config, "secrets", None)
    if repo is None or secrets is None:
        return []
    data = repo.read()
    if data.get("migrated_env"):
        return []

    def candidate(suffix):
        base = clean_text(getattr(config, suffix + "_base_url", ""), 300)
        model = clean_text(getattr(config, suffix + "_model", ""), 120)
        key = str(getattr(config, suffix + "_api_key", "") or "").strip()
        return base, model, key

    groups = {}
    for role in ROLES:
        base, model, key = candidate(role)
        if role == "task" and not model:
            continue
        if not model or not base:
            continue
        groups.setdefault((base, model), {"key": "", "roles": []})
        groups[(base, model)]["roles"].append(role)
        if key and not groups[(base, model)]["key"]:
            groups[(base, model)]["key"] = key

    created = []
    for (base, model), info in groups.items():
        saved = repo.upsert(
            {
                "name": "%s · %s" % (provider_label(provider_for_url(base)), model),
                "provider": provider_for_url(base),
                "base_url": base,
                "model": model,
                "roles": info["roles"],
                "enabled": True,
                "imported_from_env": True,
            }
        )
        if not saved.get("ok"):
            if logger:
                logger.warning("旧模型配置没能迁移：%s", saved.get("error"))
            continue
        row = saved["config"]
        if info["key"]:
            stored = secrets.save(row["secret_id"], info["key"])
            if not stored.get("ok") and logger:
                logger.warning("旧 API Key 没能存进安全存储：%s", stored.get("error"))
        created.append(row)
        for role in info["roles"]:
            repo.set_default(role, row["id"])
    repo.mark_migrated()
    if created and logger:
        logger.info("已从旧配置迁移 %d 组模型设置", len(created))
    return created
