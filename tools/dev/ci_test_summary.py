"""把 pytest 的 junit 报告转成 GitHub Actions 注解。

CI 的运行日志要登录才能下载，但检查注解不需要。测试失败时，
`python -m pytest` 只会让步骤以 "exit code 1" 结束，看不出是哪个用例挂了
—— 这个脚本把失败用例名、文件和首行原因逐条打成 ``::error``，
在 workflow run 页面的 Annotations 里直接能看。

用法：

    python tools/dev/ci_test_summary.py .local/junit.xml

有失败时返回 1，没有失败返回 0，方便直接当 workflow 的一步。
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# 注解太多会把页面撑爆，前面这些足够定位问题
MAX_REPORTED = 40
# 单条原因截断长度，避免整段 traceback 挤进一行注解
REASON_LIMIT = 220


def _first_line(node: ET.Element) -> str:
    """失败原因的首行 —— 通常是 ``assert x == y`` 这种最有信息量的一句。"""
    message = (node.get("message") or "").strip()
    if not message:
        text = (node.text or "").strip()
        message = text
    if not message:
        return "(没有 message，需要看完整日志)"
    return message.splitlines()[0][:REASON_LIMIT]


def summarize(xml_path: Path) -> int:
    try:
        root = ET.parse(xml_path).getroot()
    except OSError as exc:
        print(f"::error::读不到测试报告 {xml_path}: {exc}")
        return 1
    except ET.ParseError as exc:
        print(f"::error::测试报告不是合法 XML（多半是进程崩溃，写到一半断了）: {exc}")
        return 1

    suite = root if root.tag == "testsuite" else root.find("testsuite")
    total = (suite.get("tests") if suite is not None else None) or "?"

    failed: list[tuple[ET.Element, ET.Element]] = []
    for case in root.iter("testcase"):
        node = case.find("failure")
        if node is None:
            node = case.find("error")
        if node is not None:
            failed.append((case, node))

    if not failed:
        print(f"全部通过（{total} 项）")
        return 0

    print(f"失败 {len(failed)} 项 / 共 {total} 项")
    for case, node in failed[:MAX_REPORTED]:
        where = case.get("file") or case.get("classname") or "tests"
        name = case.get("name") or "?"
        print(f"::error file={where}::{case.get('classname')}::{name} — {_first_line(node)}")

    if len(failed) > MAX_REPORTED:
        print(f"::error::还有 {len(failed) - MAX_REPORTED} 项失败未列出，完整清单见 {xml_path}")
    return 1


def main(argv: list[str] | None = None) -> int:
    # Windows runner 的默认输出编码是 cp1252，打印中文用例名会直接
    # UnicodeEncodeError —— 那一步本来就是"失败后才跑"，再崩一次就什么也看不到。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        # workflow 里不传参数：pwsh 下 "$JUNIT_XML" 是未定义变量，会被展开成空，
        # 脚本会拿到空路径。统一从环境变量取，bash / pwsh 都一样。
        env_path = os.environ.get("JUNIT_XML", "").strip()
        if env_path:
            args = [env_path]
    if len(args) != 1:
        print("用法: JUNIT_XML=<junit.xml> python tools/dev/ci_test_summary.py [junit.xml]")
        return 2
    return summarize(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
