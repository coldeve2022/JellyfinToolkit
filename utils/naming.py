"""文件名/命名相关的纯函数 — 有码判断、restored 还原等。"""

import re
from typing import Iterable, Optional


def is_uncensored_filename(filename: str, keywords: Iterable[str]) -> bool:
    """根据文件名判断是否为无码/破解版视频。

    判断规则（任一命中即为无码）：
    1. 文件名包含关键词列表中的任意一个（如 fc2 / tokyo hot / 无码 / uncensored）
    2. FC2 编号格式：fc2-ppv-123456 或 fc2 123456
    3. 以 -U 或 _U 结尾（如 ABC-123-U）

    Args:
        filename: 文件名（不含路径，如 "ABC-123.mp4"）
        keywords: 无码关键词列表

    Returns:
        是否为无码视频
    """
    lower = filename.lower()
    name_no_ext = re.sub(r"\.[^.]+$", "", filename).lower()

    for kw in keywords:
        if kw.lower() in lower:
            return True

    if re.search(r"fc2\s*[-_]?\s*(ppv)?\s*[-_]?\s*\d+", lower):
        return True

    if re.search(r"[-_]u$", name_no_ext):
        return True

    return False


def clean_restored_name(filename: str, marker: str = "restored") -> Optional[str]:
    """去除文件名中的 restored 标记，还原为原始媒体库文件名。

    支持多种书写形式：
        "Movie.restored.mp4"      → "Movie.mp4"
        "Movie - restored.mp4"    → "Movie.mp4"
        "Movie_restored.mp4"      → "Movie.mp4"
        "Movie (restored).mp4"    → "Movie.mp4"

    Args:
        filename: 待还原的文件名
        marker: 破解标记词，默认 "restored"

    Returns:
        还原后的文件名；如果文件名中不含标记则返回 None
    """
    escaped = re.escape(marker)
    # 匹配 [可选空白/分隔符] + marker + [可选空白/括号/分隔符]，替换为单个 .
    pattern = re.compile(
        rf"\s*[._\-\(\[\s]*{escaped}[._\-\)\]\s]*",
        re.IGNORECASE,
    )
    if not pattern.search(filename):
        return None

    cleaned = pattern.sub(".", filename, count=1)
    # 规整可能出现的前置点/连续点/尾点：".Movie.mp4" → "Movie.mp4"
    cleaned = re.sub(r"^\.+", "", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"\.+$", "", cleaned)
    return cleaned
