"""계약·제어·저장소·화면 축소 계산 단위 테스트."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from rpa.core.contracts import (
    CheckResult,
    CheckStatus,
    EffectKind,
    ExecutionStatus,
    StepDefinition,
    VerificationMode,
)
from rpa.core.control import ControlState
from rpa.core.errors import InvalidTransition, RunCancelled
from rpa.core.screen_scale import MAX_LONG_EDGE, MAX_VISUAL_TOKENS, plan_screenshot, visual_tokens


# --- contracts --------------------------------------------------------------
def test_change_step_requires_operation_key():
    with pytest.raises(ValueError):
        StepDefinition(id="x", handler=lambda c, e: None, verifier=lambda c, e, a: None,
                       effect_kind=EffectKind.EXTERNAL_CHANGE)


def test_check_result_roundtrip():
    c = CheckResult(CheckStatus.PASS, VerificationMode.HUMAN, "K-1", {"a": "0012"}, {"a": "0012"}, "확인")
    again = CheckResult.from_dict(c.to_dict())
    assert again == c
    assert c.to_dict()["status"] == "PASS" and c.to_dict()["verification_mode"] == "HUMAN"


# --- control ----------------------------------------------------------------
def test_sleep_is_cancellable_quickly():
    c = ControlState()
    threading.Timer(0.1, c.request_cancel).start()
    t0 = time.monotonic()
    with pytest.raises(RunCancelled):
        c.sleep(30)
    assert time.monotonic() - t0 < 1.0


def test_wait_until_deadline():
    c = ControlState()
    assert c.wait_until(lambda: False, timeout=0.2, interval=0.05) is False
    assert c.wait_until(lambda: True, timeout=0.2) is True


def test_checkpoint_blocks_while_paused():
    c = ControlState()
    c.request_pause()
    threading.Timer(0.2, c.request_resume).start()
    t0 = time.monotonic()
    assert c.checkpoint() is True
    assert time.monotonic() - t0 >= 0.15
    assert c.checkpoint() is False


# --- run_store --------------------------------------------------------------
def _ctx(run_id="r1"):
    from rpa.core.run_context import RunContext

    return RunContext(run_id, "wf", "1", "c", "cfg", "in", "K-1", {"customer_id": "K-1"}, "/tmp/x")


def test_store_rejects_invalid_transition(tmp_path):
    from rpa.core.run_store import RunStore

    s = RunStore(tmp_path / "db.sqlite")
    s.create_run(_ctx())
    with pytest.raises(InvalidTransition):
        s.set_status("r1", ExecutionStatus.PAUSED)  # CREATED → PAUSED 불가
    s.set_status("r1", ExecutionStatus.RUNNING)
    s.set_status("r1", ExecutionStatus.FINISHED)
    with pytest.raises(InvalidTransition):
        s.set_status("r1", ExecutionStatus.RUNNING)


def test_store_intent_is_durable_and_idempotent(tmp_path):
    from rpa.core.run_store import RunStore

    db = tmp_path / "db.sqlite"
    s = RunStore(db)
    s.create_run(_ctx())
    row, created = s.record_intent("op1", "r1", "step", "K-1", "h", "t", "i")
    assert created and row["dispatch_state"] == "INTENT_RECORDED"
    s.mark_dispatching("op1")
    s.close()
    # 다른 연결(재시작)에서도 보인다
    s2 = RunStore(db)
    row2, created2 = s2.record_intent("op1", "r1", "step", "K-1", "h", "t", "i")
    assert not created2 and row2["dispatch_state"] == "UNKNOWN" and row2["dispatch_count"] == 1


# --- screen_scale -----------------------------------------------------------
@pytest.mark.parametrize("w,h", [(1920, 1080), (1920, 1200), (2560, 1440), (3840, 2160), (5120, 1440), (1366, 768)])
def test_screenshot_plan_within_api_limits(w, h):
    p = plan_screenshot(w, h)
    assert max(p.width, p.height) <= MAX_LONG_EDGE
    assert visual_tokens(p.width, p.height) <= MAX_VISUAL_TOKENS
    assert p.width <= w and p.height <= h
    many = plan_screenshot(w, h, many_images=True)
    assert max(many.width, many.height) <= 2000


def test_small_screen_not_upscaled():
    p = plan_screenshot(1366, 768)
    assert (p.width, p.height, p.scale) == (1366, 768, 1.0)


def test_coordinate_roundtrip_with_origin():
    p = plan_screenshot(3840, 2160, origin_x=-3840, origin_y=0)
    assert p.width < 3840
    sx, sy = p.to_screen(p.width - 1, p.height - 1)
    assert -3840 <= sx < 0 and 0 <= sy < 2160
    mx, my = p.to_model(*p.to_screen(100, 200))
    assert abs(mx - 100) <= 1 and abs(my - 200) <= 1
    with pytest.raises(ValueError):
        p.to_screen(p.width, 10)  # 범위 밖 좌표는 클램프하지 않고 거부


# --- GUI 모듈 비의존 ----------------------------------------------------------
def test_engine_and_demo_do_not_import_gui_modules(tmp_path):
    code = f"""
import sys
from rpa.adapters.fake import make_fake_adapters
from rpa.core.engine import Engine
from rpa.core.ownership import InProcessLock
from rpa.core.run_store import RunStore
from rpa.workflows.demo_fake.workflow import build
d = r"{tmp_path}"
e = Engine(RunStore(d + "/db"), make_fake_adapters(), d, runner_lock=InProcessLock())
e.start(build(), {{"customer_id": "DEMO-001"}})
bad = [m for m in ("pyautogui", "pywinauto", "playwright", "comtypes", "win32api") if m in sys.modules]
assert not bad, bad
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
