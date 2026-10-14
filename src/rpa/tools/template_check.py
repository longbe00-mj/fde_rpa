"""기준 이미지 교차 검증 — 각 기준 이미지가 각 상태 캡처에서 몇 곳 찾아지는지 표로 만든다 (화면 조작 없음).

좋은 기준 이미지: 해당 상태 캡처에서만 정확히 1곳, 다른 상태에서는 0곳.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def count_matches(template: Any, haystack: Any, confidence: float) -> list[tuple[int, int]]:
    import pyscreeze

    try:
        boxes = list(pyscreeze.locateAll(template, haystack, confidence=confidence))
    except pyscreeze.ImageNotFoundException:
        boxes = []
    uniq: list[tuple[int, int]] = []
    w, h = template.size
    for b in boxes:
        if not any(abs(int(b.left) - u[0]) < w // 2 and abs(int(b.top) - u[1]) < h // 2 for u in uniq):
            uniq.append((int(b.left), int(b.top)))
    return uniq


def check(templates: list[Path], screens: list[Path], confidence: float = 0.9) -> dict[str, Any]:
    """PIL로 열어 넘긴다 (OpenCV는 한글 경로를 못 읽음)."""
    from PIL import Image

    def load(p: Path):
        with Image.open(p) as im:
            return im.convert("RGB")

    shots = {p.name: load(p) for p in screens}
    rows = []
    for t in templates:
        tpl = load(t)
        counts = {name: count_matches(tpl, img, confidence) for name, img in shots.items()}
        ones = [n for n, c in counts.items() if len(c) == 1]
        multi = [n for n, c in counts.items() if len(c) > 1]
        rows.append({"template": t.name, "size": list(tpl.size),
                     "counts": {n: len(c) for n, c in counts.items()},
                     "positions": {n: c for n, c in counts.items() if c},
                     "unique_in": ones, "ambiguous_in": multi})
    return {"confidence": confidence, "screens": list(shots), "templates": rows}


def render(result: dict[str, Any]) -> str:
    screens = result["screens"]
    width = max([len("기준 이미지")] + [len(r["template"]) for r in result["templates"]])
    head = "기준 이미지".ljust(width) + " | " + " | ".join(s[:18].ljust(18) for s in screens)
    lines = [head, "-" * len(head)]
    for r in result["templates"]:
        cells = []
        for s in screens:
            n = r["counts"][s]
            cells.append((f"{n}" + (" ⚠여러곳" if n > 1 else "")).ljust(18))
        lines.append(r["template"].ljust(width) + " | " + " | ".join(cells))
    lines.append("")
    lines.append(f"confidence={result['confidence']}. 좋은 기준 이미지: 해당 상태 캡처에서만 1, 나머지 0. "
                 "여러 곳(⚠)이면 더 넓게 잘라 고유하게 만드세요.")
    return "\n".join(lines)
