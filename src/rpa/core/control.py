"""실행 제어 플래그 (명세 §6 제어 동작).

제어 수신 스레드는 request_* 메서드로 플래그만 바꾼다. GUI를 호출하지 않는다.
실행 스레드는 동작 경계마다 checkpoint()를 호출하고, 대기는 sleep()/wait_until()으로 한다.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Optional

from rpa.core.errors import ControlChannelLost, RunCancelled

# 긴 대기도 이 간격 이하로 쪼개 취소를 확인한다
POLL_INTERVAL = 0.05


class ControlRequest(str, Enum):
    NONE = "NONE"
    PAUSE = "PAUSE"
    CANCEL = "CANCEL"


class ControlState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._pause_requested = False
        self._cancel_requested = False
        self._channel_lost = False

    # --- 제어 스레드 측 -------------------------------------------------
    def request_pause(self) -> None:
        with self._changed:
            self._pause_requested = True
            self._changed.notify_all()

    def request_resume(self) -> None:
        with self._changed:
            self._pause_requested = False
            self._changed.notify_all()

    def request_cancel(self) -> None:
        with self._changed:
            self._cancel_requested = True
            self._changed.notify_all()

    def mark_channel_lost(self) -> None:
        with self._changed:
            self._channel_lost = True
            self._changed.notify_all()

    # --- 조회 -----------------------------------------------------------
    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def pause_requested(self) -> bool:
        with self._lock:
            return self._pause_requested

    # --- 실행 스레드 측 -------------------------------------------------
    def raise_if_stopped(self) -> None:
        """취소·채널 끊김이면 예외. 일시정지는 확인하지 않는다."""
        with self._lock:
            if self._cancel_requested:
                raise RunCancelled("취소 요청")
            if self._channel_lost:
                raise ControlChannelLost("Controller 제어 채널 끊김")

    def checkpoint(
        self,
        on_paused: Optional[Callable[[], None]] = None,
        on_resumed: Optional[Callable[[], None]] = None,
    ) -> bool:
        """동작 경계. 취소면 RunCancelled, 일시정지 요청이면 재개/취소까지 대기.

        반환값: 이번 호출에서 일시정지 후 재개되었으면 True (호출자는 화면·업무 건을 재확인해야 함).
        """
        self.raise_if_stopped()
        with self._changed:
            if not self._pause_requested:
                return False
        if on_paused:
            on_paused()
        with self._changed:
            while self._pause_requested and not self._cancel_requested and not self._channel_lost:
                self._changed.wait(timeout=POLL_INTERVAL * 4)
        self.raise_if_stopped()
        if on_resumed:
            on_resumed()
        return True

    def sleep(self, seconds: float) -> None:
        """취소를 확인하는 대기. time.sleep(30) 같은 긴 블로킹을 쓰지 않는다."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.raise_if_stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            with self._changed:
                self._changed.wait(timeout=min(POLL_INTERVAL, remaining))

    def wait_until(self, predicate: Callable[[], bool], timeout: float, interval: float = 0.2) -> bool:
        """단조 시계 deadline까지 조건 대기. 성공 True, 시간 초과 False, 취소 시 예외."""
        deadline = time.monotonic() + timeout
        while True:
            self.raise_if_stopped()
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(min(interval, max(0.0, deadline - time.monotonic())))
