"""DesktopVisual 어댑터 — PyAutoGUI 기반 화면 입력 (명세 §9).

책임: 대상 창 식별·연결/실행, 포그라운드 확인, 창 기준 좌표 클릭, 키 입력, 한글 텍스트 입력,
클립보드 readback, 이미지 기준점 탐색, 창/영역 캡처.
UIA 컨트롤 조작은 3단계 desktop_uia 어댑터의 책임이다.

안전 규칙
- 모든 GUI 호출은 처음 호출한 스레드(Runner 실행 스레드)에서만 허용.
- 매 입력 직전: 취소 플래그, PyAutoGUI FAILSAFE, 대상 창 존재·포그라운드, 좌표가 대상 창 클라이언트·주 모니터 안,
  해당 지점이 다른 창에 가려지지 않았는지 확인. 하나라도 어긋나면 입력하지 않는다.
- 포커스는 activate()에서 한 번만 시도한다. 입력 중 포커스가 바뀌면 FocusLost(사용자 확인)로 멈춘다.
- FailSafeException은 FailSafeTriggered로 바꿔 올린다 (재시도 대상 아님).
- 좌표는 화면 절대값이 아니라 대상 창 클라이언트 영역 기준 물리 픽셀 (selector 파일에 저장).
- 지원 범위: 주 모니터에 있는 창. 그 밖의 위치는 시작 전 거부한다.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from rpa.core.control import ControlState
from rpa.core.errors import FailSafeTriggered, PreconditionFailed, TransientError, UserActionRequired


class FocusLost(UserActionRequired):
    """대상 창이 포그라운드가 아님 — 후속 입력을 멈추고 사용자 확인을 요청한다."""


class WrongThread(RuntimeError):
    pass


class _Ambiguous(PreconditionFailed):
    """이미지 기준점이 여러 곳에서 발견됨."""

    def __init__(self, msg: str, points: list["Point"]) -> None:
        super().__init__(msg)
        self.points = points


@dataclass(frozen=True)
class WindowSpec:
    """대상 창 식별 조건. 최소 하나는 지정해야 한다. 실제 앱 조사 결과로만 채운다."""

    process_name: Optional[str] = None  # 예: "notepad.exe" (대소문자 무시)
    title_regex: Optional[str] = None
    class_name: Optional[str] = None

    def __post_init__(self) -> None:
        if not (self.process_name or self.title_regex or self.class_name):
            raise ValueError("WindowSpec에는 process_name/title_regex/class_name 중 하나 이상이 필요합니다")

    def matches(self, w: Any) -> bool:
        if self.process_name and w.process_name.lower() != self.process_name.lower():
            return False
        if self.class_name and w.class_name != self.class_name:
            return False
        if self.title_regex and not re.search(self.title_regex, w.title):
            return False
        return True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WindowSpec":
        return cls(d.get("process_name"), d.get("title_regex"), d.get("class_name"))


@dataclass(frozen=True)
class Point:
    """대상 창 클라이언트 영역 기준 물리 픽셀 좌표."""

    x: int
    y: int
    name: str = ""


@dataclass
class TargetWindow:
    spec: WindowSpec
    hwnd: int
    pid: int
    process_name: str
    title: str
    owned: bool  # 이 실행이 띄운 앱이면 True — 사용자가 연 앱은 닫지 않는다
    elevated: Optional[bool]
    bound_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spec"] = asdict(self.spec)
        return d


@dataclass
class FieldResult:
    name: str
    expected: str
    observed: Optional[str]  # None이면 readback 불가 (판정 불가)
    method: str

    @property
    def match(self) -> Optional[bool]:
        return None if self.observed is None else self.observed == self.expected


@dataclass(frozen=True)
class VisualSettings:
    pause: float = 0.05  # PyAutoGUI 동작 사이 기본 지연
    type_chunk: int = 16  # 이만큼 보낼 때마다 취소·FAILSAFE·포커스 재확인
    chunk_delay: float = 0.01
    text_method: str = "unicode"  # unicode | paste
    allow_click_activate: bool = True  # SetForegroundWindow 실패 시 제목 표시줄 1회 클릭 허용
    clipboard_timeout: float = 1.0
    image_confidence: Optional[float] = 0.9  # None이면 정확 일치 (OpenCV 불필요)
    # 활성화 시 대상 창을 덮은 다른 앱 창을 최소화 (기록 후 restore_obstructions로 복원). 업무별 opt-in
    minimize_obstructions: bool = False


def _load_pyautogui(settings: VisualSettings):
    # pyautogui는 import 시 SetProcessDPIAware()(시스템 DPI)를 호출한다.
    # 먼저 Per-Monitor v2를 설정해 두면 그 호출은 무시되고 물리 픽셀 좌표가 유지된다.
    from rpa.platform.windows.display import set_dpi_awareness

    set_dpi_awareness()
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = settings.pause
    return pyautogui


def _default_win():
    from rpa.platform.windows import win32

    return win32


def _default_monitor():
    from rpa.platform.windows.display import list_monitors, set_dpi_awareness
    from rpa.platform.windows.win32 import Rect

    set_dpi_awareness()
    p = next((m for m in list_monitors() if m.primary), None)
    if p is None:
        raise PreconditionFailed("주 모니터를 찾을 수 없습니다")
    return Rect(p.left, p.top, p.width, p.height)


class DesktopVisualAdapter:
    def __init__(
        self,
        control: ControlState,
        settings: VisualSettings = VisualSettings(),
        *,
        win: Any = None,
        pyautogui_loader: Optional[Callable[[VisualSettings], Any]] = None,
        monitor_loader: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.control = control
        self.settings = settings
        self._win = win
        self._pg = None
        self._pg_loader = pyautogui_loader or _load_pyautogui
        self._monitor_loader = monitor_loader or _default_monitor
        self._monitor = None
        self._owner_thread: Optional[int] = None
        self.target: Optional[TargetWindow] = None
        self._launched: list[subprocess.Popen] = []
        self.minimized: list[dict[str, Any]] = []  # 이 실행이 최소화한 다른 앱 창 (복원 대상)

    # ------------------------------------------------------------------
    # 내부 준비
    # ------------------------------------------------------------------
    @property
    def win(self):
        if self._win is None:
            self._win = _default_win()
        return self._win

    @property
    def pg(self):
        if self._pg is None:
            self._pg = self._pg_loader(self.settings)
        return self._pg

    @property
    def monitor(self):
        if self._monitor is None:
            self._monitor = self._monitor_loader()
        return self._monitor

    def _check_thread(self) -> None:
        me = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = me
        elif self._owner_thread != me:
            raise WrongThread("GUI 호출은 Runner 실행 스레드에서만 할 수 있습니다")

    def _guard(self) -> None:
        """모든 입력 직전 공통 검사."""
        self._check_thread()
        self.control.raise_if_stopped()
        self._call(self.pg.failSafeCheck)

    def _call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "FailSafeException":
                raise FailSafeTriggered("마우스가 화면 모서리로 이동해 FAILSAFE가 작동했습니다") from e
            raise

    def _require_target(self) -> TargetWindow:
        if self.target is None:
            raise PreconditionFailed("대상 창이 연결되지 않았습니다 (attach/launch 먼저)")
        return self.target

    # ------------------------------------------------------------------
    # 대상 창
    # ------------------------------------------------------------------
    def find_windows(self, spec: WindowSpec, exclude: frozenset[int] = frozenset()) -> list[Any]:
        return [w for w in self.win.top_level_windows() if spec.matches(w) and w.hwnd not in exclude]

    def wait_window(self, spec: WindowSpec, timeout: float, exclude: frozenset[int] = frozenset()) -> Any:
        found: list[Any] = []

        def probe() -> bool:
            found[:] = self.find_windows(spec, exclude)
            return len(found) >= 1

        self.control.wait_until(probe, timeout=timeout, interval=0.2)
        if not found:
            raise TransientError(f"대상 창을 찾지 못함: {spec}")
        if len(found) > 1:
            raise PreconditionFailed(
                f"조건에 맞는 창이 {len(found)}개입니다 — 하나로 특정할 수 없어 입력하지 않습니다: "
                + ", ".join(f"{w.title!r}(pid {w.pid})" for w in found)
            )
        return found[0]

    def attach(
        self, spec: WindowSpec, timeout: float = 5.0,
        _exclude: frozenset[int] = frozenset(), _owned: bool = False,
    ) -> TargetWindow:
        """이미 열린 창에 연결. 화면 범위·권한(UIPI)을 확인한다."""
        self._check_thread()
        w = self.wait_window(spec, timeout, _exclude)
        self._check_window_supported(w)
        target_elev = self.win.process_elevated(w.pid)
        me_elev = self.win.current_process_elevated()
        if target_elev and not me_elev:
            raise PreconditionFailed(
                f"대상 앱({w.process_name})이 관리자 권한으로 실행 중입니다. 일반 권한 Runner의 입력은 UIPI로 차단됩니다."
            )
        self.target = TargetWindow(spec, w.hwnd, w.pid, w.process_name, w.title, _owned, target_elev)
        return self.target

    def launch(self, args: Sequence[str], spec: WindowSpec, timeout: float = 15.0) -> TargetWindow:
        """업무에 필요한 앱을 새로 실행해 연결. 이 실행이 띄운 창·프로세스만 close_owned()로 닫는다.

        실행 파일이 런처(예: venv python.exe)일 수 있어 PID 대신 '실행 전에 없던 창'으로 식별한다.
        """
        self._check_thread()
        before = frozenset(w.hwnd for w in self.find_windows(spec))
        proc = subprocess.Popen(list(args))
        self._launched.append(proc)
        return self.attach(spec, timeout=timeout, _exclude=before, _owned=True)

    def attach_or_launch(self, spec: WindowSpec, args: Optional[Sequence[str]], timeout: float = 15.0) -> TargetWindow:
        if self.find_windows(spec):
            return self.attach(spec, timeout=1.0)
        if not args:
            raise UserActionRequired(f"대상 앱 창이 없습니다: {spec}", "대상 앱을 실행한 뒤 resolve done 하세요.")
        return self.launch(args, spec, timeout)

    def close_owned(self, timeout: float = 5.0) -> None:
        """이 실행이 띄운 앱만 닫는다. 사용자가 원래 열어 둔 앱(owned=False)은 건드리지 않는다."""
        # 정리는 취소·FAILSAFE 뒤에도 수행되어야 하므로 제어 플래그를 보지 않는 대기를 쓴다
        t = self.target
        if t is not None and t.owned and self.win.is_window(t.hwnd):
            self.win.close_window(t.hwnd)
            deadline = time.monotonic() + timeout
            while self.win.is_window(t.hwnd) and time.monotonic() < deadline:
                time.sleep(0.1)
        for proc in self._launched:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._launched.clear()

    def _check_window_supported(self, w: Any) -> None:
        if w.minimized:
            return  # activate()에서 복원 후 다시 검사한다
        if not self.monitor.contains_rect(w.frame):
            raise PreconditionFailed(
                f"대상 창({w.frame})이 주 모니터({self.monitor}) 밖에 걸쳐 있습니다. "
                "지원 범위는 주 모니터 안의 창입니다 — 창을 주 모니터로 옮긴 뒤 다시 시작하세요."
            )

    def refresh(self) -> Any:
        t = self._require_target()
        w = self.win.window_info(t.hwnd) if self.win.is_window(t.hwnd) else None
        if w is None or w.pid != t.pid:
            raise UserActionRequired("대상 창이 사라졌습니다", "대상 앱 상태를 확인한 뒤 resolve done 하세요.")
        return w

    # ------------------------------------------------------------------
    # 포커스
    # ------------------------------------------------------------------
    def is_foreground(self) -> bool:
        return self.win.foreground_hwnd() == self._require_target().hwnd

    def ensure_foreground(self) -> Any:
        """입력 직전 확인. 활성화를 시도하지 않는다."""
        w = self.refresh()
        fg = self.win.foreground_hwnd()
        if fg != w.hwnd:
            other = self.win.window_info(fg) if fg else None
            desc = f"{other.title!r}({other.process_name})" if other else "없음"
            raise FocusLost(
                f"포커스가 대상 창이 아닙니다 (현재: {desc})",
                "사용자 조작과 겹쳤을 수 있습니다. 대상 화면 상태를 확인하고 resolve done 후 재개하세요.",
            )
        return w

    def _sample_points(self, w: Any, extra: Optional[tuple[int, int]] = None) -> list[tuple[int, int]]:
        c = w.client
        pts = [(c.left + int(c.width * fx), c.top + int(c.height * fy))
               for fx in (0.05, 0.3, 0.5, 0.7, 0.95) for fy in (0.02, 0.25, 0.5, 0.75, 0.97)]
        if extra:
            pts.insert(0, extra)
        return [p for p in pts if self.monitor.contains(*p)]

    def clear_obstructions(self, extra_point: Optional[tuple[int, int]] = None) -> list[dict[str, Any]]:
        """대상 창 위를 실제로 덮고 있는 다른 앱 창을 최소화한다 (설정 minimize_obstructions 필요).

        표본 지점에서 '보이는 최상위 창'을 확인해 대상이 아닌 창만 고른다. 최소화할 수 없는 창
        (셸·도구 창, 최소화 버튼 없음)이 덮고 있으면 UserActionRequired.
        """
        if not self.settings.minimize_obstructions:
            return []
        t = self._require_target()
        done: list[dict[str, Any]] = []
        for _ in range(3):  # 최소화하면 그 아래 창이 드러날 수 있어 몇 번 반복
            w = self.refresh()
            blockers: dict[int, Any] = {}
            for x, y in self._sample_points(w, extra_point):
                h = self.win.root_at_point(x, y)
                if h and h != t.hwnd and h not in blockers:
                    blockers[h] = self.win.window_info(h)
            if not blockers:
                break
            for h, info in blockers.items():
                desc = {"hwnd": h, "process": getattr(info, "process_name", "?"), "title": getattr(info, "title", "")}
                if not self.win.can_minimize(h):
                    raise UserActionRequired(
                        f"대상 창을 덮은 창을 최소화할 수 없습니다: {desc['process']} {desc['title']!r}",
                        "그 창을 직접 치운 뒤 resolve done 하세요.",
                    )
                if self.win.minimize_window(h):
                    done.append(desc)
                    self.minimized.append(desc)
        return done

    def restore_obstructions(self) -> int:
        """이 실행이 최소화한 창을 활성화 없이 원래대로 되돌린다."""
        n = 0
        for d in reversed(self.minimized):
            try:
                self.win.restore_window_noactivate(d["hwnd"])
                n += 1
            except Exception:  # noqa: BLE001 — 이미 닫힌 창 등
                pass
        self.minimized.clear()
        return n

    def activate(self, click_point: Optional[Point] = None) -> Any:
        """대상 창을 앞으로 가져오기를 한 번만 시도한다 (반복 탈취 금지).

        SetForegroundWindow가 막히면 OS 제목 표시줄을 한 번 클릭한다. 제목 표시줄이 없는 커스텀 창
        (Electron 등)은 click_point로 클라이언트 안의 '눌러도 아무 동작이 없는 지점'(드래그 영역 등)을 지정한다.
        """
        self._guard()
        t = self._require_target()
        if self.win.foreground_hwnd() == t.hwnd:
            return self.refresh()
        if self.win.try_set_foreground(t.hwnd):
            w = self.refresh()
            self._check_window_supported(w)
            return w
        w = self.refresh()
        self._check_window_supported(w)
        caption_h = w.client.top - w.frame.top
        target_xy: Optional[tuple[int, int]] = None
        if click_point is not None and 0 <= click_point.x < w.client.width and 0 <= click_point.y < w.client.height:
            target_xy = (w.client.left + click_point.x, w.client.top + click_point.y)
        elif caption_h >= 8:
            target_xy = (w.frame.left + w.frame.width // 2, w.frame.top + caption_h // 2)
        if self.settings.allow_click_activate and target_xy is not None:
            x, y = target_xy
            if (self.settings.minimize_obstructions and self.monitor.contains(x, y)
                    and self.win.root_at_point(x, y) != t.hwnd):
                self.clear_obstructions(extra_point=(x, y))
                if self.win.try_set_foreground(t.hwnd):
                    return self.refresh()
            if self.monitor.contains(x, y) and self.win.root_at_point(x, y) == t.hwnd:
                self._call(self.pg.click, x, y)
                self.control.sleep(0.15)
                if self.win.foreground_hwnd() == t.hwnd:
                    return self.refresh()
        raise FocusLost(
            "대상 창을 앞으로 가져오지 못했습니다",
            "대상 창을 직접 클릭해 앞으로 가져온 뒤 resolve done 하세요.",
        )

    # ------------------------------------------------------------------
    # 좌표
    # ------------------------------------------------------------------
    def to_screen(self, p: Point, w: Any = None) -> tuple[int, int]:
        w = w or self.refresh()
        if not (0 <= p.x < w.client.width and 0 <= p.y < w.client.height):
            raise PreconditionFailed(
                f"좌표 {p.name or ''}({p.x},{p.y})가 대상 창 클라이언트 영역 {w.client.width}x{w.client.height} 밖입니다 "
                "(창 크기·배율이 selector 수집 때와 다를 수 있음)"
            )
        x, y = w.client.left + p.x, w.client.top + p.y
        if not self.monitor.contains(x, y):
            raise PreconditionFailed(f"좌표 ({x},{y})가 주 모니터 밖입니다")
        if self.win.root_at_point(x, y) != w.hwnd:
            raise UserActionRequired(
                f"{p.name or '대상 지점'}({x},{y})이 다른 창에 가려져 있습니다",
                "가리는 창을 치운 뒤 resolve done 하세요.",
            )
        return x, y

    # ------------------------------------------------------------------
    # 입력
    # ------------------------------------------------------------------
    def click(self, p: Point, button: str = "left", clicks: int = 1) -> tuple[int, int]:
        self._guard()
        w = self.ensure_foreground()
        x, y = self.to_screen(p, w)
        self._call(self.pg.click, x, y, clicks=clicks, button=button)
        return x, y

    def key(self, *keys: str) -> None:
        """단일 키 또는 조합 (예: key('enter'), key('ctrl', 'a'))."""
        self._guard()
        self.ensure_foreground()
        if len(keys) == 1:
            self._call(self.pg.press, keys[0])
        else:
            self._call(self.pg.hotkey, *keys)

    def type_text(self, text: str, method: Optional[str] = None) -> None:
        method = method or self.settings.text_method
        self._guard()
        self.ensure_foreground()
        if method == "unicode":
            n = max(1, self.settings.type_chunk)
            for i in range(0, len(text), n):
                if i:
                    self._guard()
                    self.ensure_foreground()
                try:
                    self.win.send_unicode(text[i:i + n])
                except OSError as e:
                    raise PreconditionFailed(f"키 입력이 차단됨: {e}") from e
                if self.settings.chunk_delay:
                    self.control.sleep(self.settings.chunk_delay)
        elif method == "paste":
            self._with_clipboard(lambda: self._paste(text))
        else:
            raise ValueError(f"알 수 없는 입력 방식: {method}")

    def _paste(self, text: str) -> None:
        self.win.set_clipboard_text(text)
        self.key("ctrl", "v")
        self.control.sleep(0.05)

    def _with_clipboard(self, fn: Callable[[], Any]) -> Any:
        """클립보드에 텍스트만 있을 때만 사용하고, 끝나면 원래 텍스트로 되돌린다."""
        if not self.win.clipboard_has_only_text():
            raise PreconditionFailed(
                "클립보드에 텍스트 외 데이터(서식·이미지·파일)가 있어 덮어쓰지 않습니다. 클립보드 방식이 필요한 단계입니다."
            )
        saved = self.win.get_clipboard_text()
        try:
            return fn()
        finally:
            self.win.set_clipboard_text(saved)

    def read_focused_text(self) -> Optional[str]:
        """현재 포커스 필드의 값을 전체 선택 + 복사로 읽는다. 읽지 못하면 None (판정 불가)."""

        def copy() -> Optional[str]:
            self.win.set_clipboard_text(None)
            seq = self.win.clipboard_sequence()
            self.key("ctrl", "a")
            self.key("ctrl", "c")
            if not self.control.wait_until(lambda: self.win.clipboard_sequence() != seq,
                                           timeout=self.settings.clipboard_timeout, interval=0.05):
                return None
            return self.win.get_clipboard_text()

        self._guard()
        return self._with_clipboard(copy)

    def copy_by_click(self, p: Point) -> Optional[str]:
        """앱의 '복사' 버튼을 눌러 복사된 텍스트를 읽는다 (원래 클립보드 텍스트 복원). 실패 시 None."""

        def run() -> Optional[str]:
            self.win.set_clipboard_text(None)
            seq = self.win.clipboard_sequence()
            self.click(p)
            if not self.control.wait_until(lambda: self.win.clipboard_sequence() != seq,
                                           timeout=max(self.settings.clipboard_timeout, 2.0), interval=0.05):
                return None
            return self.win.get_clipboard_text()

        self._guard()
        return self._with_clipboard(run)

    def type_multiline(self, text: str, newline_keys: tuple[str, ...] = ("shift", "enter")) -> None:
        """줄바꿈은 지정한 키 조합으로 입력 (채팅 입력창에서 Enter는 전송이므로 Shift+Enter)."""
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i:
                self.key(*newline_keys)
            if line:
                self.type_text(line)

    def set_field(self, p: Point, value: str, readback: bool = True, clear: bool = True) -> FieldResult:
        """필드 클릭 → (전체 선택) → 입력 → 값 다시 읽기. 비교 판단은 verifier가 한다."""
        self.click(p)
        if clear:
            self.key("ctrl", "a")
        if value:
            self.type_text(value)
        elif clear:
            self.key("delete")
        observed = self.read_focused_text() if readback else None
        return FieldResult(p.name, value, observed, self.settings.text_method if readback else "none")

    # ------------------------------------------------------------------
    # 이미지 기준점
    # ------------------------------------------------------------------
    def find_images(self, template: str | Path, confidence: Optional[float] = -1.0) -> list[Point]:
        """대상 창 안의 모든 매칭(겹침 제거)을 클라이언트 좌표 Point(중심) 목록으로 반환. 없으면 빈 목록.

        상태 판정(예: '생성 중 표시가 보이는가')처럼 0개도 정상인 경우에 쓴다.
        """
        try:
            return [self.locate_image(template, confidence)]
        except TransientError:
            return []
        except _Ambiguous as e:
            return e.points

    def locate_image(self, template: str | Path, confidence: Optional[float] = -1.0) -> Point:
        """대상 창 클라이언트 영역 안에서만 탐색. 정확히 하나일 때만 반환 (첫 매칭 클릭 금지)."""
        self._guard()
        w = self._visible_target()
        conf = self.settings.image_confidence if confidence == -1.0 else confidence
        region = (w.client.left, w.client.top, w.client.width, w.client.height)
        kwargs: dict[str, Any] = {"region": region}
        if conf is not None:
            kwargs["confidence"] = conf
        try:
            boxes = list(self._call(self.pg.locateAllOnScreen, _load_template(template), **kwargs))
        except Exception as e:  # noqa: BLE001 — ImageNotFoundException 등
            if type(e).__name__ == "ImageNotFoundException":
                boxes = []
            else:
                raise
        boxes = _dedupe_boxes(boxes)
        name = Path(template).stem
        points = [Point(b[0] + b[2] // 2 - w.client.left, b[1] + b[3] // 2 - w.client.top, name) for b in boxes]
        if not points:
            raise TransientError(f"이미지 기준점을 찾지 못함: {Path(template).name}")
        if len(points) > 1:
            raise _Ambiguous(f"이미지 기준점이 {len(points)}곳에서 발견되어 하나로 특정할 수 없습니다", points)
        return points[0]

    def click_image(self, template: str | Path, confidence: Optional[float] = -1.0) -> Point:
        p = self.locate_image(template, confidence)
        self.click(p)
        return p

    def _visible_target(self) -> Any:
        """화면 캡처 전 확인: 영역 캡처는 '그 위치에 보이는 것'을 찍으므로 대상 창이 맨 앞이어야 한다."""
        w = self.ensure_foreground()
        c = w.client
        for x, y in ((c.left + c.width // 2, c.top + c.height // 2), (c.left + 5, c.top + 5),
                     (c.right - 6, c.bottom - 6)):
            if self.monitor.contains(x, y) and self.win.root_at_point(x, y) != w.hwnd:
                raise UserActionRequired(
                    "대상 창이 다른 창(항상 위 창 등)에 가려져 있어 화면을 판정할 수 없습니다",
                    "가리는 창을 치운 뒤 resolve done 하세요.",
                )
        return w

    def wait_visually_stable(
        self,
        timeout: float = 5.0,
        interval: float = 0.2,
        stable_frames: int = 2,
        region: Optional[tuple[int, int, int, int]] = None,
    ) -> None:
        """대상 창(또는 클라이언트 기준 region=(x, y, w, h))이 연속 stable_frames번 같은 모습이 될 때까지 대기.

        창이 나타난 직후에는 아직 그려지는 중일 수 있다 (클릭 유실·이미지 기준점 불일치 원인).
        커서 깜빡임처럼 계속 바뀌는 부분은 region으로 제외한다.
        매 프레임 대상 창이 맨 앞인지 확인한다 — 다른 창 화면을 비교하지 않는다.
        """
        self._check_thread()
        last: Optional[bytes] = None
        same = 0
        deadline = time.monotonic() + timeout
        while True:
            self.control.raise_if_stopped()
            w = self._visible_target()
            if region is None:
                box = (w.client.left, w.client.top, w.client.width, w.client.height)
            else:
                box = (w.client.left + region[0], w.client.top + region[1], region[2], region[3])
            img = self._call(self.pg.screenshot, region=box)
            cur = img.tobytes()
            same = same + 1 if cur == last else 0
            if same >= stable_frames - 1 and last is not None:
                return
            last = cur
            if time.monotonic() >= deadline:
                raise TransientError("대상 창 화면이 안정되지 않음 (그리는 중이거나 애니메이션)")
            self.control.sleep(interval)

    # ------------------------------------------------------------------
    # 캡처
    # ------------------------------------------------------------------
    def capture(self, path: str | Path, area: str = "client") -> dict[str, Any]:
        """창/영역만 저장. 좌표 해석에 필요한 origin·크기·DPI를 함께 반환한다."""
        self._check_thread()
        if area == "screen":
            m = self.monitor
            l, t, wd, ht, dpi = m.left, m.top, m.width, m.height, None
        else:
            w = self._visible_target()
            r = w.client if area == "client" else w.frame
            l, t, wd, ht, dpi = r.left, r.top, r.width, r.height, w.dpi
            # 주 모니터 밖 부분은 캡처하지 않는다
            l2, t2 = max(l, self.monitor.left), max(t, self.monitor.top)
            wd = min(l + wd, self.monitor.right) - l2
            ht = min(t + ht, self.monitor.bottom) - t2
            l, t = l2, t2
            if wd <= 0 or ht <= 0:
                raise PreconditionFailed("캡처할 영역이 주 모니터 안에 없습니다")
        img = self._call(self.pg.screenshot, region=(l, t, wd, ht))
        if area != "screen" and self.win.foreground_hwnd() != self._require_target().hwnd:
            raise FocusLost("캡처 도중 다른 창이 앞으로 와서 증거 화면을 신뢰할 수 없습니다",
                            "대상 창 상태를 확인한 뒤 resolve done 하세요.")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(path))
        return {"path": str(path), "area": area, "origin_x": l, "origin_y": t, "width": wd, "height": ht,
                "dpi": dpi, "captured_at": time.time()}

    # ------------------------------------------------------------------
    # 엔진 훅
    # ------------------------------------------------------------------
    def failure_evidence(self, out_dir: Path) -> list[str]:
        """실패·대기 시 화면 증거. 대상 창이 있으면 그 창만, 없으면 저장하지 않는다."""
        if self.target is None or self._pg is None:
            return []
        stamp = int(time.time())
        try:
            return [self.capture(out_dir / f"failure_{stamp}.png", area="frame")["path"]]
        except Exception:  # noqa: BLE001 — 증거 수집 실패가 원래 오류를 가리면 안 된다
            pass
        try:
            # 대상 창이 가려졌거나 포커스를 잃은 경우: 무엇이 앞에 있었는지 전체 화면으로 남긴다
            return [self.capture(out_dir / f"failure_{stamp}_fullscreen.png", area="screen")["path"]]
        except Exception:  # noqa: BLE001
            return []

    def release_inputs(self) -> None:
        """정상 중단 경로 finally에서 호출: 눌린 수정자 키·마우스 버튼 해제."""
        if self._owner_thread is None:
            return
        try:
            self.restore_obstructions()  # 최소화했던 다른 앱 창을 되돌림 (실행 종료·사용자 대기 시)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.win.release_modifiers()
        except Exception:  # noqa: BLE001
            pass
        if self._pg is not None:
            try:
                self._pg.mouseUp()
            except Exception:  # noqa: BLE001
                pass


def _load_template(template: str | Path) -> Any:
    """기준 이미지를 PIL로 읽어 넘긴다.

    OpenCV(cv2.imread)는 Windows에서 비ASCII 경로(예: C:\\workspace\\삼성\\...)를 읽지 못하므로
    경로 문자열을 pyscreeze에 그대로 넘기지 않는다.
    """
    from PIL import Image

    with Image.open(Path(template)) as im:
        return im.convert("RGB")


def _dedupe_boxes(boxes: list[Any]) -> list[tuple[int, int, int, int]]:
    """confidence 매칭은 같은 위치를 여러 번 돌려주므로 겹치는 상자를 하나로 합친다."""
    out: list[tuple[int, int, int, int]] = []
    for b in boxes:
        b = tuple(int(v) for v in (b[0], b[1], b[2], b[3]))
        if not any(abs(b[0] - o[0]) < o[2] // 2 and abs(b[1] - o[1]) < o[3] // 2 for o in out):
            out.append(b)  # type: ignore[arg-type]
    return out
