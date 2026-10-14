"""전역 중단·일시정지 단축키 (RegisterHotKey).

등록한 조합이 눌렸다는 신호만 받는다. 다른 키 입력은 수집·저장하지 않는다 (키보드 훅 아님).
제어 스레드에서 동작하며 ControlState 플래그만 바꾼다 — GUI를 호출하지 않는다.

기본값: Ctrl+Alt+Q = 취소, Ctrl+Alt+P = 일시정지/재개 전환
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

from rpa.core.control import ControlState

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HK_CANCEL, HK_PAUSE = 0xB001, 0xB002

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


class ControlHotkeys:
    def __init__(
        self,
        control: ControlState,
        cancel_key: str = "Q",
        pause_key: str = "P",
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.control = control
        self.cancel_vk = ord(cancel_key.upper())
        self.pause_vk = ord(pause_key.upper())
        self.notify = notify or (lambda msg: None)
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.registered: list[str] = []
        self.errors: list[str] = []

    def start(self) -> "ControlHotkeys":
        self._thread = threading.Thread(target=self._loop, name="rpa-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self

    def stop(self) -> None:
        if self._thread and self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "ControlHotkeys":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        for hk_id, vk, label in ((HK_CANCEL, self.cancel_vk, f"Ctrl+Alt+{chr(self.cancel_vk)}=취소"),
                                 (HK_PAUSE, self.pause_vk, f"Ctrl+Alt+{chr(self.pause_vk)}=일시정지/재개")):
            if user32.RegisterHotKey(None, hk_id, mods, vk):
                self.registered.append(label)
            else:
                self.errors.append(f"{label} 등록 실패 (다른 프로그램이 사용 중일 수 있음)")
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message != WM_HOTKEY:
                    continue
                if msg.wParam == HK_CANCEL:
                    self.control.request_cancel()
                    self.notify("단축키: 취소 요청 (다음 동작 경계에서 중단)")
                elif msg.wParam == HK_PAUSE:
                    if self.control.pause_requested:
                        self.control.request_resume()
                        self.notify("단축키: 재개 요청 (다음 단계에서 화면·업무 건을 다시 확인)")
                    else:
                        self.control.request_pause()
                        self.notify("단축키: 일시정지 요청 (현재 단계가 끝나는 경계에서 멈춤)")
        finally:
            user32.UnregisterHotKey(None, HK_CANCEL)
            user32.UnregisterHotKey(None, HK_PAUSE)
