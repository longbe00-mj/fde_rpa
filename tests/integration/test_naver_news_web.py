"""naver_news 업무 통합 테스트 — 가짜 네이버 메인(fixtures/web/naver_like_site.py) + headless Edge.

실제 naver.com에는 접속하지 않는다. 선택자(selectors.json)가 조사한 구조에서 동작하는지, 실패를 올바로 분류하는지 확인.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "fixtures" / "web"))

from naver_like_site import start_site  # noqa: E402


class NewsHarness:
    def __init__(self, tmp: Path, base_url: str) -> None:
        from rpa.core.control import ControlState
        from rpa.core.engine import Engine
        from rpa.core.ownership import InProcessLock
        from rpa.core.run_store import RunStore
        from rpa.workflows.naver_news import workflow as wfmod

        self.tmp, self.wfmod = tmp, wfmod
        self.workflow = wfmod.build()
        self.control = ControlState()
        self.events: list[dict] = []
        self.adapters = wfmod.make_adapters(tmp, self.control, base_url=base_url, headless=True)
        self.adapters["job"] = wfmod.NewsJobSettings(output_dir=str(tmp / "outputs" / "naver_news"), page_wait=5.0)
        self.store = RunStore(tmp / "runs.db")
        self.engine = Engine(self.store, self.adapters, tmp, control=self.control,
                             runner_lock=InProcessLock("naver-it"), event_sink=self.events.append)

    def start(self, **inputs: str) -> str:
        return self.engine.start(self.workflow, inputs)

    def run(self, run_id: str) -> dict:
        return self.store.get_run(run_id)


@pytest.fixture
def site(request):
    url, server = start_site(getattr(request, "param", "normal"))
    yield url
    server.shutdown()


@pytest.fixture
def nh(tmp_path, site):
    return NewsHarness(tmp_path, site)


def test_collects_ten_and_saves_files(nh, tmp_path):
    run_id = nh.start(request_id="IT-NEWS-1")
    run = nh.run(run_id)
    assert run["execution_status"] == "FINISHED", run["status_reason"]
    assert run["business_outcome"] == "AUTO_VERIFIED"
    ctx = nh.store.load_context(run_id)
    c = ctx.step_outputs["collect_news"]
    assert [i["rank"] for i in c["items"]] == list(range(1, 11))
    assert len(c["pages_read"]) == 3  # 4 + 4 + 2
    assert c["items"][0]["title"] == '[합성] 테스트 기사 제목 01 · "따옴표" & <특수>'
    jpath = tmp_path / "outputs" / "naver_news" / "IT-NEWS-1.json"
    mpath = jpath.with_suffix(".md")
    assert json.loads(jpath.read_text(encoding="utf-8"))["items"] == c["items"]
    md = mpath.read_text(encoding="utf-8")
    assert md.startswith("# 네이버 메인 뉴스 10개") and "10. [합성] 테스트 기사 제목 10" in md
    kinds = [e["kind"] for e in nh.store.evidence(run_id)]
    assert kinds.count("file") == 2 and "failure_screen" not in kinds


def test_count_input(nh):
    run_id = nh.start(request_id="IT-NEWS-2", count="5")
    assert nh.run(run_id)["execution_status"] == "FINISHED"
    assert len(nh.store.load_context(run_id).step_outputs["collect_news"]["items"]) == 5


@pytest.mark.parametrize("site", ["overlap"], indirect=True)
def test_duplicate_articles_across_pages_are_removed(nh):
    run_id = nh.start(request_id="IT-NEWS-3")
    assert nh.run(run_id)["execution_status"] == "FINISHED"
    items = nh.store.load_context(run_id).step_outputs["collect_news"]["items"]
    assert len(items) == 10 and len({i["url"] for i in items}) == 10


@pytest.mark.parametrize("site", ["ui_v2"], indirect=True)
def test_ui_change_fails_without_guessing(nh, tmp_path):
    run_id = nh.start(request_id="IT-NEWS-4")
    run = nh.run(run_id)
    assert run["execution_status"] == "FAILED"
    assert "UI" in (run["status_reason"] or "") or "찾지 못함" in (run["status_reason"] or "")
    assert not (tmp_path / "outputs" / "naver_news" / "IT-NEWS-4.json").exists()


@pytest.mark.parametrize("site", ["short"], indirect=True)
def test_not_enough_articles_fails_verification(nh, tmp_path):
    run_id = nh.start(request_id="IT-NEWS-5")
    run = nh.run(run_id)
    assert run["execution_status"] != "FINISHED"
    assert not (tmp_path / "outputs" / "naver_news" / "IT-NEWS-5.json").exists()
