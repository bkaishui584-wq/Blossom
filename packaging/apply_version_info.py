"""把 Blossom 的 Windows 版本资源写进已生成的 EXE。"""

import sys
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    load_version_info_from_text_file,
    write_version_info_to_executable,
)


def main(argv):
    if len(argv) != 3:
        print("usage: apply_version_info.py <exe> <version-info-file>", file=sys.stderr)
        return 2
    exe = Path(argv[1])
    info_file = Path(argv[2])
    if not exe.is_file():
        print("missing exe: %s" % exe, file=sys.stderr)
        return 3
    if not info_file.is_file():
        print("missing version info: %s" % info_file, file=sys.stderr)
        return 4
    info = load_version_info_from_text_file(str(info_file))
    write_version_info_to_executable(str(exe), info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
