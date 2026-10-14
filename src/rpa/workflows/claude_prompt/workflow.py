"""업무: Claude 앱 새 대화에 프롬프트 전송 → 응답 완료 대기 → 응답을 파일로 저장.

앱 조작·상태 판정은 커넥터(apps/claude_desktop)가 하고, 이 파일은 단계 순서·완료 검증·대조만 정의한다.

안전 규칙
- 빈 새 대화 화면과 '채팅' 모드를 확인한 뒤에만 입력한다 (기존 대화·Cowork로 잘못 보내지 않기).
- 입력창 readback이 원문과 정확히 같을 때만 전송한다.
- 전송은 request_id당 한 번 (operation_key). 결과가 불명확하면 다시 보내지 않고 사용자 확인.
- 대기 중 다른 창이 앞으로 오면 판정을 멈춘다 (다른 앱 화면을 Claude 화면으로 오인하지 않음).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rpa.apps.claude_desktop.connector import APP, ClaudeDesktop, make_connector
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
from rpa.core.errors import PreconditionFailed, UserActionRequired
from rpa.core.run_context import RunContext
from rpa.vision.contracts import as_observed, describe_ko
from rpa.workflows.common import rule

WORKFLOW_ID = "claude_prompt"


@dataclass(frozen=True)
class PromptJobSettings:
    output_dir: str
    response_timeout: float = 300.0
    # 시간 초과 시 화면 해석이 '아직 생성 중'이라고 판정하면 한 번 더 기다리는 시간 (입력 없음, 해석이 켜졌을 때만)
    response_extra_wait: float = 300.0


def make_adapters(data_dir, control, faults=None):
    dv, claude = make_connector(control)
    return {
        "desktop_visual": dv,  # 엔진 훅(입력 해제·창 복원·실패 화면) 대상
        "claude": claude,
        "job": PromptJobSettings(output_dir=str(Path(data_dir) / "outputs" / "claude")),
    }


def _c(env: StepEnv) -> ClaudeDesktop:
    return env.adapter("claude")


def _prompt(ctx: RunContext) -> str:
    return ctx.validated_inputs["prompt"]


# --- 1) Claude 열기 (READ_ONLY) ---------------------------------------------------
def open_claude(ctx: RunContext, env: StepEnv) -> ActionResult:
    from rpa.platform.windows.preflight import run_preflight

    pf = run_preflight(probe_capture=True)
    if not pf.ok:
        fails = "; ".join(c.message for c in pf.checks if c.level == "FAIL")
        raise UserActionRequired(f"실행 환경 점검 실패: {fails}", "화면 잠금 해제 등 조치 후 resolve done 하세요.")
    c = _c(env)
    c.ensure_ready(env)
    warnings = [x.message for x in pf.checks if x.level == "WARN"] + c.environment_warnings()
    shot = c.capture(Path(ctx.run_dir) / "open_claude" / "before.png")
    env.add_evidence("screen", shot["path"])
    return ActionResult("open_claude", DispatchState.NOT_DISPATCHED, outputs={
        "hwnd": c.dv.target.hwnd, "app_version": c.app_version(), "warnings": warnings,
        "minimized_windows": [d["process"] for d in c.dv.minimized],
    })


def verify_open(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    ok = _c(env).dv.is_foreground()
    return rule(ctx, CheckStatus.PASS if ok else CheckStatus.UNKNOWN, {"foreground": True}, {"foreground": ok},
                "Claude 창 활성화 확인" if ok else "Claude 창이 포그라운드가 아님")


def require_app(ctx: RunContext, env: StepEnv) -> None:
    _c(env).ensure_ready(env)


# --- 2) 새 대화 (READ_ONLY: 메시지를 보내기 전까지 서버 변경 없음) -------------------
def new_chat(ctx: RunContext, env: StepEnv) -> ActionResult:
    return ActionResult("new_chat", DispatchState.NOT_DISPATCHED, outputs=_c(env).new_chat(env))


def verify_new_chat(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    o = action.outputs
    expected = {"placeholder": 1, "chat_mode": 1, "action_bar": 0}
    if o == expected:
        return rule(ctx, CheckStatus.PASS, expected, o, "빈 새 대화 + 채팅 모드 확인")
    a = _c(env).diagnose(ctx, env, "Pressed the new-chat shortcut and expected the empty new-chat screen in 채팅 "
                                   f"mode, but template checks found {o}.", _prompt(ctx))
    if o.get("placeholder") == 1 and o.get("chat_mode") == 0:
        return rule(ctx, CheckStatus.FAIL, expected, o | as_observed(a),
                    "채팅 모드가 아님 (Cowork 등) — 자동으로 바꾸지 않음" + describe_ko(a))
    return rule(ctx, CheckStatus.UNKNOWN, expected, o | as_observed(a), "빈 새 대화 화면을 확인하지 못함" + describe_ko(a))


# --- 3) 프롬프트 입력 (LOCAL_CHANGE: 전송 전 입력창 상태, 다시 입력해도 안전) ----------
def enter_prompt(ctx: RunContext, env: StepEnv) -> ActionResult:
    return ActionResult("enter_prompt", DispatchState.DISPATCHED,
                        outputs={"editor_text": _c(env).type_prompt(_prompt(ctx))})


def verify_prompt(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    expected = {"editor_text": _prompt(ctx)}
    observed = action.outputs
    if observed["editor_text"] is None:
        return rule(ctx, CheckStatus.UNKNOWN, expected, observed, "입력창 값을 읽지 못함")
    if observed["editor_text"] != expected["editor_text"]:
        return rule(ctx, CheckStatus.FAIL, expected, observed, "입력창 값이 원문과 다름 (전송하지 않음)")
    return rule(ctx, CheckStatus.PASS, expected, observed, "입력창 readback이 원문과 정확히 일치")


def reconcile_prompt(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    c = _c(env)
    c.ensure_ready(env)
    text = c.read_editor()
    if text == _prompt(ctx):
        return ReconcileResult(ReconcileStatus.APPLIED, {"editor_text": text}, "입력창에 원문이 그대로 있음")
    return ReconcileResult(ReconcileStatus.NOT_APPLIED, {"editor_text": text}, "입력창 값이 원문과 다름 — 다시 입력")


# --- 4) 전송 (EXTERNAL_CHANGE: 대화 생성·사용량 소모) ------------------------------
def require_ready_to_send(ctx: RunContext, env: StepEnv) -> None:
    """재개·일시정지 뒤에도 '빈 새 대화 + 입력창 원문 + 채팅 모드'일 때만 전송."""
    c = _c(env)
    c.ensure_ready(env)
    if c.is_generating() or c.reply_rows():
        return  # 이미 전송된 화면: 엔진의 대조 경로가 처리
    if not c.is_chat_mode():
        raise UserActionRequired("전송 직전 '채팅' 모드 입력창을 확인하지 못함", "Claude 화면을 확인한 뒤 resolve done 하세요.")
    text = c.read_editor()
    if text != _prompt(ctx):
        a = c.diagnose(ctx, env, "About to send; the prompt input box should contain exactly the expected prompt, "
                                 "but the read-back text differs.", _prompt(ctx))
        raise UserActionRequired(
            f"전송 직전 입력창 값이 원문과 다릅니다 ({len(text or '')}자){describe_ko(a)}",
            "사용자 조작으로 바뀌었을 수 있습니다. 실행을 취소하고 다시 시작하세요.",
        )


def send_prompt(ctx: RunContext, env: StepEnv) -> ActionResult:
    c = _c(env)
    state = c.send(env)
    shot = c.capture(Path(ctx.run_dir) / "send_prompt" / "after_send.png")
    env.add_evidence("screen", shot["path"])
    return ActionResult("send_prompt", DispatchState.DISPATCHED, outputs=state)


def verify_send(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    o = action.outputs
    if o.get("generating") or o.get("done"):
        return rule(ctx, CheckStatus.PASS, {"acknowledged": True}, o, "전송 후 생성 중/완료 표시 확인")
    a = _c(env).diagnose(ctx, env, "Pressed Enter to send the prompt; expected a user bubble with generating or "
                                   "completed reply, but neither the stop button nor the reply icon row was found.",
                         _prompt(ctx))
    return rule(ctx, CheckStatus.UNKNOWN, {"acknowledged": True}, o | as_observed(a),
                "전송 후 생성 중/완료 표시를 확인하지 못함" + describe_ko(a))


def reconcile_send(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    c = _c(env)
    c.ensure_ready(env)
    if c.is_generating() or c.reply_rows() == 1:
        return ReconcileResult(ReconcileStatus.APPLIED, {"acknowledged": True}, "화면에 생성 중/완료 표시가 있음")
    # 보이지 않는다고 '안 보냄'을 확정하지 않는다 (다른 대화로 이동했을 수 있음)
    return ReconcileResult(ReconcileStatus.UNKNOWN, reason="현재 화면에서 전송 여부를 판정할 수 없음")


# --- 5) 응답 완료 대기 (READ_ONLY) --------------------------------------------------
def wait_response(ctx: RunContext, env: StepEnv) -> ActionResult:
    c = _c(env)
    job: PromptJobSettings = env.adapter("job")
    finished, state = c.wait_reply(env, job.response_timeout)
    if not finished:
        # 기준 이미지로 완료를 못 봤을 때만 화면 해석. '아직 생성 중'이면 한 번만 더 기다린다 (입력 없음)
        a = c.diagnose(ctx, env, f"Waited {job.response_timeout:.0f}s for the assistant reply to finish; template "
                                 f"checks: stop_button_visible={state.get('generating')}, "
                                 f"reply_icon_rows={state.get('action_bars')}.", _prompt(ctx))
        state |= as_observed(a)
        if a is not None and a.usable and a.state == "generating" and a.recommended_action == "keep_waiting":
            state["extended_wait"] = job.response_extra_wait
            finished, more = c.wait_reply(env, job.response_extra_wait)
            state |= more
        state["diagnosis"] = describe_ko(a)
    state["finished"] = finished
    return ActionResult("wait_response", DispatchState.NOT_DISPATCHED, outputs=state)


def verify_wait(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    o = action.outputs
    if o["finished"]:
        return rule(ctx, CheckStatus.PASS, {"finished": True}, o, "생성 중 표시 사라짐 + 응답 아이콘 줄 1개")
    return rule(ctx, CheckStatus.UNKNOWN, {"finished": True}, o,
                "시간 안에 응답 완료를 확인하지 못함" + o.get("diagnosis", ""))


# --- 6) 응답 저장 (LOCAL_CHANGE) ----------------------------------------------------
def _out_path(ctx: RunContext, env: StepEnv) -> Path:
    return Path(env.adapter("job").output_dir) / f"{ctx.business_key}.md"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_response(ctx: RunContext, env: StepEnv) -> ActionResult:
    c = _c(env)
    try:
        text = c.copy_reply()
    except PreconditionFailed as e:
        a = c.diagnose(ctx, env, "Need exactly one reply icon row to click its copy button, but template "
                                 "matching did not find exactly one.", _prompt(ctx))
        raise UserActionRequired(f"{e}{describe_ko(a)}", "Claude 화면을 확인한 뒤 resolve done 하세요.") from e
    if text is None or not text.strip():
        raise PreconditionFailed("응답 복사 결과가 비어 있음")
    path = _out_path(ctx, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise PreconditionFailed(f"같은 request_id의 다른 응답 파일이 이미 있습니다: {path} (덮어쓰지 않음)")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    env.add_evidence("file", str(path))
    shot = c.capture(Path(ctx.run_dir) / "save_response" / "response.png")
    env.add_evidence("screen", shot["path"])
    return ActionResult("save_response", DispatchState.DISPATCHED,
                        outputs={"path": str(path), "chars": len(text), "sha256": _sha(text)})


def verify_save(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    path = Path(action.outputs["path"])
    expected = {"sha256": action.outputs["sha256"]}
    if not path.exists():
        return rule(ctx, CheckStatus.FAIL, expected, {}, "응답 파일이 없음")
    text = path.read_text(encoding="utf-8")
    observed = {"sha256": _sha(text), "chars": len(text)}
    if observed["sha256"] != expected["sha256"]:
        return rule(ctx, CheckStatus.FAIL, expected, observed, "저장 파일 내용이 복사한 응답과 다름")
    if text.strip() == _prompt(ctx).strip():
        return rule(ctx, CheckStatus.FAIL, expected, observed, "복사된 내용이 응답이 아니라 보낸 프롬프트임")
    return rule(ctx, CheckStatus.PASS, expected, observed, "응답 파일을 다시 읽어 복사 내용과 일치 확인")


def reconcile_save(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    path = _out_path(ctx, env)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        text = path.read_text(encoding="utf-8")
        return ReconcileResult(ReconcileStatus.APPLIED, {"path": str(path), "chars": len(text), "sha256": _sha(text)})
    return ReconcileResult(ReconcileStatus.NOT_APPLIED, reason="응답 파일 없음 — 다시 복사 가능")


def build() -> WorkflowDefinition:
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        version="2",
        description="Claude 데스크톱 앱: 새 대화에 프롬프트 전송 → 응답 완료 대기 → 응답을 파일로 저장 (화면 기반)",
        inputs=(
            InputField("request_id", pattern=r"[A-Za-z0-9_-]{1,64}", description="요청 식별자 (중복 전송 방지 키)"),
            InputField("prompt", description="보낼 프롬프트 (여러 줄 가능 — 줄바꿈은 Shift+Enter로 입력)"),
        ),
        business_key_field="request_id",
        source_files=(__file__, *APP.source_files()),
        environment_requirements=("windows", "unlocked_desktop", "claude_app_logged_in", "scale_125"),
        settings={"apps": [APP.app_id], "selectors": APP.selectors()},
        steps=(
            StepDefinition("open_claude", open_claude, verify_open, timeout=40, action_timeout=15),
            StepDefinition("new_chat", new_chat, verify_new_chat, precondition=require_app, timeout=30, action_timeout=15),
            StepDefinition(
                "enter_prompt", enter_prompt, verify_prompt, effect_kind=EffectKind.LOCAL_CHANGE,
                precondition=require_app, operation_key="prompt_input:{run_id}", reconcile=reconcile_prompt,
                retry_policy=RetryPolicy(max_attempts=2), timeout=120, action_timeout=15,
            ),
            StepDefinition(
                "send_prompt", send_prompt, verify_send, effect_kind=EffectKind.EXTERNAL_CHANGE,
                precondition=require_ready_to_send, operation_key="claude_send:{business_key}",
                reconcile=reconcile_send, timeout=60, action_timeout=25,
                manual_resolution=ManualResolution(
                    "Claude 앱에서 이 요청의 대화가 생성됐는지 확인한 뒤 resolve applied(전송됨) 또는 "
                    "not_applied(전송 안 됨)를 제출하세요. 확인 없이 다시 보내지 않습니다."
                ),
            ),
            StepDefinition("wait_response", wait_response, verify_wait, precondition=require_app,
                           timeout=700, action_timeout=300),
            StepDefinition(
                "save_response", save_response, verify_save, effect_kind=EffectKind.LOCAL_CHANGE,
                precondition=require_app, operation_key="save_response:{business_key}", reconcile=reconcile_save,
                retry_policy=RetryPolicy(max_attempts=2), timeout=30, action_timeout=10,
            ),
        ),
    )


registry.register(WORKFLOW_ID, build, make_adapters)
