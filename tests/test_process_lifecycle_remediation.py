import sys
import time

from utils.proc import run_streaming


def test_started_callback_receives_live_process_and_cancel_reaps_it():
    seen = []
    result = run_streaming(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        on_started=lambda proc: seen.append(proc),
        should_stop=lambda: bool(seen),
        timeout=4,
    )
    assert result["stopped"]
    assert len(seen) == 1
    assert seen[0].poll() is not None


def test_stop_callback_failure_reaps_child_and_reports_error():
    seen = []

    def broken_stop():
        raise RuntimeError("callback broke")

    try:
        result = run_streaming(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            on_started=seen.append,
            should_stop=broken_stop,
        )
        assert "callback broke" in result["error"]
        assert seen[0].poll() is not None
    finally:
        for proc in seen:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_parent_exit_does_not_leave_descendant_holding_output_pipe():
    code = (
        "import subprocess,sys,os; "
        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(4)"]); '
        "print(p.pid,flush=True); os._exit(0)"
    )
    started = time.monotonic()
    result = run_streaming([sys.executable, "-c", code], timeout=2)
    assert result["code"] == 0
    assert time.monotonic() - started < 2
