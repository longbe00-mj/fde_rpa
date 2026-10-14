"""업무(라이브 데모): 테스트 Windows 폼에 고객 정보 입력·저장 → 저장소 재조회로 검증.

앱 조작은 커넥터(apps/test_form)가 한다. 재시작 후 재개 시 커넥터가 대상 창을 다시 찾아 연결한다.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from rpa.apps.test_form.connector import APP, FIELDS, TestFormApp, make_connector
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
    WorkflowDefinition,
)
from rpa.core.errors import UserActionRequired
from rpa.core.run_context import RunContext
from rpa.workflows.common import rule

WORKFLOW_ID = "demo_test_form"


def make_adapters(data_dir, control, faults=None, *, dv=None):
    dv, form = make_connector(data_dir, control, dv)
    return {"desktop_visual": dv, "form": form}


def _f(env: StepEnv) -> TestFormApp:
    return env.adapter("form")


def _expected(ctx: RunContext) -> dict[str, str]:
    return {k: ctx.validated_inputs[k] for k in FIELDS}


def _compare(ctx: RunContext, observed: dict[str, Optional[str]], what: str) -> CheckResult:
    expected = _expected(ctx)
    unknown = [k for k, v in observed.items() if v is None]
    diffs = [k for k in FIELDS if observed.get(k) is not None and observed[k] != expected[k]]
    if diffs:
        return rule(ctx, CheckStatus.FAIL, expected, observed, f"{what} 값 불일치: {', '.join(diffs)}")
    if unknown:
        return rule(ctx, CheckStatus.UNKNOWN, expected, observed, f"{what} 값을 읽지 못함: {', '.join(unknown)}")
    return rule(ctx, CheckStatus.PASS, expected, observed, f"{what} 값이 입력 원본과 일치")


# --- 1) 폼 열기 (READ_ONLY) ------------------------------------------------------
def open_form(ctx: RunContext, env: StepEnv) -> ActionResult:
    from rpa.platform.windows.preflight import run_preflight

    pf = run_preflight(probe_capture=True)
    if not pf.ok:
        fails = "; ".join(c.message for c in pf.checks if c.level == "FAIL")
        raise UserActionRequired(f"실행 환경 점검 실패: {fails}", "화면 잠금 해제 등 조치 후 resolve done 하세요.")
    f = _f(env)
    f.ensure_ready(env, launch=True)
    f.dv.wait_visually_stable(timeout=min(5.0, env.call_timeout()))
    t = f.dv.target
    ctx.owned_resources = [r for r in ctx.owned_resources if r.get("kind") != "window"] + [
        {"kind": "window", **t.to_dict()}]
    shot = f.capture(Path(ctx.run_dir) / "open_form" / "form_opened.png")
    env.add_evidence("screen", shot["path"])
    return ActionResult("open_form", DispatchState.NOT_DISPATCHED, outputs={
        "hwnd": t.hwnd, "process": t.process_name, "owned": t.owned,
        "warnings": [c.message for c in pf.checks if c.level == "WARN"],
        "capture": {k: shot[k] for k in ("origin_x", "origin_y", "width", "height", "dpi")},
    })


def verify_open(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    dv = _f(env).dv
    ok = dv.target is not None and dv.is_foreground()
    return rule(ctx, CheckStatus.PASS if ok else CheckStatus.UNKNOWN, {"foreground": True},
                {"foreground": ok, "hwnd": action.outputs.get("hwnd")},
                "대상 폼 연결·활성화 확인" if ok else "대상 폼이 포그라운드가 아님")


def require_app(ctx: RunContext, env: StepEnv) -> None:
    _f(env).ensure_ready(env)


# --- 2) 입력 (LOCAL_CHANGE: 저장 전 화면 상태, 다시 입력해도 안전) -----------------
def fill_form(ctx: RunContext, env: StepEnv) -> ActionResult:
    return ActionResult("fill_form", DispatchState.DISPATCHED,
                        outputs={"fields": _f(env).fill_fields(_expected(ctx))})


def verify_fill(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    return _compare(ctx, action.outputs["fields"], "입력 직후 readback")


def reconcile_fill(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    f = _f(env)
    f.ensure_ready(env)
    observed = f.read_fields()
    if observed == _expected(ctx):
        return ReconcileResult(ReconcileStatus.APPLIED, {"fields": observed}, "폼에 입력값이 이미 있음")
    return ReconcileResult(ReconcileStatus.NOT_APPLIED, {"fields": observed}, "폼 값이 입력과 다름 — 다시 입력 가능")


# --- 3) 저장 (EXTERNAL_CHANGE) ----------------------------------------------------
def require_filled_form(ctx: RunContext, env: StepEnv) -> None:
    """일시정지·재시작 뒤에도 화면 값이 그대로인지 확인한 다음에만 저장한다."""
    f = _f(env)
    f.ensure_ready(env)
    if f.lookup(ctx.business_key):
        return  # 이미 저장된 건: 엔진의 대조 경로가 처리
    observed = f.read_fields()
    if observed != _expected(ctx):
        raise UserActionRequired(
            f"저장 직전 폼 값이 입력과 다릅니다: {observed}",
            "사용자 조작으로 값이 바뀌었을 수 있습니다. 실행을 취소하고 다시 시작하세요.")


def save_record(ctx: RunContext, env: StepEnv) -> ActionResult:
    f = _f(env)
    rec = f.save(env, ctx.business_key)
    shot = f.capture(Path(ctx.run_dir) / "save_record" / "after_save.png")
    env.add_evidence("screen", shot["path"])
    return ActionResult("save_record", DispatchState.DISPATCHED,
                        outputs={"record_id": rec.get("record_id") if rec else None})


def verify_save(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    r = _f(env).lookup(ctx.business_key)
    if r is None:
        return rule(ctx, CheckStatus.UNKNOWN, _expected(ctx), {}, "저장소 재조회 결과 없음 (저장 중이거나 미반영 — 판정 불가)")
    chk = _compare(ctx, {k: r.get(k) for k in FIELDS}, "저장소 재조회")
    chk.observed = {k: r.get(k) for k in FIELDS} | {"record_id": r.get("record_id")}
    return chk


def reconcile_save(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    f = _f(env)
    deadline = time.monotonic() + 3.0  # 앱이 아직 저장 중일 수 있어 잠시 더 확인
    while True:
        r = f.lookup(ctx.business_key)
        if r:
            return ReconcileResult(ReconcileStatus.APPLIED, {"record_id": r["record_id"]}, "저장소에 레코드 존재")
        if time.monotonic() >= deadline:
            # 레코드가 없다는 사실만으로 '미반영'을 확정하지 않는다 (앱 처리 중일 가능성)
            return ReconcileResult(ReconcileStatus.UNKNOWN, reason="저장소에 레코드 없음 — 앱 처리 상태 확인 필요")
        env.control.sleep(0.2)


# --- 4) 정리 (READ_ONLY) ----------------------------------------------------------
def close_owned_app(ctx: RunContext, env: StepEnv) -> ActionResult:
    return ActionResult("close_owned_app", DispatchState.NOT_DISPATCHED, outputs={"closed": _f(env).close_if_owned()})


def verify_close(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    dv = _f(env).dv
    if action.outputs["closed"] and dv.target is not None and dv.win.is_window(dv.target.hwnd):
        return rule(ctx, CheckStatus.UNKNOWN, {"closed": True}, {"closed": False}, "이 실행이 띄운 창이 아직 열려 있음")
    return rule(ctx, CheckStatus.PASS, {}, action.outputs,
                "사용자가 연 앱은 닫지 않음" if not action.outputs["closed"] else "띄운 앱 종료")


def build() -> WorkflowDefinition:
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        version="2",
        description="테스트 Windows 폼에 고객 정보 입력·저장 후 저장소 재조회로 검증 (PyAutoGUI 라이브)",
        inputs=(
            InputField("customer_id", pattern=r"[A-Z]+-\d+"),
            InputField("name"),
            InputField("phone", pattern=r"[0-9-]+"),
        ),
        business_key_field="customer_id",
        source_files=(__file__, *APP.source_files()),
        environment_requirements=("windows", "unlocked_desktop", "primary_monitor"),
        settings={"apps": [APP.app_id], "selectors": APP.selectors()},
        steps=(
            StepDefinition("open_form", open_form, verify_open, timeout=30, action_timeout=15),
            StepDefinition(
                "fill_form", fill_form, verify_fill, effect_kind=EffectKind.LOCAL_CHANGE,
                precondition=require_app, operation_key="fill:{run_id}", reconcile=reconcile_fill,
                retry_policy=RetryPolicy(max_attempts=2), timeout=60, action_timeout=10,
            ),
            StepDefinition(
                "save_record", save_record, verify_save, effect_kind=EffectKind.EXTERNAL_CHANGE,
                precondition=require_filled_form, operation_key="save:{business_key}", reconcile=reconcile_save,
                manual_resolution=ManualResolution(
                    "테스트 폼 저장소(test_form_store.json) 또는 화면의 '저장됨' 표시로 저장 여부를 확인한 뒤 "
                    "resolve applied/not_applied 를 제출하세요."),
                timeout=30, action_timeout=10,
            ),
            StepDefinition("close_owned_app", close_owned_app, verify_close, timeout=15, action_timeout=10),
        ),
    )


registry.register(WORKFLOW_ID, build, make_adapters)
