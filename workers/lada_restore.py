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

from utils import cpu_affinity, gpu, lada_tool


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


#: 兼容旧名字（有调用方按这个名字引用过）
gpu_vram_used_gb = gpu.vram_used_gb


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
        #: 绑核用的核心列表（run() 里按配置解析一次，避免每个文件都重算）
        self.affinity_info: dict = {}
        self.affinity_cores: list = []

    def stop(self) -> None:
        self._stopped = True

    # ── 运行 ──
    def run(self) -> None:
        cfg = self.cfg
        n = max(1, min(4, int(getattr(cfg, "lada_parallel_workers", 1) or 1)))
        total = len(self.videos)
        self.log.emit(f"开始破解：共 {total} 个文件，并发 {n}")
        self.log.emit(f"破解工具：{lada_tool.describe_cli(self.cli_path)}")

        # 绑核：按配置解析一次，并把**判定来源**明确告诉用户 ——
        # 这项优化只对 Intel 混合架构有明显效果，别的 CPU 上会自动退回全部核心。
        self.affinity_info = cpu_affinity.detect_affinity(
            cfg.lada_cpu_affinity, cfg.lada_cpu_affinity_cores)
        self.affinity_cores = list(self.affinity_info.get("cores") or [])
        self.log.emit(f"CPU 绑定：{cpu_affinity.describe_affinity(self.affinity_info)}")
        if self.affinity_info.get("mode") == "auto" and \
                self.affinity_info.get("source") != cpu_affinity._SRC_API:
            self.log.emit("    （自动探测没拿到精确拓扑，已按说明退回；"
                          "想让任务跑在特定核心上，可在设置里改成自定义）")
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
                used = gpu.vram_used_gb()
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
            cpu_affinity=self.affinity_cores or None,
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
    """兼容旧名字：等价于自动模式的绑核结果。

    真正的判定逻辑在 :mod:`utils.cpu_affinity` —— 那边会先走 Windows API 读
    真实拓扑，读不到才退回启发式。**这里原来的启发式在本机的混合架构 CPU 上
    算不出 P 核（逻辑核数 ≠ 物理核数 × 2），会静默退回"全部核心"，
    等于绑核没生效**，所以换成精确探测。
    """
    return cpu_affinity.detect_affinity("auto")["cores"]
