from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from rpa.adapters.fake import make_fake_adapters
from rpa.core.control import ControlState
from rpa.core.engine import Engine
from rpa.core.ownership import InProcessLock
from rpa.core.run_store import RunStore
from rpa.workflows.demo_fake.workflow import build


class Harness:
    """한 테스트 안에서 '프로세스 재시작'을 흉내 내기 위해 엔진을 새로 만들 수 있게 한다."""

    def __init__(self, tmp: Path) -> None:
        self.data_dir = tmp
        self.backend_path = tmp / "fake_backend.json"
        self.events: list[dict[str, Any]] = []
        self.workflow = build()
        self.new_engine()

    def new_engine(self, faults: Optional[dict[str, list[str]]] = None, control: Optional[ControlState] = None) -> Engine:
        self.store = RunStore(self.data_dir / "runs.db")
        self.adapters = make_fake_adapters(self.backend_path, faults)
        self.control = control or ControlState()
        self.engine = Engine(
            self.store, self.adapters, self.data_dir, control=self.control,
            runner_lock=InProcessLock("test-runner"), event_sink=self.events.append,
        )
        return self.engine

    @property
    def backend(self):
        return self.adapters["desktop"].backend

    def run(self, customer_id: str = "DEMO-001") -> str:
        return self.engine.start(self.workflow, {"customer_id": customer_id})

    def run_row(self, run_id: str) -> dict[str, Any]:
        return self.store.get_run(run_id)

    def event_types(self) -> list[str]:
        return [e["type"] for e in self.events]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
