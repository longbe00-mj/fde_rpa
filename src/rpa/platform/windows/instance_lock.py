"""Windows named mutex 기반 단일 Runner 잠금.

프로세스가 강제 종료되어도 커널이 핸들을 닫으므로 잠금이 남지 않는다.
같은 로그인 세션(Local\\)으로 범위를 한정한다.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from rpa.core.errors import RunnerBusy

ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class NamedMutexLock:
    def __init__(self, name: str = "Local\\LocalRPA.Runner") -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> None:
        if self._handle:
            return
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            raise OSError(err, f"CreateMutexW 실패: {self.name}")
        if err == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            raise RunnerBusy("이미 활성 Runner가 있습니다 (다른 LocalRPA 프로세스 실행 중)")
        self._handle = handle

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "NamedMutexLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
