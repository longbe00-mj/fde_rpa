"""엔진 정상/실패/취소/재개/결과 미확정 시나리오 (명세 §15.4, §16)."""

from __future__ import annotations

import threading

import pytest

from rpa.adapters.fake import SimulatedCrash
from rpa.core.contracts import BusinessOutcome, DispatchState, ExecutionStatus
from rpa.core.errors import InvalidInput, InvalidTransition, RunnerBusy, VersionMismatch
from rpa.core.ownership import InProcessLock

S = ExecutionStatus
B = BusinessOutcome
SAVE_OP = "demo_web_to_desktop:save_customer:{}"


def test_normal_run_auto_verified_and_preserves_input(h):
    run_id = h.run("DEMO-002")
    run = h.run_row(run_id)
    assert run["execution_status"] == S.FINISHED.value
    assert run["business_outcome"] == B.AUTO_VERIFIED.value
    rec = h.backend.get("DEMO-002")
    # 한글·특수문자·선행 0 보존
    assert rec["name"] == "김철수 & Co. <특수>"
    assert rec["phone"] == "0001"
    ctx = h.store.load_context(run_id)
    assert ctx.business_key == "DEMO-002"
    assert ctx.step_outputs["save_customer"]["record_id"] == rec["record_id"]
    assert all(c == {"status": "PASS", "mode": "RULE"} for c in ctx.step_checks.values())
    assert any(e["kind"] == "file" for e in h.store.evidence(run_id))


def test_invalid_input_rejected_before_run(h):
    with pytest.raises(InvalidInput):
        h.engine.start(h.workflow, {"customer_id": "demo 1"})
    with pytest.raises(InvalidInput):
        h.engine.start(h.workflow, {"customer_id": "DEMO-001", "extra": "x"})
    assert h.store.list_runs() == []


def test_transient_read_error_retried_within_policy(h):
    h.new_engine(faults={"web.get_customer": ["transient", "transient"]})
    run_id = h.run()
    assert h.run_row(run_id)["execution_status"] == S.FINISHED.value
    assert h.adapters["web"].calls == 3
    assert h.event_types().count("step_retry") == 2


def test_transient_read_error_exhausted_fails(h):
    h.new_engine(faults={"web.get_customer": ["transient"] * 3})
    run_id = h.run()
    run = h.run_row(run_id)
    assert run["execution_status"] == S.FAILED.value
    assert run["business_outcome"] == B.UNVERIFIED.value  # 변경 동작 없음
    assert h.backend.count() == 0


def test_login_expired_goes_waiting_user_without_write(h):
    h.new_engine(faults={"web.get_customer": ["login_expired"]})
    run_id = h.run()
    assert h.run_row(run_id)["execution_status"] == S.WAITING_USER.value
    assert h.backend.save_calls == 0
    with pytest.raises(InvalidTransition):  # 해결 전 재개 불가
        h.engine.resume(h.workflow, run_id)
    h.engine.resolve(h.workflow, run_id, "done", "전용 브라우저에서 다시 로그인함")
    assert h.engine.resume(h.workflow, run_id) == S.FINISHED
    assert h.run_row(run_id)["business_outcome"] == B.AUTO_VERIFIED.value


def test_error_after_apply_no_blind_retry_reconcile_restores(h):
    """변경 호출 직후 예외(실제로는 저장됨) → 재전송 없이 재조회로 결과 복원, 중복 없음."""
    h.new_engine(faults={"desktop.save": ["error_after_apply"]})
    run_id = h.run()
    run = h.run_row(run_id)
    assert run["execution_status"] == S.FINISHED.value
    assert run["business_outcome"] == B.AUTO_VERIFIED.value
    assert h.backend.save_calls == 1
    assert "reconciled" in h.event_types()


def test_error_before_apply_reconcile_not_applied_then_redispatch(h):
    h.new_engine(faults={"desktop.save": ["error_before_apply"]})
    run_id = h.run()
    assert h.run_row(run_id)["execution_status"] == S.FINISHED.value
    assert h.backend.save_calls == 1
    op = h.store.get_operation(SAVE_OP.format("DEMO-001"))
    assert op["dispatch_count"] == 2
    assert op["dispatch_state"] == DispatchState.CONFIRMED_APPLIED.value


def test_change_retry_limit_goes_waiting_user(h):
    h.new_engine(faults={"desktop.save": ["error_before_apply", "error_before_apply"]})
    run_id = h.run()
    assert h.run_row(run_id)["execution_status"] == S.WAITING_USER.value
    assert h.backend.save_calls == 0


