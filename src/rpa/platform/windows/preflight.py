"""실행 전 환경 점검 — 실제 화면 측정값으로 판정한다 (사전 합의 해상도 없음).

FAIL이 하나라도 있으면 화면 입력을 시작하지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from rpa.core.screen_scale import ScreenshotPlan, plan_screenshot
from rpa.platform.windows.display import DisplayReport, measure

MIN_WIDTH, MIN_HEIGHT = 1280, 720


@dataclass
class Check:
    id: str
    level: str  # PASS | WARN | FAIL
    message: str


@dataclass
class PreflightResult:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    display: Optional[dict] = None
    screenshot_plan: Optional[dict] = None
    screenshot_plan_many_images: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _plan_dict(p: ScreenshotPlan) -> dict:
    return {**asdict(p), "visual_tokens": p.tokens}


def evaluate(report: DisplayReport) -> PreflightResult:
    checks: list[Check] = []

    def add(id_: str, ok: bool, msg_ok: str, msg_bad: str, bad_level: str = "FAIL") -> None:
        checks.append(Check(id_, "PASS" if ok else bad_level, msg_ok if ok else msg_bad))

    add("dpi_awareness", report.dpi_awareness_set,
        "Per-Monitor DPI v2 설정됨 (물리 픽셀 좌표)",
        "DPI 인식 설정 실패: 배율 환경에서 좌표·캡처가 어긋날 수 있음")
    add("input_desktop", report.input_desktop_accessible,
        "입력 데스크톱 접근 가능",
        "입력 데스크톱에 접근 불가: 화면 잠금 또는 UAC 보안 데스크톱 상태")

    primary = report.primary
    if primary is None:
        checks.append(Check("primary_monitor", "FAIL", "주 모니터를 찾지 못함"))
    else:
        scale = f", 배율 {primary.scale_percent}%" if primary.scale_percent else ""
        add("primary_resolution", primary.width >= MIN_WIDTH and primary.height >= MIN_HEIGHT,
            f"주 모니터 {primary.width}x{primary.height}{scale}",
            f"주 모니터 {primary.width}x{primary.height}{scale}: {MIN_WIDTH}x{MIN_HEIGHT} 미만이라 화면 요소가 잘릴 수 있음",
            "WARN")
        cap = report.capture
        if cap is None:
            checks.append(Check("capture", "WARN", "캡처 점검을 건너뜀"))
        elif not cap.ok:
            checks.append(Check("capture", "FAIL", f"화면 캡처 실패: {cap.error}"))
        elif (cap.width, cap.height) != (primary.width, primary.height):
            checks.append(Check("capture", "FAIL",
                                f"캡처 크기 {cap.width}x{cap.height} ≠ 모니터 {primary.width}x{primary.height} (DPI 가상화 의심)"))
        elif cap.distinct_samples <= 1:
            checks.append(Check("capture", "FAIL",
                                "캡처가 단색: 화면 꺼짐·잠금·RDP 최소화/연결 해제 상태일 수 있음"))
        else:
            checks.append(Check("capture", "PASS", f"주 모니터 실제 캡처 {cap.width}x{cap.height} 정상"))

    add("monitor_count", len(report.monitors) <= 1,
        "모니터 1대",
        f"모니터 {len(report.monitors)}대: PyAutoGUI 입력·캡처는 주 모니터만 지원 — 대상 창을 주 모니터에 두어야 함",
        "WARN")
    scales = {m.scale_percent for m in report.monitors if m.scale_percent}
    if len(scales) > 1:
        checks.append(Check("mixed_scaling", "WARN", f"모니터별 배율이 다름 {sorted(scales)}: 창 이동 시 좌표 재계산 필요"))
    add("remote_session", not report.remote_session,
        "로컬 콘솔 세션",
        "원격(RDP) 세션: 최소화·연결 해제 시 화면 입력/캡처가 중단됨", "WARN")

    result = PreflightResult(
        ok=not any(c.level == "FAIL" for c in checks),
        checks=checks,
        display=report.to_dict(),
    )
    if primary is not None:
        result.screenshot_plan = _plan_dict(plan_screenshot(primary.width, primary.height, primary.left, primary.top))
        result.screenshot_plan_many_images = _plan_dict(
            plan_screenshot(primary.width, primary.height, primary.left, primary.top, many_images=True)
        )
    return result


def run_preflight(probe_capture: bool = True) -> PreflightResult:
    return evaluate(measure(probe_capture=probe_capture))
