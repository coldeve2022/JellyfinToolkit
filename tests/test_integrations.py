"""第三方工具集成（批量生成字幕 / 马赛克破解）的单元测试。

这两块功能的共同点是：**核心能力在别人的程序里**（``infer.exe`` / ``lada-cli.exe``），
所以测试的重点不是"识别准不准"，而是围绕它们最容易出错的那些地方：

1. **能力探测**：参数要按工具**实际支持**的发，不能写死 ——
   实测上游改过参数名（``--language`` 压根不存在），写死就会在用户机器上报
   "未识别参数"；
2. **表格解析**：Lada 的输出里有**描述为空**的行（``v2`` 模型），
   按空格切列会把路径塞进描述列 → 必须按分隔线定列；
3. **找不到工具时**：要给可执行的指引，而不是抛异常或静默失败；
4. **字幕清理**：去重与长句拆分的边界。

测试全程用**打桩的文本**，不依赖机器上真的装了这两个工具（CI 上也没有）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import lada_tool, subtitle_clean, whisper_tool  # noqa: E402

# ── 真实工具的 --help 片段（从实际输出里摘的，保持格式）──
INFER_HELP = """
usage: infer.exe [-h] [--model_name_or_path MODEL_NAME_OR_PATH]
                 [--device DEVICE] [--compute_type COMPUTE_TYPE] [--overwrite]
                 [--audio_suffixes AUDIO_SUFFIXES] [--sub_formats SUB_FORMATS]
                 [--output_dir OUTPUT_DIR] [--enable_batching]
                 [--batch_size BATCH_SIZE] ...

基于自定义VAD注入的Whisper转录

options:
  -h, --help            show this help message and exit
  --model_name_or_path MODEL_NAME_OR_PATH
                        Whisper模型路径
  --device DEVICE       运行模型的设备 (cpu, cuda, auto)
  --overwrite           覆盖现有字幕文件
  --sub_formats SUB_FORMATS
                        字幕格式列表（逗号分隔）：lrc, srt, vtt, txt
  --enable_batching     Enable batched inference (use --list-devices to check)
  --batch_size BATCH_SIZE
                        Batch size for batched inference
"""

LADA_PRESETS = """
Available encoding presets:
  Name                      Description
  ------------------------  -----------------------------------------------------------------
  h264-cpu-fast             H.264 / AVC, x264 software encoder, Fast, Medium File Size
  hevc-nvidia-gpu-hq        H.265 / HEVC, Nvidia hardware encoder, High Quality, Medium File Size
"""

LADA_DETECT = """
Available detection models:
  Name         Description                                            Path
  -----------  -----------------------------------------------------  ---------------------------
  v2                                                                  D:/x/lada_v2.pt
  v4-fast      Fast and efficient. Recommended                        D:/x/lada_v4_fast.pt
