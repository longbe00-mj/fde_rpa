"""FakeAdapter — GUI 없이 엔진의 정상/실패/취소/결과 미확정 경로를 시험하기 위한 가짜 웹·데스크톱.

FakeBackend는 '외부 시스템'을 흉내 낸다. JSON 파일로 저장하면 프로세스가 죽은 뒤에도
반영 여부를 재조회(reconcile)할 수 있다.

장애 주입(faults)은 호출마다 앞에서부터 하나씩 소비된다:
  web.get_customer:  "transient" | "login_expired"
  desktop.save:      "error_before_apply" | "error_after_apply" | "crash_before_apply"
                     | "crash_after_apply" | "corrupt" | "slow"
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from rpa.core.control import ControlState
from rpa.core.errors import TransientError, UserActionRequired


class SimulatedCrash(BaseException):
    """프로세스 강제 종료 흉내. 엔진의 except Exception에 잡히지 않는다."""


class FaultPlan:
    def __init__(self, faults: Optional[dict[str, list[str]]] = None) -> None:
        self._faults = {k: list(v) for k, v in (faults or {}).items()}
        self._lock = threading.Lock()

    def next(self, op: str) -> Optional[str]:
        with self._lock:
            q = self._faults.get(op)
            return q.pop(0) if q else None


class FakeBackend:
    """가짜 데스크톱 앱의 저장소. path가 있으면 JSON 파일로 영속화."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._records: dict[str, dict[str, Any]] = {}
        self.save_calls = 0
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._records = data.get("records", {})
            self.save_calls = data.get("save_calls", 0)

    def _flush(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"records": self._records, "save_calls": self.save_calls}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def put(self, customer_id: str, values: dict[str, Any]) -> str:
        self.save_calls += 1
        record_id = f"REC-{len(self._records) + 1:04d}"
        self._records[customer_id] = {"record_id": record_id, **values}
        self._flush()
        return record_id

    def get(self, customer_id: str) -> Optional[dict[str, Any]]:
        r = self._records.get(customer_id)
        return dict(r) if r else None

    def count(self) -> int:
        return len(self._records)


DEFAULT_CUSTOMERS: dict[str, dict[str, str]] = {
    "DEMO-001": {"customer_id": "DEMO-001", "name": "홍길동", "phone": "010-0012-3456"},
    "DEMO-002": {"customer_id": "DEMO-002", "name": "김철수 & Co. <특수>", "phone": "0001"},
}


class FakeWebAdapter:
    """가짜 웹 조회 화면."""

    def __init__(self, customers: Optional[dict[str, dict[str, str]]] = None, faults: Optional[FaultPlan] = None):
        self.customers = customers if customers is not None else dict(DEFAULT_CUSTOMERS)
        self.faults = faults or FaultPlan()
        self.calls = 0

    def get_customer(self, customer_id: str) -> Optional[dict[str, str]]:
        self.calls += 1
        fault = self.faults.next("web.get_customer")
        if fault == "transient":
            raise TransientError("페이지 로딩 지연 (가짜)")
        if fault == "login_expired":
            raise UserActionRequired("로그인 만료 (가짜)", "RPA 전용 브라우저에서 다시 로그인한 뒤 resolve done")
        c = self.customers.get(customer_id)
        return dict(c) if c else None


class FakeDesktopAdapter:
    """가짜 Windows 입력 앱."""

    def __init__(self, backend: FakeBackend, faults: Optional[FaultPlan] = None):
        self.backend = backend
        self.faults = faults or FaultPlan()

    def save_customer(self, values: dict[str, Any], control: ControlState, timeout: float) -> str:
        fault = self.faults.next("desktop.save")
        if fault == "error_before_apply":
            raise RuntimeError("저장 버튼 클릭 실패 (가짜, 미반영)")
        if fault == "crash_before_apply":
            raise SimulatedCrash("저장 전 프로세스 종료 (가짜)")
        if fault == "slow":
            control.sleep(min(timeout, 2.0))  # 취소 가능한 대기
        stored = dict(values)
        if fault == "corrupt":
            stored["name"] = stored["name"] + "?"  # 입력 깨짐 흉내
        record_id = self.backend.put(values["customer_id"], stored)
        if fault == "error_after_apply":
            raise TimeoutError("저장 응답 시간 초과 (가짜, 실제로는 반영됨)")
        if fault == "crash_after_apply":
            raise SimulatedCrash("저장 직후 프로세스 종료 (가짜)")
        return record_id

    def find_customer(self, customer_id: str) -> Optional[dict[str, Any]]:
        return self.backend.get(customer_id)


def make_fake_adapters(
    backend_path: Optional[str | Path] = None,
    faults: Optional[dict[str, list[str]]] = None,
) -> dict[str, Any]:
    plan = FaultPlan(faults)
    backend = FakeBackend(backend_path)
    return {
        "web": FakeWebAdapter(faults=plan),
        "desktop": FakeDesktopAdapter(backend, faults=plan),
    }
