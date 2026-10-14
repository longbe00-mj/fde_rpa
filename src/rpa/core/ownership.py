"""Runner 실행 소유권 인터페이스 — 활성 Runner는 최대 한 개.

프로세스 수준 잠금은 platform.windows.instance_lock.NamedMutexLock이 구현한다.
RunStore는 별도로 '미완료 실행이 있으면 새 실행 금지'를 보장한다.
"""

from __future__ import annotations

import threading
from typing import Protocol

from rpa.core.errors import RunnerBusy


class RunnerLock(Protocol):
    def acquire(self) -> None:
        """획득 실패 시 RunnerBusy."""

    def release(self) -> None: ...


class InProcessLock:
    """테스트·비Windows용 잠금. 같은 프로세스 안에서만 배타적이다."""

    _held: set[str] = set()
    _guard = threading.Lock()

    def __init__(self, name: str = "LocalRPA.Runner") -> None:
        self.name = name
        self._owned = False

    def acquire(self) -> None:
        with InProcessLock._guard:
            if self.name in InProcessLock._held:
                raise RunnerBusy("이미 활성 Runner가 있습니다")
            InProcessLock._held.add(self.name)
            self._owned = True

    def release(self) -> None:
        with InProcessLock._guard:
            if self._owned:
                InProcessLock._held.discard(self.name)
                self._owned = False

    def __enter__(self) -> "InProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def default_runner_lock() -> RunnerLock:
    import sys

    if sys.platform == "win32":
        from rpa.platform.windows.instance_lock import NamedMutexLock

        return NamedMutexLock()
    return InProcessLock()
