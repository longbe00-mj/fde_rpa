"""실행 결과 리포트 (전달용).

실행이 끝난 뒤(또는 사용자 조치 대기·실패로 멈춘 뒤) 요청자에게 결과를 전달하기 위한 문서를 만든다.
- 결론(성공/조치 필요/실패)과 다음 할 일을 맨 앞에 둔다.
- 산출물(kind=file 증거)은 경로·무결성(sha256 재확인)·텍스트 미리보기를 함께 싣는다.
- 실행 상태(execution_status)와 업무 결과(business_outcome)를 섞지 않고 둘 다 표시한다.

GUI 라이브러리를 import하지 않는다 (core 규칙). 저장소(RunStore)만 읽고, 파일은 run_dir에만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from rpa.core.contracts import now_iso

REPORT_VERSION = 1
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".log", ".xml", ".html", ".yaml", ".yml"}
DEFAULT_PREVIEW_CHARS = 4000
INPUT_VALUE_LIMIT = 500

# (execution_status, business_outcome) -> (verdict, 제목)
_STATUS_LABEL = {
    "FINISHED": "완료",
    "WAITING_USER": "사용자 조치 대기",
    "CANCELLED": "취소됨",
    "FAILED": "실패",
    "RUNNING": "실행 중 또는 비정상 종료",
    "CREATED": "생성됨 (실행 전)",
    "PAUSED": "일시정지",
    "PAUSE_REQUESTED": "일시정지 요청됨",
    "CANCEL_REQUESTED": "취소 요청됨",
}
_OUTCOME_LABEL = {
    "AUTO_VERIFIED": "자동 검증됨",
    "HUMAN_CONFIRMED": "사용자 확인됨",
    "UNVERIFIED": "검증 안 됨",
    "MISMATCH": "불일치",
    "UNKNOWN": "결과 불명",
}
_CHECK_LABEL = {"PASS": "통과", "FAIL": "실패", "UNKNOWN": "불명"}


def _verdict(status: str, outcome: str) -> tuple[str, str]:
    if status == "FINISHED":
        if outcome in ("AUTO_VERIFIED", "HUMAN_CONFIRMED"):
            return "SUCCESS", f"업무 완료 ({_OUTCOME_LABEL[outcome]})"
        return "COMPLETED_UNVERIFIED", f"실행은 끝났으나 업무 결과가 확인되지 않음 ({_OUTCOME_LABEL.get(outcome, outcome)})"
    if status == "WAITING_USER":
        return "NEEDS_USER", "사용자 조치가 필요해 멈춤"
    if status == "CANCELLED":
        return "CANCELLED", "실행이 취소됨"
    if status == "FAILED":
        return "FAILED", "실행 실패"
    return "INCOMPLETE", f"실행이 끝나지 않음 ({_STATUS_LABEL.get(status, status)}) — resume으로 대조 후 재개 필요"


def _resolve_path(data_dir: Path, rel_path: str) -> Path:
    p = Path(rel_path)
    return p if p.is_absolute() else Path(data_dir) / p


def _sha256(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _preview(p: Path, limit: int) -> tuple[Optional[str], bool]:
    if limit <= 0 or p.suffix.lower() not in TEXT_SUFFIXES:
        return None, False
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, False
    text = text.replace("\r\n", "\n")
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _clip(value: Any, limit: int = INPUT_VALUE_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… (총 {len(value)}자)"
    return value


def _workflow_steps(workflow_id: str) -> tuple[list[str], dict[str, str], Optional[str]]:
    """등록된 workflow의 단계 순서·변경 종류·설명. 등록이 사라졌으면 빈 값."""
    try:
        from rpa.core import registry

        wf = registry.get(workflow_id)
    except Exception:  # noqa: BLE001 - 리포트는 workflow 로드 실패와 무관하게 만들어져야 한다
        return [], {}, None
    return [s.id for s in wf.steps], {s.id: s.effect_kind.value for s in wf.steps}, wf.description


def build_report(store, run_id: str, data_dir: Path, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        raise KeyError(f"실행을 찾을 수 없습니다: {run_id}")
    data_dir = Path(data_dir)
    ctx = json.loads(run["context_json"])
    status, outcome = run["execution_status"], run["business_outcome"]
    verdict, headline = _verdict(status, outcome)

    order, effects, description = _workflow_steps(run["workflow_id"])
    attempts = store.attempts(run_id)
    by_step: dict[str, list[dict[str, Any]]] = {}
    for a in attempts:
        by_step.setdefault(a["step_id"], []).append(a)
    for sid in by_step:
        if sid not in order:
            order.append(sid)
    checks = ctx.get("step_checks", {})
    steps = []
    for sid in order:
        tries = by_step.get(sid, [])
        last = tries[-1] if tries else None
        reason = ""
        if last and last.get("check_json"):
            reason = json.loads(last["check_json"]).get("reason", "")
        chk = checks.get(sid, {})
        steps.append({
            "step_id": sid,
            "effect_kind": effects.get(sid),
            "check": chk.get("status") or ("NOT_RUN" if not tries else None),
            "verification_mode": chk.get("mode"),
            "attempts": len(tries),
            "last_attempt_status": last["status"] if last else None,
            "error": last.get("error") if last else None,
            "check_reason": reason,
        })

    evidence, deliverables, failure_screens = [], [], []
    for e in store.evidence(run_id):
        p = _resolve_path(data_dir, e["rel_path"])
        item = {"step_id": e["step_id"], "kind": e["kind"], "path": str(p), "exists": p.is_file(),
                "sha256": e["sha256"], "created_at": e["created_at"]}
        evidence.append(item)
        if e["kind"] == "failure_screen":
            failure_screens.append(str(p))
        if e["kind"] == "file":
            current = _sha256(p) if p.is_file() else None
            text, truncated = _preview(p, preview_chars) if p.is_file() else (None, False)
            deliverables.append({
                **item,
                "size": p.stat().st_size if p.is_file() else None,
                "integrity": ("MISSING" if current is None else
                              "OK" if e["sha256"] in (None, current) else "CHANGED_AFTER_RUN"),
                "preview": text,
                "preview_truncated": truncated,
            })

    interventions = store.interventions(run_id)
    operations = [{k: o[k] for k in ("step_id", "dispatch_state", "dispatch_count", "target", "updated_at")}
                  for o in store.operations(run_id)]

    next_actions: list[str] = []
    open_itv = next((i for i in reversed(interventions) if not i["resolved_at"]), None)
    if verdict == "NEEDS_USER" and open_itv:
        next_actions.append(f"[{open_itv['step_id'] or '-'}] {open_itv['reason']}")
        if open_itv.get("instruction"):
            next_actions.append(f"조치 안내: {open_itv['instruction']}")
        next_actions.append(
            f"조치 후: rpa resolve {run_id} --resolution done|applied|not_applied --note \"확인한 사실\" --resume"
        )
    elif verdict == "INCOMPLETE":
        next_actions.append(f"rpa resume {run_id} 로 대조 후 재개하거나, rpa cancel {run_id} 로 마감")
    elif verdict == "FAILED":
        next_actions.append("실패 단계의 오류와 실패 화면을 확인한 뒤 원인을 해소하고 새로 실행")
    elif verdict == "COMPLETED_UNVERIFIED":
        next_actions.append("산출물과 대상 시스템을 직접 확인해 업무 반영 여부를 판단")
    for d in deliverables:
        if d["integrity"] != "OK":
            next_actions.append(f"산출물 확인 필요 ({d['integrity']}): {d['path']}")

    return {
        "report_version": REPORT_VERSION,
        "generated_at": now_iso(),
        "verdict": verdict,
        "headline": headline,
        "run": {
            "run_id": run_id,
            "workflow_id": run["workflow_id"],
            "workflow_description": description,
            "workflow_version": run["workflow_version"],
            "workflow_code_hash": run["workflow_code_hash"],
            "business_key": run["business_key"],
            "execution_status": status,
            "business_outcome": outcome,
            "status_reason": run["status_reason"],
            "current_step": run["current_step"],
            "last_checkpoint": run["last_checkpoint"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "run_dir": ctx.get("run_dir"),
        },
        "inputs": ctx.get("validated_inputs", {}),
        "outputs": ctx.get("step_outputs", {}),
        "deliverables": deliverables,
        "steps": steps,
        "next_actions": next_actions,
        "failure_screens": failure_screens,
        "interventions": interventions,
        "operations": operations,
        "evidence": evidence,
    }


def _md_cell(value: Any) -> str:
    s = "" if value is None else str(value)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _fence(text: str, lang: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{lang}\n{text}\n{fence}"


def render_markdown(report: dict[str, Any]) -> str:
    r = report["run"]
    out: list[str] = []
    out.append(f"# RPA 실행 결과 — {r['workflow_id']} / {r['business_key'] or r['run_id']}")
    out.append("")
    out.append(f"**결론: {report['headline']}** (`{report['verdict']}`)")
    out.append("")
    out.append(f"- 실행 상태: {_STATUS_LABEL.get(r['execution_status'], r['execution_status'])} "
               f"(`{r['execution_status']}`){' — ' + r['status_reason'] if r['status_reason'] else ''}")
    out.append(f"- 업무 결과: {_OUTCOME_LABEL.get(r['business_outcome'], r['business_outcome'])} "
               f"(`{r['business_outcome']}`)")
    out.append(f"- 업무: {r['workflow_description'] or r['workflow_id']} (v{r['workflow_version']})")
    out.append(f"- 실행 ID: `{r['run_id']}` · 시작 {r['created_at']} · 마지막 갱신 {r['updated_at']}")

    if report["next_actions"]:
        out += ["", "## 다음 할 일", ""]
        out += [f"- {a}" for a in report["next_actions"]]

    out += ["", "## 산출물", ""]
    if not report["deliverables"]:
        out.append("- 없음")
    for d in report["deliverables"]:
        size = f"{d['size']:,} bytes" if d["size"] is not None else "-"
        out.append(f"- `{d['path']}` ({d['step_id']}, {size}, 무결성 {d['integrity']})")
    for d in report["deliverables"]:
        if d["preview"] is not None:
            lang = {".json": "json", ".md": "markdown"}.get(Path(d["path"]).suffix.lower(), "")
            title = Path(d["path"]).name + (" (앞부분만)" if d["preview_truncated"] else "")
            out += ["", f"### {title}", "", _fence(d["preview"], lang)]

    if report["inputs"]:
        out += ["", "## 입력", "", "| 항목 | 값 |", "|---|---|"]
        out += [f"| {_md_cell(k)} | {_md_cell(_clip(v))} |" for k, v in report["inputs"].items()]

    out += ["", "## 단계별 결과", "", "| 단계 | 종류 | 검증 | 시도 | 마지막 상태 | 근거·오류 |", "|---|---|---|---|---|---|"]
    for s in report["steps"]:
        chk = "미실행" if s["check"] == "NOT_RUN" else _CHECK_LABEL.get(s["check"], s["check"] or "-")
        if s["verification_mode"]:
            chk += f" ({s['verification_mode']})"
        note = s["error"] or s["check_reason"]
        out.append(f"| {_md_cell(s['step_id'])} | {_md_cell(s['effect_kind'] or '-')} | {_md_cell(chk)} | "
                   f"{s['attempts']} | {_md_cell(s['last_attempt_status'] or '-')} | {_md_cell(_clip(note, 200))} |")

    outputs = {k: {kk: _clip(vv, 200) for kk, vv in v.items()} for k, v in report["outputs"].items() if v}
    if outputs:
        out += ["", "## 단계 출력값", "", _fence(json.dumps(outputs, ensure_ascii=False, indent=2), "json")]

    if report["interventions"]:
        out += ["", "## 사용자 조치 이력", ""]
        for i in report["interventions"]:
            state = f"해결 {i['resolved_at']} — {i['resolution']}: {i['evidence_note']}" if i["resolved_at"] else "미해결"
            out.append(f"- [{i['step_id'] or '-'}] {i['reason']} (요청 {i['requested_at']}, {state})")

    if report["failure_screens"]:
        out += ["", "## 실패 화면", ""]
        out += [f"- `{p}`" for p in report["failure_screens"]]

    others = [e for e in report["evidence"] if e["kind"] not in ("file", "failure_screen")]
    if others:
        out += ["", "## 기타 증거", ""]
        out += [f"- {e['kind']} · {e['step_id']} · `{e['path']}`{'' if e['exists'] else ' (파일 없음)'}" for e in others]

    if report["operations"]:
        out += ["", "## 변경 작업 기록", "", "| 단계 | 전송 상태 | 전송 횟수 |", "|---|---|---|"]
        out += [f"| {_md_cell(o['step_id'])} | {o['dispatch_state']} | {o['dispatch_count']} |"
                for o in report["operations"]]

    out += ["", "---",
            f"리포트 생성 {report['generated_at']} · workflow 코드 해시 `{r['workflow_code_hash'][:12]}` · "
            f"실행 폴더 `{r['run_dir']}`", ""]
    return "\n".join(out)


def write_report(report: dict[str, Any], out_dir: Optional[Path] = None) -> dict[str, str]:
    """report.md·report.json을 실행 폴더(또는 out_dir)에 원자적으로 쓴다."""
    target = Path(out_dir) if out_dir else Path(report["run"]["run_dir"])
    target.mkdir(parents=True, exist_ok=True)
    paths = {"md": target / "report.md", "json": target / "report.json"}
    bodies = {"md": render_markdown(report), "json": json.dumps(report, ensure_ascii=False, indent=2)}
    for key, path in paths.items():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(bodies[key], encoding="utf-8")
        tmp.replace(path)
    return {k: str(v.resolve()) for k, v in paths.items()}
