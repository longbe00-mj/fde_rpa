"""앱 커넥터 공통 정의와 목록.

AppInfo는 커넥터 패키지의 메타데이터다. 커넥터 코드·selectors.json·assets/·vision_profile.py는
그 앱을 쓰는 모든 업무의 버전 해시에 포함된다 (앱 지식이 바뀌면 진행 중 실행의 재개 시 감지).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_KINDS = ("desktop_visual", "desktop_uia", "web")


@dataclass(frozen=True)
class AppInfo:
    app_id: str
    name: str
    kind: str  # desktop_visual | desktop_uia | web
    description: str
    package_dir: Path
    selectors_file: str = "selectors.json"

    def __post_init__(self) -> None:
        if self.kind not in APP_KINDS:
            raise ValueError(f"알 수 없는 앱 종류: {self.kind}")

    @property
    def selectors_path(self) -> Path:
        return self.package_dir / self.selectors_file

    def selectors(self) -> dict[str, Any]:
        return json.loads(self.selectors_path.read_text(encoding="utf-8"))

    def asset(self, rel: str) -> Path:
        p = (self.package_dir / rel).resolve()
        if self.package_dir.resolve() not in p.parents:
            raise ValueError(f"커넥터 폴더 밖 경로: {rel}")
        return p

    def source_files(self) -> tuple[str, ...]:
        """버전 해시 대상: 커넥터 코드 + 선택자 + 기준 이미지 등 앱 지식 전부."""
        files = [p for p in self.package_dir.rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json", ".png")]
        return tuple(str(p) for p in sorted(files))

    def summary(self) -> dict[str, Any]:
        sel = self.selectors()
        return {
            "app_id": self.app_id, "name": self.name, "kind": self.kind, "description": self.description,
            "app_version": sel.get("app_version"), "verified_versions": sel.get("verified_versions", {}),
            "environment": sel.get("environment", {}), "path": str(self.package_dir),
        }


_APPS: dict[str, AppInfo] = {}


def register_app(info: AppInfo) -> AppInfo:
    _APPS[info.app_id] = info
    return info


def list_apps() -> list[AppInfo]:
    _load_builtin()
    return [_APPS[k] for k in sorted(_APPS)]


def get_app(app_id: str) -> AppInfo:
    _load_builtin()
    return _APPS[app_id]


def _load_builtin() -> None:
    import rpa.apps.claude_desktop.connector  # noqa: F401
    import rpa.apps.test_form.connector  # noqa: F401
    import rpa.apps.web_portal.connector  # noqa: F401
