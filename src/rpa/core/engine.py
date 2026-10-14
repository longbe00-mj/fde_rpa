"""단계 실행 엔진 (명세 §6, §7).

단계 실행 알고리즘:
 1. 명령·취소 상태 확인 (일시정지면 안전 경계에서 PAUSED)
 2. 사전조건 검사 (대상 앱·창·업무 식별자를 새로 확인하는 것은 precondition/handler의 책임)
 3. 변경 단계면 logical_operation_id·입력 해시·의도를 영구 저장하고 커밋 확인
 4. 취소 상태를 다시 확인하고 실제 동작 전송
 5. 업무별 verifier로 결과 검사
 6. 결과·증거·checkpoint 영구 저장
 7. PASS면 다음 단계, FAIL/UNKNOWN이면 대조·사용자 확인·실패 처리

변경 동작은 timeout·예외만으로 재실행하지 않는다. reconcile이 NOT_APPLIED를 확인했을 때만 다시 수행한다.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from rpa.core.contracts import (
    ActionResult,
    BusinessOutcome,
    CheckResult,
    CheckStatus,
    DispatchState,
    ExecutionStatus,
    ReconcileResult,
    ReconcileStatus,
    StepDefinition,
    StepEnv,
    VerificationMode,
    WorkflowDefinition,
    now_iso,
)
from rpa.core.control import ControlState
from rpa.core.errors import (
    ControlChannelLost,
    FailSafeTriggered,
    InvalidTransition,
    RpaError,
    RunCancelled,
    TransientError,
    UserActionRequired,
    VersionMismatch,
)
from rpa.core.inputs import validate_inputs
from rpa.core.ownership import RunnerLock, default_runner_lock
from rpa.core.run_context import RunContext, hash_files, hash_value
from rpa.core.run_store import RunStore

S = ExecutionStatus
EventSink = Callable[[dict[str, Any]], None]

# 이 예외들은 업무 오류가 아니라 실행 중단 신호다. 재시도·대조 없이 위로 전달한다
_STOP_SIGNALS = (RunCancelled, FailSafeTriggered, ControlChannelLost)

_UNRESOLVED_OP_STATES = (DispatchState.UNKNOWN.value, DispatchState.DISPATCHED.value)


class StepFailed(RpaError):
    """verifier FAIL 또는 재시도 소진. 외부 미반영을 뜻하지 않는다."""


def workflow_hashes(workflow: WorkflowDefinition) -> tuple[str, str]:
    code_hash = hash_files(workflow.source_files) if workflow.source_files else hash_value(workflow.version)
    return code_hash, hash_value(workflow.settings)


class Engine:
    def __init__(
        self,
        store: RunStore,
        adapters: dict[str, Any],
        data_dir: str | Path,
        control: Optional[ControlState] = None,
        runner_lock: Optional[RunnerLock] = None,
        event_sink: Optional[EventSink] = None,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.data_dir = Path(data_dir)
        self.control = control or ControlState()
        self.runner_lock = runner_lock or default_runner_lock()
        self.event_sink = event_sink

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    def start(self, workflow: WorkflowDefinition, raw_inputs: dict[str, Any]) -> str:
        """입력 검증 → 활성 Runner/미완료 실행 확인 → 버전 고정 → 실행. run_id 반환."""
        inputs = validate_inputs(workflow, raw_inputs)
        code_hash, config_hash = workflow_hashes(workflow)
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        run_dir = self.data_dir / "runs" / run_id
        ctx = RunContext(
            run_id=run_id,
            workflow_id=workflow.id,
            workflow_version=workflow.version,
            workflow_code_hash=code_hash,
            config_hash=config_hash,
            input_hash=hash_value(inputs),
            business_key=str(inputs[workflow.business_key_field]) if workflow.business_key_field else None,
            validated_inputs=inputs,
            run_dir=str(run_dir),
        )
        self.runner_lock.acquire()
        try:
            self.store.create_run(ctx)
            run_dir.mkdir(parents=True, exist_ok=True)
            self._emit(run_id, "run_created", workflow_id=workflow.id, business_key=ctx.business_key)
            self._run(workflow, ctx)
        finally:
            self.runner_lock.release()
        return run_id

    def resume(self, workflow: WorkflowDefinition, run_id: str, accept_new_version: bool = False) -> ExecutionStatus:
        """미완료 실행 재개. 화면·로그인이 복구됐다고 가정하지 않고 사전조건·대조를 다시 거친다."""
        run = self._require_run(run_id)
        status = S(run["execution_status"])
        if status.is_terminal:
            raise InvalidTransition(f"종료된 실행({status.value})은 재개할 수 없습니다")
        if self.store.open_intervention(run_id):
            raise InvalidTransition("사용자 조치가 해결되지 않았습니다. 먼저 resolve하세요.")
        ctx = self.store.load_context(run_id)
        if ctx.workflow_id != workflow.id:
            raise VersionMismatch(f"실행의 workflow는 {ctx.workflow_id}입니다")
        code_hash, config_hash = workflow_hashes(workflow)
        if (code_hash, config_hash) != (ctx.workflow_code_hash, ctx.config_hash):
            if not accept_new_version:
                raise VersionMismatch(
                    "실행 시작 이후 workflow 코드 또는 설정이 바뀌었습니다. "
                    "기존 코드로 재개하거나, 명시적으로 새 버전 수용(accept_new_version)을 선택하세요."
                )
            self._emit(
                run_id, "version_changed",
                old={"code": ctx.workflow_code_hash, "config": ctx.config_hash},
                new={"code": code_hash, "config": config_hash},
            )
            ctx.workflow_code_hash, ctx.config_hash, ctx.workflow_version = code_hash, config_hash, workflow.version
            self.store.save_context(ctx)
        self.runner_lock.acquire()
        try:
            if status == S.CANCEL_REQUESTED:
                self._finish_cancelled(ctx, "재개 전 취소 요청 상태")
            else:
                self._emit(run_id, "run_resumed", from_status=status.value)
                self._run(workflow, ctx)
        finally:
            self.runner_lock.release()
        return S(self._require_run(run_id)["execution_status"])

    def resolve(
        self,
        workflow: WorkflowDefinition,
        run_id: str,
        resolution: str,
        note: str,
        outputs: Optional[dict[str, Any]] = None,
    ) -> None:
        """WAITING_USER 해결 기록.

        resolution:
          done        — 사용자가 조치를 마침 (로그인 등). 재개 시 사전조건·대조를 다시 수행
          applied     — 사용자가 해당 변경이 반영된 것을 확인 (HUMAN 검증, RULE 성공으로 바꾸지 않음)
          not_applied — 사용자가 해당 변경이 반영되지 않은 것을 확인 (재개 시 다시 수행)
        """
        if resolution not in ("done", "applied", "not_applied"):
            raise ValueError(f"알 수 없는 resolution: {resolution}")
        if not note.strip():
            raise ValueError("확인 근거(note)를 입력해야 합니다")
        run = self._require_run(run_id)
        if S(run["execution_status"]) != S.WAITING_USER:
            raise InvalidTransition(f"WAITING_USER 상태가 아닙니다 ({run['execution_status']})")
        itv = self.store.open_intervention(run_id)
        if not itv:
            raise InvalidTransition("열려 있는 사용자 조치가 없습니다")
        ctx = self.store.load_context(run_id)
        if resolution in ("applied", "not_applied"):
            step = workflow.step(itv["step_id"])
            if not step.effect_kind.is_change:
                raise ValueError(f"{step.id}는 변경 단계가 아닙니다. resolution=done을 사용하세요.")
            op_id = self._operation_id(workflow, ctx, step)
            op = self.store.get_operation(op_id)
            if not op:
                raise ValueError(f"{step.id}의 변경 기록이 없습니다. resolution=done을 사용하세요.")
            if resolution == "applied":
                prev = json.loads(op["result_json"] or "{}")
                check = CheckResult(
                    status=CheckStatus.PASS,
                    verification_mode=VerificationMode.HUMAN,
                    business_key=ctx.business_key,
                    observed=outputs or {},
                    reason=note,
                    evidence=[{"type": "human_note", "source": "user", "ref": f"intervention:{itv['id']}"}],
                )
                self.store.set_operation_state(
                    op_id,
                    DispatchState.CONFIRMED_APPLIED,
                    {"outputs": outputs if outputs is not None else prev.get("outputs", {}), "check": check.to_dict()},
                )
            else:
                self.store.set_operation_state(op_id, DispatchState.CONFIRMED_NOT_APPLIED)
        self.store.resolve_intervention(itv["id"], resolution, note)
        self._emit(run_id, "user_resolved", step_id=itv["step_id"], resolution=resolution)

    def cancel_unfinished(self, run_id: str, reason: str = "사용자 취소") -> None:
        """Runner가 돌고 있지 않은 미완료 실행(WAITING_USER/PAUSED/비정상 종료)을 취소로 마감."""
        run = self._require_run(run_id)
        if S(run["execution_status"]).is_terminal:
            return
        self.runner_lock.acquire()
        try:
            ctx = self.store.load_context(run_id)
            itv = self.store.open_intervention(run_id)
            if itv:
                self.store.resolve_intervention(itv["id"], "cancelled", reason)
            self._finish_cancelled(ctx, reason)
        finally:
            self.runner_lock.release()

    # ------------------------------------------------------------------
    # 실행 루프
    # ------------------------------------------------------------------
    def _run(self, workflow: WorkflowDefinition, ctx: RunContext) -> None:
        try:
            self._run_steps(workflow, ctx)
        finally:
            # 정상 중단 경로에서 눌린 키·버튼 해제, 이 실행이 연 세션(브라우저 등) 정리 (강제 종료 시에는 보장 불가)
            for hook in ("release_inputs", "end_session"):
                for adapter in self.adapters.values():
                    fn = getattr(adapter, hook, None)
                    if fn:
                        try:
                            fn()
                        except Exception:  # noqa: BLE001
                            pass

    def _run_steps(self, workflow: WorkflowDefinition, ctx: RunContext) -> None:
        run_id = ctx.run_id
        self._set_status(run_id, S.RUNNING)
        try:
            start = 0 if ctx.last_checkpoint is None else workflow.step_index(ctx.last_checkpoint) + 1
            for step in workflow.steps[start:]:
                self._execute_step(workflow, ctx, step)
            outcome = self._compute_outcome(ctx, finished=True)
            ctx.current_step = None
            self.store.save_context(ctx)
            self._set_status(run_id, S.FINISHED, "완료", outcome)
            self._emit(run_id, "run_finished", business_outcome=outcome.value)
        except UserActionRequired as e:
            self._collect_failure_evidence(ctx)
            self._wait_user(ctx, e.reason, e.instruction)
        except RunCancelled as e:
            self._finish_cancelled(ctx, str(e))
        except FailSafeTriggered as e:
            self._finish_cancelled(ctx, f"FAILSAFE: {e}")
        except ControlChannelLost as e:
            self._finish(ctx, S.FAILED, f"제어 채널 끊김: {e}")
        except StepFailed as e:
            self._collect_failure_evidence(ctx)
            self._finish(ctx, S.FAILED, str(e))
        except Exception as e:  # noqa: BLE001 — 예기치 않은 오류도 상태로 남긴다
            self._collect_failure_evidence(ctx)
            self._finish(ctx, S.FAILED, f"{type(e).__name__}: {e}")

    def _collect_failure_evidence(self, ctx: RunContext) -> None:
        step_id = ctx.current_step or "run"
        out_dir = Path(ctx.run_dir) / step_id
        for adapter in self.adapters.values():
            collect = getattr(adapter, "failure_evidence", None)
            if not collect:
                continue
            try:
                for path in collect(out_dir):
                    self._add_evidence(ctx, step_id, "failure_screen", path)
            except Exception:  # noqa: BLE001 — 증거 수집 실패가 원래 오류를 가리면 안 된다
                pass

    def _execute_step(self, workflow: WorkflowDefinition, ctx: RunContext, step: StepDefinition) -> None:
        run_id = ctx.run_id
        resumed = self.control.checkpoint(
            on_paused=lambda: self._on_paused(run_id, step.id),
            on_resumed=lambda: self._on_resumed(run_id, step.id),
        )
        ctx.current_step = step.id
        ctx.logical_operation_id = None
        self.store.save_context(ctx)
        self._emit(run_id, "step_started", step_id=step.id, after_pause=resumed)

        env = StepEnv(
            step_id=step.id,
            adapters=self.adapters,
            control=self.control,
            deadline=time.monotonic() + step.timeout,
            action_timeout=step.action_timeout,
            _add_evidence=lambda kind, path: self._add_evidence(ctx, step.id, kind, path),
            _notify=lambda msg: self._emit(run_id, "step_notice", step_id=step.id, message=msg),
        )
        if step.precondition:
            step.precondition(ctx, env)

        if step.effect_kind.is_change:
            action, check = self._run_change(workflow, ctx, step, env)
        else:
            action, check = self._run_read(ctx, step, env)

        ctx.step_checks[step.id] = {"status": check.status.value, "mode": check.verification_mode.value}
        if check.status == CheckStatus.FAIL:
            self.store.save_context(ctx)
            self._emit(run_id, "step_failed", step_id=step.id, check=check.to_dict())
            raise StepFailed(f"{step.id} 검증 실패: {check.reason}")
        if check.status == CheckStatus.UNKNOWN:
            self.store.save_context(ctx)
            mr = step.manual_resolution
            raise UserActionRequired(
                f"{step.id} 결과 판정 불가: {check.reason}",
                mr.instruction if mr else "대상 화면에서 결과를 확인한 뒤 resolve로 확인 결과를 제출하세요.",
            )

        for ref in action.evidence_refs:
            self._add_evidence(ctx, step.id, "action", ref)
        ctx.step_outputs[step.id] = action.outputs
        ctx.last_checkpoint = step.id
        self.store.save_context(ctx)
        self._emit(run_id, "step_passed", step_id=step.id, mode=check.verification_mode.value)

    def _run_read(self, ctx: RunContext, step: StepDefinition, env: StepEnv) -> tuple[ActionResult, CheckResult]:
        tries = 0
        while True:
            tries += 1
            attempt_id, _ = self.store.start_attempt(ctx.run_id, step.id)
            ctx.attempt_id = attempt_id
            try:
                action = step.handler(ctx, env)
            except TransientError as e:
                self.store.finish_attempt(attempt_id, "TRANSIENT_ERROR", str(e))
                self._emit(ctx.run_id, "step_retry", step_id=step.id, attempt=tries, error=str(e))
                if tries >= step.retry_policy.max_attempts or env.remaining() <= 0:
                    raise StepFailed(f"{step.id} 재시도 소진 ({tries}회): {e}") from e
                self.control.sleep(step.retry_policy.backoff_seconds)
                continue
            except BaseException as e:
                self.store.finish_attempt(attempt_id, type(e).__name__, str(e))
                raise
            check = self._verify(ctx, step, env, action)
            self.store.finish_attempt(attempt_id, f"CHECK_{check.status.value}", None, action.to_dict(), check.to_dict())
            return action, check

    def _run_change(
        self, workflow: WorkflowDefinition, ctx: RunContext, step: StepDefinition, env: StepEnv
    ) -> tuple[ActionResult, CheckResult]:
        op_id = self._operation_id(workflow, ctx, step)
        ctx.logical_operation_id = op_id
        env.logical_operation_id = op_id
        step_input_hash = self._step_input_hash(ctx, step)
        op, created = self.store.record_intent(
            op_id, ctx.run_id, step.id, ctx.business_key, step_input_hash,
            target=step.id, intent=f"{workflow.id}:{step.id}",
        )
        self._emit(ctx.run_id, "intent_recorded", step_id=step.id, operation_id=op_id, existing=not created)

        if not created and op["input_hash"] != step_input_hash:
            raise UserActionRequired(
                f"같은 업무 건({op_id})에 다른 입력으로 기록된 변경이 있습니다",
                "이전 처리 결과를 확인하세요. 자동으로 덮어쓰지 않습니다.",
            )

        state = DispatchState(op["dispatch_state"])
        if state == DispatchState.CONFIRMED_APPLIED:
            # 이미 반영 확인된 변경: 다시 전송하지 않고 결과 복원
            result = json.loads(op["result_json"] or "{}")
            action = ActionResult(step.id, DispatchState.CONFIRMED_APPLIED, outputs=result.get("outputs", {}))
            self._emit(ctx.run_id, "operation_restored", step_id=step.id, operation_id=op_id)
            if "check" in result:
                return action, CheckResult.from_dict(result["check"])
            return action, self._confirm_check(op_id, action, self._verify(ctx, step, env, action))

        if not created and state != DispatchState.CONFIRMED_NOT_APPLIED:
            # 이전 시도의 결과 미확정(의도만 저장된 경우 포함) → 보수적으로 대조
            restored = self._reconcile_or_wait(ctx, step, env, op_id, reason="이전 시도 결과 미확정")
            if restored is not None:
                return restored

        while True:
            op = self.store.get_operation(op_id)
            assert op is not None
            if op["dispatch_count"] >= step.retry_policy.max_attempts:
                raise UserActionRequired(
                    f"{step.id} 변경 시도 한도({step.retry_policy.max_attempts}) 도달",
                    "대상 시스템에서 처리 여부를 확인하고 resolve하세요.",
                )
            self.control.raise_if_stopped()  # 전송 직전 취소 재확인
            attempt_id, _ = self.store.start_attempt(ctx.run_id, step.id)
            ctx.attempt_id = attempt_id
            self.store.mark_dispatching(op_id)  # 이 시점 이후 종료되면 결과는 UNKNOWN
            try:
                action = step.handler(ctx, env)
            except _STOP_SIGNALS as e:
                self.store.finish_attempt(attempt_id, "INTERRUPTED_DURING_DISPATCH", str(e))
                raise
            except UserActionRequired as e:
                self.store.finish_attempt(attempt_id, "USER_ACTION_REQUIRED", str(e))
                raise
            except Exception as e:  # noqa: BLE001
                # 변경 호출 직후 예외: blind retry 금지. 대조로만 판단
                self.store.finish_attempt(attempt_id, "DISPATCH_ERROR", f"{type(e).__name__}: {e}")
                self._emit(ctx.run_id, "dispatch_error", step_id=step.id, error=str(e))
                restored = self._reconcile_or_wait(ctx, step, env, op_id, reason=f"전송 중 오류: {e}")
                if restored is not None:
                    return restored
                continue  # 미반영 확인됨 → 한도 안에서 다시 전송
            self.store.set_operation_state(op_id, DispatchState.DISPATCHED, {"outputs": action.outputs})
            check = self._verify(ctx, step, env, action)
            self.store.finish_attempt(attempt_id, f"CHECK_{check.status.value}", None, action.to_dict(), check.to_dict())
            return action, self._confirm_check(op_id, action, check)

    def _reconcile_or_wait(
        self, ctx: RunContext, step: StepDefinition, env: StepEnv, op_id: str, reason: str
    ) -> Optional[tuple[ActionResult, CheckResult]]:
        """대조 결과 APPLIED면 (action, check) 반환, NOT_APPLIED면 None(재전송 허용), 그 외 WAITING_USER."""
        rec = self._reconcile(ctx, step, env)
        self._emit(ctx.run_id, "reconciled", step_id=step.id, operation_id=op_id, status=rec.status.value, reason=rec.reason)
        if rec.status == ReconcileStatus.APPLIED:
            action = ActionResult(step.id, DispatchState.CONFIRMED_APPLIED, outputs=rec.observed)
            return action, self._confirm_check(op_id, action, self._verify(ctx, step, env, action))
        if rec.status == ReconcileStatus.NOT_APPLIED:
            self.store.set_operation_state(op_id, DispatchState.CONFIRMED_NOT_APPLIED)
            return None
        self.store.set_operation_state(op_id, DispatchState.UNKNOWN)
        mr = step.manual_resolution
        raise UserActionRequired(
            f"{step.id} 변경 결과 미확정 ({reason}; 대조: {rec.reason or rec.status.value})",
            mr.instruction if mr else "대상 시스템에서 반영 여부를 확인하고 resolve applied/not_applied로 제출하세요.",
        )

    def _reconcile(self, ctx: RunContext, step: StepDefinition, env: StepEnv) -> ReconcileResult:
        if step.reconcile is None:
            return ReconcileResult(ReconcileStatus.UNKNOWN, reason="reconcile 함수 없음")
        try:
            return step.reconcile(ctx, env)
        except _STOP_SIGNALS:
            raise
        except Exception as e:  # noqa: BLE001
            return ReconcileResult(ReconcileStatus.UNKNOWN, reason=f"대조 실패: {type(e).__name__}: {e}")

    def _verify(self, ctx: RunContext, step: StepDefinition, env: StepEnv, action: ActionResult) -> CheckResult:
        try:
            return step.verifier(ctx, env, action)
        except (*_STOP_SIGNALS, UserActionRequired):
            raise
        except Exception as e:  # noqa: BLE001 — 검증 불가는 FAIL이 아니라 UNKNOWN
            return CheckResult(
                status=CheckStatus.UNKNOWN,
                verification_mode=VerificationMode.RULE,
                business_key=ctx.business_key,
                reason=f"검증 중 오류: {type(e).__name__}: {e}",
            )

    def _confirm_check(self, op_id: str, action: ActionResult, check: CheckResult) -> CheckResult:
        if check.status == CheckStatus.PASS:
            self.store.set_operation_state(
                op_id, DispatchState.CONFIRMED_APPLIED, {"outputs": action.outputs, "check": check.to_dict()}
            )
        # FAIL/UNKNOWN: 외부 미반영으로 단정하지 않는다. 상태는 DISPATCHED/UNKNOWN 그대로 둔다
        return check

    # ------------------------------------------------------------------
    # 상태·결과
    # ------------------------------------------------------------------
    def _compute_outcome(self, ctx: RunContext, finished: bool) -> BusinessOutcome:
        if any(c["status"] == CheckStatus.FAIL.value for c in ctx.step_checks.values()):
            return BusinessOutcome.MISMATCH
        ops = self.store.operations(ctx.run_id)
        if any(o["dispatch_state"] in _UNRESOLVED_OP_STATES for o in ops):
            return BusinessOutcome.UNKNOWN
        if not finished:
            return BusinessOutcome.UNVERIFIED
        if any(c["mode"] == VerificationMode.HUMAN.value for c in ctx.step_checks.values()):
            return BusinessOutcome.HUMAN_CONFIRMED
        return BusinessOutcome.AUTO_VERIFIED

    def _finish(self, ctx: RunContext, status: ExecutionStatus, reason: str) -> None:
        outcome = self._compute_outcome(ctx, finished=False)
        self._set_status(ctx.run_id, status, reason, outcome)
        self._emit(ctx.run_id, f"run_{status.value.lower()}", reason=reason, business_outcome=outcome.value)

    def _finish_cancelled(self, ctx: RunContext, reason: str) -> None:
        self._finish(ctx, S.CANCELLED, reason)

    def _wait_user(self, ctx: RunContext, reason: str, instruction: str) -> None:
        itv_id = self.store.request_intervention(ctx.run_id, ctx.current_step, reason, instruction)
        outcome = self._compute_outcome(ctx, finished=False)
        self._set_status(ctx.run_id, S.WAITING_USER, reason, outcome)
        self._emit(
            ctx.run_id, "waiting_user", step_id=ctx.current_step, reason=reason,
            instruction=instruction, intervention_id=itv_id, business_outcome=outcome.value,
        )

    def _on_paused(self, run_id: str, step_id: str) -> None:
        self._set_status(run_id, S.PAUSE_REQUESTED)
        self._set_status(run_id, S.PAUSED, f"{step_id} 시작 전 일시정지")
        self._emit(run_id, "paused", before_step=step_id)

    def _on_resumed(self, run_id: str, step_id: str) -> None:
        self._set_status(run_id, S.RUNNING)
        self._emit(run_id, "resumed", before_step=step_id)

    def _set_status(
        self, run_id: str, status: ExecutionStatus, reason: Optional[str] = None,
        outcome: Optional[BusinessOutcome] = None,
    ) -> None:
        self.store.set_status(run_id, status, reason, outcome)
        self._emit(run_id, "status", execution_status=status.value, reason=reason,
                   business_outcome=outcome.value if outcome else None)

    # ------------------------------------------------------------------
    # 보조
    # ------------------------------------------------------------------
    def _operation_id(self, workflow: WorkflowDefinition, ctx: RunContext, step: StepDefinition) -> str:
        assert step.operation_key
        key = step.operation_key.format(business_key=ctx.business_key or "", run_id=ctx.run_id)
        return f"{workflow.id}:{key}"

    def _step_input_hash(self, ctx: RunContext, step: StepDefinition) -> str:
        mapped = {}
        for ref in step.input_mapping:
            src, _, key = ref.partition(".")
            mapped[ref] = ctx.output(src, key)
        return hash_value({"inputs": ctx.validated_inputs, "mapped": mapped})

    def _add_evidence(self, ctx: RunContext, step_id: str, kind: str, path: str) -> None:
        p = Path(path)
        try:
            rel = str(p.relative_to(self.data_dir)) if p.is_absolute() else str(p)
        except ValueError:
            rel = str(p)
        sha = None
        if p.is_absolute() and p.is_file():
            import hashlib

            sha = hashlib.sha256(p.read_bytes()).hexdigest()
        self.store.add_evidence(ctx.run_id, step_id, kind, rel, sha)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(f"실행을 찾을 수 없습니다: {run_id}")
        return run

    def _emit(self, run_id: str, type_: str, **payload: Any) -> None:
        seq = self.store.add_event(run_id, type_, payload)
        if self.event_sink:
            self.event_sink({"seq": seq, "ts": now_iso(), "run_id": run_id, "type": type_, **payload})
