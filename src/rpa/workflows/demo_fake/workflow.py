"""데모 업무: 웹에서 고객 조회 → Windows 앱에 입력·저장 → 처리 결과 파일 저장.

FakeAdapter로 동작한다. 실제 웹/앱 버전은 같은 단계 구조에 실제 어댑터를 연결해 만든다.
"""

from __future__ import annotations

import json
from pathlib import Path

from rpa.core import registry
from rpa.core.contracts import (
    ActionResult,
    CheckResult,
    CheckStatus,
    DispatchState,
    EffectKind,
    InputField,
    ManualResolution,
    ReconcileResult,
    ReconcileStatus,
    RetryPolicy,
    StepDefinition,
    StepEnv,
    VerificationMode,
    WorkflowDefinition,
)
from rpa.core.errors import PreconditionFailed
from rpa.core.run_context import RunContext

WORKFLOW_ID = "demo_web_to_desktop"
COMPARED_FIELDS = ("customer_id", "name", "phone")


def _check(ctx: RunContext, status: CheckStatus, expected: dict, observed: dict, reason: str) -> CheckResult:
    return CheckResult(
        status=status,
        verification_mode=VerificationMode.RULE,
        business_key=ctx.business_key,
        expected=expected,
        observed=observed,
        reason=reason,
    )


# 1) 웹 조회 (READ_ONLY) -------------------------------------------------
def fetch_customer(ctx: RunContext, env: StepEnv) -> ActionResult:
    c = env.adapter("web").get_customer(ctx.validated_inputs["customer_id"])
    if c is None:
        raise PreconditionFailed(f"웹에서 고객 {ctx.validated_inputs['customer_id']}를 찾을 수 없습니다")
    return ActionResult("fetch_customer", DispatchState.NOT_DISPATCHED, outputs=c)


