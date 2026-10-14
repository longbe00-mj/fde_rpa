"""PyAutoGUI 어댑터 라이브 테스트 (테스트 폼 대상). LOCALRPA_LIVE=1 필요."""

from __future__ import annotations

import time

import pytest

NAME = "홍길동 & Co. <테스트> ①"
PHONE = "0012-0034"


def test_live_normal_flow_korean_special_leading_zero(live):
    h = live()
    run_id = h.start(customer_id="LIVE-001", name=NAME, phone=PHONE)
    run = h.run(run_id)
    assert run["execution_status"] == "FINISHED", run["status_reason"]
    assert run["business_outcome"] == "AUTO_VERIFIED"
    rec = h.record("LIVE-001")
    assert (rec["customer_id"], rec["name"], rec["phone"]) == ("LIVE-001", NAME, PHONE)
    shots = [e for e in h.store.evidence(run_id) if e["kind"] == "screen"]
    assert len(shots) == 2
    ctx = h.store.load_context(run_id)
    assert ctx.owned_resources[0]["owned"] is True  # 이 실행이 띄운 앱이므로 종료함


def test_live_focus_lost_stops_input_then_resume_reverifies(live, distractor):
    from rpa.platform.windows import win32

    # 첫 두 글자 입력 직후 방해 창이 포커스를 가져간다
    h = live(type_chunk=2, hook=lambda: (win32.try_set_foreground(distractor), time.sleep(0.3)))
    run_id = h.start(customer_id="LIVE-002", name=NAME, phone=PHONE)
    run = h.run(run_id)
    assert run["execution_status"] == "WAITING_USER", run["status_reason"]
    assert "포커스" in run["status_reason"]
    assert h.win.sent == ["LI"]  # 포커스를 잃은 뒤 추가 입력 없음
    assert h.record("LIVE-002") is None
    assert any(e["kind"] == "failure_screen" for e in h.store.evidence(run_id))

    # 사용자가 확인 후 재개 → 폼 값 대조(불일치) → 다시 입력 → 저장 → 재조회 검증
    h.engine.resolve(h.workflow, run_id, "done", "방해 창 확인, 폼으로 돌아감")
    assert h.engine.resume(h.workflow, run_id).value == "FINISHED", h.run(run_id)["status_reason"]
    rec = h.record("LIVE-002")
    assert (rec["name"], rec["phone"]) == (NAME, PHONE)
    assert any(e["type"] == "reconciled" for e in h.events)


def test_live_failsafe_cancels_and_marks_unknown(live):
    import pyautogui

    h = live(type_chunk=2, hook=lambda: pyautogui.moveTo(0, 0))
    run_id = h.start(customer_id="LIVE-003", name=NAME, phone=PHONE)
    run = h.run(run_id)
    assert run["execution_status"] == "CANCELLED"
    assert "FAILSAFE" in run["status_reason"]
    assert run["business_outcome"] == "UNKNOWN"  # 입력 도중 중단 → '아무것도 안 함'으로 표시하지 않음
    assert h.win.sent == ["LI"]
    assert h.record("LIVE-003") is None
    h.dv.close_owned()


def test_live_cancel_flag_between_chunks(live):
    holder = {}
    h = live(type_chunk=2, hook=lambda: holder["h"].control.request_cancel())
    holder["h"] = h
    run_id = h.start(customer_id="LIVE-004", name=NAME, phone=PHONE)
    assert h.run(run_id)["execution_status"] == "CANCELLED"
    assert h.win.sent == ["LI"]
    h.dv.close_owned()


def test_live_hotkey_sets_cancel_flag():
    import pyautogui

    from rpa.core.control import ControlState
    from rpa.platform.windows.hotkeys import ControlHotkeys

    control = ControlState()
    with ControlHotkeys(control) as hk:
        if hk.errors:
            pytest.skip("; ".join(hk.errors))
        pyautogui.hotkey("ctrl", "alt", "p")
        assert control.wait_until(lambda: control.pause_requested, timeout=2)
        pyautogui.hotkey("ctrl", "alt", "p")
        assert control.wait_until(lambda: not control.pause_requested, timeout=2)
        pyautogui.hotkey("ctrl", "alt", "q")
        for _ in range(40):
            if control.cancel_requested:
                break
            time.sleep(0.05)
    assert control.cancel_requested


def test_live_image_anchor_on_save_button(live, tmp_path):
    """저장 버튼 영역을 기준 이미지로 잘라 창 안에서 정확히 한 곳으로 찾는지 확인."""
    from PIL import Image

    from rpa.adapters.desktop_visual import WindowSpec

    h = live()
    dv = h.dv
    dv.launch(h.adapters["form"].launch_args, WindowSpec(title_regex="^LocalRPA Test Form$"))
    dv.activate()
    dv.wait_visually_stable()
    shot = dv.capture(tmp_path / "client.png", area="client")
    # 저장 버튼: 클라이언트 (120,180) 120x40 — 테두리 안쪽만 사용
    Image.open(shot["path"]).crop((124, 184, 236, 216)).save(tmp_path / "save_btn.png")
    p = dv.locate_image(tmp_path / "save_btn.png")
    assert abs(p.x - 180) <= 2 and abs(p.y - 200) <= 2
    dv.close_owned()