def test_verifier_fail_is_mismatch_not_assumed_unapplied(h):
    h.new_engine(faults={"desktop.save": ["corrupt"]})
    run_id = h.run()
    run = h.run_row(run_id)
    assert run["execution_status"] == S.FAILED.value
    assert run["business_outcome"] == B.MISMATCH.value
    op = h.store.get_operation(SAVE_OP.format("DEMO-001"))
    assert op["dispatch_state"] == DispatchState.DISPATCHED.value  # NOT_APPLIED로 바꾸지 않음
    assert h.backend.save_calls == 1


def test_crash_after_apply_then_resume_restores_without_duplicate(h):
    """저장 직후 프로세스 종료(5↔9 사이) → 재시작 후 대조로 복원."""
    h.new_engine(faults={"desktop.save": ["crash_after_apply"]})
    with pytest.raises(SimulatedCrash):
        h.run()
    run = h.store.unfinished_run()
    run_id = run["run_id"]
    assert run["execution_status"] == S.RUNNING.value
    assert h.store.get_operation(SAVE_OP.format("DEMO-001"))["dispatch_state"] == DispatchState.UNKNOWN.value
    with pytest.raises(RunnerBusy):  # 이전 결과 대조 전 새 실행 금지
        h.run("DEMO-002")

    h.new_engine()  # 프로세스 재시작
    assert h.engine.resume(h.workflow, run_id) == S.FINISHED
    assert h.run_row(run_id)["business_outcome"] == B.AUTO_VERIFIED.value
    assert h.backend.save_calls == 1


def test_crash_before_apply_then_resume_redispatches_once(h):
    """의도만 저장되고 전송 전 종료(5↔6 사이) → 단정하지 않고 대조, 미반영 확인 후 1회 전송."""
    h.new_engine(faults={"desktop.save": ["crash_before_apply"]})
    with pytest.raises(SimulatedCrash):
        h.run()
    run_id = h.store.unfinished_run()["run_id"]
    h.new_engine()
    assert h.engine.resume(h.workflow, run_id) == S.FINISHED
    assert h.backend.save_calls == 1
    assert "reconciled" in h.event_types()


def test_cancel_before_change_step(h):
    """취소하면 이후 입력이 나가지 않는다."""
    original = h.workflow.steps[0].handler

    def fetch_then_cancel(ctx, env):
        result = original(ctx, env)
        h.control.request_cancel()
        return result

    import dataclasses

    wf = dataclasses.replace(
        h.workflow, steps=(dataclasses.replace(h.workflow.steps[0], handler=fetch_then_cancel), *h.workflow.steps[1:])
    )
    run_id = h.engine.start(wf, {"customer_id": "DEMO-001"})
    run = h.run_row(run_id)
    assert run["execution_status"] == S.CANCELLED.value
    assert run["business_outcome"] == B.UNVERIFIED.value
    assert h.backend.save_calls == 0


def test_cancel_during_dispatch_is_unknown_not_nothing(h):
    """변경 전송 중 취소 → CANCELLED + UNKNOWN (아무 것도 안 했다고 표시하지 않음)."""
    h.new_engine(faults={"desktop.save": ["slow"]})
    threading.Timer(0.3, h.control.request_cancel).start()
    run_id = h.run()
    run = h.run_row(run_id)
    assert run["execution_status"] == S.CANCELLED.value
    assert run["business_outcome"] == B.UNKNOWN.value


def test_pause_then_resume_at_step_boundary(h):
    h.control.request_pause()
    statuses = []

    def resume_later():
        # 엔진이 PAUSED에 도달할 때까지 기다린 뒤 재개
        import time

        for _ in range(100):
            # 다른 스레드에서는 store를 쓰지 않고 이벤트 목록만 본다
            if any(e["type"] == "paused" for e in list(h.events)):
                statuses.append("paused_seen")
                h.control.request_resume()
                return
            time.sleep(0.02)

    t = threading.Thread(target=resume_later)
    t.start()
    run_id = h.run()
    t.join()
    assert statuses == ["paused_seen"]
    assert h.run_row(run_id)["execution_status"] == S.FINISHED.value
    types = h.event_types()
    assert types.index("paused") < types.index("resumed") < types.index("step_started")
    status_seq = [e["execution_status"] for e in h.events if e["type"] == "status"]
    assert status_seq[:4] == ["RUNNING", "PAUSE_REQUESTED", "PAUSED", "RUNNING"]


def test_cancel_while_paused(h):
    h.control.request_pause()
    threading.Timer(0.3, h.control.request_cancel).start()
    run_id = h.run()
    assert h.run_row(run_id)["execution_status"] == S.CANCELLED.value
    assert h.backend.save_calls == 0


