"""등록된 Workflow 목록. 설정 파일에서 임의 import/eval을 허용하지 않고, 코드로만 등록한다.

각 workflow는 필요한 어댑터를 만드는 함수를 함께 등록한다 (GUI 어댑터는 그 안에서 지연 import).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from rpa.core.contracts import WorkflowDefinition
from rpa.core.control import ControlState

# make_adapters(data_dir, control, faults) -> dict[str, adapter]
AdapterFactory = Callable[[Path, ControlState, Optional[dict[str, list[str]]]], dict[str, Any]]

_FACTORIES: dict[str, Callable[[], WorkflowDefinition]] = {}
_ADAPTERS: dict[str, AdapterFactory] = {}


def register(workflow_id: str, factory: Callable[[], WorkflowDefinition], make_adapters: AdapterFactory) -> None:
    _FACTORIES[workflow_id] = factory
    _ADAPTERS[workflow_id] = make_adapters


def get(workflow_id: str) -> WorkflowDefinition:
    _load_builtin()
    try:
        return _FACTORIES[workflow_id]()
    except KeyError:
        raise KeyError(f"등록되지 않은 workflow: {workflow_id}") from None


def adapters_for(
    workflow_id: str, data_dir: Path, control: ControlState, faults: Optional[dict[str, list[str]]] = None
) -> dict[str, Any]:
    _load_builtin()
    return _ADAPTERS[workflow_id](data_dir, control, faults)


def list_ids() -> list[str]:
    _load_builtin()
    return sorted(_FACTORIES)


def _load_builtin() -> None:
    # 명시적으로 등록된 내장 workflow만 로드한다
    import rpa.workflows.demo_fake.workflow  # noqa: F401
    import rpa.workflows.demo_test_form.workflow  # noqa: F401
    import rpa.workflows.claude_prompt.workflow  # noqa: F401
    import rpa.workflows.demo_web_portal.workflow  # noqa: F401
    import rpa.workflows.naver_news.workflow  # noqa: F401
