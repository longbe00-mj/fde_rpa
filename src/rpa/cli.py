"""LocalRPA CLI (1단계: FakeAdapter로 엔진 검증).

stdout: 사람이 읽는 출력 또는 --json 시 JSON 이벤트(한 줄에 하나). 일반 로그는 stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

from rpa.core import registry
from rpa.core.control import ControlState
from rpa.core.errors import RpaError


def default_data_dir() -> Path:
    if os.environ.get("LOCALRPA_HOME"):
        return Path(os.environ["LOCALRPA_HOME"])
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "LocalRPA"


def default_report_dir(run_id: str) -> Optional[Path]:
    """LOCALRPA_REPORT_DIR가 있으면 <그 폴더>/<run_id>, 없으면 None(=실행 폴더)."""
    root = os.environ.get("LOCALRPA_REPORT_DIR")
    return Path(root) / run_id if root else None


def _parse_kv(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"{what} 형식은 key=value 입니다: {item}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _parse_faults(items: list[str]) -> dict[str, list[str]]:
    faults: dict[str, list[str]] = {}
    for k, v in _parse_kv(items, "--fault").items():
        faults.setdefault(k, []).extend(v.split(","))
    return faults


class _Printer:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def event(self, e: dict[str, Any]) -> None:
        if self.as_json:
            print(json.dumps(e, ensure_ascii=False), flush=True)
            return
        t = e["type"]
        if t == "status":
            line = f"[상태] {e['execution_status']}"
            if e.get("business_outcome"):
                line += f" / 업무 결과 {e['business_outcome']}"
            if e.get("reason"):
                line += f" — {e['reason']}"
        elif t == "waiting_user":
            line = f"[사용자 조치 필요] {e['reason']}\n    → {e['instruction']}"
        elif t == "step_notice":
            line = f"[안내] {e['step_id']}: {e['message']}"
        elif t in ("step_started", "step_passed", "step_failed", "step_retry"):
            extra = {k: v for k, v in e.items() if k not in ("seq", "ts", "run_id", "type", "step_id", "check")}
            line = f"[{t}] {e['step_id']} {extra if extra else ''}".rstrip()
        else:
            extra = {k: v for k, v in e.items() if k not in ("seq", "ts", "run_id", "type")}
            line = f"[{t}] {extra}"
        print(line, flush=True)


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.data_dir) if args.data_dir else default_data_dir()


def _store(args: argparse.Namespace):
    from rpa.core.run_store import RunStore

    return RunStore(_data_dir(args) / "runs.db")


def _engine(
    args: argparse.Namespace, printer: _Printer, workflow_id: str, faults: Optional[dict[str, list[str]]] = None
):
    from rpa.core.engine import Engine

    data_dir = _data_dir(args)
    store = _store(args)
    control = ControlState()
    adapters = registry.adapters_for(workflow_id, data_dir, control, faults)
    engine = Engine(store, adapters, data_dir, control=control, event_sink=printer.event)
    return engine, store, control


def _workflow_of(store, run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        raise SystemExit(f"실행을 찾을 수 없습니다: {run_id}")
    return run["workflow_id"]


class _ControlInputs:
    """Ctrl+C와 전역 단축키(Windows)를 제어 플래그에 연결. 업무 키 입력은 수집하지 않는다."""

    def __init__(self, control: ControlState, use_hotkeys: bool) -> None:
        self.control = control
        self.hotkeys = None
        if use_hotkeys and sys.platform == "win32":
            from rpa.platform.windows.hotkeys import ControlHotkeys

            self.hotkeys = ControlHotkeys(control, notify=lambda m: print(m, file=sys.stderr, flush=True))

    def __enter__(self) -> "_ControlInputs":
        def handler(signum, frame):  # noqa: ARG001
            print("취소 요청: 다음 동작 경계에서 멈춥니다 (이미 전송된 입력은 취소되지 않음)", file=sys.stderr, flush=True)
            self.control.request_cancel()

        signal.signal(signal.SIGINT, handler)
        if self.hotkeys:
            self.hotkeys.start()
            for msg in self.hotkeys.registered:
                print(f"단축키: {msg}", file=sys.stderr)
            for msg in self.hotkeys.errors:
                print(f"경고: {msg}", file=sys.stderr)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.hotkeys:
            self.hotkeys.stop()


def _summary(store, run_id: str, printer: _Printer) -> int:
    run = store.get_run(run_id)
    if printer.as_json:
        print(json.dumps({"type": "summary", "run_id": run_id, "execution_status": run["execution_status"],
                          "business_outcome": run["business_outcome"], "reason": run["status_reason"]},
                         ensure_ascii=False), flush=True)
    else:
        print(f"\n실행 {run_id}: {run['execution_status']} / 업무 결과 {run['business_outcome']}"
              f"{' — ' + run['status_reason'] if run['status_reason'] else ''}")
    return {"FINISHED": 0, "WAITING_USER": 3, "CANCELLED": 4}.get(run["execution_status"], 1)


def _emit_report(args: argparse.Namespace, store, run_id: str, printer: _Printer) -> dict[str, str]:
    """실행 결과 리포트(report.md/report.json)를 실행 폴더에 쓰고 위치를 알린다."""
    from rpa.core.report import build_report, write_report

    report = build_report(store, run_id, _data_dir(args), preview_chars=getattr(args, "preview_chars", 4000))
    out_dir = getattr(args, "out_dir", None) or default_report_dir(run_id)
    paths = write_report(report, out_dir)
    if printer.as_json:
        print(json.dumps({"type": "report", "run_id": run_id, "verdict": report["verdict"],
                          "headline": report["headline"], "paths": paths,
                          "deliverables": [d["path"] for d in report["deliverables"]],
                          "next_actions": report["next_actions"]}, ensure_ascii=False), flush=True)
    else:
        print(f"리포트: {paths['md']}")
    return paths


def _finish_cmd(args: argparse.Namespace, store, run_id: str, printer: _Printer) -> int:
    code = _summary(store, run_id, printer)
    if getattr(args, "report", False):
        _emit_report(args, store, run_id, printer)
    return code


def _with_crash_guard(fn, store, printer: _Printer) -> int:
    from rpa.adapters.fake import SimulatedCrash

    try:
        return fn()
    except SimulatedCrash as e:
        run = store.unfinished_run()
        rid = run["run_id"] if run else "?"
        print(f"\n[시뮬레이션] 프로세스 강제 종료: {e}\n실행 {rid}는 {run['execution_status'] if run else '?'} 상태로 남았습니다. "
              f"'resume {rid}'로 대조 후 재개하세요.", file=sys.stderr)
        return 70


def cmd_workflows(args: argparse.Namespace) -> int:
    if args.json:
        for wid in registry.list_ids():
            wf = registry.get(wid)
            print(json.dumps({
                "type": "workflow", "id": wid, "version": wf.version, "description": wf.description,
                "inputs": [{"name": f.name, "type": f.type, "required": f.required, "pattern": f.pattern,
                            "description": f.description} for f in wf.inputs],
                "steps": [{"id": s.id, "effect_kind": s.effect_kind.value} for s in wf.steps],
                "changes_data": any(s.effect_kind.is_change for s in wf.steps),
                "environment_requirements": list(wf.environment_requirements),
            }, ensure_ascii=False), flush=True)
        return 0
    for wid in registry.list_ids():
        wf = registry.get(wid)
        print(f"{wid} (v{wf.version}) — {wf.description}")
        for f in wf.inputs:
            print(f"    입력 {f.name}: {f.type}{' 필수' if f.required else ''}{' 형식 ' + f.pattern if f.pattern else ''}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    printer = _Printer(args.json)
    wf = registry.get(args.workflow)
    engine, store, control = _engine(args, printer, wf.id, _parse_faults(args.fault))
    inputs = _parse_kv(args.input, "--input")

    def go() -> int:
        with _ControlInputs(control, not args.no_hotkeys):
            run_id = engine.start(wf, inputs)
        return _finish_cmd(args, store, run_id, printer)

    return _with_crash_guard(go, store, printer)


def cmd_resume(args: argparse.Namespace) -> int:
    printer = _Printer(args.json)
    wf = registry.get(_workflow_of(_store(args), args.run_id))
    engine, store, control = _engine(args, printer, wf.id, _parse_faults(args.fault))

    def go() -> int:
        with _ControlInputs(control, not args.no_hotkeys):
            engine.resume(wf, args.run_id, accept_new_version=args.accept_new_version)
        return _finish_cmd(args, store, args.run_id, printer)

    return _with_crash_guard(go, store, printer)


def cmd_resolve(args: argparse.Namespace) -> int:
    printer = _Printer(args.json)
    wf = registry.get(_workflow_of(_store(args), args.run_id))
    engine, store, control = _engine(args, printer, wf.id)
    outputs = json.loads(args.outputs) if args.outputs else None
    engine.resolve(wf, args.run_id, args.resolution, args.note, outputs)
    if args.resume:
        with _ControlInputs(control, not args.no_hotkeys):
            engine.resume(wf, args.run_id)
    return _finish_cmd(args, store, args.run_id, printer)


def cmd_cancel(args: argparse.Namespace) -> int:
    printer = _Printer(args.json)
    wf_id = _workflow_of(_store(args), args.run_id)
    engine, store, _ = _engine(args, printer, wf_id)
    engine.cancel_unfinished(args.run_id, args.reason)
    return _summary(store, args.run_id, printer)


def cmd_report(args: argparse.Namespace) -> int:
    from rpa.core.report import build_report, render_markdown, write_report

    printer = _Printer(args.json)
    store = _store(args)
    run_id = args.run_id
    if not run_id:
        runs = store.list_runs(limit=1)
        if not runs:
            raise SystemExit("실행 기록이 없습니다")
        run_id = runs[0]["run_id"]
    if not store.get_run(run_id):
        raise SystemExit(f"실행을 찾을 수 없습니다: {run_id}")
    if args.stdout:
        report = build_report(store, run_id, _data_dir(args), preview_chars=args.preview_chars)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_markdown(report))
        return 0
    _emit_report(args, store, run_id, printer)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    if not args.run_id:
        for r in store.list_runs():
            print(f"{r['run_id']}  {r['workflow_id']}  {r['business_key']}  {r['execution_status']:<14} "
                  f"{r['business_outcome']:<16} {r['status_reason'] or ''}")
        return 0
    run = store.get_run(args.run_id)
    if not run:
        raise SystemExit(f"실행을 찾을 수 없습니다: {args.run_id}")
    detail = {
        "run": {k: v for k, v in run.items() if k != "context_json"},
        "context": json.loads(run["context_json"]),
        "operations": store.operations(args.run_id),
        "attempts": store.attempts(args.run_id),
        "interventions": store.interventions(args.run_id),
        "evidence": store.evidence(args.run_id),
    }
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("preflight는 Windows에서만 동작합니다", file=sys.stderr)
        return 2
    from rpa.platform.windows.preflight import run_preflight

    result = run_preflight(probe_capture=not args.no_capture)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        for c in result.checks:
            print(f"[{c.level:<4}] {c.id}: {c.message}")
        p = result.screenshot_plan
        if p:
            print(f"\nComputer Use 스크린샷: {p['source_width']}x{p['source_height']} → {p['width']}x{p['height']} "
                  f"(축소 {p['scale']:.4f}, visual token {p['visual_tokens']})")
            m = result.screenshot_plan_many_images
            print(f"  이미지 20장 초과 대화용: {m['width']}x{m['height']} (visual token {m['visual_tokens']})")
        print(f"\n판정: {'실행 가능' if result.ok else '실행 불가 (FAIL 항목 해결 필요)'}")
    return 0 if result.ok else 1


def cmd_vision_key(args: argparse.Namespace) -> int:
    """Anthropic API 키를 Windows 자격 증명 관리자에 저장/확인/삭제. 키 값은 출력하지 않는다."""
    import getpass

    import keyring

    from rpa.vision.interpreter import KEYRING_SERVICE, KEYRING_USER

    if args.delete:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            print("저장된 키를 삭제했습니다")
        except keyring.errors.PasswordDeleteError:
            print("저장된 키가 없습니다")
        return 0
    if args.check:
        print("저장된 키: " + ("있음" if keyring.get_password(KEYRING_SERVICE, KEYRING_USER) else "없음"))
        return 0
    key = getpass.getpass("Anthropic API 키 (입력 내용은 표시되지 않음): ").strip()
    if not key:
        print("입력이 없어 저장하지 않았습니다", file=sys.stderr)
        return 2
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    print(f"Windows 자격 증명 관리자에 저장했습니다 ({KEYRING_SERVICE}/{KEYRING_USER})")
    return 0


VISION_PROFILES = {"claude": ("rpa.apps.claude_desktop.vision_profile", "CLAUDE_PROFILE")}


def cmd_vision_eval(args: argparse.Namespace) -> int:
    """라벨된 스크린샷 세트로 화면 해석 정확도·비용·지연을 측정 (labels.json)."""
    import importlib

    from rpa.vision.contracts import ScreenObservation
    from rpa.vision.interpreter import ScreenInterpreter

    mod, attr = VISION_PROFILES[args.profile]
    profile = getattr(importlib.import_module(mod), attr)
    d = Path(args.dir)
    labels = json.loads((d / "labels.json").read_text(encoding="utf-8"))
    interp = ScreenInterpreter(model=args.model, effort=args.effort, max_calls=len(labels))
    ok, why = interp.available()
    if not ok:
        print(f"오류: {why}", file=sys.stderr)
        return 2
    results, correct, match_correct, match_total, inj_correct, inj_total = [], 0, 0, 0, 0, 0
    out_dir = d / "results" / time.strftime("%Y%m%d-%H%M%S")
    for item in labels:
        if interp.total_usage["estimated_usd"] >= args.max_usd:
            print(f"비용 상한 ${args.max_usd} 도달 — 중단", file=sys.stderr)
            break
        img = d / item["file"]
        from PIL import Image

        with Image.open(img) as im:
            w, h = im.size
        obs = ScreenObservation.from_capture(
            {"path": str(img), "origin_x": 0, "origin_y": 0, "width": w, "height": h, "captured_at": time.time()})
        a = interp.assess(obs, profile, item.get("situation", "Evaluation: classify the current screen."),
                          expected=item.get("expected", ""), save_dir=out_dir)
        hit = a.state == item["state"]
        correct += hit
        if "matches_expected" in item:
            match_total += 1
            match_correct += a.matches_expected == item["matches_expected"]
        if "screen_contains_instructions" in item:
            inj_total += 1
            inj_correct += a.screen_contains_instructions == item["screen_contains_instructions"]
        results.append({"file": item["file"], "label": item["state"], **a.to_dict(), "correct": hit})
        print(f"{'O' if hit else 'X'} {item['file']:<34} 정답 {item['state']:<22} 판정 {a.state:<22} "
              f"확신 {a.confidence:.2f} 일치 {a.matches_expected:<7} {a.latency_s:>5.1f}s "
              f"${a.usage.get('estimated_usd', 0):.4f}{'  ERR ' + a.error if a.error else ''}")
    n = len(results)
    summary = {
        "model": args.model, "effort": args.effort, "items": n,
        "state_accuracy": round(correct / n, 3) if n else None,
        "match_accuracy": round(match_correct / match_total, 3) if match_total else None,
        "instruction_detection": f"{inj_correct}/{inj_total}" if inj_total else None,
        "total_usage": interp.total_usage,
        "avg_latency_s": round(sum(r["latency_s"] for r in results) / n, 2) if n else None,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps({"summary": summary, "results": results},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n상태 정확도 {correct}/{n}  대상 일치 정확도 {match_correct}/{match_total}  "
          f"화면 지시문 감지 {inj_correct}/{inj_total}  "
          f"추정 비용 ${interp.total_usage['estimated_usd']:.4f}  평균 {summary['avg_latency_s']}s")
    print(f"결과: {out_dir / 'results.json'}")
    return 0


def cmd_apps(args: argparse.Namespace) -> int:
    """등록된 앱 커넥터와 그 앱을 쓰는 업무 목록."""
    from rpa.apps.base import list_apps

    uses: dict[str, list[str]] = {}
    for wid in registry.list_ids():
        for app_id in registry.get(wid).settings.get("apps", []):
            uses.setdefault(app_id, []).append(wid)
    for app in list_apps():
        s = app.summary() | {"workflows": uses.get(app.app_id, [])}
        if args.json:
            print(json.dumps(s, ensure_ascii=False), flush=True)
            continue
        env = s["environment"]
        env_txt = f" · 환경 {env.get('scale_percent')}% {env.get('theme', '')}".rstrip() if env else ""
        ver = f" · 검증 버전 {', '.join(s['verified_versions'])}" if s["verified_versions"] else ""
        print(f"{app.app_id:<16} [{app.kind}] {app.name}{ver}{env_txt}")
        print(f"    업무: {', '.join(s['workflows']) or '(없음)'}  ·  {app.package_dir}")
    return 0


def cmd_inspect_window(args: argparse.Namespace) -> int:
    """대상 창 조사 (읽기 전용): 기술·DPI·권한·UIA 노출 정도 → 조작 방식 추천."""
    if sys.platform != "win32":
        print("Windows에서만 동작합니다", file=sys.stderr)
        return 2
    from rpa.tools.inspect_window import inspect_windows

    if not (args.process or args.title or args.class_name):
        print("--process / --title / --class 중 하나 이상을 지정하세요", file=sys.stderr)
        return 2
    found = inspect_windows(args.process, args.title, args.class_name, uia=not args.no_uia)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0 if found else 1
    if not found:
        print("조건에 맞는 창이 없습니다")
        return 1
    for w in found:
        print(f"■ {w['title']!r}  ({w['process']} pid {w['pid']}, class {w['class_name']})")
        print(f"   경로 {w['image_path']}{'  · 패키지 버전 ' + w['package_version'] if w['package_version'] else ''}")
        print(f"   클라이언트 {w['client']['width']}x{w['client']['height']} @({w['client']['left']},{w['client']['top']}) "
              f"· DPI {w['dpi']} ({w['scale_percent']}%) · 관리자 권한 {w['elevated']} · Chromium {w['chromium']}")
        if w["minimized"]:
            print("   ⚠ 최소화 상태 — 크기·위치는 의미 없음. 창을 연 상태에서 다시 조사하세요")
        u = w.get("uia")
        if u and u.get("available"):
            top = ", ".join(f"{k} {v}" for k, v in list(u["control_types"].items())[:8])
            print(f"   UIA 노드 {u['nodes']}{'+' if u['truncated'] else ''} · 조작 후보 {u['actionable']} "
                  f"(이름 있는 표본 {u['named_count_capped']}) · {top}")
            for s in u["named_actionable_samples"][:8]:
                print(f"      - {s['control_type']:<10} name={s['name']!r} automation_id={s['automation_id']!r}")
        elif u:
            print(f"   UIA 조사 불가: {u.get('error')}")
        print(f"   ▶ 추천: {w['recommendation']} — {w['reason']}")
        print(f"   selectors.json window 초안: {json.dumps(w['window_spec'], ensure_ascii=False)}")
    return 0


def cmd_template_check(args: argparse.Namespace) -> int:
    """기준 이미지 교차 검증 (저장된 캡처 대상, 화면 조작 없음)."""
    from rpa.tools.template_check import check, render

    result = check([Path(t) for t in args.template], [Path(s) for s in args.screens], args.confidence)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="localrpa", description="Windows 로컬 RPA 실행기")
    p.add_argument("--data-dir", help="실행 DB·증거 저장 위치 (기본: %%LOCALAPPDATA%%\\LocalRPA 또는 LOCALRPA_HOME)")
    p.add_argument("--json", action="store_true", help="stdout에 JSON 이벤트만 출력")
    p.add_argument("--no-hotkeys", action="store_true",
                   help="전역 단축키(Ctrl+Alt+Q 취소, Ctrl+Alt+P 일시정지/재개) 등록 안 함")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("workflows", help="등록된 업무 목록").set_defaults(fn=cmd_workflows)

    r = sub.add_parser("run", help="업무 실행")
    r.add_argument("workflow")
    r.add_argument("--input", "-i", action="append", default=[], help="입력 key=value")
    r.add_argument("--fault", action="append", default=[], help="장애 주입 (FakeAdapter) op=kind[,kind]")
    r.add_argument("--report", action="store_true", help="끝난 뒤 실행 결과 리포트(report.md/json) 생성")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("resume", help="미완료 실행 재개 (대조 포함)")
    s.add_argument("run_id")
    s.add_argument("--accept-new-version", action="store_true", help="코드/설정 변경을 명시적으로 수용")
    s.add_argument("--fault", action="append", default=[])
    s.add_argument("--report", action="store_true", help="끝난 뒤 실행 결과 리포트 생성")
    s.set_defaults(fn=cmd_resume)

    v = sub.add_parser("resolve", help="사용자 조치 결과 제출")
    v.add_argument("run_id")
    v.add_argument("--resolution", required=True, choices=["done", "applied", "not_applied"])
    v.add_argument("--note", required=True, help="확인한 사실·근거")
    v.add_argument("--outputs", help="applied 시 확인한 결과값 JSON (예: '{\"record_id\": \"REC-0001\"}')")
    v.add_argument("--resume", action="store_true", help="해결 후 바로 재개")
    v.add_argument("--report", action="store_true", help="끝난 뒤 실행 결과 리포트 생성")
    v.set_defaults(fn=cmd_resolve)

    c = sub.add_parser("cancel", help="Runner가 없는 미완료 실행을 취소로 마감")
    c.add_argument("run_id")
    c.add_argument("--reason", default="사용자 취소")
    c.set_defaults(fn=cmd_cancel)

    st = sub.add_parser("status", help="실행 목록 또는 상세")
    st.add_argument("run_id", nargs="?")
    st.set_defaults(fn=cmd_status)

    rp = sub.add_parser("report", help="실행 결과 리포트 생성 (실행 폴더에 report.md/report.json)")
    rp.add_argument("run_id", nargs="?", help="생략하면 가장 최근 실행")
    rp.add_argument("--stdout", action="store_true", help="파일로 쓰지 않고 출력만 (--json이면 전체 JSON)")
    rp.add_argument("--out-dir", help="리포트 저장 폴더 (기본: LOCALRPA_REPORT_DIR\\<run_id> 또는 실행 폴더)")
    rp.add_argument("--preview-chars", type=int, default=4000, help="산출물 텍스트 미리보기 글자 수 (0이면 생략)")
    rp.set_defaults(fn=cmd_report)

    pf = sub.add_parser("preflight", help="실제 화면 측정 기반 환경 점검")
    pf.add_argument("--no-capture", action="store_true", help="화면 캡처 점검 생략")
    pf.set_defaults(fn=cmd_preflight)

    vk = sub.add_parser("vision-key", help="화면 해석용 Anthropic API 키를 Windows 자격 증명 관리자에 저장")
    vk.add_argument("--check", action="store_true", help="저장 여부만 확인")
    vk.add_argument("--delete", action="store_true", help="저장된 키 삭제")
    vk.set_defaults(fn=cmd_vision_key)

    ve = sub.add_parser("vision-eval", help="라벨된 스크린샷으로 화면 해석 정확도·비용 측정 (실제 API 호출)")
    ve.add_argument("dir", help="labels.json과 이미지가 있는 폴더")
    ve.add_argument("--profile", default="claude", choices=sorted(VISION_PROFILES))
    ve.add_argument("--model", default="claude-opus-5-5")
    ve.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ve.add_argument("--max-usd", type=float, default=1.0, help="이 금액(추정)을 넘으면 중단")
    ve.set_defaults(fn=cmd_vision_eval)

    ap = sub.add_parser("apps", help="등록된 앱 커넥터 목록 (검증 버전·환경·사용 업무)")
    ap.set_defaults(fn=cmd_apps)

    iw = sub.add_parser("inspect-window", help="새 앱 조사: 창 기술·DPI·권한·UIA 노출 → 조작 방식 추천 (읽기 전용)")
    iw.add_argument("--process", help="프로세스 이름 (예: notepad.exe)")
    iw.add_argument("--title", help="창 제목 정규식")
    iw.add_argument("--class", dest="class_name", help="창 클래스 이름")
    iw.add_argument("--no-uia", action="store_true", help="UIA 트리 조사 생략")
    iw.set_defaults(fn=cmd_inspect_window)

    tc = sub.add_parser("template-check", help="기준 이미지 교차 검증: 각 캡처에서 몇 곳 찾아지는지 표로 출력")
    tc.add_argument("screens", nargs="+", help="상태별 화면 캡처 파일들")
    tc.add_argument("--template", "-t", action="append", required=True, help="기준 이미지 (여러 번 지정)")
    tc.add_argument("--confidence", type=float, default=0.9)
    tc.set_defaults(fn=cmd_template_check)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except RpaError as e:
        print(f"오류: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
