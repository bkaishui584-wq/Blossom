"""串口枚举与打开。

枚举优先用 pyserial（装了就用，它认得出描述）；没装就退回 Windows 自带的
注册表项，只用标准库也能列出 COM 口。两条路都拿不到就返回空，不编。

打开串口时能设波特率就设，设不了如实标出来，不假装设过。
"""

import os
import time

DEFAULT_BAUDRATE = 115200


def _pyserial():
    """拿 pyserial。没装就返回 None，不抛异常。"""
    try:
        import serial  # noqa: F401
        from serial.tools import list_ports as serial_list_ports
    except Exception:
        return None
    return serial_list_ports


def backend_name():
    return "pyserial" if _pyserial() is not None else "系统自带"


def _from_pyserial():
    serial_list_ports = _pyserial()
    if serial_list_ports is None:
        return None
    rows = []
    for item in serial_list_ports.comports():
        rows.append(
            {
                "port": str(item.device or ""),
                "description": str(item.description or ""),
                "hwid": str(item.hwid or ""),
                "source": "pyserial",
            }
        )
    return rows


def _from_registry():
    """Windows 把自己认到的 COM 口写在这个注册表项里。"""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    rows = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            index = 0
            while True:
                try:
                    _name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                rows.append(
                    {
                        "port": str(value or ""),
                        "description": "",
                        "hwid": "",
                        "source": "系统自带",
                    }
                )
                index += 1
    except OSError:
        return []
    return rows


def list_ports():
    """列出本机认得的串口。列不出来就是空列表，不猜。"""
    rows = _from_pyserial()
    if rows is None:
        rows = _from_registry()
    if not rows:
        return []
    seen = set()
    out = []
    for row in sorted(rows, key=lambda item: str(item.get("port") or "")):
        port = str(row.get("port") or "").strip()
        if not port or port.upper() in seen:
            continue
        seen.add(port.upper())
        out.append(row)
    return out


def port_names():
    return [row["port"] for row in list_ports()]


def open_raw_serial(port):
    """只用标准库打开串口。Windows 上转成设备路径写法，COM10 以上才认得出。

    这一条路设不了波特率，也用不了原生流控，属于「pyserial 不在时的退路」。
    """
    name = str(port)
    if os.name == "nt" and not name.startswith("\\\\.\\"):
        name = "\\\\.\\" + name
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(name, flags)
    non_blocking = True
    try:
        os.set_blocking(descriptor, False)
    except Exception:
        # 设不了非阻塞就别硬读：阻塞读会把界面卡死。
        non_blocking = False
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    setattr(handle, "non_blocking", non_blocking)
    setattr(handle, "baud_set", False)
    setattr(handle, "backend", "系统自带")
    return handle


def open_port(port, baudrate=DEFAULT_BAUDRATE):
    """打开串口。能用 pyserial 就用它，波特率也一起设好。

    返回的对象上带三个标记：non_blocking、baud_set、backend，给界面和诊断看。
    """
    name = str(port or "").strip()
    if not name:
        raise ValueError("没有指定串口名，例如 COM3")
    try:
        import serial
    except Exception:
        return open_raw_serial(name)
    baud = int(baudrate or DEFAULT_BAUDRATE)
    # timeout=0 就是非阻塞读：读不到立刻返回，不会把桌宠卡住。
    handle = serial.Serial(port=name, baudrate=baud, timeout=0, write_timeout=1.0)
    try:
        handle.reset_input_buffer()
    except Exception:
        pass
    setattr(handle, "non_blocking", True)
    setattr(handle, "baud_set", True)
    setattr(handle, "backend", "pyserial")
    setattr(handle, "opened_at", time.time())
    return handle
