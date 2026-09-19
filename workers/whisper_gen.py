"""批量生成字幕的后台线程。

按队列逐个文件调用第三方的 ``infer.exe``（详见 :mod:`utils.whisper_tool`），
生成完可选地做一次去重 / 长句拆分（:mod:`utils.subtitle_clean`）。

线程里只做"排队 + 调工具 + 汇报"，所有参数怎么拼由 ``whisper_tool.build_command``
决定 —— 这样参数逻辑可以脱离 Qt 单测。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from utils import subtitle_clean, whisper_tool


class WhisperGenWorker(QThread):
    """逐个生成字幕。

    信号
    ----
    ``log(str)``            一行人类可读的日志（同时也会把子进程的原始输出转出来）
    ``progress(int)``       总体进度 0~100
    ``file_done(str, bool, str)``  单个文件结束（路径 / 是否成功 / 补充说明）
    ``finished(dict)``      ``{"ok","fail","fails","cancelled"}``
    """

    log = Signal(str)
    progress = Signal(int)
    file_done = Signal(str, bool, str)
    finished = Signal(dict)

    def __init__(self, cfg, files, infer_exe, options=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.files = [str(f) for f in (files or [])]
        self.infer_exe = infer_exe
        #: 工具支持的选项集合（来自 --help 探测）；None = 未探测，按最小命令发
        self.options = options
        self._stopped = False
        self._paused = False

    # ── 外部控制 ──
    def stop(self) -> None:
        """请求停止：当前文件会被结束（含它的子进程），队列不再继续。"""
        self._stopped = True

    def pause(self) -> None:
        """请求暂停：**当前文件跑完**后挂起（中途打断会留下半成品字幕）。"""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # ── 主流程 ──
    def run(self) -> None:
        cfg = self.cfg
        ok = fail = 0
        fails: list = []
        total = len(self.files)
        self.log.emit(f"开始批量生成字幕：共 {total} 个文件")
        self.log.emit(f"识别工具：{whisper_tool.describe_exe(self.infer_exe)}")

        # 记一次参数摘要，出问题时用户能直接把这段贴出来
        skipped = whisper_tool.skipped_arguments({
            "model": cfg.whisper_model, "device": cfg.whisper_device,
            "compute_type": cfg.whisper_compute_type,
            "audio_suffixes": cfg.whisper_audio_suffixes,
            "sub_formats": cfg.whisper_sub_formats,
            "output_dir": cfg.whisper_output_dir,
            "language": cfg.whisper_language,
        }, self.options)
        if skipped:
            self.log.emit("⚠️ 以下参数当前版本的工具不支持，已跳过："
                          + "、".join(skipped))

        for index, path in enumerate(self.files):
            if self._stopped:
                break
            while self._paused and not self._stopped:
                self.msleep(200)
            if self._stopped:
                break

            self.log.emit("")
            self.log.emit(f"[{datetime.now():%H:%M:%S}] ({index + 1}/{total}) "
                          f"{Path(path).name}")
            success, note = self._one(path)
            if success:
                ok += 1
            else:
                fail += 1
                fails.append((path, note))
            self.file_done.emit(path, success, note)
            if total:
                self.progress.emit(int((index + 1) / total * 100))

        cancelled = self._stopped
        if cancelled:
            self.log.emit("已停止，队列中剩余文件未处理。")
        else:
            self.log.emit("")
            self.log.emit(f"全部完成：成功 {ok} / 失败 {fail}")
        self.finished.emit({"ok": ok, "fail": fail, "fails": fails,
                            "cancelled": cancelled})

    def _one(self, path: str) -> tuple:
        """处理单个文件，返回 ``(是否成功, 说明)``。"""
        cfg = self.cfg
        suffixes = cfg.whisper_audio_suffixes or \
            whisper_tool.default_audio_suffixes(cfg.video_extensions)

        result = whisper_tool.run_one(
            self.infer_exe, path,
            options=self.options,
            model=cfg.whisper_model,
            device=cfg.whisper_device,
            compute_type=cfg.whisper_compute_type,
            audio_suffixes=suffixes,
            sub_formats=cfg.whisper_sub_formats,
            output_dir=cfg.whisper_output_dir,
            overwrite=cfg.whisper_overwrite,
            language=cfg.whisper_language,
            enable_batching=cfg.whisper_batching and cfg.whisper_device != "cpu",
            batch_size=cfg.whisper_batch_size,
            max_batch_size=cfg.whisper_max_batch_size,
            merge_segments=cfg.whisper_merge_segments,
            vad_threshold=cfg.whisper_vad_threshold,
            generation_config=cfg.whisper_generation_config,
            on_line=self._on_tool_line,
            should_stop=lambda: self._stopped,
        )

        # 退出码非 0 不代表一定没产出（0xC0000409 就是个典型），
        # 所以先看磁盘上到底有没有字幕文件。
        formats = [f.strip() for f in str(cfg.whisper_sub_formats).split(",") if f.strip()]
        made = subtitle_clean.find_subtitles(path, formats or ["srt"],
                                             cfg.whisper_output_dir or None)

        if result["error"] and not made:
            return False, result["error"]

        note = ""
        if result["error"] and made:
            note = f"工具报错但字幕已生成（{result['error'].split('。')[0]}）"
            self.log.emit(f"    ⚠️ {note}")

        if cfg.whisper_cleanup and made:
            removed, changed = 0, 0
            for f in made:
                r, c = subtitle_clean.clean_file(
                    f, cfg.whisper_cleanup_max_duration,
                    cfg.whisper_cleanup_similarity)
                removed += r
                changed += 1 if c else 0
            if changed:
                extra = f"去重/拆分 {changed} 个文件（{removed} 处）"
                note = f"{note}；{extra}" if note else extra
                self.log.emit(f"    🧹 {extra}")

        if not made:
            return False, "工具执行结束但没找到生成的字幕文件"
        self.log.emit(f"    ✅ 完成（{', '.join(Path(m).name for m in made)}）")
        return True, note

    def _on_tool_line(self, line: str) -> None:
        """把子进程的原始输出转发到日志面板（空行丢掉，避免刷屏）。"""
        text = (line or "").rstrip()
        if text:
            self.log.emit("    │ " + text)