def verify_fetch(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    expected = {"customer_id": ctx.business_key}
    observed = {k: action.outputs.get(k) for k in COMPARED_FIELDS}
    if observed["customer_id"] != ctx.business_key:
        return _check(ctx, CheckStatus.FAIL, expected, observed, "조회된 고객 ID가 입력과 다름")
    if not observed["name"]:
        return _check(ctx, CheckStatus.UNKNOWN, expected, observed, "고객명을 읽지 못함")
    return _check(ctx, CheckStatus.PASS, expected, observed, "조회 결과의 고객 ID 일치")


# 2) 데스크톱 저장 (EXTERNAL_CHANGE) ----------------------------------------
def _values(ctx: RunContext) -> dict:
    src = ctx.step_outputs["fetch_customer"]
    return {k: src[k] for k in COMPARED_FIELDS}


def require_fetched(ctx: RunContext, env: StepEnv) -> None:
    if "fetch_customer" not in ctx.step_outputs:
        raise PreconditionFailed("웹 조회 결과가 없습니다")


def save_customer(ctx: RunContext, env: StepEnv) -> ActionResult:
    record_id = env.adapter("desktop").save_customer(_values(ctx), env.control, env.call_timeout())
    return ActionResult("save_customer", DispatchState.DISPATCHED, outputs={"record_id": record_id})


def reconcile_save(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    r = env.adapter("desktop").find_customer(ctx.business_key)
    if r is None:
        return ReconcileResult(ReconcileStatus.NOT_APPLIED, reason="앱에서 해당 고객 레코드 없음")
    return ReconcileResult(ReconcileStatus.APPLIED, observed={"record_id": r["record_id"]}, reason="앱에 레코드 존재")


def verify_save(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    expected = _values(ctx)
    r = env.adapter("desktop").find_customer(ctx.business_key)
    if r is None:
        return _check(ctx, CheckStatus.UNKNOWN, expected, {}, "재조회 결과 없음 (반영 여부 판정 불가)")
    observed = {k: r.get(k) for k in COMPARED_FIELDS} | {"record_id": r["record_id"]}
    diffs = [k for k in COMPARED_FIELDS if r.get(k) != expected[k]]
    if diffs:
        return _check(ctx, CheckStatus.FAIL, expected, observed, f"재조회 값 불일치: {', '.join(diffs)}")
    if action.outputs.get("record_id") not in (None, r["record_id"]):
        return _check(ctx, CheckStatus.FAIL, expected, observed, "반환된 record_id와 재조회 record_id 불일치")
    return _check(ctx, CheckStatus.PASS, expected, observed, "재조회한 레코드 값이 원본과 일치")


# 3) 결과 파일 저장 (LOCAL_CHANGE) ------------------------------------------
def _receipt_path(ctx: RunContext) -> Path:
    return Path(ctx.run_dir) / "export_receipt" / f"receipt_{ctx.business_key}.json"


def _receipt_body(ctx: RunContext) -> dict:
    return {**_values(ctx), "record_id": ctx.output("save_customer", "record_id"), "run_id": ctx.run_id}


def export_receipt(ctx: RunContext, env: StepEnv) -> ActionResult:
    path = _receipt_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():  # 덮어쓰기 정책: 같은 실행의 같은 파일만 허용, 다른 내용이면 거부
        if json.loads(path.read_text(encoding="utf-8")) != _receipt_body(ctx):
            raise PreconditionFailed(f"다른 내용의 결과 파일이 이미 있습니다: {path}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_receipt_body(ctx), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    env.add_evidence("file", str(path))
    return ActionResult("export_receipt", DispatchState.DISPATCHED, outputs={"path": str(path)})


def reconcile_receipt(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    path = _receipt_path(ctx)
    if not path.exists():
        return ReconcileResult(ReconcileStatus.NOT_APPLIED, reason="결과 파일 없음")
    return ReconcileResult(ReconcileStatus.APPLIED, observed={"path": str(path)})


def verify_receipt(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    path = _receipt_path(ctx)
    expected = _receipt_body(ctx)
    if not path.exists():
        return _check(ctx, CheckStatus.FAIL, expected, {}, "결과 파일이 없음")
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return _check(ctx, CheckStatus.FAIL, expected, {}, f"결과 파일 파싱 실패: {e}")
    if observed != expected:
        return _check(ctx, CheckStatus.FAIL, expected, observed, "결과 파일 내용 불일치")
    return _check(ctx, CheckStatus.PASS, expected, observed, "결과 파일을 다시 읽어 내용 일치 확인")


def build() -> WorkflowDefinition:
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        version="1",
        description="웹에서 고객 조회 → Windows 앱 저장 → 처리 결과 파일 저장 (FakeAdapter 데모)",
        inputs=(InputField("customer_id", pattern=r"[A-Z]+-\d+", description="고객 ID (예: DEMO-001)"),),
        business_key_field="customer_id",
        source_files=(__file__,),
        steps=(
            StepDefinition(
                id="fetch_customer",
                handler=fetch_customer,
                verifier=verify_fetch,
                effect_kind=EffectKind.READ_ONLY,
                retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.05),
                timeout=30,
                action_timeout=10,
            ),
            StepDefinition(
                id="save_customer",
                handler=save_customer,
                verifier=verify_save,
                effect_kind=EffectKind.EXTERNAL_CHANGE,
                precondition=require_fetched,
                input_mapping=("fetch_customer.name", "fetch_customer.phone"),
                operation_key="save_customer:{business_key}",
                reconcile=reconcile_save,
                retry_policy=RetryPolicy(max_attempts=2),
                manual_resolution=ManualResolution(
                    "Windows 앱에서 고객 ID로 조회해 저장 여부를 확인한 뒤 resolve applied 또는 not_applied를 제출하세요."
                ),
                timeout=60,
                action_timeout=15,
            ),
            StepDefinition(
                id="export_receipt",
                handler=export_receipt,
                verifier=verify_receipt,
                effect_kind=EffectKind.LOCAL_CHANGE,
                operation_key="receipt:{run_id}",
                input_mapping=("save_customer.record_id",),
                reconcile=reconcile_receipt,
                timeout=10,
                action_timeout=5,
            ),
        ),
    )


def make_adapters(data_dir, control, faults=None):
    from rpa.adapters.fake import make_fake_adapters

    return make_fake_adapters(Path(data_dir) / "fake_backend.json", faults)


registry.register(WORKFLOW_ID, build, make_adapters)
