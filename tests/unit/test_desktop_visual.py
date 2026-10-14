"""DesktopVisualAdapter 안전 규칙 — 가짜 Win32/PyAutoGUI로 실제 입력 없이 검증."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from rpa.adapters.desktop_visual import (
    DesktopVisualAdapter,
    FocusLost,
    Point,
    VisualSettings,
    WindowSpec,
    WrongThread,
    _dedupe_boxes,
)
from rpa.core.control import ControlState
from rpa.core.errors import FailSafeTriggered, PreconditionFailed, RunCancelled, TransientError, UserActionRequired


@dataclass(frozen=True)
class R:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self):
        return self.left + self.width

    @property
    def bottom(self):
        return self.top + self.height

    def contains(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def contains_rect(self, o):
        return self.left <= o.left and self.top <= o.top and o.right <= self.right and o.bottom <= self.bottom


@dataclass(frozen=True)
class W:
    hwnd: int
    pid: int = 100
    title: str = "LocalRPA Test Form"
    class_name: str = "TkTopLevel"
    process_name: str = "python.exe"
    visible: bool = True
    minimized: bool = False
    frame: R = R(100, 100, 522, 339)
    client: R = R(101, 138, 520, 300)
    dpi: int = 120


class FakeWin:
    def __init__(self, windows: list[W]):
        self.windows = {w.hwnd: w for w in windows}
        self.fg = windows[0].hwnd if windows else 0
        self.covered_by: Optional[int] = None
        self.sent: list[str] = []
        self.clipboard: Optional[str] = "사용자 원래 클립보드"
        self.only_text = True
        self.seq = 0
        self.set_fg_works = True
        self.elevated = {}
        self.me_elevated = False
        self.released = 0
        self.after_send = None  # 콜백 (첫 청크 후 포커스 변경 등)
        self.closed: list[int] = []
        self.z: list[int] = []  # 위에서부터의 z-order (비어 있으면 windows 순서)
        self.minimizable: set[int] = set()
        self.minimized: list[int] = []
        self.restored: list[int] = []

    # 조회
    def top_level_windows(self):
        return list(self.windows.values())

    def window_info(self, hwnd):
        return self.windows.get(hwnd)

    def is_window(self, hwnd):
        return hwnd in self.windows

    def foreground_hwnd(self):
        return self.fg

    def root_at_point(self, x, y):
        if self.covered_by:
            return self.covered_by
        order = self.z or list(self.windows)
        return next((h for h in order if h in self.windows and h not in self.minimized
                     and self.windows[h].frame.contains(x, y)), 0)

    def can_minimize(self, hwnd):
        return hwnd in self.minimizable

    def minimize_window(self, hwnd):
        self.minimized.append(hwnd)
        return True

    def restore_window_noactivate(self, hwnd):
        self.restored.append(hwnd)

    def try_set_foreground(self, hwnd):
        if self.set_fg_works:
            self.fg = hwnd
        return self.fg == hwnd

    def process_elevated(self, pid):
        return self.elevated.get(pid, False)

    def current_process_elevated(self):
        return self.me_elevated

    def close_window(self, hwnd):
        self.closed.append(hwnd)
        self.windows.pop(hwnd, None)

    # 입력
    def send_unicode(self, text):
        self.sent.append(text)
        if self.after_send:
            self.after_send(self)

    def release_modifiers(self):
        self.released += 1
        return []

    # 클립보드
    def clipboard_has_only_text(self):
        return self.only_text

    def get_clipboard_text(self):
        return self.clipboard

    def set_clipboard_text(self, t):
        self.clipboard = t
        self.seq += 1

    def clipboard_sequence(self):
        return self.seq


class FailSafeException(Exception):
    pass


class FakePG:
    def __init__(self, win: FakeWin):
        self.win = win
        self.calls: list[tuple] = []
        self.failsafe = False
        self.field_value = ""
        self.boxes: list[tuple] = []

    def failSafeCheck(self):
        if self.failsafe:
            raise FailSafeException("corner")

    def click(self, *a, **k):
        self.calls.append(("click", a, k))
        # 제목 표시줄 클릭으로 활성화되는 경우 흉내
        for w in self.win.windows.values():
            if w.frame.contains(a[0], a[1]) and not w.client.contains(a[0], a[1]):
                self.win.fg = w.hwnd

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))
        if keys == ("ctrl", "c"):
            self.win.set_clipboard_text(self.field_value)

    def locateAllOnScreen(self, template, **kw):
        self.calls.append(("locate", template, kw))
        return iter(self.boxes)

    def mouseUp(self):
        self.calls.append(("mouseUp",))

    frames: list[bytes] = []

    def screenshot(self, region):
        self.calls.append(("screenshot", region))
        data = self.frames.pop(0) if self.frames else b"same"

        class Img:
            def save(self, p):
                open(p, "wb").write(b"png")

            def tobytes(self):
                return data

        return Img()


MON = R(0, 0, 1920, 1200)


def make(windows=None, settings=VisualSettings(chunk_delay=0)):
    win = FakeWin(windows if windows is not None else [W(1)])
    pg = FakePG(win)
    control = ControlState()
    dv = DesktopVisualAdapter(control, settings, win=win, pyautogui_loader=lambda s: pg, monitor_loader=lambda: MON)
    return dv, win, pg, control


def attached(**kw):
    dv, win, pg, control = make(**kw)
    dv.attach(WindowSpec(title_regex="^LocalRPA Test Form$"))
    return dv, win, pg, control


def clicks(pg):
    return [c for c in pg.calls if c[0] == "click"]


# --- 대상 창 ---------------------------------------------------------------------
def test_spec_requires_condition():
    with pytest.raises(ValueError):
        WindowSpec()


def test_attach_ambiguous_windows_refused():
    dv, *_ = make([W(1), W(2)])
    with pytest.raises(PreconditionFailed, match="2개"):
        dv.attach(WindowSpec(title_regex="Test Form"))


def test_attach_missing_window_is_transient():
    dv, *_ = make([])
    with pytest.raises(TransientError):
        dv.attach(WindowSpec(title_regex="x"), timeout=0.1)


def test_attach_window_outside_primary_monitor_refused():
    dv, *_ = make([W(1, frame=R(1800, 100, 522, 339), client=R(1801, 138, 520, 300))])
    with pytest.raises(PreconditionFailed, match="주 모니터"):
        dv.attach(WindowSpec(title_regex="Test"))


def test_attach_elevated_target_refused_uipi():
    dv, win, *_ = make()
    win.elevated[100] = True
    with pytest.raises(PreconditionFailed, match="UIPI"):
        dv.attach(WindowSpec(title_regex="Test"))


def test_launch_identifies_new_window_not_preexisting(monkeypatch):
    dv, win, *_ = make([W(1)])

    class P:
        pid = 999

        def poll(self):
            return None

    def fake_popen(args):
        win.windows[2] = W(2, pid=555)  # 런처의 자식 프로세스가 연 새 창
        return P()

    monkeypatch.setattr("rpa.adapters.desktop_visual.subprocess.Popen", fake_popen)
    t = dv.launch(["app.exe"], WindowSpec(title_regex="Test"))
    assert t.hwnd == 2 and t.owned


def test_close_owned_only_closes_launched_window():
    dv, win, *_ = attached()
    dv.close_owned()
    assert win.closed == []  # 사용자가 연 창(attach)은 닫지 않음


# --- 포커스·좌표 -------------------------------------------------------------------
def test_click_refused_when_focus_elsewhere():
    dv, win, pg, _ = attached()
    win.windows[9] = W(9, title="메신저", frame=R(800, 600, 300, 200), client=R(800, 630, 300, 170))
    win.fg = 9
    with pytest.raises(FocusLost):
        dv.click(Point(300, 86, "name"))
    assert clicks(pg) == []


def test_click_outside_client_refused():
    dv, win, pg, _ = attached()
    with pytest.raises(PreconditionFailed, match="클라이언트 영역"):
        dv.click(Point(600, 10))
    assert clicks(pg) == []


def test_click_on_covered_point_refused():
    dv, win, pg, _ = attached()
    win.covered_by = 77
    with pytest.raises(UserActionRequired, match="가려져"):
        dv.click(Point(300, 86, "name"))
    assert clicks(pg) == []


def test_click_uses_client_relative_coordinates():
    dv, win, pg, _ = attached()
    assert dv.click(Point(300, 86)) == (401, 224)
    assert clicks(pg)[0][1] == (401, 224)


def test_activate_tries_once_then_caption_click_then_fails():
    dv, win, pg, _ = attached()
    win.windows[9] = W(9, title="other", frame=R(1500, 900, 300, 200), client=R(1500, 930, 300, 170))
    win.fg = 9
    win.set_fg_works = False
    dv.activate()  # 제목 표시줄 클릭으로 활성화됨
    assert win.fg == 1
    assert len(clicks(pg)) == 1
    caption_y = clicks(pg)[0][1][1]
    assert 100 <= caption_y < 138  # 클라이언트가 아닌 제목 표시줄

    win.fg = 9
    win.covered_by = 9  # 제목 표시줄도 가려짐 → 클릭하지 않고 사용자에게 요청
    with pytest.raises(FocusLost):
        dv.activate()
    assert len(clicks(pg)) == 1


def test_activate_custom_titlebar_uses_click_point():
    # Electron처럼 OS 제목 표시줄이 없는 창: frame == client
    full = R(0, 0, 1920, 1140)
    dv, win, pg, _ = make([W(1, frame=full, client=full)])
    dv.attach(WindowSpec(title_regex="Test"))
    win.windows[9] = W(9, title="terminal", frame=R(300, 300, 800, 500), client=R(300, 330, 800, 470))
    win.fg, win.set_fg_works = 9, False
    with pytest.raises(FocusLost):
        dv.activate()  # 제목 표시줄도 click_point도 없음 → 클릭하지 않음
    assert clicks(pg) == []
    pg.click = lambda x, y, **k: (pg.calls.append(("click", (x, y), k)), setattr(win, "fg", 1))
    dv.activate(Point(960, 16, "titlebar"))
    assert clicks(pg)[0][1] == (960, 16) and win.fg == 1


def _covered_custom_window(settings):
    full = R(0, 0, 1920, 1140)
    dv, win, pg, _ = make([W(1, frame=full, client=full)], settings=settings)
    dv.attach(WindowSpec(title_regex="Test"))
    # 다른 앱(전체 화면)이 대상 위에 있고, 메모장 일부가 그 아래·대상 위에 있음
    win.windows[8] = W(8, title="Hermes", process_name="Hermes.exe", frame=full, client=full)
    win.windows[9] = W(9, title="메모장", process_name="Notepad.exe", frame=R(70, 60, 1400, 800), client=R(70, 90, 1400, 770))
    win.z = [8, 9, 1]
    win.fg, win.set_fg_works = 8, False

    def click(x, y, **k):
        pg.calls.append(("click", (x, y), k))
        win.fg = win.root_at_point(x, y)

    pg.click = click
    return dv, win, pg


def test_activate_does_not_minimize_without_opt_in():
    dv, win, pg = _covered_custom_window(VisualSettings(chunk_delay=0))
    with pytest.raises(FocusLost):
        dv.activate(Point(960, 16))
    assert win.minimized == [] and clicks(pg) == []


def test_activate_minimizes_covering_windows_then_restores():
    dv, win, pg = _covered_custom_window(VisualSettings(chunk_delay=0, minimize_obstructions=True))
    win.minimizable = {8, 9}
    dv.activate(Point(960, 16))
    assert win.minimized == [8, 9]  # 위에서부터 드러나는 창 순서대로
    assert clicks(pg)[0][1] == (960, 16) and win.fg == 1
    assert [d["process"] for d in dv.minimized] == ["Hermes.exe", "Notepad.exe"]
    assert dv.restore_obstructions() == 2
    assert win.restored == [9, 8] and dv.minimized == []


def test_activate_refuses_when_cover_cannot_be_minimized():
    dv, win, pg = _covered_custom_window(VisualSettings(chunk_delay=0, minimize_obstructions=True))
    win.minimizable = set()  # 예: 셸·도구 창
    with pytest.raises(UserActionRequired, match="최소화할 수 없"):
        dv.activate(Point(960, 16))
    assert clicks(pg) == []


def test_activate_without_click_permission():
    dv, win, pg, _ = make(settings=VisualSettings(allow_click_activate=False))
    dv.attach(WindowSpec(title_regex="Test"))
    win.windows[9] = W(9, title="other", frame=R(1500, 900, 300, 200), client=R(1500, 930, 300, 170))
    win.fg, win.set_fg_works = 9, False
    with pytest.raises(FocusLost):
        dv.activate()
    assert clicks(pg) == []


def test_target_window_closed_mid_run():
    dv, win, pg, _ = attached()
    del win.windows[1]
    with pytest.raises(UserActionRequired, match="사라"):
        dv.click(Point(10, 10))


# --- 입력 ---------------------------------------------------------------------
def test_type_text_chunks_and_stops_when_focus_lost():
    dv, win, pg, _ = attached(settings=VisualSettings(type_chunk=4, chunk_delay=0))

    def steal(w):
        w.windows[9] = W(9, title="other", frame=R(1500, 900, 300, 200), client=R(1500, 930, 300, 170))
        w.fg = 9

    win.after_send = steal
    with pytest.raises(FocusLost):
        dv.type_text("홍길동 & Co. <테스트> 0012")
    assert win.sent == ["홍길동 "]  # 첫 청크 이후 추가 입력 없음


def test_type_text_stops_on_cancel_between_chunks():
    dv, win, pg, control = attached(settings=VisualSettings(type_chunk=2, chunk_delay=0))
    win.after_send = lambda w: control.request_cancel()
    with pytest.raises(RunCancelled):
        dv.type_text("000123")
    assert win.sent == ["00"]


def test_failsafe_maps_to_failsafe_triggered_and_blocks_input():
    dv, win, pg, _ = attached()
    pg.failsafe = True
    with pytest.raises(FailSafeTriggered):
        dv.type_text("abc")
    assert win.sent == []


def test_gui_calls_only_from_owner_thread():
    dv, *_ = attached()
    err = []

    def other():
        try:
            dv.click(Point(1, 1))
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert isinstance(err[0], WrongThread)


def test_set_field_reads_back_and_restores_clipboard():
    dv, win, pg, _ = attached()
    pg.field_value = "홍길동"
    r = dv.set_field(Point(300, 86, "name"), "홍길동")
    assert r.match is True and r.observed == "홍길동"
    assert win.sent == ["홍길동"]
    assert ("hotkey", ("ctrl", "a")) in pg.calls
    assert win.clipboard == "사용자 원래 클립보드"


def test_readback_mismatch_reported_not_hidden():
    dv, win, pg, _ = attached()
    pg.field_value = "12"  # 선행 0 손실 흉내
    r = dv.set_field(Point(300, 136, "phone"), "0012")
    assert r.match is False and r.observed == "12"


def test_readback_unknown_when_copy_does_not_change_clipboard():
    dv, win, pg, _ = attached(settings=VisualSettings(chunk_delay=0, clipboard_timeout=0.1))
    pg.hotkey = lambda *k: pg.calls.append(("hotkey", k))  # 복사 실패
    r = dv.set_field(Point(300, 86, "name"), "값")
    assert r.observed is None and r.match is None


def test_clipboard_with_non_text_is_not_overwritten():
    dv, win, pg, _ = attached()
    win.only_text = False
    with pytest.raises(PreconditionFailed, match="클립보드"):
        dv.read_focused_text()
    with pytest.raises(PreconditionFailed):
        dv.type_text("x", method="paste")
    assert win.clipboard == "사용자 원래 클립보드"


def test_paste_method_restores_clipboard():
    dv, win, pg, _ = attached()
    dv.type_text("붙여넣기 값", method="paste")
    assert ("hotkey", ("ctrl", "v")) in pg.calls
    assert win.clipboard == "사용자 원래 클립보드"


# --- 이미지 ---------------------------------------------------------------------
@pytest.fixture
def save_png(tmp_path):
    from PIL import Image

    p = tmp_path / "save.png"
    Image.new("RGB", (40, 20), "white").save(p)
    return p


def test_template_in_non_ascii_path_is_loaded_with_pil(tmp_path):
    """OpenCV는 한글 경로를 못 읽는다 — 경로 대신 이미지 객체를 넘겨야 한다."""
    from PIL import Image

    d = tmp_path / "삼성" / "기준이미지"
    d.mkdir(parents=True)
    p = d / "버튼.png"
    Image.new("RGB", (10, 6), "red").save(p)
    dv, win, pg, _ = attached()
    pg.boxes = [(200, 300, 10, 6)]
    dv.locate_image(p)
    passed = pg.calls[-1][1]
    assert not isinstance(passed, (str, Path)) and passed.size == (10, 6)


def test_locate_image_requires_exactly_one_match(save_png, monkeypatch):
    monkeypatch.chdir(save_png.parent)
    dv, win, pg, _ = attached()
    pg.boxes = [(200, 300, 40, 20), (202, 301, 40, 20)]  # 같은 위치 중복 → 하나
    p = dv.locate_image("save.png")
    assert (p.x, p.y) == (200 + 20 - 101, 300 + 10 - 138)
    region = pg.calls[-1][2]["region"]
    assert region == (101, 138, 520, 300)  # 대상 창 영역으로 제한

    pg.boxes = [(200, 300, 40, 20), (400, 300, 40, 20)]
    with pytest.raises(PreconditionFailed, match="2곳"):
        dv.locate_image("save.png")
    assert len(dv.find_images("save.png")) == 2  # 상태 판정용: 여러 개도 목록으로
    pg.boxes = []
    with pytest.raises(TransientError):
        dv.locate_image("save.png")
    assert dv.find_images("save.png") == []


def test_dedupe_boxes():
    assert len(_dedupe_boxes([(0, 0, 10, 10), (2, 2, 10, 10), (30, 0, 10, 10)])) == 2


# --- 캡처·정리 -------------------------------------------------------------------
def test_capture_clips_to_primary_monitor(tmp_path):
    dv, win, pg, _ = attached()
    info = dv.capture(tmp_path / "c.png", area="client")
    assert (info["origin_x"], info["origin_y"], info["width"], info["height"]) == (101, 138, 520, 300)
    assert info["dpi"] == 120
    assert (tmp_path / "c.png").exists()


def test_wait_visually_stable_waits_for_painting_to_finish():
    dv, win, pg, _ = attached()
    pg.frames = [b"blank", b"half", b"done", b"done"]
    dv.wait_visually_stable(timeout=2, interval=0.01)
    assert len([c for c in pg.calls if c[0] == "screenshot"]) == 4


def test_wait_visually_stable_times_out_on_animation():
    dv, win, pg, _ = attached()
    pg.frames = [bytes([i]) for i in range(200)]
    with pytest.raises(TransientError, match="안정"):
        dv.wait_visually_stable(timeout=0.1, interval=0.01)


def _other_in_front(win):
    win.windows[9] = W(9, title="다른 앱", frame=R(0, 0, 1920, 1140), client=R(0, 0, 1920, 1140))
    win.fg = 9
    win.covered_by = 9


def test_capture_refused_when_other_window_in_front(tmp_path):
    """영역 캡처는 그 위치에 보이는 것을 찍는다 — 다른 앱 화면을 증거로 남기지 않는다."""
    dv, win, pg, _ = attached()
    _other_in_front(win)
    with pytest.raises(FocusLost):
        dv.capture(tmp_path / "c.png", area="client")
    assert not (tmp_path / "c.png").exists()


def test_capture_refused_when_covered_by_topmost(tmp_path):
    dv, win, pg, _ = attached()
    win.covered_by = 9  # 포그라운드는 대상이지만 항상-위 창이 덮음
    with pytest.raises(UserActionRequired, match="가려져"):
        dv.capture(tmp_path / "c.png")


def test_wait_stable_stops_immediately_when_focus_lost():
    dv, win, pg, _ = attached()
    _other_in_front(win)
    with pytest.raises(FocusLost):
        dv.wait_visually_stable(timeout=5, interval=0.01)
    assert [c for c in pg.calls if c[0] == "screenshot"] == []


def test_wait_stable_region_is_client_relative():
    dv, win, pg, _ = attached()
    dv.wait_visually_stable(timeout=1, interval=0.01, region=(0, 0, 520, 200))
    assert pg.calls[-1] == ("screenshot", (101, 138, 520, 200))


def test_failure_evidence_falls_back_to_fullscreen_when_not_visible(tmp_path):
    dv, win, pg, _ = attached()
    dv.click(Point(5, 5))
    _other_in_front(win)
    paths = dv.failure_evidence(tmp_path)
    assert len(paths) == 1 and paths[0].endswith("_fullscreen.png")


def test_release_inputs_after_use():
    dv, win, pg, _ = attached()
    dv.click(Point(5, 5))
    dv.release_inputs()
    assert win.released == 1 and ("mouseUp",) in pg.calls
