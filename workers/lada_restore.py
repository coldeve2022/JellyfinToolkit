"""马赛克破解（Lada）的后台线程：能力探测 + 批量破解队列。

- :class:`LadaProbeWorker` —— 读这台机器上**真正可用**的预设/模型/设备。
  每次探测都要起 5 次 ``lada-cli.exe``（实测约 20 秒），所以必须放后台，
  否则界面会假死。
- :class:`LadaRestoreWorker` —— 逐个（或并发 N 个）调用 ``lada-cli.exe``。

两者的参数拼装都在 :mod:`utils.lada_tool` 里，线程只负责排队与汇报。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from utils import lada_tool


class LadaProbeWorker(QThread):
    """探测 Lada 的能力（预设 / 检测模型 / 修复模型 / 设备 / 支持的选项）。"""

    log = Signal(str)
    done = Signal(dict)

    def __init__(self, cli_path, parent=None):
        super().__init__(parent)
        self.cli_path = cli_path

    def run(self) -> None:
        self.log.emit("正在读取 Lada 的能力（约 10~30 秒，要起好几次它的 CLI）…")
        info = lada_tool.probe(self.cli_path)
        if info.get("ok"):
            self.log.emit(
                f"读取完成：预设 {len(info['presets'])} 个、"
                f"检测模型 {len(info['detection_models'])} 个、"
                f"修复模型 {len(info['restoration_models'])} 个、"
                f"设备 {len(info['devices'])} 个")
        else:
            self.log.emit(f"读取失败：{info.get('error') or '未知原因'}")
        self.done.emit(info)


def gpu_vram_used_gb() -> float:
    """当前 GPU 已用显存（GB）。读不到返回 ``-1``。

    用 ``nvidia-smi`` 而不是额外的 Python 库：只要装了 NVIDIA 驱动就有它，
    不必再给用户加一个依赖。读不到时调用方应当**放行**（不做门控），
    而不是把队列卡死。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
            creationflags=(0x08000000 if __import__("os").name == "nt" else 0),
        )
    except (OSError, subprocess.SubprocessError):
        return -1.0
    if out.returncode != 0 or not (out.stdout or "").strip():
        return -1.0
    try:
        first = out.stdout.strip().splitlines()[0].strip()
        return float(first) / 1024.0
    except (ValueError, IndexError):
        return -1.0


