"""업무(웹 데모): 포털 로그인 확인 → 고객 검색 → 상세(iframe) 확인 → CSV 다운로드 검증 → 접수 등록(재조회 대조).

사이트 조작은 커넥터(apps/web_portal)가 한다. 각 단계는 필요한 화면으로 직접 이동해 시작한다
— 재시작·재개 시 이전 화면이 남아 있다고 가정하지 않는다.
"""

from __future__ import annotations

import csv
import hashlib

import os
from pathlib import Path
from typing import Optional

from rpa.apps.web_portal.connector import APP, PortalSite, make_connector
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
from rpa.core.run_context import RunContext
from rpa.workflows.common import rule

WORKFLOW_ID = "demo_web_portal"
FIELDS = ("customer_id", "name", "phone")


def make_adapters(data_dir, control, faults=None, *, base_url: Optional[str] = None, headless: Optional[bool] = None,
                  login_wait: Optional[float] = None):
    browser, portal = make_connector(
        data_dir, control,
        base_url=base_url or os.environ.get("LOCALRPA_PORTAL_URL", "http://127.0.0.1:8765"),
        headless=headless if headless is not None
        else os.environ.get("LOCALRPA_BROWSER_HEADLESS", "") in ("1", "true", "on"),
        login_wait=login_wait if login_wait is not None else 180.0,
    )
    return {"browser": browser, "portal": portal}


def _p(env: StepEnv) -> PortalSite:
    return env.adapter("portal")


# --- 1) 포털 열기 (READ_ONLY) -------------------------------------------------------
def open_portal(ctx: RunContext, env: StepEnv) -> ActionResult:
    p = _p(env)
    url = p.open(env)
    return ActionResult("open_portal", DispatchState.NOT_DISPATCHED, outputs={"url": url, "channel": p.b.settings.channel})


