"""웹 포털 업무 통합 테스트 (명세 §8, §16 웹 사례)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

MEMO = "방문 요청 · 0012 확인 <긴급> & \"따옴표\""


def test_normal_flow_with_attachment(h, tmp_path):
    h.user_logs_in()
    att = tmp_path / "첨부 파일.txt"
    att.write_text("첨부 내용", encoding="utf-8")
    run_id = h.start(customer_id="DEMO-002", memo=MEMO, attachment=str(att))
    run = h.run(run_id)
    assert run["execution_status"] == "FINISHED", run["status_reason"]
    assert run["business_outcome"] == "AUTO_VERIFIED"
    recs = h.portal.records()
    assert len(recs) == 1
    r = recs[0]
    assert (r["customer_id"], r["name"], r["phone"], r["memo"]) == ("DEMO-002", "김철수 & Co. <특수>", "0001", MEMO)
    assert r["attachment"] == "첨부 파일.txt" and r["attachment_size"] == len("첨부 내용".encode("utf-8"))
    ctx = h.store.load_context(run_id)
    assert ctx.step_outputs["read_detail"]["memo"] == "특수문자 <b>태그</b> 아님"  # iframe
    dl = Path(ctx.step_outputs["download_export"]["path"])
    assert dl.name == "customer_DEMO-002.csv" and Path(ctx.run_dir) in dl.parents
    kinds = [e["kind"] for e in h.store.evidence(run_id)]
    assert "download" in kinds and not any(k == "failure_screen" for k in kinds)


def test_not_logged_in_waits_then_user_logs_in(h):
    run_id = h.start(customer_id="DEMO-001")
    run = h.run(run_id)
    assert run["execution_status"] == "WAITING_USER" and "로그인" in run["status_reason"]
    assert "step_notice" in h.types()  # 'RPA 브라우저에서 로그인하세요' 안내
    assert h.portal.records() == []
    ev = [Path(e["rel_path"]).name for e in h.store.evidence(run_id) if e["kind"] == "failure_screen"]
    assert any(n.startswith("browser_failure_") for n in ev) and any(n.endswith(".zip") for n in ev)

    h.user_logs_in()
    h.engine.resolve(h.workflow, run_id, "done", "RPA 브라우저에서 로그인함")
    assert h.engine.resume(h.workflow, run_id).value == "FINISHED"
    assert len(h.portal.records()) == 1


def test_session_expires_before_register_no_write(h):
    h.user_logs_in()
    step = h.workflow.step("download_export")
    orig = step.handler

    def download_then_expire(ctx, env):
        result = orig(ctx, env)
        h.portal.admin("expire")  # 다음 단계 전에 세션 만료
        return result

    wf = dataclasses.replace(h.workflow, steps=tuple(
        dataclasses.replace(s, handler=download_then_expire) if s.id == "download_export" else s
        for s in h.workflow.steps))
    run_id = h.engine.start(wf, {"customer_id": "DEMO-001"})
    run = h.run(run_id)
    assert run["execution_status"] == "WAITING_USER" and "로그인" in run["status_reason"]
    assert h.portal.records() == []  # 쓰기 동작 미수행
    h.user_logs_in()
    h.engine.resolve(wf, run_id, "done", "다시 로그인")
    assert h.engine.resume(wf, run_id).value == "FINISHED"
    assert len(h.portal.records()) == 1


def test_server_error_on_search_is_retried_then_fails(h):
    h.user_logs_in()
    h.portal.admin("mode?search_error=1")
    run_id = h.start(customer_id="DEMO-001")
    run = h.run(run_id)
    assert run["execution_status"] == "FAILED" and "재시도 소진" in run["status_reason"]
    assert h.types().count("step_retry") == 3
    assert run["business_outcome"] == "UNVERIFIED" and h.portal.records() == []


def test_lost_response_after_register_is_reconciled_without_duplicate(h):
    h.user_logs_in()
    h.portal.admin("mode?register_lost_response=1")  # 서버는 저장했지만 502 응답
    run_id = h.start(customer_id="DEMO-001", memo="응답 유실 테스트")
    run = h.run(run_id)
    assert run["execution_status"] == "FINISHED", run["status_reason"]
    assert run["business_outcome"] == "AUTO_VERIFIED"
    assert len(h.portal.records()) == 1  # 재전송 없음
    assert "dispatch_error" in h.types() and "reconciled" in h.types()


def test_ui_change_is_reported_not_clicked_by_coordinates(h):
    h.user_logs_in()
    h.portal.admin("mode?ui_v2=1")  # 검색 버튼 이름이 '조회'로 바뀜
    run_id = h.start(customer_id="DEMO-001")
    run = h.run(run_id)
    assert run["execution_status"] == "FAILED"
    assert "UI" in run["status_reason"] and "좌표" in run["status_reason"]


def test_ambiguous_submit_button_is_never_clicked(h):
    h.user_logs_in()
    h.portal.admin("mode?duplicate_button=1")
    run_id = h.start(customer_id="DEMO-001")
    run = h.run(run_id)
    assert run["execution_status"] == "WAITING_USER"
    assert h.portal.records() == []
    errors = [a["error"] or "" for a in h.store.attempts(run_id) if a["step_id"] == "register_record"]
    assert any("2개" in e for e in errors)


def test_same_customer_second_run_does_not_register_twice(h):
    h.user_logs_in()
    h.start(customer_id="DEMO-010")
    second = h.start(customer_id="DEMO-010")
    assert h.run(second)["execution_status"] == "FINISHED"
    assert len(h.portal.records()) == 1
    assert "operation_restored" in h.types()


def test_navigation_outside_allowed_origin_is_blocked(h):
    from rpa.core.errors import PreconditionFailed

    b = h.adapters["browser"]
    try:
        with pytest.raises(PreconditionFailed, match="허용되지 않은"):
            b.goto("https://example.com/")
        b.page.evaluate("location.href = 'https://example.com/'")  # 페이지 스크립트로 이동 시도
        b.page.wait_for_timeout(500)
        assert b.blocked_navigations and "example.com" in b.blocked_navigations[0]
        assert "example.com" not in b.page.url
    finally:
        b.end_session()