class LadaRestoreWorker(QThread):
    """批量破解队列。

    信号
    ----
    ``log(str)``            人类可读日志（含子进程原始输出）
    ``progress(int)``       总体进度 0~100（按完成个数算）
    ``file_progress(str,int)`` 单个文件的百分比（Lada 只在处理视频时输出进度）
    ``file_done(str,bool,str)`` 单个文件结束
    ``finished(dict)``      ``{"ok","fail","fails","cancelled","stopped_by_gate"}``

    并发：``cfg.lada_parallel_workers`` 个内部线程同时取任务。
    显存不够就保持 1 —— 每个实例都会各自占一份显存。
    """

    log = Signal(str)
    progress = Signal(int)
    file_progress = Signal(str, int)
    file_done = Signal(str, bool, str)
    finished = Signal(dict)

    def __init__(self, cfg, videos, cli_path, options=None, ffprobe: str = "ffprobe",
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.videos = [str(v) for v in (videos or [])]
        self.cli_path = cli_path
        self.options = options
        self.ffprobe = ffprobe
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    # ── 运行 ──
    def run(self) -> None:
        cfg = self.cfg
        n = max(1, min(4, int(getattr(cfg, "lada_parallel_workers", 1) or 1)))
        total = len(self.videos)
        self.log.emit(f"开始破解：共 {total} 个文件，并发 {n}")
        self.log.emit(f"破解工具：{lada_tool.describe_cli(self.cli_path)}")
        if cfg.lada_vram_gate:
            self.log.emit(f"显存门控已开启：占用超过 {cfg.lada_vram_high:.1f} GB 时暂停，"
                          f"低于 {cfg.lada_vram_low:.1f} GB 时继续")

        pending = list(self.videos)
        lock = threading.Lock()
        stats = {"ok": 0, "fail": 0, "fails": [], "gated": False}
        done_count = {"n": 0}

        def take():
            with lock:
                return pending.pop(0) if pending else None

        def gate():
            """显存门控：占用过高时等待。返回是否因为停止而退出。"""
            if not cfg.lada_vram_gate:
                return False
            while not self._stopped:
                used = gpu_vram_used_gb()
                if used < 0 or used <= cfg.lada_vram_high:
                    return False
                stats["gated"] = True
                with lock:
                    first = not stats.get("_gate_logged")
                    stats["_gate_logged"] = True
                if first:
                    self.log.emit(f"⏸ 显存占用 {used:.1f} GB，超过阈值，暂缓启动新任务…")
                self.msleep(2000)
            return True

        def body():
            wid = threading.current_thread().name
            while not self._stopped:
                if gate():
                    return
                path = take()
                if path is None:
                    return
                self.log.emit("")
                self.log.emit(f"[{datetime.now():%H:%M:%S}] {wid} 开始：{Path(path).name}")
                ok, note = self._one(path, wid)
                with lock:
                    if ok:
                        stats["ok"] += 1
                    else:
                        stats["fail"] += 1
                        stats["fails"].append((path, note))
                    done_count["n"] += 1
                    current = done_count["n"]
                self.file_done.emit(path, ok, note)
                if total:
                    self.progress.emit(int(current / total * 100))

        threads = [threading.Thread(target=body, name=f"lada-{i + 1}", daemon=True)
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        cancelled = self._stopped
        if cancelled:
            self.log.emit("已停止，队列中剩余文件未处理。")
        else:
            self.log.emit("")
            self.log.emit(f"全部完成：成功 {stats['ok']} / 失败 {stats['fail']}")
        self.finished.emit({
            "ok": stats["ok"], "fail": stats["fail"], "fails": stats["fails"],
            "cancelled": cancelled, "stopped_by_gate": bool(stats["gated"]),
        })

    def _one(self, path: str, wid: str) -> tuple:
        """处理单个视频，返回 ``(是否成功, 说明)``。"""
        cfg = self.cfg
        video = Path(path)
        out_dir = Path(cfg.lada_output_dir) if cfg.lada_output_dir else video.parent
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"输出目录不可用：{e}"

        # 每个并发槽位用各自的临时目录：共用会让 Lada 的中间文件互相覆盖
        temp_dir = cfg.lada_temp_dir or out_dir / "_tmp"
        if n_slots(self.cfg) > 1:
            temp_dir = Path(temp_dir) / wid.replace("lada-", "w")

        pattern = cfg.lada_output_pattern or lada_tool.OUTPUT_PATTERN
        expected = Path(out_dir) / pattern.replace("{orig_file_name}", video.stem)

        result = lada_tool.run_one(
            self.cli_path, path, str(out_dir),
            options=self.options,
            device=cfg.lada_device,
            encoding_preset=cfg.lada_encoding_preset,
            detection_model=cfg.lada_detection_model,
            restoration_model=cfg.lada_restoration_model,
            max_clip_length=cfg.lada_max_clip_length,
            fp16=cfg.lada_fp16,
            temporary_dir=str(temp_dir),
            detect_face_mosaics=cfg.lada_detect_face_mosaics,
            mp4_fast_start=cfg.lada_mp4_fast_start,
            output_pattern=pattern,
            on_line=self._on_tool_line,
            progress=lambda pct, p=path: self.file_progress.emit(p, pct),
            should_stop=lambda: self._stopped,
            cpu_affinity=p_core_threads() if cfg.lada_pin_to_p_core else None,
        )
        if result.get("error"):
            return False, result["error"]

        # 退出码 0 ≠ 一定成功：Lada 有过"静默产出坏文件"的情况
        if not expected.is_file() or expected.stat().st_size == 0:
            tail = (result["lines"] or ["(无输出)"])[-1][:120]
            return False, f"未生成输出文件（末行：{tail}）"

        if cfg.lada_validate_output:
            ok, why = lada_tool.validate_output_timing(
                path, expected, self.ffprobe)
            if not ok:
                return False, f"{why}（输出文件已保留，请自行确认）"

        size_mb = expected.stat().st_size / 1048576
        self.log.emit(f"    ✅ 完成：{expected.name}（{size_mb:.0f} MB）")
        return True, ""

    def _on_tool_line(self, line: str) -> None:
        text = (line or "").rstrip()
        if text:
            self.log.emit("    │ " + text)


def n_slots(cfg) -> int:
    """并发槽位数（与 :meth:`LadaRestoreWorker.run` 里的一致）。"""
    return max(1, min(4, int(getattr(cfg, "lada_parallel_workers", 1) or 1)))


def p_core_threads() -> list:
    """P 核逻辑线程号列表；取不到返回空（表示不绑核）。

    直接复用系统调度也行，但实测不绑核时 Windows 会把后台进程甩到 E 核上跑，
    长任务（几十分钟）的差距很明显。
    """
    try:
        import psutil

        if not hasattr(psutil, "cpu_count"):
            return []
        phys = psutil.cpu_count(logical=False) or 0
        logical = psutil.cpu_count(logical=True) or 0
        if logical and phys and logical == phys * 2:
            # 有超线程：前 2*phys 个逻辑核通常就是含 HT 的 P 核
            return list(range(min(logical, phys * 2)))
        return list(range(logical)) if logical else []
    except Exception:  # noqa: BLE001
        return []
