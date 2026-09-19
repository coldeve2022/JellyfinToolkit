"""由 `version.py` 生成 PyInstaller 用的 Windows 版本资源文件。

作用：打包后 exe 右键 →「属性」→「详细信息」里能看到产品名/版本/版权，
而不是一片空白。版本号只有 `version.py` 一个来源，避免手工维护漂移
（出现"界面写 3.6、文件属性写 3.5"这种不一致）。

用法：
    python tools/make_version_info.py            # 写到 build/version_info.txt
    python tools/make_version_info.py --stdout   # 直接打印，便于 CI 检查
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.console import force_utf8_stdout  # noqa: E402

force_utf8_stdout()

from version import (  # noqa: E402
    APP_DESCRIPTION,
    APP_NAME,
    APP_NAME_CN,
    VERSION_TUPLE,
    __author__,
    __license__,
    __version__,
    PROJECT_URL,
)

TEMPLATE = '''# 由 tools/make_version_info.py 自动生成，请勿手工编辑。
# 唯一版本来源: version.py (v{version})
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple_str},
    prodvers={tuple_str},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {author!r}),
         StringStruct('FileDescription', {description!r}),
         StringStruct('FileVersion', {version!r}),
         StringStruct('InternalName', {app_name!r}),
         StringStruct('LegalCopyright', {copyright!r}),
         StringStruct('License', {license!r}),
         StringStruct('OriginalFilename', {original!r}),
         StringStruct('ProductName', {product!r}),
         StringStruct('ProductVersion', {version!r}),
         StringStruct('ProjectUrl', {url!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
'''


def render() -> str:
    return TEMPLATE.format(
        version=__version__,
        tuple_str=str(VERSION_TUPLE),
        author=__author__,
        description=APP_DESCRIPTION,
        app_name=APP_NAME,
        copyright=f"© {__author__} — {__license__} License",
        license=__license__,
        original=f"{APP_NAME}.exe",
        product=f"{APP_NAME_CN} ({APP_NAME})",
        url=PROJECT_URL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Windows 版本资源")
    parser.add_argument("--out", default=str(ROOT / "build" / "version_info.txt"))
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()

    text = render()
    if args.stdout:
        print(text)
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"已生成 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