def verify_open(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    p = _p(env)
    ok = p.is_ready()
    return rule(ctx, CheckStatus.PASS if ok else CheckStatus.UNKNOWN, {"logged_in": True}, {"url": p.b.current_url()},
                "로그인된 고객 조회 화면 확인" if ok else "고객 조회 화면을 확인하지 못함")


# --- 2) 고객 검색 (READ_ONLY) ------------------------------------------------------
def search_customer(ctx: RunContext, env: StepEnv) -> ActionResult:
    typed, rows = _p(env).search(env, ctx.business_key)
    matches = [r for r in rows if r.get("customer_id") == ctx.business_key]
    return ActionResult("search_customer", DispatchState.NOT_DISPATCHED, outputs={
        "typed": typed, "row_count": len(rows), "matches": len(matches),
        "customer": matches[0] if len(matches) == 1 else None,
    })


def verify_search(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    o = action.outputs
    exp = {"typed": ctx.business_key, "matches": 1}
    if o["typed"] != ctx.business_key:
        return rule(ctx, CheckStatus.FAIL, exp, o, "검색어 readback이 입력과 다름")
    if o["matches"] == 0:
        return rule(ctx, CheckStatus.FAIL, exp, o, "검색 결과에 해당 고객ID가 없음")
    if o["matches"] > 1:
        return rule(ctx, CheckStatus.FAIL, exp, o, "같은 고객ID 행이 여러 개 — 하나로 특정할 수 없음")
    return rule(ctx, CheckStatus.PASS, exp, o, "검색 결과에서 고객ID가 정확히 한 행 일치")


# --- 3) 상세 확인 (READ_ONLY) ------------------------------------------------------
def read_detail(ctx: RunContext, env: StepEnv) -> ActionResult:
    return ActionResult("read_detail", DispatchState.NOT_DISPATCHED, outputs=_p(env).open_detail(env, ctx.business_key))


def verify_detail(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    exp = ctx.step_outputs["search_customer"]["customer"]
    obs = {k: action.outputs[k] for k in FIELDS}
    diffs = [k for k in FIELDS if obs[k] != exp[k]]
    if diffs:
        return rule(ctx, CheckStatus.FAIL, exp, obs, f"상세 화면 값이 검색 결과와 다름: {', '.join(diffs)}")
    return rule(ctx, CheckStatus.PASS, exp, obs, "상세 화면 값이 검색 결과와 일치 (메모는 iframe에서 읽음)")


# --- 4) CSV 내보내기 (LOCAL_CHANGE) --------------------------------------------------
def _export_dir(ctx: RunContext) -> Path:
    return Path(ctx.run_dir) / "download_export"


def download_export(ctx: RunContext, env: StepEnv) -> ActionResult:
    info = _p(env).export_csv(env, ctx.business_key, _export_dir(ctx))
    env.add_evidence("download", info["path"])
    return ActionResult("download_export", DispatchState.DISPATCHED, outputs=info)


def verify_export(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    path = Path(action.outputs["path"])
    exp = {k: ctx.step_outputs["read_detail"][k] for k in FIELDS}
    if not path.is_file() or _export_dir(ctx) not in path.parents:
        return rule(ctx, CheckStatus.FAIL, exp, {"path": str(path)}, "이번 실행 폴더에 다운로드 파일이 없음")
    try:
        rows = PortalSite.parse_export(path)
    except (UnicodeDecodeError, csv.Error) as e:
        return rule(ctx, CheckStatus.FAIL, exp, {"path": str(path)}, f"CSV를 읽지 못함: {e}")
    obs = {"rows": rows, "path": str(path)}
    if rows != [exp]:
        return rule(ctx, CheckStatus.FAIL, exp, obs, "CSV 내용이 상세 화면 값과 다름 (파일명만으로 판정하지 않음)")
    return rule(ctx, CheckStatus.PASS, exp, obs, "다운로드 CSV를 열어 한 행이 상세 화면 값과 일치")


def reconcile_export(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    files = sorted(_export_dir(ctx).glob("*.csv")) if _export_dir(ctx).exists() else []
    if len(files) == 1:
        data = files[0].read_bytes()
        return ReconcileResult(ReconcileStatus.APPLIED, {"path": str(files[0]), "size": len(data),
                                                          "sha256": hashlib.sha256(data).hexdigest()})
    return ReconcileResult(ReconcileStatus.NOT_APPLIED if not files else ReconcileStatus.UNKNOWN,
                           reason=f"실행 폴더의 CSV {len(files)}개")


# --- 5) 접수 등록 (EXTERNAL_CHANGE) --------------------------------------------------
def _expected_record(ctx: RunContext) -> dict[str, str]:
    d = ctx.step_outputs["read_detail"]
    att = ctx.validated_inputs.get("attachment")
    return {"customer_id": ctx.business_key, "name": d["name"], "phone": d["phone"],
            "memo": ctx.validated_inputs.get("memo", ""), "attachment": Path(att).name if att else ""}


def register_record(ctx: RunContext, env: StepEnv) -> ActionResult:
    exp = _expected_record(ctx)
    out = _p(env).register(env, exp, ctx.validated_inputs.get("attachment"))
    return ActionResult("register_record", DispatchState.DISPATCHED, outputs=out)


def verify_register(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    exp = _expected_record(ctx)
    rows = _p(env).lookup_records(env, ctx.business_key)
    obs = {"records": rows}
    if not rows:
        return rule(ctx, CheckStatus.UNKNOWN, exp, obs, "접수 목록 재조회 결과 없음")
    if len(rows) > 1:
        return rule(ctx, CheckStatus.FAIL, exp, obs, f"같은 고객의 접수가 {len(rows)}건 — 중복 등록")
    row = rows[0]
    diffs = [k for k in exp if row.get(k) != exp[k]]
    if diffs:
        return rule(ctx, CheckStatus.FAIL, exp, obs, f"재조회 값 불일치: {', '.join(diffs)}")
    rid = action.outputs.get("record_id")
    if rid and rid != row["record_id"]:
        return rule(ctx, CheckStatus.FAIL, exp, obs, "완료 화면 접수번호와 재조회 접수번호가 다름")
    return rule(ctx, CheckStatus.PASS, exp, obs | {"record_id": row["record_id"]}, "접수 목록 재조회 값이 입력과 일치 (1건)")


def reconcile_register(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    rows = _p(env).lookup_records(env, ctx.business_key)
    if len(rows) == 1:
        return ReconcileResult(ReconcileStatus.APPLIED, {"record_id": rows[0]["record_id"]}, "접수 목록에 1건 존재")
    if not rows:
        return ReconcileResult(ReconcileStatus.NOT_APPLIED, reason="접수 목록에 없음 (서버 재조회 기준)")
    return ReconcileResult(ReconcileStatus.UNKNOWN, reason=f"접수 {len(rows)}건 — 사람이 확인 필요")


def build() -> WorkflowDefinition:
    retry = RetryPolicy(max_attempts=3, backoff_seconds=1.0)
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        version="2",
        description="웹 포털: 로그인 확인 → 고객 검색 → 상세(iframe) → CSV 다운로드 검증 → 접수 등록(재조회 대조)",
        inputs=(
            InputField("customer_id", pattern=r"[A-Z]+-\d+"),
            InputField("memo", required=False, description="접수 메모"),
            InputField("attachment", required=False, description="첨부 파일 경로 (선택)"),
        ),
        business_key_field="customer_id",
        source_files=(__file__, *APP.source_files()),
        environment_requirements=("browser:msedge", "portal_reachable", "portal_login_by_user"),
        settings={"apps": [APP.app_id], "selectors": APP.selectors()},
        steps=(
            StepDefinition("open_portal", open_portal, verify_open, retry_policy=retry, timeout=240, action_timeout=30),
            StepDefinition("search_customer", search_customer, verify_search, retry_policy=retry,
                           timeout=240, action_timeout=30),
            StepDefinition("read_detail", read_detail, verify_detail, retry_policy=retry, timeout=240, action_timeout=30),
            StepDefinition("download_export", download_export, verify_export, effect_kind=EffectKind.LOCAL_CHANGE,
                           operation_key="export:{run_id}", reconcile=reconcile_export,
                           retry_policy=RetryPolicy(max_attempts=2), timeout=240, action_timeout=30),
            StepDefinition("register_record", register_record, verify_register, effect_kind=EffectKind.EXTERNAL_CHANGE,
                           operation_key="web_register:{business_key}", reconcile=reconcile_register,
                           retry_policy=RetryPolicy(max_attempts=2), timeout=240, action_timeout=30,
                           manual_resolution=ManualResolution(
                               "포털의 접수 목록에서 이 고객의 접수 여부를 확인하고 resolve applied/not_applied 하세요.")),
        ),
    )


registry.register(WORKFLOW_ID, build, make_adapters)
