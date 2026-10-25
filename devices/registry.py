"""设备注册表。

职责已经由 devices.manager.DeviceManager 承担（注册、查找、连接、健康、审计），
这里不再造第二份，只把名字对上，方便按 V11 的模块划分读代码。
"""

from devices.manager import DeviceManager

DeviceRegistry = DeviceManager

__all__ = ["DeviceRegistry"]