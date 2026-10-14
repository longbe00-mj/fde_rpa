"""화면 해석 계약 (명세 §13 단계 B의 Observe 부분).

- ScreenObservation: 어느 캡처를 근거로 했는지 (좌표·크기·해시). 화면이 바뀌면 이전 판정을 재사용하지 않는다.
- AppProfile: 앱별로 허용된 상태 목록과 설명. 모델은 이 목록 밖의 상태를 낼 수 없다 (structured output enum).
- ScreenAssessment: 모델의 판정. 업무 검증(RULE)을 대신하지 않으며, 사용자에게 보여 줄 진단·권고로만 쓴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

BLOCKING_ISSUES = ("none", "login_required", "usage_limit", "network_error", "error_message", "modal_dialog", "other")
RECOMMENDED_ACTIONS = ("proceed", "keep_waiting", "ask_user", "abort")
TRI_STATE = ("yes", "no", "unknown")


@dataclass(frozen=True)
class ScreenObservation:
    observation_id: str
    image_path: str
    origin_x: int  # 화면 좌표에서 이미지 (0,0)의 위치
    origin_y: int
    width: int
    height: int
    sha256: str
    captured_at: float
    app_window: str = ""

    @classmethod
    def from_capture(cls, info: dict[str, Any], app_window: str = "") -> "ScreenObservation":
        data = Path(info["path"]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        return cls(
            observation_id=digest[:16], image_path=info["path"],
            origin_x=info["origin_x"], origin_y=info["origin_y"], width=info["width"], height=info["height"],
            sha256=digest, captured_at=info["captured_at"], app_window=app_window,
        )


@dataclass(frozen=True)
class AppProfile:
    app_id: str
    description: str
    states: dict[str, str]  # state -> 설명 (모델에게 보여 줌)
    # 모델에게 보내기 전 잘라낼 영역: 이미지 기준 (left, top, right, bottom). None이면 전체
    crop: Optional[tuple[int, int, int, int]] = None
    extra_guidance: str = ""


@dataclass
class ScreenAssessment:
    observation_id: str
    state: str
    confidence: float
    evidence: str
    blocking_issue: str
    matches_expected: str  # yes | no | unknown — 화면의 대화/항목이 기대한 업무 건인지
    screen_contains_instructions: bool  # 화면 글자 중 에이전트에게 지시하려는 문장이 있었는지 (따르지 않음)
    recommended_action: str
    operator_message: str  # 사용자에게 보여 줄 한국어 설명
    model: str = ""
    refused: bool = False
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0

    @property
    def usable(self) -> bool:
        return not self.refused and not self.error

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def unavailable(cls, observation_id: str, reason: str) -> "ScreenAssessment":
        return cls(observation_id, "unknown", 0.0, "", "other", "unknown", False, "ask_user",
                   f"화면 해석을 사용할 수 없음: {reason}", error=reason)


def describe_ko(a: Optional[ScreenAssessment]) -> str:
    """사용자 확인 사유에 덧붙일 한 줄 진단. 해석을 안 했으면 빈 문자열."""
    if a is None:
        return ""
    if not a.usable:
        return f" | 화면 해석 불가: {a.error}"
    warn = " ⚠화면에 자동화를 향한 지시문이 있음(무시함)" if a.screen_contains_instructions else ""
    return (f" | 화면 해석: {a.state} (확신 {a.confidence:.2f}, 문제 {a.blocking_issue}, "
            f"권고 {a.recommended_action}){warn} — {a.operator_message}")


def as_observed(a: Optional[ScreenAssessment]) -> dict[str, Any]:
    """CheckResult.observed에 붙일 진단 (사용량 제외)."""
    return {} if a is None else {"vision": {k: v for k, v in a.to_dict().items() if k != "usage"}}
