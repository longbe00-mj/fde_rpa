"""실행 결과 리포트 (rpa report / --report)."""

from __future__ import annotations

import json
from pathlib import Path

from rpa.cli import main
from rpa.core.report import build_report, render_markdown, write_report


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_success_report_has_verdict_deliverable_and_steps(h):
    run_id = h.run("DEMO-002")
    rep = build_report(h.store, run_id, h.data_dir)
    assert rep["verdict"] == "SUCCESS"
    assert rep["run"]["business_key"] == "DEMO-002"
    assert [s["step_id"] for s in rep["steps"]] == ["fetch_customer", "save_customer", "export_receipt"]
    assert all(s["check"] == "PASS" for s in rep["steps"])
    (d,) = rep["deliverables"]
    assert d["integrity"] == "OK" and d["path"].endswith("receipt_DEMO-002.json")
    # 한글·특수문자가 산출물 미리보기에 그대로 보존
    assert "김철수 & Co. <특수>" in d["preview"]
    assert rep["next_actions"] == []

    md = render_markdown(rep)
    assert md.startswith("# RPA 실행 결과 — demo_web_to_desktop / DEMO-002")
    assert "업무 완료 (자동 검증됨)" in md
    assert "receipt_DEMO-002.json" in md

    paths = write_report(rep)
    assert Path(paths["md"]).read_text(encoding="utf-8") == md
    assert json.loads(Path(paths["json"]).read_text(encoding="utf-8"))["verdict"] == "SUCCESS"
    assert Path(paths["md"]).parent == Path(rep["run"]["run_dir"]).resolve()


def test_waiting_user_report_lists_next_actions_and_unrun_steps(h):
    h.new_engine(faults={"web.get_customer": ["login_expired"]})
    run_id = h.run("DEMO-001")
    rep = build_report(h.store, run_id, h.data_dir)
    assert rep["verdict"] == "NEEDS_USER"
    assert any("rpa resolve " + run_id in a for a in rep["next_actions"])
    by_id = {s["step_id"]: s for s in rep["steps"]}
    assert by_id["save_customer"]["check"] == "NOT_RUN"
    assert by_id["save_customer"]["effect_kind"] == "EXTERNAL_CHANGE"
    assert "## 다음 할 일" in render_markdown(rep)


def test_changed_deliverable_is_flagged(h):
    run_id = h.run("DEMO-001")
    path = Path(build_report(h.store, run_id, h.data_dir)["deliverables"][0]["path"])
    path.write_text("{}", encoding="utf-8")
    rep = build_report(h.store, run_id, h.data_dir)
    assert rep["deliverables"][0]["integrity"] == "CHANGED_AFTER_RUN"
    assert any("CHANGED_AFTER_RUN" in a for a in rep["next_actions"])


def test_markdown_escapes_table_cells_and_long_inputs():
    from rpa.core.report import _clip, _md_cell

    assert _md_cell("a|b\nc") == "a\\|b c"
    assert _clip("x" * 600).endswith("(총 600자)")


def test_cli_run_with_report_and_report_command(tmp_path, capsys):
    base = ["--data-dir", str(tmp_path), "--json", "--no-hotkeys"]
    assert main(base + ["run", "demo_web_to_desktop", "-i", "customer_id=DEMO-001", "--report"]) == 0
    events = _events(capsys)
    assert [e["type"] for e in events[-2:]] == ["summary", "report"]
    rep_event = events[-1]
    assert rep_event["verdict"] == "SUCCESS"
    assert Path(rep_event["paths"]["md"]).is_file()
    assert rep_event["deliverables"] and Path(rep_event["deliverables"][0]).is_file()

    # run_id 생략 시 최근 실행, --stdout --json이면 전체 리포트 JSON
    assert main(base + ["report", "--stdout"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["run"]["run_id"] == rep_event["run_id"]


def test_cli_workflows_json(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path), "--json", "workflows"]) == 0
    items = {e["id"]: e for e in _events(capsys)}
    assert items["demo_web_to_desktop"]["changes_data"] is True
    assert items["claude_prompt"]["inputs"][0]["name"] == "request_id"


def test_default_report_dir_follows_env(monkeypatch, tmp_path):
    from rpa.cli import default_report_dir

    monkeypatch.delenv("LOCALRPA_REPORT_DIR", raising=False)
    assert default_report_dir("R1") is None  # 미설정이면 실행 폴더
    monkeypatch.setenv("LOCALRPA_REPORT_DIR", str(tmp_path / "report"))
    assert default_report_dir("R1") == tmp_path / "report" / "R1"
