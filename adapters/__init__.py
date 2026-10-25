"""可选后端适配层。

每个适配器只做一件事：把外部组件的能力包成稳定接口，并如实报告「现在能不能用」。
没装的就说没装，不假装跑得起来；默认都不启用，也不自动下载任何东西。

这一层不碰 Live2D 资产、不碰数据库结构，也不绕开权限中心。
"""

from adapters.agent_runtime import BuiltinRuntime, PiRuntimeAdapter, build_runtime
from adapters.memory_backend import CogneeAdapter, LocalMemoryBackend, build_memory_backend
from adapters.output_reducer import CommandOutputReducer
from adapters.vision_provider import NullVisionProvider, UiaVisionProvider, build_vision_provider
from adapters.voice_provider import SystemVoiceProvider, TenVoiceProvider, WhisperCppProvider, build_voice_provider

__all__ = [
    "BuiltinRuntime",
    "CogneeAdapter",
    "CommandOutputReducer",
    "LocalMemoryBackend",
    "NullVisionProvider",
    "PiRuntimeAdapter",
    "SystemVoiceProvider",
    "TenVoiceProvider",
    "UiaVisionProvider",
    "WhisperCppProvider",
    "build_memory_backend",
    "build_runtime",
    "build_vision_provider",
    "build_voice_provider",
]
