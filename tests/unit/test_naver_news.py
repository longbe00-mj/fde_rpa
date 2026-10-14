"""naver_news 업무의 규칙 함수 (브라우저 없음)."""

from __future__ import annotations

import pytest

from rpa.core.errors import InvalidInput
from rpa.core.inputs import validate_inputs
from rpa.workflows.naver_news import workflow as wf


def _item(i: int, **kw) -> dict:
    return {"rank": i, "title": f"제목 {i}", "press": "가상일보", "time": "10월 14일 09:00",
            "url": f"https://n.news.naver.com/article/001/{i:010d}", **kw}


def test_registered_and_inputs():
    d = wf.build()
    assert d.id == "naver_news" and [s.id for s in d.steps] == ["open_naver", "collect_news", "save_news"]
    assert d.steps[2].effect_kind.is_change and not d.steps[1].effect_kind.is_change
    assert validate_inputs(d, {"request_id": "N-1"}) == {"request_id": "N-1"}
    assert validate_inputs(d, {"request_id": "N-1", "count": "40"})["count"] == "40"
    for bad in ("0", "41", "abc", "-1"):
        with pytest.raises(InvalidInput):
            validate_inputs(d, {"request_id": "N-1", "count": bad})
    with pytest.raises(InvalidInput):
        validate_inputs(d, {"request_id": "../x"})


def test_page_indicator_matches_observed_naver_text():
    assert wf.parse_page_indicator("이전 페이지 언론사 더보기1 페이지 전체 /21 다음 페이지") == (1, 21)
    assert wf.parse_page_indicator("언론사 더보기12 페이지 전체 / 21") == (12, 21)
    assert wf.parse_page_indicator("페이징 없음") is None


def test_merge_items_dedupes_by_url_and_keeps_order():
    got: list[dict] = []
    assert wf.merge_items(got, [_item(1), _item(2)], 1) == 2
    assert wf.merge_items(got, [_item(2), _item(3), {"title": "x", "url": ""}], 2) == 1
    assert [g["url"][-2:] for g in got] == ["01", "02", "03"] and got[2]["page"] == 2


def test_check_items_rules():
    ok = [_item(i) for i in range(1, 11)]
    assert wf.check_items(ok, 10) == []
    assert any("요청 10개" in p for p in wf.check_items(ok[:9], 10))
    assert "중복 URL" in wf.check_items(ok[:9] + [_item(10, url=ok[0]["url"])], 10)
    assert any("제목 비어" in p for p in wf.check_items(ok[:9] + [_item(10, title=" ")], 10))
    assert any("언론사 비어" in p for p in wf.check_items(ok[:9] + [_item(10, press="")], 10))
    assert any("주소 형식" in p for p in wf.check_items(ok[:9] + [_item(10, url="https://evil.example/a")], 10))


def test_render_markdown_keeps_special_characters():
    md = wf.render_markdown({"source": wf.SOURCE, "collected_at": "2026-10-14T10:00:00+0900",
                             "items": [_item(1, title='"따옴표" & <특수>')]})
    assert md.startswith("# 네이버 메인 뉴스 1개")
    assert '1. "따옴표" & <특수> — 가상일보 · 10월 14일 09:00' in md
    assert "https://n.news.naver.com/article/001/0000000001" in md
