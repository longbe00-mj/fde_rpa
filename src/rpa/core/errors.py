"""엔진이 구분해서 처리하는 오류 (명세 §6, §7)."""

from __future__ import annotations


class RpaError(Exception):
    """LocalRPA 오류의 기반 클래스."""


class TransientError(RpaError):
    """조회·요소 탐색에서 정의된 일시 오류. READ_ONLY 단계에서만 제한 재시도한다."""


class PreconditionFailed(RpaError):
    """대상 화면·입력·업무 건이 단계의 사전조건을 만족하지 않음."""


class UserActionRequired(RpaError):
    """로그인 만료 등 사용자 조치가 필요한 상태. WAITING_USER로 전환한다."""

    def __init__(self, reason: str, instruction: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.instruction = instruction


class RunCancelled(RpaError):
    """취소 요청을 동작 경계에서 감지함. 이미 전송한 작업의 취소를 뜻하지 않는다."""


class FailSafeTriggered(RpaError):
    """PyAutoGUI FAILSAFE 등 사용자 비상 중단. 재시도 대상이 아니다."""


class ControlChannelLost(RpaError):
    """Controller 제어 채널이 끊김. 다음 동작 경계에서 입력을 멈춘다."""


class RunnerBusy(RpaError):
    """이미 활성 Runner가 있음."""


class VersionMismatch(RpaError):
    """재개 시 workflow 코드/설정 해시가 실행 시작 시점과 다름."""


class InvalidInput(RpaError):
    """입력 스키마 검증 실패."""


class InvalidTransition(RpaError):
    """허용되지 않는 실행 상태 전이."""
