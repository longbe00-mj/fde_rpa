"""Win32 창·포커스·입력·클립보드 헬퍼 (ctypes).

- 창 식별: 프로세스·제목·클래스, 클라이언트 영역의 화면 좌표, 다른 창에 가려졌는지(WindowFromPoint)
- 포커스: SetForegroundWindow는 한 번만 시도한다. 반복해서 포커스를 빼앗지 않는다.
- 텍스트 입력: SendInput KEYEVENTF_UNICODE (한글·특수문자·선행 0을 그대로 전송, IME 상태 비의존)
- 클립보드: 텍스트 읽기/쓰기와 원래 텍스트 복원
좌표는 모두 물리 픽셀 (프로세스가 Per-Monitor DPI v2여야 함 — display.set_dpi_awareness).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

GA_ROOT = 2
GA_ROOTOWNER = 3
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
DWMWA_EXTENDED_FRAME_BOUNDS = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
MODIFIER_VKS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def contains_rect(self, other: "Rect") -> bool:
        return (self.left <= other.left and self.top <= other.top
                and other.right <= self.right and other.bottom <= self.bottom)


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    process_name: str
    visible: bool
    minimized: bool
    frame: Rect  # DWM 확장 프레임 (그림자 제외)
    client: Rect  # 클라이언트 영역의 화면 좌표
    dpi: int


# --- 창 조회 -----------------------------------------------------------------
def _text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_image(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def frame_rect(hwnd: int) -> Rect:
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)


def client_rect(hwnd: int) -> Rect:
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    p = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return Rect(p.x, p.y, r.right - r.left, r.bottom - r.top)


def window_info(hwnd: int) -> Optional[WindowInfo]:
    if not user32.IsWindow(hwnd):
        return None
    pid = window_pid(hwnd)
    try:
        dpi = int(user32.GetDpiForWindow(hwnd))
    except AttributeError:
        dpi = 96
    return WindowInfo(
        hwnd=int(hwnd), pid=pid, title=_text(hwnd), class_name=_class(hwnd),
        process_name=Path(process_image(pid)).name, visible=bool(user32.IsWindowVisible(hwnd)),
        minimized=bool(user32.IsIconic(hwnd)), frame=frame_rect(hwnd), client=client_rect(hwnd), dpi=dpi,
    )


def top_level_windows(visible_only: bool = True) -> list[WindowInfo]:
    hwnds: list[int] = []

    def _cb(hwnd, _):
        if not visible_only or user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return [w for w in (window_info(h) for h in hwnds) if w is not None]


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def root_owner(hwnd: int) -> int:
    return int(user32.GetAncestor(hwnd, GA_ROOTOWNER) or 0) if hwnd else 0


def root_at_point(x: int, y: int) -> int:
    """해당 화면 좌표에 실제로 보이는 창의 최상위 창. 가려짐 검사에 사용."""
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    return int(user32.GetAncestor(h, GA_ROOT) or 0) if h else 0


GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_MINIMIZEBOX = 0x00020000
WS_EX_TOOLWINDOW = 0x00000080
SW_SHOWMINNOACTIVATE = 7
SW_SHOWNOACTIVATE = 4
# 최소화 대상에서 제외하는 셸·시스템 창 클래스
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "IslandWindow"}


def can_minimize(hwnd: int) -> bool:
    """최소화 버튼이 있는 일반 창만 True (도구 창·셸 창 제외)."""
    if _class(hwnd) in SHELL_CLASSES:
        return False
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return bool(style & WS_MINIMIZEBOX) and not (exstyle & WS_EX_TOOLWINDOW)


def minimize_window(hwnd: int) -> bool:
    """활성 창을 바꾸지 않고 최소화. 성공하면 True."""
    user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVATE)
    return bool(user32.IsIconic(hwnd))


def restore_window_noactivate(hwnd: int) -> None:
    if user32.IsWindow(hwnd) and user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def close_window(hwnd: int) -> None:
    """WM_CLOSE 요청 (앱이 정상 종료 절차를 밟게 한다)."""
    user32.PostMessageW(hwnd, 0x0010, 0, 0)


def try_set_foreground(hwnd: int) -> bool:
    """한 번만 시도. Windows 포그라운드 잠금 규칙 때문에 실패할 수 있다."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return foreground_hwnd() == hwnd


