"""开机启动开关与桌面快捷方式（Windows）。

默认关闭。只有用户在设置里明确打开，才会写当前用户自己的注册表启动项。
只动 HKCU 下的 Run 键，不碰系统项、不动别的账号。
不是 Windows、没有权限、读不到，都如实返回 False，绝不影响桌宠运行。

桌面快捷方式走独立的几个函数（desktop_shortcut_path / create_desktop_shortcut）：
装完之后没图标的话，桌宠自己也能补一个，不用回头找安装脚本。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from security.config import PROGRAM_DIR

# 启动项的固定名字，卸载/关闭时按这个名字找。
VALUE_NAME = "Blossom"
# 改名之前用过的名字。老装机的启动项还在用旧名字，关掉时要一起收掉，
# 不然会出现两条开机启动项或者关了还开机自启。
LEGACY_VALUE_NAMES = ("DeskPet",)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

_NOOP = {"supported": False, "enabled": False, "command": "", "error": "当前系统不支持"}


def _registry():
    """拿到 winreg；不是 Windows 或没有这个模块就返回 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    return winreg


def value_names():
    """要处理的启动项名字：新名字在前，旧名字跟着一起收。"""
    return (VALUE_NAME,) + tuple(LEGACY_VALUE_NAMES)


def command_line():
    """开机要执行的命令行：打包后是 EXE，开发时是 python app/main.py。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    script = Path(PROGRAM_DIR) / "app" / "main.py"
    return f'"{sys.executable}" "{script}"'


def is_enabled():
    """注册表里现在有没有这一项。读不到就当没有。"""
    winreg = _registry()
    if winreg is None:
        return False
    for name in value_names():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except Exception:
            continue
        if str(value or "").strip():
            return True
    return False


def enable(command=None):
    """写入开机启动项。写不进去返回 False。"""
    winreg = _registry()
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key, VALUE_NAME, 0, winreg.REG_SZ, str(command or command_line())
            )
            # 旧名字那条一并删掉，避免开机启动两个桌宠。
            for name in LEGACY_VALUE_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except Exception:
                    continue
        return True
    except Exception:
        return False


def disable():
    """删掉开机启动项。本来就没有也算成功。"""
    winreg = _registry()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name in value_names():
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    continue
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def set_enabled(flag, command=None):
    """一个开关：打开就写，关掉就删。返回操作之后这个开关的真实状态。"""
    if flag:
        if not enable(command):
            return False
    else:
        disable()
    return is_enabled()


def describe():
    """界面用的一行状态。绝不发给模型。"""
    if _registry() is None:
        return dict(_NOOP)
    return {
        "supported": True,
        "enabled": is_enabled(),
        "command": command_line(),
        "error": "",
    }


# --------------------------------------------------------------------------
# 桌面快捷方式
#
# 用户反馈过「装完桌面没有图标」。安装脚本现在会自己建，但那要求用户真的运行了
# 安装脚本；解压绿色版直接双击 EXE 的人就什么都没有。所以桌宠自己也带一份，
# 用户在任何一次使用里点一下就能补上，路径、工作目录、图标都指对。
# --------------------------------------------------------------------------

SHORTCUT_NAME = "Blossom.lnk"
# 改名前留下的图标，补图标的时候顺手收掉，免得桌面上两个图标指向两个包。
LEGACY_SHORTCUT_NAMES = ("DeskPet.lnk",)

# 建快捷方式交给 PowerShell 做：WScript.Shell 是系统自带的，不用装任何东西。
# 参数按位置传，路径里有空格、中文、引号都不会被拆错。
_SHORTCUT_SCRIPT = """$ErrorActionPreference = "Stop"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($args[0])
$link.TargetPath = $args[1]
$link.WorkingDirectory = $args[2]
$link.IconLocation = "$($args[1]),0"
$link.Description = "Blossom"
$link.Save()
"""


def desktop_dir():
    """桌面目录。问 Windows 拿，桌面被重定向到 OneDrive 时也能拿到。拿不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY: 当前用户的桌面。比拼 %USERPROFILE%\\Desktop 可靠。
        code = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer)
        if code == 0 and buffer.value:
            return Path(buffer.value)
    except Exception:
        pass
    try:
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
    except Exception:
        return None


def desktop_shortcut_path():
    """桌面图标的完整路径。找不到桌面返回 None。"""
    desktop = desktop_dir()
    if desktop is None:
        return None
    return desktop / SHORTCUT_NAME


def has_desktop_shortcut():
    path = desktop_shortcut_path()
    return bool(path and path.is_file())


def remove_legacy_shortcuts():
    """收掉改名前留下的图标。删掉了几个就返回几。"""
    desktop = desktop_dir()
    if desktop is None:
        return 0
    removed = 0
    for name in LEGACY_SHORTCUT_NAMES:
        try:
            path = desktop / name
            if path.is_file():
                path.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def create_desktop_shortcut(target="", logger=None):
    """在桌面建一个指向桌宠的快捷方式。

    返回 {"ok": bool, "path": str, "error": str}。做不成也说清为什么，不假装成功。
    """
    if os.name != "nt":
        return {"ok": False, "path": "", "error": "当前系统不支持桌面快捷方式"}
    if not getattr(sys, "frozen", False):
        # 开发模式下 sys.executable 是 python.exe，建出来的图标一点就不是桌宠，
        # 会让人误会，所以如实说明，交给 build.ps1 -ShortcutOnly。
        return {"ok": False, "path": "", "error": "开发模式不建快捷方式，打包版才有"}
    exe = Path(target).resolve() if target else Path(sys.executable).resolve()
    if not exe.is_file():
        return {"ok": False, "path": "", "error": "找不到程序文件：%s" % exe}
    link = desktop_shortcut_path()
    if link is None:
        return {"ok": False, "path": "", "error": "找不到桌面目录"}

    script = Path(tempfile.gettempdir()) / "blossom-shortcut.ps1"
    try:
        script.write_text(_SHORTCUT_SCRIPT, encoding="utf-8-sig")
    except Exception as exc:
        return {"ok": False, "path": "", "error": "写不了临时脚本：%s" % exc}
    try:
        done = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                str(link),
                str(exe),
                str(exe.parent),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "path": "", "error": "调 PowerShell 失败：%s" % exc}
    finally:
        try:
            script.unlink()
        except Exception:
            pass

    if done.returncode != 0 or not link.is_file():
        detail = ((done.stderr or "") + (done.stdout or "")).strip().splitlines()
        error = detail[-1] if detail else "建立快捷方式失败"
        if logger:
            logger.warning("桌面快捷方式没建成：%s", error)
        return {"ok": False, "path": "", "error": error}

    remove_legacy_shortcuts()
    if logger:
        logger.info("桌面快捷方式已建立：%s", link)
    return {"ok": True, "path": str(link), "error": ""}


def describe_shortcut():
    """界面用的一行状态：桌面上有没有图标、图标指哪。"""
    path = desktop_shortcut_path()
    if path is None:
        return {"supported": False, "exists": False, "path": "", "target": str(Path(sys.executable).resolve())}
    return {
        "supported": True,
        "exists": path.is_file(),
        "path": str(path),
        "target": str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) else command_line(),
    }
