"""공통 계약 (명세 §5).

UIA 핸들, Playwright Page/Locator, 열린 파일 객체는 여기에 담지 않는다.
여기의 타입은 모두 JSON으로 직렬화할 수 있어야 한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from rpa.core.control import ControlState
    from rpa.core.run_context import RunContext


class ExecutionStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    WAITING_USER = "WAITING_USER"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    FINISHED = "FINISHED"

    @property
    def is_terminal(self) -> bool:
        return self in (ExecutionStatus.CANCELLED, ExecutionStatus.FAILED, ExecutionStatus.FINISHED)


class BusinessOutcome(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    AUTO_VERIFIED = "AUTO_VERIFIED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class EffectKind(str, Enum):
    READ_ONLY = "READ_ONLY"
    LOCAL_CHANGE = "LOCAL_CHANGE"
    EXTERNAL_CHANGE = "EXTERNAL_CHANGE"

    @property
    def is_change(self) -> bool:
        return self is not EffectKind.READ_ONLY


class DispatchState(str, Enum):
    """변경 동작의 전송 상태. ActionResult와 operations 테이블에서 사용."""

    INTENT_RECORDED = "INTENT_RECORDED"  # 의도만 저장. 전송 여부 미확정으로 취급
    DISPATCHED = "DISPATCHED"  # 전송 호출이 반환됨. 업무 성공 아님
    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"  # 검증/대조로 반영 확인
    CONFIRMED_NOT_APPLIED = "CONFIRMED_NOT_APPLIED"  # 대조로 미반영 확인
    UNKNOWN = "UNKNOWN"  # 전송 중 예외·중단 등으로 결과 불명확
    NOT_DISPATCHED = "NOT_DISPATCHED"  # READ_ONLY 동작 등 전송 개념 없음


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class VerificationMode(str, Enum):
    RULE = "RULE"
    HUMAN = "HUMAN"


class ReconcileStatus(str, Enum):
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ActionResult:
    """실행 요청을 했다는 관측. 업무 성공 자체가 아니다."""

    action_id: str
    dispatch_state: DispatchState
    outputs: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dispatch_state"] = self.dispatch_state.value
        return d


@dataclass
class CheckResult:
    status: CheckStatus
    verification_mode: VerificationMode
    business_key: Optional[str]
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    checked_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["verification_mode"] = self.verification_mode.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CheckResult":
        d = dict(d)
        d["status"] = CheckStatus(d["status"])
        d["verification_mode"] = VerificationMode(d["verification_mode"])
        return cls(**d)


@dataclass
class ReconcileResult:
    """외부 처리 여부 재조회 결과."""

    status: ReconcileStatus
    observed: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class RetryPolicy:
    """READ_ONLY 단계의 정의된 일시 오류(TransientError)에만 적용된다.

    변경 단계는 이 정책으로 재시도하지 않는다. reconcile이 NOT_APPLIED를 확인했을 때만
    max_attempts 안에서 다시 수행한다.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.0


@dataclass(frozen=True)
class ManualResolution:
    """사용자에게 필요한 작업과 재개 조건."""

    instruction: str
    resume_condition: str = "사용자가 확인 결과를 제출하면 재개"


@dataclass
class StepEnv:
    """단계 함수에 전달되는 실행 환경. 직렬화 대상이 아니다."""

    step_id: str
    adapters: dict[str, Any]
    control: "ControlState"
    deadline: float  # time.monotonic() 기준 단계 전체 상한
    action_timeout: float
    logical_operation_id: Optional[str] = None
    _add_evidence: Optional[Callable[[str, str], None]] = None
    _notify: Optional[Callable[[str], None]] = None

    def adapter(self, name: str) -> Any:
        try:
            return self.adapters[name]
        except KeyError:
            raise KeyError(f"어댑터 {name!r}가 등록되지 않았습니다") from None

    def remaining(self) -> float:
        import time

        return max(0.0, self.deadline - time.monotonic())

    def call_timeout(self) -> float:
        """개별 호출에 넘길 timeout: action_timeout과 남은 단계 시간 중 작은 값."""
        return min(self.action_timeout, self.remaining())

    def add_evidence(self, kind: str, path: str) -> None:
        if self._add_evidence:
            self._add_evidence(kind, path)

    def notify(self, message: str) -> None:
        """실행 중 사용자에게 알릴 안내 (예: 'RPA 브라우저에서 로그인하세요'). 이벤트 step_notice로 전달."""
        if self._notify:
            self._notify(message)


# handler(ctx, env) -> ActionResult
StepHandler = Callable[["RunContext", StepEnv], ActionResult]
# verifier(ctx, env, action_result) -> CheckResult
StepVerifier = Callable[["RunContext", StepEnv, ActionResult], CheckResult]
# precondition(ctx, env) -> None, 실패 시 PreconditionFailed/UserActionRequired 발생
StepPrecondition = Callable[["RunContext", StepEnv], None]
# reconcile(ctx, env) -> ReconcileResult
StepReconcile = Callable[["RunContext", StepEnv], ReconcileResult]


@dataclass(frozen=True)
class StepDefinition:
    id: str
    handler: StepHandler
    verifier: StepVerifier
    effect_kind: EffectKind = EffectKind.READ_ONLY
    version: str = "1"
    input_mapping: tuple[str, ...] = ()
    precondition: Optional[StepPrecondition] = None
    timeout: float = 60.0
    action_timeout: float = 15.0
    # 동일 논리 변경의 재시도·재개에서도 유지되는 식별자 템플릿. {business_key}, {run_id} 사용 가능
    operation_key: Optional[str] = None
    retry_policy: RetryPolicy = RetryPolicy()
    reconcile: Optional[StepReconcile] = None
    manual_resolution: Optional[ManualResolution] = None

    def __post_init__(self) -> None:
        if self.effect_kind.is_change and not self.operation_key:
            raise ValueError(f"변경 단계 {self.id!r}에는 operation_key가 필요합니다")
        if self.timeout <= 0 or self.action_timeout <= 0:
            raise ValueError(f"단계 {self.id!r}의 timeout은 유한한 양수여야 합니다")


@dataclass(frozen=True)
class InputField:
    name: str
    type: str = "string"  # string | integer | number | boolean
    required: bool = True
    pattern: Optional[str] = None
    description: str = ""


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    version: str
    description: str
    steps: tuple[StepDefinition, ...]
    inputs: tuple[InputField, ...] = ()
    # 입력에서 업무 식별자를 뽑을 필드명
    business_key_field: Optional[str] = None
    environment_requirements: tuple[str, ...] = ()
    supported_modes: tuple[str, ...] = ("attended",)
    # 소스 해시 계산 대상(모듈 파일 경로). 실행 중 코드 변경 감지에 사용
    source_files: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Workflow {self.id!r}에 중복된 step id가 있습니다")

    def step(self, step_id: str) -> StepDefinition:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def step_index(self, step_id: str) -> int:
        return [s.id for s in self.steps].index(step_id)