"""


# ── 1. 能力探测 ─────────────────────────────────────────────

def test_parse_options_ignores_names_mentioned_in_prose():
    """说明文字里顺带提到的参数名不能被当成"支持它"。"""
    opts = whisper_tool.parse_options(INFER_HELP)
    assert "--model_name_or_path" in opts
    assert "--overwrite" in opts
    # 出现在说明文字里的 --list-devices 不该被算进来
    assert "--list-devices" not in opts
    # 上游确实没有这个参数
    assert "--language" not in opts


def test_build_command_never_sends_unsupported_flags():
    """**核心保障**：用户设了工具不支持的参数时，跳过它而不是发出去。

    argparse 遇到未知参数会直接以退出码 2 结束，用户只会看到"跑不起来"。
    """
    opts = whisper_tool.parse_options(INFER_HELP)
    cmd = whisper_tool.build_command(
        "infer.exe", "movie.mp4", options=opts, model="small", device="cuda",
        language="zh", sub_formats="srt", overwrite=True)
    assert not any("--language" in c for c in cmd), "不支持的参数被发出去了"
    assert any(c.startswith("--model_name_or_path") for c in cmd)
    assert cmd[-1] == "movie.mp4"
    # 跳过要能告诉用户，别静默丢
    skipped = whisper_tool.skipped_arguments({"language": "zh"}, opts)
    assert skipped and "language" in skipped[0]


def test_build_command_uses_short_alias_when_canonical_missing():
    """只支持 ``--model`` 的旧版本也要能用（按探测结果选名字）。"""
    opts = {"--model", "--device"}
    cmd = whisper_tool.build_command("infer.exe", "a.mp4", options=opts, model="small")
    assert any(c.startswith("--model=") for c in cmd)
    assert not any(c.startswith("--model_name_or_path") for c in cmd)


def test_build_command_without_probe_is_optimistic():
    """还没探测过时按"乐观"语义：已提供的参数照发（能用就行）。

    探测过之后才会按支持列表过滤 —— 那是推荐路径，也能解释清楚为什么某个
    设置没生效（见 test_build_command_never_sends_unsupported_flags）。
    """
    cmd = whisper_tool.build_command("infer.exe", "a.mp4", device="cuda", model="x")
    assert "--device=cuda" in cmd
    assert "--model_name_or_path=x" in cmd
    assert cmd[-1] == "a.mp4"


def test_default_audio_suffixes_reuses_app_video_extensions():
    """扩展名要复用主程序的配置，而不是另抄一份常量。"""
    out = whisper_tool.default_audio_suffixes([".mp4", ".mkv", ".ts"])
    assert out.startswith("mp4,mkv,ts")
    assert "mp3" in out.split(",")


@pytest.mark.parametrize("code,expect", [
    (0, ""),
    (1, "文件读取"),
    (2, "参数"),
    (3221226505, "0xC0000409"),
])
def test_describe_exit_code(code, expect):
    text = whisper_tool.describe_exit_code(code)
    if expect:
        assert expect in text
    else:
        assert text == ""


def test_find_infer_exe_accepts_file_or_directory(tmp_path):
    """用户填 exe 路径和填它所在目录都得能认（两种都会出现）。"""
    exe = tmp_path / "infer.exe"
    exe.write_bytes(b"")
    assert whisper_tool.find_infer_exe(str(exe)) == exe
    assert whisper_tool.find_infer_exe(str(tmp_path)) == exe
    # 解压出来多套一层目录是很常见的情况（目录里没有 exe，但子目录里有）
    inner = tmp_path / "sub"
    inner.mkdir()
    exe2 = inner / "infer.exe"
    exe2.write_bytes(b"")
    outer = tmp_path / "outer"
    outer.mkdir()
    assert whisper_tool.find_infer_exe(str(outer)) is None
    (outer / "lada-0.11").mkdir()
    exe3 = outer / "lada-0.11" / "infer.exe"
    exe3.write_bytes(b"")
    assert whisper_tool.find_infer_exe(str(outer)) == exe3


def test_find_infer_exe_returns_none_not_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper_tool, "candidate_dirs", lambda *a, **k: [])
    assert whisper_tool.find_infer_exe(str(tmp_path / "nope")) is None
    assert "未找到" in whisper_tool.describe_exe(None)
    assert "github.com" in whisper_tool.prerequisites_hint()


# ── 2. Lada 能力探测 ────────────────────────────────────────

def test_parse_table_keeps_columns_aligned_when_description_empty():
    """``v2`` 那行描述为空 —— 按空格切会把路径挤进描述列，必须按分隔线定列。"""
    rows = lada_tool.parse_table(LADA_DETECT)
    assert [r["name"] for r in rows] == ["v2", "v4-fast"]
    v2 = rows[0]
    assert v2["description"] == ""
    assert v2["path"].endswith("lada_v2.pt"), v2
    assert ".pt" not in v2["description"]


def test_parse_table_presets():
    rows = lada_tool.parse_table(LADA_PRESETS)
    assert [r["name"] for r in rows] == ["h264-cpu-fast", "hevc-nvidia-gpu-hq"]
    assert "High Quality" in rows[1]["description"]


def test_parse_table_handles_garbage():
    assert lada_tool.parse_table("") == []
    assert lada_tool.parse_table("完全没有表格的文本") == []


def test_lada_build_command_covers_options_the_old_gui_dropped():
    """旧 GUI 的配置里有 ``mosaic_restoration_model`` 却没传下去，这里必须传。"""
    opts = {"--input", "--output", "--device", "--encoding-preset",
            "--mosaic-detection-model", "--mosaic-restoration-model",
            "--max-clip-length", "--temporary-directory", "--fp16", "--no-fp16",
            "--output-file-pattern"}
    cmd = lada_tool.build_command(
        "lada-cli.exe", "in.mp4", "out", options=opts, device="cuda:0",
        encoding_preset="hevc-nvidia-gpu-hq", detection_model="v4-fast",
        restoration_model="basicvsrpp-v1.2", max_clip_length=180, fp16=True,
        temporary_dir="out/_tmp")
    joined = " ".join(cmd)
    assert "--mosaic-restoration-model" in joined
    assert "basicvsrpp-v1.2" in joined
    assert "--fp16" in cmd and "--no-fp16" not in cmd
    assert cmd[-1] == "{orig_file_name}.restored.mp4" or \
        "{orig_file_name}.restored.mp4" in cmd


def test_lada_build_command_skips_unknown_options():
    cmd = lada_tool.build_command(
        "lada-cli.exe", "in.mp4", "out", options={"--input", "--output"},
        device="cuda:0", encoding_preset="x", fp16=True)
    assert "--device" not in cmd
    assert "--encoding-preset" not in cmd
    assert "--fp16" not in cmd


def test_lada_no_fp16_when_disabled():
    opts = {"--input", "--output", "--fp16", "--no-fp16"}
    cmd = lada_tool.build_command("c", "i", "o", options=opts, fp16=False)
    assert "--no-fp16" in cmd and "--fp16" not in cmd


def test_expected_output_matches_lada_default_pattern():
    out = lada_tool.expected_output("D:/out", "D:/media/MOVIE-001.mp4")
    assert out.name == "MOVIE-001.restored.mp4"


@pytest.mark.parametrize("line,expect", [
    ("Processing video: 42%", 42),
    ("Processing video:0%", 0),
    ("Processing video: 150%", 100),
    ("other output", None),
    ("", None),
])
def test_parse_progress(line, expect):
    assert lada_tool.parse_progress(line) == expect


def test_lada_find_accepts_file_or_directory(tmp_path):
    exe = tmp_path / "lada-cli.exe"
    exe.write_bytes(b"")
    assert lada_tool.find_lada_cli(str(exe)) == exe
    assert lada_tool.find_lada_cli(str(tmp_path)) == exe
    assert "未找到" in lada_tool.describe_cli(None)
    hint = lada_tool.prerequisites_hint()
    assert "codeberg.org" in hint and "7-Zip" in hint


# ── 3. 字幕清理 ─────────────────────────────────────────────

def test_clean_removes_adjacent_duplicates():
    srt = ("1\n00:00:01,000 --> 00:00:03,000\n你好\n\n"
           "2\n00:00:03,000 --> 00:00:05,000\n你好\n\n"
           "3\n00:00:05,000 --> 00:00:07,000\n再见\n")
    blocks = subtitle_clean.clean_blocks(subtitle_clean.parse_srt(srt))
    assert [b["text"] for b in blocks] == ["你好", "再见"]


def test_clean_splits_overlong_block_by_sentence():
    srt = "1\n00:00:00,000 --> 00:00:45,000\n第一句。第二句。第三句。\n"
    blocks = subtitle_clean.clean_blocks(subtitle_clean.parse_srt(srt))
    assert len(blocks) == 3
    assert [b["text"] for b in blocks] == ["第一句。", "第二句。", "第三句。"]
    # 时间轴必须连续且覆盖原区间
    assert blocks[0]["start"] == 0.0
    assert blocks[-1]["end"] == pytest.approx(45.0)
    for a, b in zip(blocks, blocks[1:]):
        assert a["end"] == pytest.approx(b["start"])


def test_clean_keeps_non_adjacent_repeats():
    """同一句话在片子前后都出现是正常的 —— 只回看最近若干条，不能全量去重。"""
    srt = ("1\n00:00:01,000 --> 00:00:02,000\n同一句\n\n"
           "2\n00:00:03,000 --> 00:00:04,000\n别的话\n\n"
           "3\n00:00:05,000 --> 00:00:06,000\n又一句\n\n"
           "4\n00:00:07,000 --> 00:00:08,000\n同一句\n")
    blocks = subtitle_clean.clean_blocks(subtitle_clean.parse_srt(srt),
                                         recent_window=2)
    assert len(blocks) == 4


def test_collapse_repeated_units_and_phrases():
    # 短于 6 个字符不压（避免把正常的语气词/叠词误伤）
    assert subtitle_clean.collapse_repeated_units("啊啊啊啊啊") == "啊啊啊啊啊"
    assert subtitle_clean.collapse_repeated_units("啊啊啊啊啊啊啊") != "啊啊啊啊啊啊啊"
    assert subtitle_clean.collapse_text("hello hello hello world") == "hello world"


def test_clean_file_roundtrip(tmp_path):
    f = tmp_path / "a.srt"
    f.write_text("1\n00:00:01,000 --> 00:00:03,000\n重复\n\n"
                 "2\n00:00:03,000 --> 00:00:05,000\n重复\n", encoding="utf-8")
    removed, changed = subtitle_clean.clean_file(f)
    assert changed and removed == 1
    assert f.read_text(encoding="utf-8").count("重复") == 1
    # 再跑一次不应有变化（幂等）
    assert subtitle_clean.clean_file(f) == (0, False)


def test_clean_file_ignores_unknown_extensions(tmp_path):
    f = tmp_path / "a.ass"
    f.write_text("whatever", encoding="utf-8")
    assert subtitle_clean.clean_file(f) == (0, False)
    assert subtitle_clean.clean_file(tmp_path / "missing.srt") == (0, False)


def test_vtt_and_txt_lrc_paths():
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n嘿\n\n"
           "00:00:03.000 --> 00:00:05.000\n嘿\n")
    out = subtitle_clean.clean_subtitle_text(vtt, "vtt")
    assert out.startswith("WEBVTT")
    assert out.count("嘿") == 1
    # LRC 只去**相邻**重复行，且保留时间轴
    lrc = "[00:01.00]词\n[00:02.00]词\n[00:03.00]别的\n"
    assert subtitle_clean.clean_subtitle_text(lrc, "lrc") == \
        "[00:01.00]词\n[00:03.00]别的\n"
    txt = "行\n行\n别的\n"
    assert subtitle_clean.clean_subtitle_text(txt, "txt") == "行\n别的\n"


def test_find_subtitles(tmp_path):
    video = tmp_path / "M.mp4"
    video.write_bytes(b"")
    assert subtitle_clean.find_subtitles(video) == []
    (tmp_path / "M.srt").write_text("x", encoding="utf-8")
    assert [p.name for p in subtitle_clean.find_subtitles(video)] == ["M.srt"]
    (tmp_path / "N.srt").write_text("x", encoding="utf-8")
    assert [p.name for p in subtitle_clean.find_subtitles(tmp_path / "N.mp4")] == ["N.srt"]
