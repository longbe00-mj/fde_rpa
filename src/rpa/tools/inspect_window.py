"""대상 창 조사 — 조작 방식(UIA / 화면 기반) 결정을 위한 읽기 전용 점검.

창을 활성화하거나 입력하지 않는다. UIA 트리는 노드 수 상한까지만 훑는다.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

# 조작 대상이 될 만한 UIA 컨트롤 종류
ACTIONABLE = {"Edit", "Button", "ComboBox", "CheckBox", "RadioButton", "List", "ListItem", "DataItem",
              "Tree", "TreeItem", "Tab", "TabItem", "MenuItem", "Hyperlink", "Document", "Table", "DataGrid"}
CHROMIUM_CLASSES = ("Chrome_WidgetWin_",)


def _package_version(image_path: str) -> str:
    # MSIX: ...\WindowsApps\<Name>_<Version>_<Arch>__<Publisher>\...
    for part in Path(image_path).parts:
        m = re.match(r"^[^_]+_(\d+(?:\.\d+){1,3})_", part)
        if m and "WindowsApps" in image_path:
            return m.group(1)
    return ""


def uia_survey(hwnd: int, max_nodes: int = 3000, max_seconds: float = 20.0) -> dict[str, Any]:
    """UIA 트리를 너비 우선으로 훑어 컨트롤 종류별 개수와 이름 있는 조작 후보 표본을 돌려준다."""
    try:
        from pywinauto.uia_element_info import UIAElementInfo
    except ImportError:
        return {"available": False, "error": "pywinauto 미설치 (pip install pywinauto)"}
    started = time.monotonic()
    root = UIAElementInfo(hwnd)
    queue, seen = [root], 0
    types: Counter = Counter()
    samples: list[dict[str, str]] = []
    truncated = False
    while queue:
        if seen >= max_nodes or time.monotonic() - started > max_seconds:
            truncated = True
            break
        el = queue.pop(0)
        seen += 1
        try:
            ct = el.control_type or "?"
            types[ct] += 1
            if ct in ACTIONABLE and (el.name or el.automation_id) and len(samples) < 40:
                samples.append({"control_type": ct, "name": (el.name or "")[:60], "automation_id": el.automation_id or "",
                                "class_name": (el.class_name or "")[:40]})
            queue.extend(el.children())
        except Exception:  # noqa: BLE001 — 사라진 요소 등
            continue
    actionable = sum(n for t, n in types.items() if t in ACTIONABLE)
    named = len(samples)
    return {"available": True, "nodes": seen, "truncated": truncated, "control_types": dict(types.most_common()),
            "actionable": actionable, "named_actionable_samples": samples, "named_count_capped": named,
            "seconds": round(time.monotonic() - started, 2)}


def recommend(info: dict[str, Any]) -> tuple[str, str]:
    u = info.get("uia") or {}
    chromium = info["chromium"]
    if info.get("elevated"):
        return "blocked", "관리자 권한으로 실행된 앱 — 일반 권한 Runner는 입력할 수 없음(UIPI). 앱을 일반 권한으로 실행해야 함"
    if u.get("available") and u.get("named_count_capped", 0) >= 5:
        return "desktop_uia", "UIA로 이름 있는 컨트롤이 보임 — pywinauto(UIA) 커넥터 권장 (selector: control_type + name/automation_id)"
    if chromium:
        return "desktop_visual", ("Chromium/Electron 창이며 UIA에 내용이 거의 없음 — 화면 기반(기준 이미지) 커넥터. "
                                  "앱이 --force-renderer-accessibility 실행을 허용하면 UIA 재조사 가치 있음. "
                                  "웹 앱이면 브라우저(Playwright) 커넥터가 더 적합")
    return "desktop_visual", "UIA 정보가 부족함 — 화면 기반(기준 이미지) 커넥터. Inspect.exe로 한 번 더 확인 권장"


def inspect_windows(process: Optional[str] = None, title_regex: Optional[str] = None, class_name: Optional[str] = None,
                    uia: bool = True) -> list[dict[str, Any]]:
    from rpa.platform.windows import win32
    from rpa.platform.windows.display import set_dpi_awareness

    set_dpi_awareness()
    out = []
    for w in win32.top_level_windows():
        if process and w.process_name.lower() != process.lower():
            continue
        if class_name and w.class_name != class_name:
            continue
        if title_regex and not re.search(title_regex, w.title):
            continue
        if not (process or title_regex or class_name) and not w.title:
            continue
        image = win32.process_image(w.pid)
        info: dict[str, Any] = {
            "hwnd": w.hwnd, "pid": w.pid, "process": w.process_name, "image_path": image,
            "package_version": _package_version(image), "class_name": w.class_name, "title": w.title,
            "frame": vars(w.frame), "client": vars(w.client), "dpi": w.dpi,
            "scale_percent": round(w.dpi * 100 / 96) if w.dpi else None, "minimized": w.minimized,
            "elevated": win32.process_elevated(w.pid),
            "chromium": w.class_name.startswith(CHROMIUM_CLASSES),
            "window_spec": {"process_name": w.process_name, "class_name": w.class_name,
                            "title_regex": "^" + re.escape(w.title) + "$" if w.title else None},
        }
        if uia:
            info["uia"] = uia_survey(w.hwnd)
        info["recommendation"], info["reason"] = recommend(info)
        out.append(info)
    return out
