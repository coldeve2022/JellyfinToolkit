"""生成一套**完全虚构**的演示库，让任何人都能在没有真实数据的前提下试用/截图。

产出：
    <out>/jellyfin.db                      最小可用的 Jellyfin 结构（只含本工具用到的表）
    <out>/data/library/<番号>/...          占位的视频/NFO/字幕文件（0 字节，仅用于展示路径逻辑）
    <out>/config.json                      指向上面这套演示数据的配置

用法：
    python tools/dev/seed_demo.py                    # 默认写到 ./demo/
    python tools/dev/seed_demo.py --out D:/demo --count 120

注意：这里所有番号、演员、厂商、路径都是**编造**的，与任何真实作品无关。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 全部为虚构编号；前缀刻意避开真实厂商代号
PREFIXES = ["DEMO", "SAMPLE", "MOCK", "FAKE", "TESTX"]
GENRES = ["剧情", "记录", "合集", "特典", "重制"]
ACTORS = ["示例演员甲", "示例演员乙", "示例演员丙", "示例演员丁", "示例演员戊"]
STUDIOS = ["示例工作室一", "示例工作室二", "示例工作室三"]

# 演示用根目录：中性路径，不出现构建目录、也不指向任何真实库
DEMO_ROOT = r"C:\MediaDemo\Library"


def _build_items(count: int):
    items = []
    for i in range(count):
        prefix = PREFIXES[i % len(PREFIXES)]
        num = f"{prefix}-{100 + i:03d}"
        year = 2018 + (i % 8)
        actors = [ACTORS[i % len(ACTORS)], ACTORS[(i + 2) % len(ACTORS)]]
        genres = [GENRES[i % len(GENRES)], GENRES[(i + 1) % len(GENRES)]]
        studio = STUDIOS[i % len(STUDIOS)]
        size = (600 + (i * 37) % 4000) * 1024 * 1024
        # 每 5 个一组里放一个 CD2，验证分集合并逻辑
        cd = 2 if i % 5 == 4 else None
        name = f"{num} 示例标题 {i:03d}" + (f"-cd{cd}" if cd else "")
        rel = f"{studio}\\{num}\\{name}.mp4"
        items.append({
            "id": f"demo-item-{i:04d}",
            "name": name,
            "path": f"{DEMO_ROOT}\\{rel}",
            "year": year,
            "genres": ",".join(genres),
            "tags": ",".join([f"{s}:" for s in (studio,)] + genres),
            "studios": studio,
            "size": size,
            "type": "Movie",
            # 行为：约 1/3 收藏、1/2 看过
            "favorite": 1 if i % 3 == 0 else 0,
            "play_count": (i % 4),
            "played": 1 if i % 2 == 0 else 0,
            "rating": float(5 + (i % 5)) if i % 7 == 0 else None,
            "actors": actors,
        })
    return items


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE BaseItems (
            Id TEXT PRIMARY KEY, Name TEXT, Path TEXT,
            ProductionYear INTEGER, Genres TEXT, Tags TEXT, Studios TEXT,
            Size INTEGER, Type TEXT, IsFolder INTEGER, MediaType TEXT
        );
        CREATE TABLE Peoples (Id TEXT PRIMARY KEY, Name TEXT);
        CREATE TABLE PeopleBaseItemMap (ItemId TEXT, PeopleId TEXT);
        CREATE TABLE UserData (
            ItemId TEXT PRIMARY KEY, IsFavorite INTEGER, PlayCount INTEGER,
            Played INTEGER, PlaybackPositionTicks INTEGER, LastPlayedDate TEXT,
            Rating REAL
        );
        CREATE TABLE BaseItemImageInfos (
            ItemId TEXT, ImageType INTEGER, Path TEXT
        );
        """
    )


def seed(out: Path, count: int) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "jellyfin.db"
    if db_path.exists():
        db_path.unlink()

    items = _build_items(count)
    conn = sqlite3.connect(str(db_path))
    try:
        _create_schema(conn)
        people_ids: dict[str, str] = {}
        for it in items:
            conn.execute(
                "INSERT INTO BaseItems VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (it["id"], it["name"], it["path"], it["year"], it["genres"],
                 it["tags"], it["studios"], it["size"], it["type"], 0, "Video"),
            )
            conn.execute(
                "INSERT INTO UserData VALUES (?,?,?,?,?,?,?)",
                (it["id"], it["favorite"], it["play_count"], it["played"],
                 0, "2024-01-0%dT20:00:00" % (1 + int(it["id"][-2:]) % 9),
                 it["rating"]),
            )
            for actor in it["actors"]:
                if actor not in people_ids:
                    people_ids[actor] = f"demo-person-{len(people_ids):03d}"
                    conn.execute("INSERT INTO Peoples VALUES (?,?)",
                                 (people_ids[actor], actor))
                conn.execute("INSERT INTO PeopleBaseItemMap VALUES (?,?)",
                             (it["id"], people_ids[actor]))

            # 真的把文件建出来，方便文件类功能有东西可看（0 字节占位）
            video = Path(it["path"])
            try:
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(b"")
                (video.with_suffix(".nfo")).write_text(
                    f"<movie><title>{it['name']}</title>"
                    f"<plot>演示数据，与真实作品无关。</plot></movie>",
                    encoding="utf-8")
                if int(it["id"][-2:]) % 3 == 0:
                    video.with_suffix(".zh.srt").write_text(
                        "1\n00:00:00,000 --> 00:00:02,000\n演示字幕\n",
                        encoding="utf-8")
            except OSError:
                pass
        conn.commit()
    finally:
        conn.close()

    cfg = {
        "jellyfin_db_path": str(db_path),
        "jellyfin_data_dir": str(out / "data"),
        "theme": "dark",
        "merge_backup_root": "",
        "exclude_path_keywords": [],
        "onboarding_done": True,
        "ai_enabled": False,
    }
    (out / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return db_path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成虚构演示库")
    parser.add_argument("--out", default=str(ROOT / "demo"), help="输出目录")
    parser.add_argument("--count", type=int, default=80, help="作品数量")
    args = parser.parse_args()

    out = Path(args.out).expanduser().resolve()
    db = seed(out, max(5, args.count))
    print(f"演示库已生成: {db}")
    print(f"配置已写入:   {out / 'config.json'}")
    print("\n试用方式（不影响你自己的配置）：")
    print(f'  set JELLYFIN_TOOLKIT_DATA_DIR={out}')
    print(f'  python main.py --data-dir "{out}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
