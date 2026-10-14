"""CLI 끝까지 실행 + Windows 읽기 전용 측정 테스트."""

from __future__ import annotations

import json
import sys

import pytest

from rpa.cli import main


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_cli_run_crash_resume_flow(tmp_path, capsys):
    base = ["--data-dir", str(tmp_path), "--json", "--no-hotkeys"]
    assert main(base + ["run", "demo_web_to_desktop", "-i", "customer_id=DEMO-001",
                        "--fault", "desktop.save=crash_after_apply"]) == 70
    run_id = next(e["run_id"] for e in _events(capsys) if e["type"] == "run_created")

    # 미완료 실행이 있으면 새 실행 거부
    assert main(base + ["run", "demo_web_to_desktop", "-i", "customer_id=DEMO-002"]) == 2
    capsys.readouterr()

    assert main(base + ["resume", run_id]) == 0
    summary = _events(capsys)[-1]
    assert summary == {"type": "summary", "run_id": run_id, "execution_status": "FINISHED",
                       "business_outcome": "AUTO_VERIFIED", "reason": "완료"}
    backend = json.loads((tmp_path / "fake_backend.json").read_text(encoding="utf-8"))
    assert backend["save_calls"] == 1


def test_cli_waiting_user_resolve(tmp_path, capsys):
    base = ["--data-dir", str(tmp_path), "--json", "--no-hotkeys"]
    assert main(base + ["run", "demo_web_to_desktop", "-i", "customer_id=DEMO-001",
                        "--fault", "web.get_customer=login_expired"]) == 3
    run_id = _events(capsys)[0]["run_id"]
    assert main(base + ["resolve", run_id, "--resolution", "done", "--note", "로그인 완료", "--resume"]) == 0
    assert _events(capsys)[-1]["execution_status"] == "FINISHED"


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용")
def test_preflight_measures_real_display():
    from rpa.platform.windows.preflight import run_preflight

    r = run_preflight(probe_capture=True)
    d = r.display
    assert d["monitors"], "모니터를 하나 이상 측정해야 함"
    primary = next(m for m in d["monitors"] if m["primary"])
    assert primary["width"] > 0 and primary["height"] > 0
    assert r.screenshot_plan["source_width"] == primary["width"]
    assert {c.id for c in r.checks} >= {"dpi_awareness", "input_desktop", "capture", "monitor_count"}


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용")
def test_named_mutex_is_exclusive():
    from rpa.core.errors import RunnerBusy
    from rpa.platform.windows.instance_lock import NamedMutexLock

    name = "Local\\LocalRPA.Test.Lock"
    a, b = NamedMutexLock(name), NamedMutexLock(name)
    a.acquire()
    try:
        with pytest.raises(RunnerBusy):
            b.acquire()
    finally:
        a.release()
    b.acquire()
    b.release()


def test_preflight_evaluate_flags_locked_and_black_capture():
    if sys.platform != "win32":
        pytest.skip("display 모듈은 Windows에서만 import 가능")
    from rpa.platform.windows.display import CaptureProbe, DisplayReport, MonitorInfo
    from rpa.platform.windows.preflight import evaluate

    rep = DisplayReport(
        dpi_awareness_set=True,
        monitors=[MonitorInfo("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, True, 120)],
        input_desktop_accessible=False,
        capture=CaptureProbe(True, 1920, 1080, distinct_samples=1),
    )
    r = evaluate(rep)
    levels = {c.id: c.level for c in r.checks}
    assert not r.ok
    assert levels["input_desktop"] == "FAIL" and levels["capture"] == "FAIL"
