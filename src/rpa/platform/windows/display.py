"""화면 측정 (명세 §9.3). ctypes로 Win32 API를 읽기 전용으로 호출한다.

- 프로세스를 Per-Monitor DPI Aware v2로 설정해 물리 픽셀 좌표로 측정한다.
  (설정하지 않으면 배율 125% 이상에서 좌표·캡처 크기가 논리 픽셀로 어긋난다)
- GDI BitBlt로 주 모니터를 실제 캡처해 크기와 내용(검은 화면 여부)을 확인한다.
"""

from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from typing import Optional

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    _shcore = ctypes.WinDLL("shcore")
except OSError:  # Windows 8.0 이하
    _shcore = None

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
MONITORINFOF_PRIMARY = 1
MDT_EFFECTIVE_DPI = 0
SM_REMOTESESSION = 0x1000
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DESKTOP_SWITCHDESKTOP = 0x0100


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
try:
    _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
except AttributeError:
    pass
if _shcore is not None:
    _shcore.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                         ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
    _shcore.GetDpiForMonitor.restype = ctypes.c_long
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]


@dataclass
class MonitorInfo:
    device: str
    left: int
    top: int
    width: int
    height: int
    primary: bool
    dpi: Optional[int]

    @property
    def scale_percent(self) -> Optional[int]:
        return round(self.dpi * 100 / 96) if self.dpi else None


@dataclass
class CaptureProbe:
    ok: bool
    width: int = 0
    height: int = 0
    distinct_samples: int = 0  # 표본 픽셀의 서로 다른 색 수. 1이면 단색(검은 화면 등)
    error: str = ""


@dataclass
class DisplayReport:
    dpi_awareness_set: bool
    monitors: list[MonitorInfo] = field(default_factory=list)
    virtual_screen: tuple[int, int, int, int] = (0, 0, 0, 0)
    remote_session: bool = False
    input_desktop_accessible: bool = True  # False면 잠금 화면/보안 데스크톱일 가능성
    capture: Optional[CaptureProbe] = None

    @property
    def primary(self) -> Optional[MonitorInfo]:
        return next((m for m in self.monitors if m.primary), None)

    def to_dict(self) -> dict:
        d = asdict(self)
        for m, md in zip(self.monitors, d["monitors"]):
            md["scale_percent"] = m.scale_percent
        return d


def set_dpi_awareness() -> bool:
    """Per-Monitor v2 설정. 이미 설정돼 있으면(ACCESS_DENIED) 현재 상태를 그대로 쓴다."""
    try:
        if _user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return True
        ctx = _user32.GetThreadDpiAwarenessContext()
        return bool(_user32.AreDpiAwarenessContextsEqual(ctx, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    except AttributeError:  # Windows 10 1703 이전
        return False


def list_monitors() -> list[MonitorInfo]:
    result: list[MonitorInfo] = []

    def _cb(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            dpi = None
            if _shcore is not None:
                dx, dy = wintypes.UINT(), wintypes.UINT()
                if _shcore.GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                    dpi = dx.value
            result.append(
                MonitorInfo(
                    device=info.szDevice,
                    left=r.left, top=r.top, width=r.right - r.left, height=r.bottom - r.top,
                    primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    dpi=dpi,
                )
            )
        return True

    _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return result


def input_desktop_accessible() -> bool:
    """잠금 화면·UAC 보안 데스크톱이면 입력 데스크톱을 열 수 없다."""
    h = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return False
    _user32.CloseDesktop(h)
    return True


def capture_probe(mon: MonitorInfo, samples: int = 64) -> CaptureProbe:
    """주 모니터를 GDI로 캡처해 크기와 단색 여부를 확인한다. 이미지는 저장하지 않는다."""
    w, h = mon.width, mon.height
    hdc_screen = _user32.GetDC(None)
    if not hdc_screen:
        return CaptureProbe(False, error="GetDC 실패")
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = _gdi32.SelectObject(hdc_mem, hbmp)
    try:
        if not _gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, mon.left, mon.top, SRCCOPY | CAPTUREBLT):
            return CaptureProbe(False, error=f"BitBlt 실패 (오류 {ctypes.get_last_error()})")
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h  # top-down
        bmi.biPlanes, bmi.biBitCount = 1, 32
        buf = (ctypes.c_ubyte * (w * h * 4))()
        _gdi32.SelectObject(hdc_mem, old)
        lines = _gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        if lines != h:
            return CaptureProbe(False, w, h, error=f"GetDIBits {lines}/{h}줄")
        colors = set()
        n = int(math.sqrt(samples))
        for i in range(n):
            for j in range(n):
                x = (w - 1) * (i + 0.5) / n
                y = (h - 1) * (j + 0.5) / n
                o = (int(y) * w + int(x)) * 4
                colors.add(bytes(buf[o:o + 3]))
        return CaptureProbe(True, w, h, len(colors))
    finally:
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(None, hdc_screen)


def measure(probe_capture: bool = True) -> DisplayReport:
    aware = set_dpi_awareness()
    monitors = list_monitors()
    vs = tuple(_user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))  # SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN
    report = DisplayReport(
        dpi_awareness_set=aware,
        monitors=monitors,
        virtual_screen=vs,  # type: ignore[arg-type]
        remote_session=bool(_user32.GetSystemMetrics(SM_REMOTESESSION)),
        input_desktop_accessible=input_desktop_accessible(),
    )
    if probe_capture and report.primary:
        try:
            report.capture = capture_probe(report.primary)
        except OSError as e:
            report.capture = CaptureProbe(False, error=str(e))
    return report
