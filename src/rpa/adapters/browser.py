"""Browser 어댑터 — Playwright(동기 API) 기반 웹 조작 (명세 §8).

규칙
- RPA 전용 프로필(user_data_dir)을 쓰는 persistent context. 사용자의 기본 브라우저 프로필·이미 열린 탭은 건드리지 않는다.
  같은 프로필의 동시 사용은 거부한다.
- 기본 채널은 PC에 설치된 Edge (별도 브라우저 다운로드 불필요). 실사용은 headful.
- 허용한 origin 밖으로의 최상위 이동(새 창 포함)은 차단한다.
- 요소는 role/label/test id 등 접근성 기준으로 찾고, 정확히 하나일 때만 조작한다 (첫 매칭 클릭 금지).
- 요소를 못 찾으면 원인을 분류한다: 로그인 필요 / 권한 없음 / 서버 오류 / 로딩 중 / UI 변경 가능성.
  즉시 좌표 클릭으로 대체하지 않는다.
- 다운로드는 파일명을 추측하지 않고 다운로드 이벤트로 받아 실행별 폴더에 저장한다.
- Playwright 객체는 처음 사용한 스레드(Runner 실행 스레드)에서만 생성·사용·정리한다. 동기 API만 사용.
- 실패 시 화면과 trace(zip)를 증거로 남긴다.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import urljoin, urlparse

from rpa.core.control import ControlState
from rpa.core.errors import PreconditionFailed, TransientError, UserActionRequired

Selector = Union[str, dict]


class LoginRequired(UserActionRequired):
    def __init__(self, url: str) -> None:
        super().__init__(f"로그인이 필요합니다 (세션 만료 또는 미로그인): {url}",
                         "RPA 전용 브라우저 창에서 로그인한 뒤 resolve done 하세요. 자격 증명은 RPA가 입력하지 않습니다.")


class BrowserWrongThread(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserSettings:
    user_data_dir: str
    base_url: str = ""
    allowed_origins: tuple[str, ...] = ()  # 비어 있으면 base_url의 origin만 허용
    channel: str = "msedge"
    headless: bool = False
    default_timeout_ms: int = 10_000
    navigation_timeout_ms: int = 20_000
    login_url_pattern: str = r"/login(\?|$)"  # 현재 URL이 이 패턴이면 로그인 화면으로 본다
    busy_selector: Optional[str] = None  # 로딩 표시 CSS (보이면 '로딩 중'으로 분류)
    trace: bool = True


def describe(spec: Selector) -> str:
    if isinstance(spec, str):
        return f"css={spec}"
    return ", ".join(f"{k}={v}" for k, v in spec.items())


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}".lower()


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name or "download.bin"


class BrowserAdapter:
    def __init__(self, control: ControlState, settings: BrowserSettings,
                 playwright_factory: Optional[Callable[[], Any]] = None) -> None:
        self.control = control
        self.settings = settings
        self._pw_factory = playwright_factory
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._owner: Optional[int] = None
        self._nav_count = 0
        self.last_nav_status: Optional[int] = None
        self.last_nav_url: str = ""
        self.blocked_navigations: list[str] = []
        self.popups: list[Any] = []
        allowed = settings.allowed_origins or ((_origin(settings.base_url),) if settings.base_url else ())
        self._allowed = tuple(o.lower().rstrip("/") for o in allowed)

    # ------------------------------------------------------------------
    # 수명
    # ------------------------------------------------------------------
    def _check_thread(self) -> None:
        me = threading.get_ident()
        if self._owner is None:
            self._owner = me
        elif self._owner != me:
            raise BrowserWrongThread("Playwright 객체는 Runner 실행 스레드에서만 사용할 수 있습니다")

    def _guard(self) -> None:
        self._check_thread()
        self.control.raise_if_stopped()

    @property
    def started(self) -> bool:
        return self._ctx is not None

    def start(self) -> Any:
        """RPA 전용 프로필로 브라우저를 연다 (이미 열려 있으면 재사용)."""
        self._guard()
        if self._ctx is not None:
            return self._page
        if self._pw_factory:
            self._pw = self._pw_factory()
        else:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
        Path(self.settings.user_data_dir).mkdir(parents=True, exist_ok=True)
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                self.settings.user_data_dir, channel=self.settings.channel, headless=self.settings.headless,
                accept_downloads=True,
            )
        except Exception as e:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            msg = str(e).splitlines()[0][:200]
            if "user data directory is already in use" in str(e).lower() or "processsingleton" in str(e).lower():
                raise PreconditionFailed(f"RPA 브라우저 프로필이 이미 사용 중입니다 ({self.settings.user_data_dir})") from e
            raise PreconditionFailed(f"브라우저를 시작하지 못했습니다 ({self.settings.channel}): {msg}") from e
        self._ctx.set_default_timeout(self.settings.default_timeout_ms)
        self._ctx.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        if self._allowed:
            self._ctx.route("**/*", self._route_guard)
        self._ctx.on("page", self._on_new_page)
        if self.settings.trace:
            self._ctx.tracing.start(screenshots=True, snapshots=True)
            self._ctx.tracing.start_chunk()
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._watch(self._page)
        return self._page

    def end_session(self) -> None:
        """이 실행이 연 RPA 전용 브라우저만 닫는다 (엔진이 실행 종료 시 호출)."""
        if self._ctx is None:
            return
        try:
            if self.settings.trace:
                self._ctx.tracing.stop()  # 성공 경로의 trace는 저장하지 않음
        except Exception:  # noqa: BLE001
            pass
        try:
            self._ctx.close()
        finally:
            self._ctx = self._page = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None

    # ------------------------------------------------------------------
    # 이동 감시
    # ------------------------------------------------------------------
    def _route_guard(self, route: Any, request: Any) -> None:
        try:
            nav = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:  # noqa: BLE001 — 팝업 초기화 중 frame 접근 불가 등
            nav = request.is_navigation_request()
        if nav and request.url.startswith(("http:", "https:")) and _origin(request.url) not in self._allowed:
            self.blocked_navigations.append(request.url)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def _watch(self, page: Any) -> None:
        def on_response(resp: Any) -> None:
            try:
                if resp.request.is_navigation_request() and resp.frame == page.main_frame:
                    self._nav_count += 1
                    self.last_nav_status = resp.status
                    self.last_nav_url = resp.url
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)

    def _on_new_page(self, page: Any) -> None:
        if page is not self._page:
            self.popups.append(page)

    # ------------------------------------------------------------------
    # 이동·로그인
    # ------------------------------------------------------------------
    @property
    def page(self) -> Any:
        if self._page is None:
            self.start()
        return self._page

    def url(self, path: str = "") -> str:
        return urljoin(self.settings.base_url, path) if path else self.settings.base_url

    def current_url(self) -> str:
        return self.page.url

    def is_login_page(self) -> bool:
        return bool(re.search(self.settings.login_url_pattern, urlparse(self.page.url).path
                              + ("?" + urlparse(self.page.url).query if urlparse(self.page.url).query else "")))

    def goto(self, path_or_url: str, check_login: bool = True) -> int:
        """허용 origin 확인 → 이동 → HTTP 상태 확인 → (옵션) 로그인 화면이면 LoginRequired."""
        self._guard()
        target = self.url(path_or_url)
        if self._allowed and _origin(target) not in self._allowed:
            raise PreconditionFailed(f"허용되지 않은 주소입니다: {target}")
        resp = self.page.goto(target, wait_until="domcontentloaded")
        status = resp.status if resp is not None else 0
        self._raise_for_status(status, target)
        if check_login and self.is_login_page():
            raise LoginRequired(self.page.url)
        return status

    def _raise_for_status(self, status: int, url: str) -> None:
        if status in (401, 403):
            raise UserActionRequired(f"권한이 없습니다 (HTTP {status}): {url}",
                                     "계정 권한을 확인한 뒤 resolve done 하세요.")
        if status >= 500:
            raise TransientError(f"서버 오류 (HTTP {status}): {url}")

    def wait_for_login(self, timeout: float, notify: Optional[Callable[[str], None]] = None,
                       ready: Optional[Selector] = None) -> bool:
        """로그인 화면이면 사용자가 RPA 브라우저에서 직접 로그인할 때까지 기다린다 (자격 증명 입력 안 함).

        시간 안에 로그인되면 True, 아니면 LoginRequired.
        """
        self._guard()
        if not self.is_login_page():
            return True
        if notify:
            notify(f"RPA 전용 브라우저 창에서 로그인해 주세요 (최대 {int(timeout)}초 대기). 로그인 정보는 RPA가 입력하지 않습니다.")
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        def logged_in() -> bool:
            if self.is_login_page():
                return False
            return ready is None or self._count(ready) == 1

        if self.control.wait_until(logged_in, timeout=timeout, interval=0.5):
            return True
        raise LoginRequired(self.page.url)

    # ------------------------------------------------------------------
    # 요소
    # ------------------------------------------------------------------
    def _locator(self, spec: Selector, page: Any = None) -> Any:
        root = page or self.page
        if isinstance(spec, str):
            return root.locator(spec)
        if "frame_title" in spec:
            root = root.frame_locator(f'iframe[title="{spec["frame_title"]}"]')
        elif "frame" in spec:
            root = root.frame_locator(spec["frame"])
        if "within" in spec:
            root = self._locator(spec["within"], page)
        exact = spec.get("exact", True)
        if "role" in spec:
            kw = {"name": spec["name"], "exact": exact} if "name" in spec else {}
            return root.get_by_role(spec["role"], **kw)
        if "label" in spec:
            return root.get_by_label(spec["label"], exact=exact)
        if "placeholder" in spec:
            return root.get_by_placeholder(spec["placeholder"], exact=exact)
        if "test_id" in spec:
            return root.get_by_test_id(spec["test_id"])
        if "text" in spec:
            return root.get_by_text(spec["text"], exact=exact)
        if "css" in spec:
            return root.locator(spec["css"])
        raise ValueError(f"지원하지 않는 selector: {spec}")

    def _count(self, spec: Selector) -> int:
        try:
            return self._locator(spec).count()
        except Exception:  # noqa: BLE001
            return 0

    def exists(self, spec: Selector) -> bool:
        self._guard()
        return self._count(spec) >= 1

    def one(self, spec: Selector, timeout_ms: Optional[int] = None) -> Any:
        """정확히 하나의 요소. 없으면 원인 분류, 여러 개면 거부."""
        self._guard()
        loc = self._locator(spec)
        try:
            loc.first.wait_for(state="attached", timeout=timeout_ms or self.settings.default_timeout_ms)
        except Exception as e:  # noqa: BLE001 — Playwright TimeoutError
            if type(e).__name__ != "TimeoutError":
                raise
            raise self.classify_missing(spec) from e
        n = loc.count()
        if n != 1:
            raise PreconditionFailed(f"요소가 {n}개라 하나로 특정할 수 없습니다 ({describe(spec)}) — 첫 번째를 누르지 않습니다")
        return loc

    def classify_missing(self, spec: Selector) -> Exception:
        """요소를 못 찾은 원인 분류. 좌표 클릭으로 대체하지 않는다."""
        url = self.page.url
        if self.is_login_page():
            return LoginRequired(url)
        st = self.last_nav_status
        if st in (401, 403):
            return UserActionRequired(f"권한 화면입니다 (HTTP {st}): {url}", "계정 권한을 확인한 뒤 resolve done 하세요.")
        if st is not None and st >= 500:
            return TransientError(f"서버 오류 화면입니다 (HTTP {st}): {url}")
        try:
            ready = self.page.evaluate("document.readyState")
        except Exception:  # noqa: BLE001
            ready = "unknown"
        if ready != "complete" or (self.settings.busy_selector and self._count(self.settings.busy_selector)):
            return TransientError(f"페이지가 아직 로딩 중입니다 ({describe(spec)}): {url}")
        return PreconditionFailed(
            f"요소를 찾지 못함 ({describe(spec)}) at {url} — 화면 구조(UI) 변경 가능성. 좌표 클릭으로 대체하지 않습니다")

    def click(self, spec: Selector, expect_navigation: bool = False, timeout_ms: Optional[int] = None) -> Optional[int]:
        """클릭. expect_navigation이면 새 문서 응답을 기다려 HTTP 상태를 확인하고 반환."""
        loc = self.one(spec, timeout_ms)
        before = self._nav_count
        loc.click()
        if not expect_navigation:
            return None
        wait_s = (timeout_ms or self.settings.navigation_timeout_ms) / 1000
        if not self.control.wait_until(lambda: self._nav_count > before, timeout=wait_s, interval=0.05):
            raise TransientError(f"클릭 후 페이지 이동이 확인되지 않음 ({describe(spec)})")
        self.page.wait_for_load_state("domcontentloaded")
        status = self.last_nav_status or 0
        self._raise_for_status(status, self.page.url)
        return status

    def fill(self, spec: Selector, value: str, readback: bool = True) -> Optional[str]:
        """입력 후 값을 다시 읽어 반환 (비교 판단은 verifier). 선행 0·한글·특수문자를 그대로 넣는다."""
        loc = self.one(spec)
        loc.fill(value)
        return loc.input_value() if readback else None

    def read_text(self, spec: Selector) -> str:
        return self.one(spec).inner_text().strip()

    def read_value(self, spec: Selector) -> str:
        return self.one(spec).input_value()

    def attr(self, spec: Selector, name: str) -> Optional[str]:
        return self.one(spec).get_attribute(name)

    def extract(self, spec: Selector, js: str, arg: Any = None) -> Any:
        """정확히 하나인 요소(목록 컨테이너 등)에 읽기 전용 JS를 실행해 값을 받는다 (예: 기사 목록 추출).

        조작에는 쓰지 않는다 — 클릭·입력은 click/fill로만 한다.
        """
        return self.one(spec).evaluate(js, arg)

    def table(self, spec: Selector) -> list[dict[str, str]]:
        """표를 머리글 기준 dict 목록으로 추출."""
        loc = self.one(spec)
        data = loc.evaluate(
            "t => ({h: [...t.querySelectorAll('thead th')].map(c => c.innerText.trim()),"
            " r: [...t.querySelectorAll('tbody tr')].map(r => [...r.cells].map(c => c.innerText.trim()))})")
        return [dict(zip(data["h"], row)) for row in data["r"]]

    def upload(self, spec: Selector, path: str | Path) -> None:
        p = Path(path)
        if not p.is_file():
            raise PreconditionFailed(f"업로드할 파일이 없습니다: {p}")
        self.one(spec).set_input_files(str(p))

    def click_and_download(self, spec: Selector, save_dir: str | Path, overwrite: bool = False) -> dict[str, Any]:
        """다운로드 이벤트를 받아 실행별 폴더에 저장. 파일명은 서버가 준 이름을 정리해서 사용."""
        loc = self.one(spec)
        with self.page.expect_download(timeout=self.settings.navigation_timeout_ms) as info:
            loc.click()
        dl = info.value
        failure = dl.failure()
        if failure:
            raise TransientError(f"다운로드 실패: {failure}")
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        target = save_dir / _safe_filename(dl.suggested_filename)
        tmp = target.with_name(target.name + ".part")
        dl.save_as(str(tmp))  # 브라우저 context 종료 시 지워지는 임시 다운로드와 분리
        data = tmp.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if target.exists() and not overwrite:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                tmp.unlink()
                raise PreconditionFailed(f"같은 이름의 다른 파일이 이미 있습니다: {target} (덮어쓰지 않음)")
            tmp.unlink()
        else:
            tmp.replace(target)
        return {"path": str(target), "suggested_filename": dl.suggested_filename, "url": dl.url,
                "sha256": digest, "size": len(data)}

    def click_and_popup(self, spec: Selector, timeout_ms: Optional[int] = None) -> dict[str, Any]:
        """새 창을 여는 링크를 누르고 그 창의 주소·제목을 돌려준 뒤 닫는다 (허용 origin 밖이면 차단됨)."""
        loc = self.one(spec)
        with self.page.expect_popup(timeout=timeout_ms or self.settings.navigation_timeout_ms) as info:
            loc.click()
        popup = info.value
        try:
            popup.wait_for_load_state("domcontentloaded")
            return {"url": popup.url, "title": popup.title()}
        finally:
            popup.close()

    # ------------------------------------------------------------------
    # 증거
    # ------------------------------------------------------------------
    def screenshot(self, path: str | Path, full_page: bool = True) -> str:
        self._check_thread()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=full_page)
        return str(path)

    def failure_evidence(self, out_dir: Path) -> list[str]:
        """실패·대기 시 브라우저 화면과 trace(zip). 사용자 데스크톱 전체는 찍지 않는다."""
        if self._ctx is None or self._page is None:
            return []
        out: list[str] = []
        stamp = int(time.time())
        try:
            out.append(self.screenshot(Path(out_dir) / f"browser_failure_{stamp}.png"))
        except Exception:  # noqa: BLE001
            pass
        if self.settings.trace:
            try:
                path = Path(out_dir) / f"browser_trace_{stamp}.zip"
                self._ctx.tracing.stop_chunk(path=str(path))
                out.append(str(path))
                self._ctx.tracing.start_chunk()
            except Exception:  # noqa: BLE001
                pass
        return out