def test_human_confirmation_distinct_from_auto(h):
    """판정 불가 → 사용자가 반영 확인 → HUMAN_CONFIRMED (RULE 성공으로 바꾸지 않음)."""
    h.new_engine(faults={"desktop.save": ["error_after_apply"]})
    # 대조 함수를 판정 불가로 만든다
    import dataclasses

    from rpa.core.contracts import ReconcileResult, ReconcileStatus

    save = h.workflow.step("save_customer")
    wf = dataclasses.replace(
        h.workflow,
        steps=(
            h.workflow.steps[0],
            dataclasses.replace(save, reconcile=lambda ctx, env: ReconcileResult(ReconcileStatus.UNKNOWN, reason="조회 화면 오류")),
            h.workflow.steps[2],
        ),
    )
    run_id = h.engine.start(wf, {"customer_id": "DEMO-001"})
    run = h.run_row(run_id)
    assert run["execution_status"] == S.WAITING_USER.value
    assert run["business_outcome"] == B.UNKNOWN.value
    with pytest.raises(ValueError):
        h.engine.resolve(wf, run_id, "applied", "  ")  # 근거 없는 확인 거부
    rec = h.backend.get("DEMO-001")
    h.engine.resolve(wf, run_id, "applied", "앱 조회 화면에서 레코드 확인", {"record_id": rec["record_id"]})
    assert h.engine.resume(wf, run_id) == S.FINISHED
    run = h.run_row(run_id)
    assert run["business_outcome"] == B.HUMAN_CONFIRMED.value
    assert h.store.load_context(run_id).step_checks["save_customer"]["mode"] == "HUMAN"
    assert h.backend.save_calls == 1
    itv = h.store.interventions(run_id)[0]
    assert itv["resolution"] == "applied" and itv["evidence_note"]


def test_human_not_applied_redispatches_once(h):
    """대조 불가 → 사용자가 미반영 확인 → 재개 시 한 번만 다시 전송."""
    import dataclasses

    from rpa.core.contracts import ReconcileResult, ReconcileStatus

    h.new_engine(faults={"desktop.save": ["error_before_apply"]})
    save = h.workflow.step("save_customer")
    wf = dataclasses.replace(
        h.workflow,
        steps=(
            h.workflow.steps[0],
            dataclasses.replace(save, reconcile=lambda ctx, env: ReconcileResult(ReconcileStatus.UNKNOWN)),
            h.workflow.steps[2],
        ),
    )
    run_id = h.engine.start(wf, {"customer_id": "DEMO-001"})
    assert h.run_row(run_id)["execution_status"] == S.WAITING_USER.value
    h.engine.resolve(wf, run_id, "not_applied", "앱에서 고객 조회 결과 없음")
    assert h.engine.resume(wf, run_id) == S.FINISHED
    assert h.backend.save_calls == 1
    assert h.run_row(run_id)["business_outcome"] == B.AUTO_VERIFIED.value


def test_human_not_applied_respects_attempt_limit(h):
    h.new_engine(faults={"desktop.save": ["error_before_apply", "error_before_apply"]})
    run_id = h.run()
    assert h.run_row(run_id)["execution_status"] == S.WAITING_USER.value
    h.engine.resolve(h.workflow, run_id, "not_applied", "앱에 레코드 없음 확인")
    # 시도 한도(2회)에 이미 도달 → 다시 사용자 확인 요청 (무한 재시도 없음)
    assert h.engine.resume(h.workflow, run_id) == S.WAITING_USER
    assert h.backend.save_calls == 0


def test_same_business_key_second_run_does_not_duplicate(h):
    first = h.run("DEMO-001")
    second = h.run("DEMO-001")
    assert h.run_row(second)["execution_status"] == S.FINISHED.value
    assert h.backend.save_calls == 1  # 동일 논리 변경은 이미 반영 확인됨 → 재전송 없음
    assert "operation_restored" in h.event_types()
    assert first != second


def test_version_change_detected_on_resume(h):
    h.new_engine(faults={"web.get_customer": ["login_expired"]})
    run_id = h.run()
    h.engine.resolve(h.workflow, run_id, "done", "로그인함")
    import dataclasses

    changed = dataclasses.replace(h.workflow, settings={"new": True})
    with pytest.raises(VersionMismatch):
        h.engine.resume(changed, run_id)
    assert h.engine.resume(changed, run_id, accept_new_version=True) == S.FINISHED
    assert "version_changed" in h.event_types()


def test_cancel_unfinished_run_marks_unknown_if_dispatched(h):
    h.new_engine(faults={"desktop.save": ["crash_after_apply"]})
    with pytest.raises(SimulatedCrash):
        h.run()
    run_id = h.store.unfinished_run()["run_id"]
    h.new_engine()
    h.engine.cancel_unfinished(run_id)
    run = h.run_row(run_id)
    assert run["execution_status"] == S.CANCELLED.value
    assert run["business_outcome"] == B.UNKNOWN.value


def test_single_active_runner(h):
    lock = InProcessLock("test-runner")
    lock.acquire()
    try:
        with pytest.raises(RunnerBusy):
            h.run()
    finally:
        lock.release()
    assert h.store.list_runs() == []
