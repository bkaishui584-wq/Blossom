"""模型统一接口。Agent 只依赖这里，不依赖任何具体厂商 SDK 或模型。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

# 这些主机名算"本机"：交给它们的模型不算把内容发出去。
# 放在模型这一层，是因为它判的是"模型的地址"，跟隐私中心的开关是两回事。
LOCAL_ENDPOINT_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def endpoint_host(url):
    """从模型地址里取出主机名。取不到就返回空串，调用方按"云端"处理。"""
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        return (urlparse(text).hostname or "").strip().lower()
    except Exception:
        return ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResult:
    ok: bool
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    error: str = ""
    # 统一错误类别（见 model/errors.py）。空串表示没有错误。
    error_kind: str = ""


@dataclass
class ChatDelta:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    done: bool = False


class ModelRole(str, Enum):
    """同一个 Provider 接口下的两种角色：陪伴与任务。"""

    COMPANION = "companion"
    TASK = "task"


class ModelStatus(str, Enum):
    CONNECTED = "connected"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


STATUS_LABELS = {
    ModelStatus.CONNECTED.value: "已连接",
    ModelStatus.UNCONFIGURED.value: "未配置",
    ModelStatus.UNAVAILABLE.value: "不可用",
}


@dataclass
class ModelSlot:
    """一个模型角色的独立状态。缺 Task 不影响 Companion。"""

    role: str = ModelRole.COMPANION.value
    provider: object = None
    configured: bool = False

    @property
    def role_name(self):
        return str(getattr(self.role, "value", self.role))

    def available(self):
        if self.provider is None:
            return False
        try:
            return bool(self.provider.is_available())
        except Exception:
            return False

    def status(self):
        if not self.configured:
            return ModelStatus.UNCONFIGURED.value
        if self.available():
            return ModelStatus.CONNECTED.value
        return ModelStatus.UNAVAILABLE.value

    @property
    def label(self):
        return STATUS_LABELS.get(self.status(), self.status())

    @property
    def model(self):
        return getattr(self.provider, "model", "") or ""

    def to_dict(self):
        return {"role": self.role_name, "status": self.status(), "model": self.model}


class ModelProvider(ABC):
    """Agent Core 与任意模型之间的唯一通道。"""

    name = "base"
    role = ModelRole.COMPANION.value

    @abstractmethod
    def is_available(self):
        """配置是否足以发起请求。"""

    @abstractmethod
    def chat(self, messages, tools=None, temperature=0.7):
        """返回 ChatResult。只负责通信，不做权限判断。"""

    @abstractmethod
    def stream(self, messages, tools=None, temperature=0.7):
        """逐段产出 ChatDelta。"""
