"""앱 커넥터 구조·Claude 커넥터 로직·조사 도구 (실제 화면·앱 없이)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from rpa.apps.base import AppInfo, list_apps
from rpa.core import registry
from rpa.core.control import ControlState
from rpa.core.errors import PreconditionFailed, UserActionRequired


# --- 구조 -------------------------------------------------------------------------
def test_every_app_has_selectors_and_assets():
    apps = list_apps()
    assert {a.app_id for a in apps} >= {"claude_desktop", "test_form", "web_portal"}
    for a in apps:
        sel = a.selectors()
        for t in sel.get("templates", {}).values():
            assert a.asset(t["file"]).is_file(), (a.app_id, t["file"])
        assert any(f.endswith("connector.py") for f in a.source_files())


def test_workflows_pin_app_knowledge_in_version_hash():
    """앱 커넥터 파일(선택자·기준 이미지)이 업무 버전 해시에 포함되어야 재개 시 변경이 감지된다."""
    for wid in registry.list_ids():
        wf = registry.get(wid)
        for app_id in wf.settings.get("apps", []):
            app = next(a for a in list_apps() if a.app_id == app_id)
            assert set(app.source_files()) <= set(wf.source_files), (wid, app_id)
        for f in wf.source_files:
            assert Path(f).is_file(), (wid, f)


def test_asset_path_cannot_escape_connector_dir(tmp_path):
    a = AppInfo("x", "x", "web", "x", tmp_path)
    with pytest.raises(ValueError):
        a.asset("../secret.txt")
    with pytest.raises(ValueError):
        AppInfo("x", "x", "mainframe", "x", tmp_path)


def test_workflows_do_not_contain_app_specific_labels():
    """업무 코드에 사이트 표기(표 머리글 등)가 새지 않았는지 — 커넥터가 정규화해서 넘긴다."""
    root = Path(registry.get("demo_web_portal").source_files[0])
    text = root.read_text(encoding="utf-8")
    # 안내 문장 속 단어는 괜찮다. 표 머리글을 dict 키로 쓰는 코드("고객ID")가 없어야 한다
    for header in ("고객ID", "이름", "전화", "접수번호", "메모", "첨부"):
        assert f'"{header}"' not in text and f"'{header}'" not in text, header


# --- Claude 커넥터 (가짜 화면) ------------------------------------------------------
@dataclass
class _Win:
    dpi: int = 120
    client: SimpleNamespace = None  # type: ignore[assignment]


class FakeDV:
    """기준 이미지 이름별 '화면에 보이는 개수'를 지정하는 가짜 DesktopVisualAdapter."""

    def __init__(self, visible: dict[str, int], dpi: int = 120):
        self.visible = visible
        self.dpi = dpi
        self.keys: list[tuple] = []
        self.clicks: list = []
        self.typed: list[str] = []
        self.editor = ""
        self.target = SimpleNamespace(hwnd=1, pid=1)
        self.win = SimpleNamespace(is_window=lambda h: True)
        self.minimized: list = []

    def find_images(self, path):
        n = self.visible.get(Path(path).stem, 0)
        return [SimpleNamespace(x=100 + i * 50, y=200, name=Path(path).stem) for i in range(n)]

    def key(self, *k):
        self.keys.append(k)

    def click(self, p):
        self.clicks.append(p)

    def type_multiline(self, text, newline):
        self.typed.append(text)
        self.editor = text

    def read_focused_text(self):
        return self.editor

    def copy_by_click(self, p):
        self.clicks.append(p)
        return "응답 본문\r\n둘째 줄"

    def refresh(self):
        return SimpleNamespace(dpi=self.dpi, client=SimpleNamespace(width=1920, height=1140))

    def activate(self, p=None):
        return self.refresh()


def _env(timeout: float = 1.0):
    import time

    return SimpleNamespace(control=ControlState(), call_timeout=lambda: timeout,
                           remaining=lambda: timeout, step_id="s", deadline=time.monotonic() + timeout)


def _claude(visible, dpi=120):
    from rpa.apps.claude_desktop.connector import ClaudeDesktop

    return ClaudeDesktop(FakeDV(visible, dpi))


def test_new_chat_reports_template_counts():
    c = _claude({"new_chat_placeholder": 1, "mode_chat_selected": 1})
    found = c.new_chat(_env())
    assert found == {"placeholder": 1, "chat_mode": 1, "action_bar": 0}
    assert c.dv.keys[0] == ("ctrl", "n")


def test_type_prompt_replaces_and_reads_back_multiline():
    c = _claude({"new_chat_placeholder": 1, "mode_chat_selected": 1})
    assert c.type_prompt("첫 줄\n둘째 줄 <특수> & 0012") == "첫 줄\n둘째 줄 <특수> & 0012"
    assert ("ctrl", "a") in c.dv.keys and len(c.dv.clicks) == 1  # 안내문 클릭으로 포커스


def test_focus_editor_without_placeholder_uses_mode_toggle_offset():
    c = _claude({"mode_chat_selected": 1})
    c.focus_editor()
    off = c.sel["editor_from_mode_toggle"]
    p = c.dv.clicks[0]
    assert (p.x, p.y) == (100 + off["dx"], 200 + off["dy"])


def test_focus_editor_refuses_when_mode_toggle_not_unique():
    c = _claude({"mode_chat_selected": 0})
    with pytest.raises(PreconditionFailed, match="정확히 한 곳"):
        c.focus_editor()
    assert c.dv.clicks == []


def test_send_acknowledged_by_stop_button():
    c = _claude({"stop_button": 1})
    assert c.send(_env()) == {"generating": True, "done": False}
    assert c.dv.keys[-1] == ("enter",)


def test_wait_reply_needs_exactly_one_reply_row_and_no_stop_button():
    assert _claude({"action_bar": 1}).wait_reply(_env(), 1)[0] is True
    assert _claude({"action_bar": 1, "stop_button": 1}).wait_reply(_env(0.3), 0.3)[0] is False
    assert _claude({"action_bar": 2}).wait_reply(_env(0.3), 0.3)[0] is False


def test_copy_reply_clicks_copy_offset_and_normalizes_newlines():
    c = _claude({"action_bar": 1})
    assert c.copy_reply() == "응답 본문\n둘째 줄"
    off = c.sel["templates"]["action_bar"]["copy_offset"]
    assert (c.dv.clicks[0].x, c.dv.clicks[0].y) == (100 + off["dx"], 200 + off["dy"])
    with pytest.raises(PreconditionFailed):
        _claude({"action_bar": 2}).copy_reply()


def test_ensure_ready_rejects_other_scale():
    c = _claude({}, dpi=96)
    with pytest.raises(PreconditionFailed, match="DPI 96"):
        c.ensure_ready(_env())


def test_diagnose_is_noop_when_vision_off():
    c = _claude({})
    assert c.vision is None and c.diagnose(SimpleNamespace(run_dir="."), _env(), "x") is None


# --- 조사 도구 ----------------------------------------------------------------------
def test_template_check_counts_unique_and_ambiguous(tmp_path):
    from rpa.tools.template_check import check, render

    rng = __import__("random").Random(1)
    tpl = Image.new("RGB", (24, 16))
    tpl.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(24 * 16)])
    d = tmp_path / "삼성"  # 한글 경로도 PIL로 읽어 처리
    d.mkdir()
    tpl.save(d / "btn.png")
    for name, n in (("none.png", 0), ("one.png", 1), ("two.png", 2)):
        img = Image.new("RGB", (300, 120), "white")
        for i in range(n):
            img.paste(tpl, (20 + i * 120, 40))
        img.save(d / name)
    r = check([d / "btn.png"], [d / "none.png", d / "one.png", d / "two.png"], 0.9)
    row = r["templates"][0]
    assert row["counts"] == {"none.png": 0, "one.png": 1, "two.png": 2}
    assert row["unique_in"] == ["one.png"] and row["ambiguous_in"] == ["two.png"]
    assert "⚠" in render(r)


def test_inspect_recommendation_rules():
    from rpa.tools.inspect_window import recommend

    assert recommend({"chromium": True, "elevated": False, "uia": {"available": True, "named_count_capped": 3}})[0] \
        == "desktop_visual"
    assert recommend({"chromium": False, "elevated": False, "uia": {"available": True, "named_count_capped": 30}})[0] \
        == "desktop_uia"
    assert recommend({"chromium": False, "elevated": True, "uia": {}})[0] == "blocked"
