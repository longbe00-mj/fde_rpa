"""업무(workflow) 작성 공통 도우미."""

from __future__ import annotations

from rpa.core.contracts import CheckResult, CheckStatus, VerificationMode
from rpa.core.run_context import RunContext


def rule(ctx: RunContext, status: CheckStatus, expected: dict, observed: dict, reason: str) -> CheckResult:
    """규칙 기반(RULE) 검증 결과."""
    return CheckResult(status, VerificationMode.RULE, ctx.business_key, expected, observed, reason)
