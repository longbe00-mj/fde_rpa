"""SQLite 실행 상태 저장소 (명세 §11).

- 작은 트랜잭션으로 저장하고, 외부 화면 작업 중 트랜잭션을 열어 두지 않는다.
- 연결은 RunStore 인스턴스를 만든 스레드가 소유한다 (스레드 간 공유 금지).
- 미완료(비종료) 실행이 남아 있으면 새 실행을 만들지 않는다. 이전 결과를 먼저 대조해야 한다.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

from rpa.core.contracts import (
    BusinessOutcome,
    DispatchState,
    ExecutionStatus,
    now_iso,
)
from rpa.core.errors import InvalidTransition, RunnerBusy
from rpa.core.run_context import RunContext

S = ExecutionStatus

ALLOWED_TRANSITIONS: dict[ExecutionStatus, set[ExecutionStatus]] = {
    S.CREATED: {S.RUNNING, S.CANCELLED, S.FAILED},
    S.RUNNING: {S.PAUSE_REQUESTED, S.WAITING_USER, S.CANCEL_REQUESTED, S.CANCELLED, S.FAILED, S.FINISHED},
    S.PAUSE_REQUESTED: {S.PAUSED, S.RUNNING, S.CANCEL_REQUESTED, S.CANCELLED, S.WAITING_USER, S.FAILED, S.FINISHED},
    S.PAUSED: {S.RUNNING, S.CANCEL_REQUESTED, S.CANCELLED, S.FAILED},
    S.WAITING_USER: {S.RUNNING, S.CANCEL_REQUESTED, S.CANCELLED, S.FAILED},
    S.CANCEL_REQUESTED: {S.CANCELLED, S.FAILED, S.WAITING_USER},
    S.CANCELLED: set(),
    S.FAILED: set(),
    S.FINISHED: set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    workflow_code_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    business_key TEXT,
    execution_status TEXT NOT NULL,
    business_outcome TEXT NOT NULL,
    status_reason TEXT,
    current_step TEXT,
    last_checkpoint TEXT,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error TEXT,
    action_json TEXT,
    check_json TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    logical_operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step_id TEXT NOT NULL,
    business_key TEXT,
    input_hash TEXT NOT NULL,
    target TEXT,
    intent TEXT,
    dispatch_state TEXT NOT NULL,
    dispatch_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step_id TEXT,
    kind TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step_id TEXT,
    reason TEXT NOT NULL,
    instruction TEXT,
    resolution TEXT,
    evidence_note TEXT,
    changed_inputs_json TEXT,
    requested_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_run ON step_attempts(run_id, step_id);
CREATE INDEX IF NOT EXISTS ix_ops_run ON operations(run_id);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
"""

TERMINAL = tuple(s.value for s in ExecutionStatus if s.is_terminal)


class RunStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- 트랜잭션 헬퍼 --------------------------------------------------
    def _tx(self):
        return _Tx(self._conn)

    # --- runs -----------------------------------------------------------
    def create_run(self, ctx: RunContext) -> None:
        """미완료 실행이 없을 때만 새 실행을 만든다 (BEGIN IMMEDIATE로 경쟁 방지)."""
        ts = now_iso()
        with self._tx() as c:
            row = c.execute(
                f"SELECT run_id, execution_status FROM runs WHERE execution_status NOT IN ({','.join('?' * len(TERMINAL))}) LIMIT 1",
                TERMINAL,
            ).fetchone()
            if row:
                raise RunnerBusy(
                    f"미완료 실행 {row['run_id']} ({row['execution_status']})이 있습니다. 먼저 재개·해결·취소하세요."
                )
            c.execute(
                """INSERT INTO runs (run_id, workflow_id, workflow_version, workflow_code_hash, config_hash,
                   input_hash, business_key, execution_status, business_outcome, current_step, last_checkpoint,
                   context_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ctx.run_id, ctx.workflow_id, ctx.workflow_version, ctx.workflow_code_hash, ctx.config_hash,
                    ctx.input_hash, ctx.business_key, S.CREATED.value, BusinessOutcome.UNVERIFIED.value,
                    None, None, ctx.to_json(), ts, ts,
                ),
            )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, workflow_id, business_key, execution_status, business_outcome, status_reason, "
            "current_step, updated_at FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def unfinished_run(self) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            f"SELECT * FROM runs WHERE execution_status NOT IN ({','.join('?' * len(TERMINAL))}) "
            "ORDER BY created_at DESC LIMIT 1",
            TERMINAL,
        ).fetchone()
        return dict(row) if row else None

    def load_context(self, run_id: str) -> RunContext:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        return RunContext.from_json(run["context_json"])

    def save_context(self, ctx: RunContext) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET context_json=?, current_step=?, last_checkpoint=?, updated_at=? WHERE run_id=?",
                (ctx.to_json(), ctx.current_step, ctx.last_checkpoint, now_iso(), ctx.run_id),
            )

    def set_status(
        self,
        run_id: str,
        status: ExecutionStatus,
        reason: Optional[str] = None,
        outcome: Optional[BusinessOutcome] = None,
    ) -> None:
        with self._tx() as c:
            row = c.execute("SELECT execution_status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            current = ExecutionStatus(row["execution_status"])
            if status != current and status not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} → {status.value} 전이는 허용되지 않습니다")
            if outcome is None:
                c.execute(
                    "UPDATE runs SET execution_status=?, status_reason=?, updated_at=? WHERE run_id=?",
                    (status.value, reason, now_iso(), run_id),
                )
            else:
                c.execute(
                    "UPDATE runs SET execution_status=?, status_reason=?, business_outcome=?, updated_at=? WHERE run_id=?",
                    (status.value, reason, outcome.value, now_iso(), run_id),
                )

    def set_outcome(self, run_id: str, outcome: BusinessOutcome) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET business_outcome=?, updated_at=? WHERE run_id=?",
                (outcome.value, now_iso(), run_id),
            )

    # --- step_attempts --------------------------------------------------
    def start_attempt(self, run_id: str, step_id: str) -> tuple[str, int]:
        attempt_id = uuid.uuid4().hex
        with self._tx() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM step_attempts WHERE run_id=? AND step_id=?", (run_id, step_id)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO step_attempts (attempt_id, run_id, step_id, attempt_no, started_at, status) "
                "VALUES (?,?,?,?,?,?)",
                (attempt_id, run_id, step_id, n + 1, now_iso(), "STARTED"),
            )
        return attempt_id, n + 1

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        error: Optional[str] = None,
        action: Optional[dict[str, Any]] = None,
        check: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE step_attempts SET ended_at=?, status=?, error=?, action_json=?, check_json=? WHERE attempt_id=?",
                (
                    now_iso(), status, error,
                    json.dumps(action, ensure_ascii=False) if action is not None else None,
                    json.dumps(check, ensure_ascii=False) if check is not None else None,
                    attempt_id,
                ),
            )

    def attempts(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM step_attempts WHERE run_id=? ORDER BY started_at, rowid", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- operations -----------------------------------------------------
    def record_intent(
        self,
        logical_operation_id: str,
        run_id: str,
        step_id: str,
        business_key: Optional[str],
        input_hash: str,
        target: str,
        intent: str,
    ) -> tuple[dict[str, Any], bool]:
        """변경 동작 전 의도를 영구 저장하고 커밋 완료 후 (행, 새로 생성 여부)를 반환한다.

        이미 같은 logical_operation_id가 있으면 덮어쓰지 않고 기존 행을 반환한다
        (호출자는 기존 상태가 확정되지 않았으면 reconcile해야 한다).
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM operations WHERE logical_operation_id=?", (logical_operation_id,)
            ).fetchone()
            if row:
                return dict(row), False
            c.execute(
                "INSERT INTO operations (logical_operation_id, run_id, step_id, business_key, input_hash, target, "
                "intent, dispatch_state, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    logical_operation_id, run_id, step_id, business_key, input_hash, target, intent,
                    DispatchState.INTENT_RECORDED.value, now_iso(),
                ),
            )
        return self.get_operation(logical_operation_id), True  # type: ignore[return-value]

    def get_operation(self, logical_operation_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE logical_operation_id=?", (logical_operation_id,)
        ).fetchone()
        return dict(row) if row else None

    def operations(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM operations WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_dispatching(self, logical_operation_id: str) -> None:
        """전송 직전에 호출. 이후 종료되면 UNKNOWN으로 대조해야 한다."""
        with self._tx() as c:
            c.execute(
                "UPDATE operations SET dispatch_count=dispatch_count+1, dispatch_state=?, updated_at=? "
                "WHERE logical_operation_id=?",
                (DispatchState.UNKNOWN.value, now_iso(), logical_operation_id),
            )

    def set_operation_state(
        self, logical_operation_id: str, state: DispatchState, result: Optional[dict[str, Any]] = None
    ) -> None:
        with self._tx() as c:
            if result is None:
                c.execute(
                    "UPDATE operations SET dispatch_state=?, updated_at=? WHERE logical_operation_id=?",
                    (state.value, now_iso(), logical_operation_id),
                )
            else:
                c.execute(
                    "UPDATE operations SET dispatch_state=?, result_json=?, updated_at=? WHERE logical_operation_id=?",
                    (state.value, json.dumps(result, ensure_ascii=False), now_iso(), logical_operation_id),
                )

    # --- evidence -------------------------------------------------------
    def add_evidence(
        self, run_id: str, step_id: Optional[str], kind: str, rel_path: str, sha256: Optional[str] = None
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO evidence (run_id, step_id, kind, rel_path, sha256, created_at) VALUES (?,?,?,?,?,?)",
                (run_id, step_id, kind, rel_path, sha256, now_iso()),
            )

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM evidence WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- user_interventions --------------------------------------------
    def request_intervention(self, run_id: str, step_id: Optional[str], reason: str, instruction: str) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO user_interventions (run_id, step_id, reason, instruction, requested_at) VALUES (?,?,?,?,?)",
                (run_id, step_id, reason, instruction, now_iso()),
            )
            return int(cur.lastrowid)

    def open_intervention(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM user_interventions WHERE run_id=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None

    def resolve_intervention(
        self,
        intervention_id: int,
        resolution: str,
        evidence_note: str,
        changed_inputs: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE user_interventions SET resolution=?, evidence_note=?, changed_inputs_json=?, resolved_at=? "
                "WHERE id=?",
                (
                    resolution, evidence_note,
                    json.dumps(changed_inputs, ensure_ascii=False) if changed_inputs else None,
                    now_iso(), intervention_id,
                ),
            )

    def interventions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM user_interventions WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- events ---------------------------------------------------------
    def add_event(self, run_id: str, type_: str, payload: dict[str, Any]) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO events (run_id, ts, type, payload_json) VALUES (?,?,?,?)",
                (run_id, now_iso(), type_, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq)
        ).fetchall()
        return [dict(r) for r in rows]


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
