"""Workflow 입력 검증. 문자열 값은 변형하지 않는다 (선행 0·한글·특수문자 보존)."""

from __future__ import annotations

import re
from typing import Any

from rpa.core.contracts import WorkflowDefinition
from rpa.core.errors import InvalidInput

_TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def validate_inputs(workflow: WorkflowDefinition, raw: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    known = {f.name for f in workflow.inputs}
    for name in raw:
        if name not in known:
            errors.append(f"알 수 없는 입력: {name}")
    out: dict[str, Any] = {}
    for f in workflow.inputs:
        if f.name not in raw or raw[f.name] is None or raw[f.name] == "":
            if f.required:
                errors.append(f"필수 입력 누락: {f.name}")
            continue
        v = raw[f.name]
        allowed = _TYPES.get(f.type)
        if allowed is None:
            errors.append(f"{f.name}: 지원하지 않는 타입 {f.type}")
            continue
        # bool은 int의 하위 타입이므로 integer/number에서 거부
        if not isinstance(v, allowed) or (f.type in ("integer", "number") and isinstance(v, bool)):
            errors.append(f"{f.name}: {f.type} 타입이어야 합니다 (받은 값 타입 {type(v).__name__})")
            continue
        if f.pattern and isinstance(v, str) and not re.fullmatch(f.pattern, v):
            errors.append(f"{f.name}: 형식 {f.pattern!r}과 맞지 않습니다")
            continue
        out[f.name] = v
    if errors:
        raise InvalidInput("; ".join(errors))
    return out
