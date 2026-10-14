"""테스트 업무 포털 커넥터 (웹, Playwright). 대상: fixtures/web/portal_site.py

사이트 고유의 표기(표 머리글 '고객ID'·'이름' 등, 경로, label 이름)는 이 커넥터 안에만 둔다.
업무에는 customer_id / name / phone 같은 정규화된 키로 돌려준다.
로그인은 사용자가 RPA 전용 브라우저에서 직접 한다 (자격 증명 입력·저장 안 함).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from rpa.apps.base import AppInfo, register_app
from rpa.core.contracts import StepEnv
from rpa.core.errors import PreconditionFailed

APP = register_app(AppInfo(
    app_id="web_portal",
    name="LocalRPA 테스트 업무 포털",
    kind="web",
    description="로그인·고객 조회·상세(iframe)·CSV 내보내기·접수 등록이 있는 테스트 웹 포털",
    package_dir=Path(__file__).resolve().parent,
))

CUSTOMER_COLUMNS = {"고객ID": "customer_id", "이름": "name", "전화": "phone"}
RECORD_COLUMNS = {"접수번호": "record_id", "고객ID": "customer_id", "이름": "name", "전화": "phone",
                  "메모": "memo", "첨부": "attachment"}


def _norm(row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    return {mapping[k]: v for k, v in row.items() if k in mapping}


@dataclass(frozen=True)
class PortalConfig:
    login_wait: float = 180.0  # 로그인 화면이면 사용자가 로그인할 때까지 기다리는 시간


class PortalSite:
    app = APP

    def __init__(self, browser: Any, config: PortalConfig = PortalConfig()) -> None:
        self.b = browser
        self.config = config
        self.sel = APP.selectors()

    def path(self, name: str, customer_id: str = "") -> str:
        return self.sel["paths"][name].format(customer_id=quote(customer_id))

    def goto(self, env: StepEnv, path: str) -> None:
        """이동. 로그인 화면이면 사용자가 RPA 브라우저에서 로그인할 때까지 기다린 뒤 다시 이동한다."""
        from rpa.adapters.browser import LoginRequired

        try:
            self.b.goto(path)
        except LoginRequired:
            self.b.wait_for_login(self.config.login_wait, notify=env.notify)
            self.b.goto(path)

    # --- 조회 --------------------------------------------------------------
    def open(self, env: StepEnv) -> str:
        self.b.start()
        self.goto(env, self.sel["paths"]["customers"])
        self.b.one(self.sel["login_ready"])
        return self.b.current_url()

    def is_ready(self) -> bool:
        return not self.b.is_login_page() and self.b.exists(self.sel["login_ready"])

    def search(self, env: StepEnv, customer_id: str) -> tuple[str, list[dict[str, str]]]:
        """검색어 입력(readback) → 검색 → 결과 표. (입력된 검색어, 정규화된 행 목록)"""
        s = self.sel["search"]
        self.goto(env, self.sel["paths"]["customers"])
        typed = self.b.fill(s["input"], customer_id)
        self.b.click(s["button"], expect_navigation=True)
        return typed, [_norm(r, CUSTOMER_COLUMNS) for r in self.b.table(s["table"])]

    def open_detail(self, env: StepEnv, customer_id: str) -> dict[str, str]:
        """검색 결과의 고객ID 링크로 상세 화면을 열고 값(메모는 iframe)을 읽는다."""
        d = self.sel["detail"]
        self.goto(env, self.sel["paths"]["customers"] + f"?q={quote(customer_id)}")
        self.b.click({"role": "link", "name": customer_id}, expect_navigation=True)
        self.b.one(d["heading"])
        return {"customer_id": customer_id, "name": self.b.read_text(d["name"]), "phone": self.b.read_text(d["phone"]),
                "memo": self.b.read_text(d["memo"]), "url": self.b.current_url()}

    def export_csv(self, env: StepEnv, customer_id: str, save_dir: Path) -> dict[str, Any]:
        self.goto(env, self.path("customer_detail", customer_id))
        return self.b.click_and_download(self.sel["detail"]["export"], save_dir)

    @staticmethod
    def parse_export(path: Path) -> list[dict[str, str]]:
        """내보내기 CSV(UTF-8 BOM)를 정규화된 행 목록으로. 형식이 다르면 UnicodeDecodeError/csv.Error."""
        import csv
        import io

        rows = csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8-sig")))
        return [_norm(r, CUSTOMER_COLUMNS) for r in rows]

    def lookup_records(self, env: StepEnv, customer_id: str) -> list[dict[str, str]]:
        """접수 목록 재조회 (서버 기준). 정규화된 행 목록."""
        self.goto(env, self.path("records_by_customer", customer_id))
        return [_norm(r, RECORD_COLUMNS) for r in self.b.table(self.sel["register"]["records_table"])]

    # --- 변경 --------------------------------------------------------------
    def register(self, env: StepEnv, values: dict[str, str], attachment: Optional[str] = None) -> dict[str, Any]:
        """접수 등록. 제출 전에 입력값 readback이 다르면 제출하지 않는다.

        values: customer_id, name, phone, memo. 반환: record_id, readback
        서버가 5xx를 주면 TransientError — 업무는 재조회(lookup_records)로 대조해야 한다.
        """
        r = self.sel["register"]
        self.goto(env, self.path("record_new", values["customer_id"]))
        self.b.one(r["heading"])
        readback = {
            "customer_id": self.b.read_value(r["customer_id"]),
            "name": self.b.fill(r["name"], values["name"]),
            "phone": self.b.fill(r["phone"], values["phone"]),
            "memo": self.b.fill(r["memo"], values.get("memo", "")),
        }
        diffs = [k for k, v in readback.items() if v != values.get(k, "")]
        if diffs:
            raise PreconditionFailed(f"제출 전 입력값 readback 불일치: {', '.join(diffs)}")
        if attachment:
            self.b.upload(r["attachment"], attachment)
        self.b.click(r["submit"], expect_navigation=True)
        return {"record_id": self.b.read_text(r["record_id"]), "readback": readback}


def make_connector(data_dir: Path, control: Any, *, base_url: str, headless: bool = False,
                   login_wait: float = 180.0) -> tuple[Any, PortalSite]:
    from rpa.adapters.browser import BrowserAdapter, BrowserSettings

    b = BrowserAdapter(control, BrowserSettings(
        user_data_dir=str(Path(data_dir) / "browser_profiles" / APP.app_id), base_url=base_url, headless=headless))
    return b, PortalSite(b, PortalConfig(login_wait=login_wait))
