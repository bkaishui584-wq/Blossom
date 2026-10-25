"""OpenAI 兼容适配层：一套通信实现，四个服务商身份。

OpenAI、DeepSeek、通义千问（百炼兼容模式）和自定义地址的请求格式相同，
差异在默认地址、模型名和报错文案。这里用同一份实现，子类只声明身份，
避免复制四份几乎一样的代码；协议真有差异时再单独覆写。

密钥只从 model/config.py 的配置和 security/credentials.py 里取，
不进日志、不进异常、不进界面。
"""

import json
import time

from . import errors
from .base import ChatDelta, ChatResult, ModelProvider, ModelRole, ToolCall, Usage

try:
    import httpx
except Exception:
    httpx = None

try:
    import openai
    from openai import OpenAI
except ImportError:
    openai = None
    OpenAI = None


def sdk_status():
    """自检用：模型 SDK 是否可用。只有适配层允许碰具体 SDK。"""
    if openai is None:
        return "unavailable"
    return getattr(openai, "__version__", "unknown")


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI Chat Completions 兼容实现。缺配置就如实说，不假装可用。"""

    name = "openai-compatible"
    label = "OpenAI 兼容接口"
    default_base_url = ""

    def __init__(
        self,
        base_url="",
        api_key="",
        model="",
        timeout=60.0,
        proxy="",
        retries=1,
        logger=None,
        config_id="",
        config_name="",
    ):
        self.model = model or ""
        self.base_url = (base_url or self.default_base_url or "").strip()
        self.proxy = str(proxy or "").strip()
        # 重试只做一两次：失败多半是配置问题，猛重试只会更贵。
        self.retries = max(0, min(2, int(retries or 0)))
        self.logger = logger
        self.config_id = str(config_id or "")
        self.config_name = str(config_name or "")
        self._client = None
        if OpenAI is None or not self.model or not (api_key or self.base_url):
            return
        kwargs = {
            "api_key": api_key or "local",
            "timeout": float(timeout or 60.0),
            # 重试由这一层统一做，SDK 自己再重试会叠成四五次。
            "max_retries": 0,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = self._http_client(timeout)
        if client is not None:
            kwargs["http_client"] = client
        try:
            self._client = OpenAI(**kwargs)
        except Exception as exc:
            self._client = None
            self._warn("create client failed: %s", errors.redact(str(exc)))

    def _http_client(self, timeout):
        """自己建 httpx 客户端：关掉自动重定向，避免把 Key 带到别的主机。"""
        if httpx is None:
            return None
        options = {"timeout": float(timeout or 60.0), "follow_redirects": False}
        try:
            return httpx.Client(proxy=self.proxy or None, **options)
        except TypeError:
            try:
                return httpx.Client(proxies=self.proxy or None, **options)
            except Exception:
                return None
        except Exception:
            return None

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass

    def is_available(self):
        return self._client is not None and bool(self.model)

    def chat(self, messages, tools=None, temperature=0.7):
        if not self.is_available():
            return ChatResult(
                ok=False,
                error=errors.friendly(errors.UNCONFIGURED, self.label),
                error_kind=errors.UNCONFIGURED,
                model=self.model,
            )
        last = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    **({"tools": tools} if tools else {}),
                )
                message = response.choices[0].message
                return ChatResult(
                    ok=True,
                    content=message.content or "",
                    tool_calls=_tool_calls(message),
                    usage=_usage(response),
                    model=self.model,
                )
            except Exception as exc:
                kind = errors.classify(exc)
                detail = errors.redact(str(exc))
                last = ChatResult(
                    ok=False,
                    error=errors.friendly(kind, self.label, detail),
                    error_kind=kind,
                    model=self.model,
                )
                self._warn(
                    "model call failed (%s, attempt %d): %s",
                    kind,
                    attempt + 1,
                    detail[:200],
                )
                if kind not in errors.RETRYABLE or attempt >= self.retries:
                    break
                time.sleep(min(2.0, 0.6 * (attempt + 1)))
        return last

    def stream(self, messages, tools=None, temperature=0.7):
        if not self.is_available():
            yield ChatDelta(done=True)
            return
        chunk_stream = None
        try:
            chunk_stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
                **({"tools": tools} if tools else {}),
            )
            for chunk in chunk_stream:
                if not chunk.choices:
                    usage = _usage(chunk)
                    if usage.total_tokens:
                        yield ChatDelta(usage=usage)
                    continue
                delta = chunk.choices[0].delta
                yield ChatDelta(content=delta.content or "", tool_calls=_tool_calls(delta))
            yield ChatDelta(done=True)
        finally:
            # 用户点停止时生成器被丢弃，底层连接也要主动关，别让它一直挂着。
            close = getattr(chunk_stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def probe(self):
        """真发一次最小请求。只在用户主动点「测试连接」时调用。"""
        if not self.is_available():
            return {
                "ok": False,
                "kind": errors.UNCONFIGURED,
                "status": 0,
                "latency_ms": 0,
                "message": errors.friendly(errors.UNCONFIGURED, self.label),
            }
        started = time.monotonic()
        try:
            self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            latency = int((time.monotonic() - started) * 1000)
            return {
                "ok": True,
                "kind": "ok",
                "status": 200,
                "latency_ms": latency,
                "message": "连接正常（%d ms）" % latency,
            }
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            kind = errors.classify(exc)
            detail = errors.redact(str(exc))
            self._warn("model probe failed (%s): %s", kind, detail[:200])
            return {
                "ok": False,
                "kind": kind,
                "status": errors.status_code(exc),
                "latency_ms": latency,
                "message": errors.friendly(kind, self.label, detail),
            }

    def list_models(self):
        """服务商支持就列出模型；不支持或失败就返回空，不影响手动填写。"""
        if not self.is_available():
            return []
        try:
            payload = self._client.models.list()
            rows = getattr(payload, "data", None) or []
            return [
                str(getattr(item, "id", "") or "")
                for item in rows
                if getattr(item, "id", "")
            ]
        except Exception as exc:
            self._warn("list models failed: %s", errors.redact(str(exc))[:160])
            return []


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    label = "OpenAI"
    default_base_url = "https://api.openai.com/v1"


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"
    label = "DeepSeek"
    default_base_url = "https://api.deepseek.com"


class QwenProvider(OpenAICompatibleProvider):
    name = "qwen"
    label = "通义千问"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class CustomEndpointProvider(OpenAICompatibleProvider):
    name = "custom"
    label = "自定义接口"


class OllamaProvider(OpenAICompatibleProvider):
    name = "ollama"
    label = "Ollama 本地模型"
    default_base_url = "http://127.0.0.1:11434/v1"


PROVIDERS = {
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "qwen": QwenProvider,
    "custom": CustomEndpointProvider,
    "ollama": OllamaProvider,
    # 老配置里的名字，继续认。
    "openai-compatible": CustomEndpointProvider,
}


def _usage(payload):
    usage = getattr(payload, "usage", None)
    if not usage:
        return Usage()
    return Usage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


def _tool_calls(message):
    calls = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        calls.append(
            ToolCall(
                id=getattr(call, "id", "") or "",
                name=getattr(function, "name", "") or "",
                arguments=_load_arguments(getattr(function, "arguments", "")),
            )
        )
    return calls


def _load_arguments(raw):
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _make(config, base_url, api_key, model, role):
    """按角色装配 Provider。Agent Core 只拿 ModelProvider 接口。"""
    name = str(getattr(config, "llm_provider", "") or "openai-compatible")
    factory = PROVIDERS.get(name)
    if factory is None:
        # 环境变量里写了个不认识的服务商：如实当没配置，别悄悄换成自定义地址。
        return None
    provider = factory(
        base_url=base_url or "",
        api_key=api_key or "",
        model=model or "",
        timeout=getattr(config, "llm_timeout", 60.0),
        proxy=getattr(config, "llm_proxy", ""),
        retries=getattr(config, "llm_retries", 1),
        logger=getattr(config, "logger", None),
    )
    try:
        provider.role = role
    except Exception:
        pass
    return provider


def _from_spec(spec, secret, role, config):
    """按配置仓库里的一条记录装配 Provider。"""
    factory = PROVIDERS.get(str(spec.get("provider") or "custom")) or CustomEndpointProvider
    provider = factory(
        base_url=spec.get("base_url") or "",
        api_key=secret or "",
        model=spec.get("model") or "",
        timeout=getattr(config, "llm_timeout", 60.0),
        proxy=getattr(config, "llm_proxy", ""),
        retries=getattr(config, "llm_retries", 1),
        logger=getattr(config, "logger", None),
        config_id=spec.get("id") or "",
        config_name=spec.get("name") or "",
    )
    try:
        provider.role = role
    except Exception:
        pass
    return provider


def _resolve(config, role):
    """先看配置仓库的默认项，没有就回退到 .env。返回 (spec, secret)。"""
    repo = getattr(config, "model_configs", None)
    secrets = getattr(config, "secrets", None)
    if repo is None:
        return None, ""
    try:
        spec = repo.default(role.value)
    except Exception:
        spec = None
    if not spec:
        return None, ""
    secret = ""
    if secrets is not None:
        try:
            secret = secrets.load(spec.get("secret_id"))
        except Exception:
            secret = ""
    return spec, secret


def build_provider(config):
    """旧接口：只用 llm_* 一套配置，保留兼容。"""
    return _make(
        config,
        getattr(config, "llm_base_url", ""),
        getattr(config, "llm_api_key", ""),
        getattr(config, "llm_model", ""),
        ModelRole.COMPANION,
    )


def build_companion_provider(config):
    """陪伴模型：配置仓库优先，缺省回退旧 DESKPET_COMPANION_* / AGENT_LLM_*。"""
    spec, secret = _resolve(config, ModelRole.COMPANION)
    if spec:
        return _from_spec(spec, secret, ModelRole.COMPANION, config)
    return _make(
        config,
        getattr(config, "companion_base_url", "") or getattr(config, "llm_base_url", ""),
        getattr(config, "companion_api_key", "") or getattr(config, "llm_api_key", ""),
        getattr(config, "companion_model", "") or getattr(config, "llm_model", ""),
        ModelRole.COMPANION,
    )


def build_task_provider(config):
    """任务模型。仓库里没配、env 里也没配时返回 None，不影响陪伴模型。"""
    spec, secret = _resolve(config, ModelRole.TASK)
    if spec:
        return _from_spec(spec, secret, ModelRole.TASK, config)
    model = getattr(config, "task_model", "")
    if not model:
        return None
    return _make(
        config,
        getattr(config, "task_base_url", ""),
        getattr(config, "task_api_key", ""),
        model,
        ModelRole.TASK,
    )


def build_catalog(config):
    """按配置 id 建一份 Provider 表，会话级切换时直接用。"""
    repo = getattr(config, "model_configs", None)
    secrets = getattr(config, "secrets", None)
    if repo is None:
        return {}
    catalog = {}
    for spec in repo.list():
        if not spec.get("enabled", True):
            continue
        secret = ""
        if secrets is not None:
            try:
                secret = secrets.load(spec.get("secret_id"))
            except Exception:
                secret = ""
        role = ModelRole.TASK if "task" in (spec.get("roles") or []) else ModelRole.COMPANION
        catalog[str(spec.get("id"))] = _from_spec(spec, secret, role, config)
    return catalog
