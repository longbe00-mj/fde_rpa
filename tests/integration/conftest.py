"""웹 어댑터 통합 테스트 — 로컬 테스트 포털 + headless Edge (데스크톱 입력 없음)."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "fixtures" / "web"))

from portal_site import start_portal  # noqa: E402


def pytest_collection_modifyitems(config, items):
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(channel="msedge", headless=True).close()
        return
    except Exception as e:  # noqa: BLE001
        reason = f"Edge(Playwright) 실행 불가: {str(e).splitlines()[0][:120]}"
    for item in items:
        item.add_marker(pytest.mark.skip(reason=reason))


class Portal:
    def __init__(self, tmp: Path) -> None:
        self.store = tmp / "portal_store.json"
        self.url, self.server, self.state = start_portal(self.store)

    def admin(self, path: str) -> None:
        urllib.request.urlopen(self.url + "/__admin/" + path).read()

    def records(self) -> list[dict[str, Any]]:
        return list(self.state.records()["records"].values())


@pytest.fixture
def portal(tmp_path):
    p = Portal(tmp_path)
    yield p
    p.server.shutdown()


class Harness:
    def __init__(self, tmp: Path, portal: Portal, login_wait: float = 1.0) -> None:
        from rpa.core.control import ControlState
        from rpa.core.engine import Engine
        from rpa.core.ownership import InProcessLock
        from rpa.core.run_store import RunStore
        from rpa.workflows.demo_web_portal import workflow as wfmod

        self.tmp, self.portal, self.wfmod = tmp, portal, wfmod
        self.workflow = wfmod.build()
        self.control = ControlState()
        self.events: list[dict] = []
        self.adapters = wfmod.make_adapters(tmp, self.control, base_url=portal.url, headless=True,
                                            login_wait=login_wait)
        self.store = RunStore(tmp / "runs.db")
        self.engine = Engine(self.store, self.adapters, tmp, control=self.control,
                             runner_lock=InProcessLock("web-it"), event_sink=self.events.append)

    @property
    def profile(self) -> str:
        return self.adapters["browser"].settings.user_data_dir

    def user_logs_in(self) -> None:
        """사람이 RPA 전용 브라우저에서 로그인하는 것을 흉내 (같은 프로필에 로그인 쿠키 저장)."""
        from rpa.adapters.browser import BrowserAdapter, BrowserSettings, LoginRequired
        from rpa.core.control import ControlState

        b = BrowserAdapter(ControlState(), BrowserSettings(user_data_dir=self.profile, base_url=self.portal.url,
                                                           headless=True, trace=False))
        try:
            try:
                b.goto("/customers")
            except LoginRequired:
                b.fill({"label": "아이디"}, "rpa")
                b.fill({"label": "비밀번호"}, "test", readback=False)
                b.click({"role": "button", "name": "로그인"}, expect_navigation=True)
        finally:
            b.end_session()

    def start(self, **inputs: str) -> str:
        return self.engine.start(self.workflow, inputs)

    def run(self, run_id: str) -> dict:
        return self.store.get_run(run_id)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


@pytest.fixture
def h(tmp_path, portal):
    return Harness(tmp_path, portal)
