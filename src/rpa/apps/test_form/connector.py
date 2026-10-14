"""테스트 Windows 폼 커넥터 (화면 기반, PyAutoGUI). 대상: fixtures/windows/test_form_app.py

선택자: selectors.json (창 클라이언트 기준 물리 픽셀, 앱 버전 1.0, Per-Monitor DPI v2 고정 레이아웃).
저장소(store)는 앱이 저장 버튼을 누르면 쓰는 JSON 파일 — 업무 시스템 '재조회'에 해당한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from rpa.apps.base import AppInfo, register_app
from rpa.core.contracts import StepEnv
from rpa.core.errors import PreconditionFailed

APP = register_app(AppInfo(
    app_id="test_form",
    name="LocalRPA 테스트 폼",
    kind="desktop_visual",
    description="고객ID·이름·전화 입력 후 저장하는 테스트 Windows 폼 (tkinter)",
    package_dir=Path(__file__).resolve().parent,
))
APP_SCRIPT = Path(__file__).resolve().parents[4] / "fixtures" / "windows" / "test_form_app.py"
FIELDS = ("customer_id", "name", "phone")


class TestFormStore:
    """테스트 폼의 저장소 조회."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self, customer_id: str) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return None  # 앱이 쓰는 중
        r = data.get("records", {}).get(customer_id)
        return dict(r) if r else None


class TestFormApp:
    app = APP
    fields = FIELDS

    def __init__(self, dv: Any, store: TestFormStore, launch_args: tuple[str, ...]) -> None:
        self.dv = dv
        self.store = store
        self.launch_args = launch_args
        self.sel = APP.selectors()

    def _point(self, name: str):
        from rpa.adapters.desktop_visual import Point

        p = self.sel["points"][name]
        return Point(p["x"], p["y"], name)

    def ensure_ready(self, env: StepEnv, launch: bool = False) -> Any:
        """대상 창 연결 확인 → 필요 시 (재)연결 또는 실행 → 한 번 활성화 → 레이아웃 기준 크기 확인."""
        from rpa.adapters.desktop_visual import WindowSpec

        dv = self.dv
        spec = WindowSpec.from_dict(self.sel["window"])
        if dv.target is None or not dv.win.is_window(dv.target.hwnd):
            if launch:
                dv.attach_or_launch(spec, self.launch_args, timeout=env.call_timeout())
            else:
                dv.attach(spec, timeout=min(3.0, env.call_timeout()))
        w = dv.activate()
        size = self.sel["client_size"]
        if (w.client.width, w.client.height) != (size["width"], size["height"]):
            raise PreconditionFailed(
                f"대상 창 클라이언트 크기 {w.client.width}x{w.client.height}가 선택자 수집 기준 "
                f"{size['width']}x{size['height']}와 다릅니다 (앱 버전·배율·창 크기 확인 필요)")
        return w

    def read_fields(self) -> dict[str, Optional[str]]:
        out: dict[str, Optional[str]] = {}
        for name in FIELDS:
            self.dv.click(self._point(name))
            out[name] = self.dv.read_focused_text()
        return out

    def fill_fields(self, values: dict[str, str]) -> dict[str, Optional[str]]:
        """각 필드에 입력하고 readback을 돌려준다 (저장하지 않음)."""
        return {name: self.dv.set_field(self._point(name), values[name], readback=True).observed for name in FIELDS}

    def save(self, env: StepEnv, customer_id: str) -> Optional[dict[str, Any]]:
        """저장 버튼 → 저장소에 해당 건이 생길 때까지 대기. 시간 안에 안 생기면 None (판정은 업무)."""
        self.dv.click(self._point("save_button"))
        rec: dict[str, Any] = {}

        def saved() -> bool:
            r = self.store.get(customer_id)
            if r:
                rec.update(r)
            return bool(r)

        env.control.wait_until(saved, timeout=env.call_timeout(), interval=0.2)
        return rec or None

    def lookup(self, customer_id: str) -> Optional[dict[str, Any]]:
        return self.store.get(customer_id)

    def capture(self, path: Path) -> dict[str, Any]:
        return self.dv.capture(path, area="client")

    def close_if_owned(self) -> bool:
        """이 실행이 띄운 창만 닫는다. 사용자가 연 앱이면 False."""
        if self.dv.target is not None and self.dv.target.owned:
            self.dv.close_owned()
            return True
        return False


def make_connector(data_dir: Path, control: Any, dv: Any = None) -> tuple[Any, TestFormApp]:
    from rpa.adapters.desktop_visual import DesktopVisualAdapter

    dv = dv or DesktopVisualAdapter(control)
    store_path = Path(data_dir) / "test_form_store.json"
    return dv, TestFormApp(dv, TestFormStore(store_path),
                           (sys.executable, str(APP_SCRIPT), "--store", str(store_path)))
