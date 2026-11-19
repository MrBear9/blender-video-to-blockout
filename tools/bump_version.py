"""bump_version.py —— 改 __init__.py 的 bl_info 版本号

用法：
    python bump_version.py 1 10 3

⚠ 必须用 Python 写文件。PowerShell 的 Set-Content -Encoding UTF8 会把
  CJK 按 GBK 往返一次，中文全变乱码（本项目已经因为这事重写过一遍
  __init__.py）。同理，别用 pwsh 内联 -c 传正则——引号会被 PowerShell
  先吃一遍，Python 收到的就是残缺字符串。
"""
import io
import os
import re
import sys

P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon", "blockout_helper", "__init__.py")


def main():
    if len(sys.argv) != 4:
        print("用法: python bump_version.py MAJOR MINOR PATCH")
        return 1
    ver = tuple(int(x) for x in sys.argv[1:4])
    ver_str = "%d.%d.%d" % ver

    s = io.open(P, encoding="utf-8").read()
    pat = re.compile(r'"version"\s*:\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)')
    m = pat.search(s)
    if not m:
        print("WARN: 找不到 version")
        return 1
    old = m.group(0)
    new = '"version": (%d, %d, %d)' % ver
    s = s[:m.start()] + new + s[m.end():]
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("版本 %s -> %s" % (old, new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
