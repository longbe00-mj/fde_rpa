"""라이브 Windows 테스트 — 실제 마우스·키보드를 사용한다.

opt-in: 환경 변수 LOCALRPA_LIVE=1 (scripts/test-live.ps1)
대상: fixtures/windows의 테스트 앱만. 실행 중에는 마우스·키보드를 만지지 말 것.
비상 중단: 마우스를 화면 모서리로 (PyAutoGUI FAILSAFE)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TITLES = ("LocalRPA Test Form", "LocalRPA Distractor")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LOCALRPA_LIVE") == "1" and sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="라이브 테스트는 LOCALRPA_LIVE=1 일 때만 실행 (실제 마우스·키보드 사용)")
    for item in items:
        item.add_marker(skip)


def _close_test_windows() -> None:
    from rpa.platform.windows import win32

    for w in win32.top_level_windows():
        if w.title in TITLES:
            win32.close_window(w.hwnd)
    time.sleep(0.5)


@pytest.fixture(autouse=True)
def session_ready():
    from rpa.platform.windows.preflight import run_preflight

    pf = run_preflight(probe_capture=True)
    if not pf.ok:
        pytest.skip("허용된 사용자 세션에 접근 불가: " + "; ".join(c.message for c in pf.checks if c.level == "FAIL"))
    _close_test_windows()
    yield
    _close_test_windows()
    import pyautogui

    from rpa.platform.windows import win32

    win32.release_modifiers()
    x, y = pyautogui.position()
    if (x, y) in pyautogui.FAILSAFE_POINTS:
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2)
        pyautogui.FAILSAFE = True


class WinProxy:
    """win32 모듈을 감싸 send_unicode 뒤에 훅을 끼운다 (사용자 개입 흉내)."""

    def __init__(self, hook=None) -> None:
        from rpa.platform.windows import win32

        self._w = win32
        self.hook = hook
        self.sent: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._w, name)

    def send_unicode(self, text: str) -> None:
        self._w.send_unicode(text)
        self.sent.append(text)
        if self.hook:
            hook, self.hook = self.hook, None  # 한 번만
            hook()


class LiveHarness:
    def __init__(self, tmp: Path, type_chunk: int = 16, hook=None) -> None:
        from rpa.adapters.desktop_visual import DesktopVisualAdapter, VisualSettings
        from rpa.core.control import ControlState
        from rpa.core.engine import Engine
        from rpa.core.ownership import InProcessLock
        from rpa.core.run_store import RunStore
        from rpa.workflows.demo_test_form import workflow as wfmod

        self.tmp = tmp
        self.wfmod = wfmod
        self.workflow = wfmod.build()
        self.control = ControlState()
        self.win = WinProxy(hook)
        self.events: list[dict] = []
        adapters = wfmod.make_adapters(tmp, self.control, dv=DesktopVisualAdapter(
            self.control, VisualSettings(type_chunk=type_chunk), win=self.win))
        self.adapters = adapters
        self.store = RunStore(tmp / "runs.db")
        self.engine = Engine(self.store, adapters, tmp, control=self.control,
                             runner_lock=InProcessLock("live"), event_sink=self.events.append)

    @property
    def dv(self):
        return self.adapters["desktop_visual"]

    def record(self, customer_id: str):
        return self.adapters["form"].lookup(customer_id)

    def start(self, **inputs: str) -> str:
        return self.engine.start(self.workflow, inputs)

    def run(self, run_id: str) -> dict:
        return self.store.get_run(run_id)


@pytest.fixture
def live(tmp_path):
    return lambda **kw: LiveHarness(tmp_path, **kw)


@pytest.fixture
def distractor():
    from rpa.platform.windows import win32

    proc = subprocess.Popen([sys.executable, str(ROOT / "fixtures" / "windows" / "distractor_app.py")])
    hwnd = 0
    for _ in range(50):
        ws = [w for w in win32.top_level_windows() if w.title == "LocalRPA Distractor"]
        if ws:
            hwnd = ws[0].hwnd
            break
        time.sleep(0.1)
    assert hwnd, "방해 창을 띄우지 못함"
    yield hwnd
    proc.terminate()