# --- 권한 (UIPI) ---------------------------------------------------------------
def _token_elevated(process_handle: int) -> Optional[bool]:
    tok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        val = wintypes.DWORD()
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(tok, TOKEN_ELEVATION, ctypes.byref(val), ctypes.sizeof(val),
                                            ctypes.byref(size)):
            return None
        return bool(val.value)
    finally:
        kernel32.CloseHandle(tok)


def current_process_elevated() -> Optional[bool]:
    return _token_elevated(kernel32.GetCurrentProcess())


def process_elevated(pid: int) -> Optional[bool]:
    """None이면 조회 불가 (권한 부족 — 상위 권한 프로세스일 가능성)."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        return _token_elevated(h)
    finally:
        kernel32.CloseHandle(h)


# --- 키보드 -------------------------------------------------------------------
def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        # UIPI(상위 권한 창) 또는 보안 데스크톱이면 0이 반환된다
        raise OSError(ctypes.get_last_error(), f"SendInput {sent}/{len(inputs)}건만 전송됨 (UIPI 차단 가능성)")


def send_unicode(text: str) -> None:
    """UTF-16 코드 단위별 KEYEVENTF_UNICODE down/up. 제어 문자는 허용하지 않는다."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("send_unicode에는 제어 문자(줄바꿈·탭 등)를 넣을 수 없습니다. key 동작을 사용하세요.")
    data = text.encode("utf-16-le")
    units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
    inputs: list[INPUT] = []
    for u in units:
        inputs.append(_key_input(scan=u, flags=KEYEVENTF_UNICODE))
        inputs.append(_key_input(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if inputs:
        _send(inputs)


def pressed_modifiers() -> list[int]:
    return [vk for vk in MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000]


def release_modifiers() -> list[int]:
    """눌린 채 남은 수정자 키를 해제한다 (중단 경로의 finally에서 호출)."""
    down = pressed_modifiers()
    if down:
        try:
            _send([_key_input(vk=vk, flags=KEYEVENTF_KEYUP) for vk in down])
        except OSError:
            pass
    return down


# --- 클립보드 -----------------------------------------------------------------
class ClipboardBusy(RuntimeError):
    pass


def _open_clipboard(retries: int = 10) -> None:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise ClipboardBusy("클립보드를 열 수 없습니다 (다른 프로그램이 사용 중)")


def clipboard_sequence() -> int:
    return int(user32.GetClipboardSequenceNumber())


# CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE — 텍스트와 Windows가 자동 합성하는 형식
_PLAIN_TEXT_FORMATS = {1, 7, 13, 16}
# OLE 클립보드(OleSetClipboard)로 복사하면 함께 생기는 관리용 형식. 내용 데이터가 아니다
_OLE_BOOKKEEPING_FORMATS = {"DataObject", "Ole Private Data"}


def _format_name(fmt: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    n = user32.GetClipboardFormatNameW(fmt, buf, 128)
    return buf.value if n else ""


def clipboard_has_only_text() -> bool:
    """텍스트 외 형식(서식 있는 텍스트·이미지·파일 등)이 있으면 False — 덮어쓰면 원래대로 복원할 수 없다."""
    _open_clipboard()
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                return True
            if fmt in _PLAIN_TEXT_FORMATS or _format_name(fmt) in _OLE_BOOKKEEPING_FORMATS:
                continue
            return False
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> Optional[str]:
    _open_clipboard()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: Optional[str]) -> None:
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        if text is None:
            return
        data = text.encode("utf-16-le") + b"\0\0"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise OSError(ctypes.get_last_error(), "SetClipboardData 실패")
    finally:
        user32.CloseClipboard()
