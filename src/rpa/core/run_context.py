"""RunContext (명세 §5.3).

프로세스가 재시작되면 화면과 로그인이 복구됐다고 가정하지 않는다.
여기에는 직렬화 가능한 값만 둔다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_files(paths: Iterable[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(Path(p).name.encode("utf-8"))
        h.update(b"\0")
        h.update(Path(p).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@dataclass
class RunContext:
    run_id: str
    workflow_id: str
    workflow_version: str
    workflow_code_hash: str
    config_hash: str
    input_hash: str
    business_key: Optional[str]
    validated_inputs: dict[str, Any]
    run_dir: str
    step_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # step_id -> {"status": PASS/FAIL/UNKNOWN, "mode": RULE/HUMAN}
    step_checks: dict[str, dict[str, str]] = field(default_factory=dict)
    current_step: Optional[str] = None
    last_checkpoint: Optional[str] = None  # 마지막으로 검증까지 끝난 step id
    environment: dict[str, Any] = field(default_factory=dict)
    owned_resources: list[dict[str, Any]] = field(default_factory=list)

    # 현재 시도에 대한 값. 엔진이 단계마다 설정한다 (영구 저장은 RunStore가 담당)
    logical_operation_id: Optional[str] = None
    attempt_id: Optional[str] = None

    def output(self, step_id: str, key: str, default: Any = None) -> Any:
        return self.step_outputs.get(step_id, {}).get(key, default)

    def evidence_dir(self, step_id: str) -> Path:
        p = Path(self.run_dir) / step_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "RunContext":
        return cls(**json.loads(s))
