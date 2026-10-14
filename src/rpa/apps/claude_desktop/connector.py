"""Claude 데스크톱 앱 커넥터 (화면 기반, PyAutoGUI).

Claude 앱(MSIX Electron)은 UIA로 내부 컨트롤을 노출하지 않아 화면으로 조작한다.
상태 판정은 실제 화면에서 잘라 교차 검증한 기준 이미지(assets/)로 한다 — 좌표를 추측하지 않는다.

이 커넥터가 보장하는 것
- ensure_ready: 창 연결(재시작 후 재연결 포함) → 한 번 활성화 → 배율이 기준 이미지 수집 환경과 같은지 확인
- 상태 판정: 빈 새 대화 / '채팅' 모드 / 생성 중 / 응답 완료 (기준 이미지 개수로)
- 동작: 새 대화, 입력(줄바꿈은 Shift+Enter)+readback, 전송, 완료 대기, 응답 복사
- diagnose: 판정 실패 시 화면 해석(선택, 기본 꺼짐). 결과는 진단일 뿐 검증을 대신하지 않음
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rpa.apps.base import AppInfo, register_app
from rpa.core.contracts import StepEnv
from rpa.core.errors import PreconditionFailed, TransientError, UserActionRequired
from rpa.core.run_context import RunContext

APP = register_app(AppInfo(
    app_id="claude_desktop",
    name="Claude 데스크톱 앱",
    kind="desktop_visual",
    description="Claude 데스크톱 앱 채팅 화면 (새 대화·입력·전송·완료 대기·응답 복사)",
    package_dir=Path(__file__).resolve().parent,
))

# 다른 화면 자동화 에이전트 (동시 실행 시 포커스 경합). 창 클래스 기준
OTHER_AGENT_WINDOW_CLASSES = ("Cua.AgentCursorOverlay",)


@dataclass(frozen=True)
class ClaudeConfig:
    send_ack_timeout: float = 20.0
    new_chat_timeout: float = 10.0


def _point(x: int, y: int, name: str):
    from rpa.adapters.desktop_visual import Point

    return Point(x, y, name)


class ClaudeDesktop:
    app = APP

    def __init__(self, dv: Any, config: ClaudeConfig = ClaudeConfig(), vision: Any = None) -> None:
        self.dv = dv
        self.config = config
        self.vision = vision  # ScreenInterpreter 또는 None (기본 꺼짐)
        self._sel: Optional[dict[str, Any]] = None

    @property
    def sel(self) -> dict[str, Any]:
        if self._sel is None:
            self._sel = APP.selectors()
        return self._sel

    def template(self, name: str) -> Path:
        return APP.asset(self.sel["templates"][name]["file"])

    # ------------------------------------------------------------------
    # 연결·환경
    # ------------------------------------------------------------------
    def ensure_ready(self, env: StepEnv) -> Any:
        """연결 확인 → (재)연결 → 단계 시작 시 한 번 활성화 → 측정 환경이 선택자 수집 환경과 같은지 확인."""
        from rpa.adapters.desktop_visual import WindowSpec

        dv = self.dv
        if dv.target is None or not dv.win.is_window(dv.target.hwnd):
            try:
                dv.attach(WindowSpec.from_dict(self.sel["window"]), timeout=min(3.0, env.call_timeout()))
            except TransientError:
                raise UserActionRequired("Claude 앱 창이 없습니다", "Claude 앱을 실행·로그인한 뒤 resolve done 하세요.")
        w = dv.refresh()
        ap = self.sel["activation_point"]
        w = dv.activate(_point(int(w.client.width * ap["x_fraction"]), ap["y"], "titlebar"))
        envr = self.sel["environment"]
        if w.dpi != envr["dpi"]:
            raise PreconditionFailed(
                f"Claude 창 DPI {w.dpi}가 기준 이미지 수집 환경 {envr['dpi']}({envr['scale_percent']}%)와 다릅니다 "
                "— 기준 이미지를 다시 수집해야 합니다")
        return w

    def app_version(self) -> str:
        from rpa.platform.windows import win32

        path = win32.process_image(self.dv.target.pid)
        # ...\WindowsApps\Claude_2.31226.1.0_x64__...\app\claude.exe
        for part in Path(path).parts:
            if part.startswith("Claude_"):
                return part.split("_")[1]
        return ""

    def environment_warnings(self) -> list[str]:
        from rpa.platform.windows import win32

        warnings = []
        agents = sorted({w.process_name for w in win32.top_level_windows()
                         if w.class_name in OTHER_AGENT_WINDOW_CLASSES})
        if agents:
            warnings.append(f"다른 화면 자동화 에이전트 실행 중: {', '.join(agents)} — 포커스가 바뀌면 즉시 멈춤")
        version = self.app_version() if self.dv.target else ""
        if version and version != self.sel["app_version"]:
            warnings.append(f"Claude 앱 버전 {version} ≠ 기준 이미지 검증 버전 {self.sel['app_version']} "
                            "— 화면 판정 실패 시 재수집 필요")
        return warnings

    # ------------------------------------------------------------------
    # 상태 판정 (기준 이미지)
    # ------------------------------------------------------------------
    def visible(self, name: str) -> list:
        return self.dv.find_images(self.template(name))

    def count(self, name: str) -> int:
        return len(self.visible(name))

    def one(self, name: str):
        pts = self.visible(name)
        if len(pts) != 1:
            raise PreconditionFailed(
                f"화면에서 '{self.sel['templates'][name]['meaning']}'를 정확히 한 곳 찾지 못함 ({len(pts)}곳)")
        return pts[0]

    def is_generating(self) -> bool:
        return self.count("stop_button") > 0

    def reply_rows(self) -> int:
        return self.count("action_bar")

    def is_new_chat_empty(self) -> bool:
        return self.count("new_chat_placeholder") == 1

    def is_chat_mode(self) -> bool:
        return self.count("mode_chat_selected") == 1

    # ------------------------------------------------------------------
    # 동작
    # ------------------------------------------------------------------
    def new_chat(self, env: StepEnv) -> dict[str, int]:
        """새 대화 단축키 → 빈 새 대화 화면 대기. 판정용 개수를 돌려준다 (판단은 업무 verifier)."""
        self.dv.key(*self.sel["shortcuts"]["new_chat"])
        found: dict[str, int] = {}

        def ready() -> bool:
            found["placeholder"] = self.count("new_chat_placeholder")
            return found["placeholder"] == 1

        env.control.wait_until(ready, timeout=min(self.config.new_chat_timeout, env.call_timeout()), interval=0.3)
        found["chat_mode"] = self.count("mode_chat_selected")
        found["action_bar"] = self.count("action_bar")
        return found

    def focus_editor(self) -> None:
        """빈 새 대화면 입력창 안내문을, 입력 후면 '채팅' 모드 전환 기준 위치를 클릭해 편집기에 포커스."""
        ph = self.visible("new_chat_placeholder")
        if len(ph) == 1:
            self.dv.click(ph[0])
            return
        mode = self.one("mode_chat_selected")
        off = self.sel["editor_from_mode_toggle"]
        self.dv.click(_point(mode.x + off["dx"], mode.y + off["dy"], "editor"))

    @staticmethod
    def _norm(text: Optional[str]) -> Optional[str]:
        return None if text is None else text.replace("\r\n", "\n").rstrip("\n")

    def read_editor(self) -> Optional[str]:
        self.focus_editor()
        return self._norm(self.dv.read_focused_text())

    def type_prompt(self, prompt: str) -> Optional[str]:
        """입력창 내용을 프롬프트로 교체하고 readback을 돌려준다 (전송하지 않음)."""
        self.focus_editor()
        self.dv.key("ctrl", "a")  # 남아 있는 입력이 있으면 교체
        self.dv.type_multiline(prompt, tuple(self.sel["shortcuts"]["newline"]))
        return self._norm(self.dv.read_focused_text())

    def send(self, env: StepEnv) -> dict[str, bool]:
        """전송 키 → 생성 중 또는 완료 표시가 나타날 때까지 대기."""
        self.dv.key(*self.sel["shortcuts"]["send"])
        state: dict[str, bool] = {}

        def acknowledged() -> bool:
            state["generating"] = self.count("stop_button") == 1
            state["done"] = self.count("action_bar") == 1
            return state["generating"] or state["done"]

        env.control.wait_until(acknowledged, timeout=min(self.config.send_ack_timeout, env.call_timeout()),
                               interval=0.3)
        return state

    def wait_reply(self, env: StepEnv, timeout: float) -> tuple[bool, dict[str, Any]]:
        """생성 중 표시가 사라지고 응답 아이콘 줄이 정확히 1개가 될 때까지 대기."""
        state: dict[str, Any] = {}

        def done() -> bool:
            state["generating"] = self.is_generating()
            state["action_bars"] = self.reply_rows()
            return not state["generating"] and state["action_bars"] == 1

        finished = env.control.wait_until(done, timeout=min(timeout, env.remaining()), interval=1.0)
        return finished, state

    def copy_reply(self) -> Optional[str]:
        """응답 아이콘 줄의 복사 버튼을 눌러 응답 텍스트를 읽는다 (아이콘 줄이 정확히 1개일 때만)."""
        bar = self.one("action_bar")
        off = self.sel["templates"]["action_bar"]["copy_offset"]
        text = self.dv.copy_by_click(_point(bar.x + off["dx"], bar.y + off["dy"], "copy_button"))
        return None if text is None else text.replace("\r\n", "\n")

    def capture(self, path: Path) -> dict[str, Any]:
        return self.dv.capture(path, area="client")

    # ------------------------------------------------------------------
    # 화면 해석 (선택)
    # ------------------------------------------------------------------
    def diagnose(self, ctx: RunContext, env: StepEnv, situation: str, expected: str = "") -> Optional[Any]:
        """기준 이미지 판정 실패 시 화면 해석으로 원인을 설명한다. 꺼져 있거나 실패해도 흐름을 깨지 않는다.

        대상 창이 맨 앞에 보이지 않으면 캡처 자체를 거부하므로 다른 앱 화면은 전송되지 않는다.
        """
        if self.vision is None or self.dv.target is None:
            return None
        from rpa.apps.claude_desktop.vision_profile import CLAUDE_PROFILE
        from rpa.vision.contracts import ScreenObservation

        out_dir = Path(ctx.run_dir) / env.step_id
        try:
            info = self.capture(out_dir / f"vision_{int(time.time() * 1000)}.png")
        except Exception:  # noqa: BLE001 — 가려짐·포커스 상실 등: 해석하지 않음
            return None
        obs = ScreenObservation.from_capture(info, app_window="claude")
        a = self.vision.assess(obs, CLAUDE_PROFILE, situation, expected=expected, save_dir=out_dir)
        env.add_evidence("vision_screen", info["path"])
        if (out_dir / f"vision_{obs.observation_id}.json").exists():
            env.add_evidence("vision_assessment", str(out_dir / f"vision_{obs.observation_id}.json"))
        return a


def make_connector(control: Any) -> tuple[Any, ClaudeDesktop]:
    """(DesktopVisualAdapter, ClaudeDesktop). 어댑터는 엔진 훅(입력 해제·증거)을 위해 업무 adapters에도 넣는다."""
    from rpa.adapters.desktop_visual import DesktopVisualAdapter, VisualSettings
    from rpa.vision.interpreter import ScreenInterpreter, vision_enabled

    dv = DesktopVisualAdapter(control, VisualSettings(minimize_obstructions=True, image_confidence=0.9,
                                                      chunk_delay=0.02))
    vision = ScreenInterpreter(control, model="claude-opus-5-5", effort="medium", max_calls=6) \
        if vision_enabled() else None
    return dv, ClaudeDesktop(dv, vision=vision)
