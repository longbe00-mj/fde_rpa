"""네이버 메인 뉴스 수집: naver.com → 뉴스스탠드 '언론사편집' 탭 → 페이지를 넘기며 기사 N개 → 파일 저장.

대상: https://www.naver.com (비로그인). 사이트에 쓰는 동작은 없다 (탭 전환·페이지 넘김만).
- 기사 링크는 클릭하지 않는다 (허용 origin은 www.naver.com 하나 — 다른 사이트로 이동하지 않음).
- 요소는 role/접근성 이름으로 정확히 하나일 때만 조작한다. 기사 항목은 selectors.json의 CSS 모듈 클래스 접두어로 읽는다.
- 화면 구조가 바뀌면 좌표로 대체하지 않고 'UI 변경 가능성'으로 실패한다 (selectors.json 재조사 필요).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rpa.core import registry
from rpa.core.contracts import (
    ActionResult,
    CheckResult,
    CheckStatus,
    DispatchState,
    EffectKind,
    InputField,
    ReconcileResult,
    ReconcileStatus,
    RetryPolicy,
    StepDefinition,
    StepEnv,
    VerificationMode,
    WorkflowDefinition,
)
from rpa.core.errors import PreconditionFailed, TransientError
from rpa.core.run_context import RunContext

WORKFLOW_ID = "naver_news"
HERE = Path(__file__).resolve().parent
SELECTORS_PATH = HERE / "selectors.json"
DEFAULT_COUNT = 10
SOURCE = "네이버 메인 뉴스스탠드 · 언론사편집"

# 목록 컨테이너(tabpanel) 안의 기사 항목을 읽는다 (읽기 전용)
ITEMS_JS = r"""(panel, s) => [...panel.querySelectorAll(s.item)].map(li => {
  const t = li.querySelector(s.title), p = li.querySelector(s.press);
  const m = (li.innerText || '').match(/\d+월 \d+일 \d{1,2}:\d{2}/);
  return {title: t ? t.innerText.trim() : '', url: t ? t.href : '', press: p ? p.innerText.trim() : '',
          time: m ? m[0] : ''};
})"""
TEXT_JS = "e => (e.innerText || '').split('\\n').join(' ').trim()"


def _sel() -> dict[str, Any]:
    return json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class NewsJobSettings:
    output_dir: str
    max_pages: int = 15  # 페이지 넘김 상한 (무한 루프 방지)
    page_wait: float = 10.0  # 탭 전환·페이지 넘김 후 목록이 바뀔 때까지 기다리는 시간


def make_adapters(data_dir, control, faults=None, *, base_url: Optional[str] = None, headless: Optional[bool] = None):
    from rpa.adapters.browser import BrowserAdapter, BrowserSettings

    base = base_url or os.environ.get("LOCALRPA_NAVER_URL", _sel()["base_url"])
    hl = headless if headless is not None else os.environ.get("LOCALRPA_BROWSER_HEADLESS", "") in ("1", "true", "on")
    return {
        "browser": BrowserAdapter(control, BrowserSettings(
            user_data_dir=str(Path(data_dir) / "browser_profiles" / "naver"), base_url=base, headless=hl)),
        "job": NewsJobSettings(output_dir=str(Path(data_dir) / "outputs" / "naver_news")),
    }


def _rule(ctx: RunContext, status: CheckStatus, expected: dict, observed: dict, reason: str) -> CheckResult:
    return CheckResult(status, VerificationMode.RULE, ctx.business_key, expected, observed, reason)


def _count(ctx: RunContext) -> int:
    return int(ctx.validated_inputs.get("count") or DEFAULT_COUNT)


def _wait(env: StepEnv, cond, timeout: float, what: str) -> None:
    if not env.control.wait_until(cond, timeout=timeout, interval=0.25):
        raise TransientError(f"{what} — {timeout:.0f}초 안에 확인되지 않음")


# --- 1) 네이버 열기 (READ_ONLY) ------------------------------------------------------
def open_naver(ctx: RunContext, env: StepEnv) -> ActionResult:
    b = env.adapter("browser")
    b.start()
    b.goto(_sel()["paths"]["home"], check_login=False)
    b.one(_sel()["region"])
    return ActionResult("open_naver", DispatchState.NOT_DISPATCHED,
                        outputs={"url": b.current_url(), "title": b.page.title(), "channel": b.settings.channel})


def verify_open(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    b = env.adapter("browser")
    from urllib.parse import urlparse

    exp_host = urlparse(b.settings.base_url).netloc
    host = urlparse(b.current_url()).netloc
    ok = host == exp_host and b.exists(_sel()["region"])
    return _rule(ctx, CheckStatus.PASS if ok else CheckStatus.FAIL, {"host": exp_host, "region": "뉴스스탠드"},
                 {"url": b.current_url()}, "네이버 메인과 뉴스스탠드 영역 확인" if ok else "네이버 메인/뉴스스탠드 영역을 확인하지 못함")


# --- 2) 언론사편집 기사 수집 (READ_ONLY) ----------------------------------------------
def _read_items(b) -> list[dict[str, str]]:
    s = _sel()
    return b.extract(s["panel"], ITEMS_JS, {"item": s["item_css"], "title": s["title_css"], "press": s["press_css"]})


def _page_text(b) -> str:
    return b.extract(_sel()["paging"], TEXT_JS)


def parse_page_indicator(text: str) -> Optional[tuple[int, int]]:
    m = re.search(_sel()["page_indicator_pattern"], text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def merge_items(collected: list[dict], new: list[dict], page: int) -> int:
    """새 페이지 기사 중 아직 없는 URL만 순서대로 추가. 추가한 수를 돌려준다."""
    seen = {c["url"] for c in collected}
    added = 0
    for it in new:
        if it.get("url") and it["url"] not in seen:
            collected.append({**it, "page": page})
            seen.add(it["url"])
            added += 1
    return added


def collect_news(ctx: RunContext, env: StepEnv) -> ActionResult:
    b = env.adapter("browser")
    job: NewsJobSettings = env.adapter("job")
    s = _sel()
    want = _count(ctx)
    b.goto(s["paths"]["home"], check_login=False)  # 재시도·재개 시 이전 화면을 가정하지 않음
    b.one(s["region"])
    b.click(s["edit_tab"])
    _wait(env, lambda: b.attr(s["edit_tab"], "aria-selected") == "true", job.page_wait, "언론사편집 탭 선택")
    _wait(env, lambda: len(_read_items(b)) > 0, job.page_wait, "언론사편집 기사 목록 표시")

    collected: list[dict] = []
    pages: list[str] = []
    for _ in range(job.max_pages):
        indicator = _page_text(b)
        pages.append(indicator)
        merge_items(collected, _read_items(b), len(pages))
        if len(collected) >= want:
            break
        before = [i["url"] for i in _read_items(b)]
        b.click(s["next_page"])
        _wait(env, lambda: _page_text(b) != indicator and [i["url"] for i in _read_items(b)] != before,
              job.page_wait, "다음 페이지로 목록 변경")
    items = [{"rank": n + 1, **it} for n, it in enumerate(collected[:want])]
    return ActionResult("collect_news", DispatchState.NOT_DISPATCHED, outputs={
        "source": SOURCE, "requested": want, "items": items, "pages_read": pages,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "url": b.current_url(),
    })


def check_items(items: list[dict], want: int) -> list[str]:
    """수집 결과 규칙 검사. 문제 목록(비면 통과)."""
    pat = re.compile(_sel()["article_url_pattern"])
    problems = []
    if len(items) != want:
        problems.append(f"기사 {len(items)}개 (요청 {want}개)")
    urls = [i.get("url") for i in items]
    if len(set(urls)) != len(urls):
        problems.append("중복 URL")
    for i in items:
        if not (i.get("title") or "").strip():
            problems.append(f"{i.get('rank')}번 제목 비어 있음")
        if not (i.get("press") or "").strip():
            problems.append(f"{i.get('rank')}번 언론사 비어 있음")
        if not pat.match(i.get("url") or ""):
            problems.append(f"{i.get('rank')}번 기사 주소 형식 다름: {i.get('url')}")
    return problems


def verify_collect(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    items = action.outputs["items"]
    problems = check_items(items, _count(ctx))
    exp = {"count": _count(ctx), "unique_urls": True, "url_pattern": _sel()["article_url_pattern"]}
    obs = {"count": len(items), "pages_read": action.outputs["pages_read"]}
    if problems:
        return _rule(ctx, CheckStatus.FAIL, exp, obs, "수집 결과 규칙 위반: " + "; ".join(problems[:5]))
    return _rule(ctx, CheckStatus.PASS, exp, obs,
                 f"기사 {len(items)}개 — 제목·언론사 있음, URL 중복 없음, 네이버 뉴스 기사 주소 형식")


# --- 3) 결과 파일 저장 (LOCAL_CHANGE) --------------------------------------------------
def _paths(ctx: RunContext, env: StepEnv) -> tuple[Path, Path]:
    base = Path(env.adapter("job").output_dir) / ctx.business_key
    return base.with_suffix(".json"), base.with_suffix(".md")


def render_markdown(data: dict[str, Any]) -> str:
    lines = [f"# 네이버 메인 뉴스 {len(data['items'])}개", "",
             f"- 출처: {data['source']} (https://www.naver.com)", f"- 수집 시각: {data['collected_at']}", ""]
    for it in data["items"]:
        when = f" · {it['time']}" if it.get("time") else ""
        lines.append(f"{it['rank']}. {it['title']} — {it['press']}{when}")
        lines.append(f"   {it['url']}")
    return "\n".join(lines) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise PreconditionFailed(f"같은 request_id의 다른 결과 파일이 이미 있습니다: {path} (덮어쓰지 않음)")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_news(ctx: RunContext, env: StepEnv) -> ActionResult:
    c = ctx.step_outputs["collect_news"]
    data = {"request_id": ctx.business_key, "source": c["source"], "collected_at": c["collected_at"], "items": c["items"]}
    jpath, mpath = _paths(ctx, env)
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jtext = json.dumps(data, ensure_ascii=False, indent=2)
    mtext = render_markdown(data)
    _write(jpath, jtext)
    _write(mpath, mtext)
    env.add_evidence("file", str(mpath))
    env.add_evidence("file", str(jpath))
    return ActionResult("save_news", DispatchState.DISPATCHED, outputs={
        "json": str(jpath), "md": str(mpath), "json_sha256": _sha(jtext), "md_sha256": _sha(mtext),
        "count": len(data["items"])})


def verify_save(ctx: RunContext, env: StepEnv, action: ActionResult) -> CheckResult:
    o = action.outputs
    jpath, mpath = Path(o["json"]), Path(o["md"])
    exp = {"json_sha256": o["json_sha256"], "md_sha256": o["md_sha256"], "items": ctx.step_outputs["collect_news"]["items"]}
    if not jpath.is_file() or not mpath.is_file():
        return _rule(ctx, CheckStatus.FAIL, {"files": [str(jpath), str(mpath)]}, {}, "결과 파일이 없음")
    jtext, mtext = jpath.read_text(encoding="utf-8"), mpath.read_text(encoding="utf-8")
    obs = {"json_sha256": _sha(jtext), "md_sha256": _sha(mtext)}
    if obs["json_sha256"] != o["json_sha256"] or obs["md_sha256"] != o["md_sha256"]:
        return _rule(ctx, CheckStatus.FAIL, exp, obs, "저장 파일 내용이 쓴 내용과 다름")
    if json.loads(jtext)["items"] != exp["items"]:
        return _rule(ctx, CheckStatus.FAIL, exp, obs, "JSON 기사 목록이 수집 결과와 다름")
    return _rule(ctx, CheckStatus.PASS, {"count": len(exp["items"])}, obs | {"count": o["count"]},
                 "결과 파일(JSON·MD)을 다시 읽어 수집 결과와 일치 확인")


def reconcile_save(ctx: RunContext, env: StepEnv) -> ReconcileResult:
    jpath, mpath = _paths(ctx, env)
    if not jpath.exists() and not mpath.exists():
        return ReconcileResult(ReconcileStatus.NOT_APPLIED, reason="결과 파일 없음 — 다시 저장 가능")
    try:
        items = json.loads(jpath.read_text(encoding="utf-8"))["items"]
    except (OSError, ValueError, KeyError) as e:
        return ReconcileResult(ReconcileStatus.UNKNOWN, reason=f"결과 파일을 읽지 못함: {e}")
    if mpath.exists() and items == ctx.step_outputs.get("collect_news", {}).get("items"):
        jtext, mtext = jpath.read_text(encoding="utf-8"), mpath.read_text(encoding="utf-8")
        return ReconcileResult(ReconcileStatus.APPLIED, {"json": str(jpath), "md": str(mpath), "json_sha256": _sha(jtext),
                                                         "md_sha256": _sha(mtext), "count": len(items)})
    return ReconcileResult(ReconcileStatus.UNKNOWN, reason="결과 파일이 이번 수집 결과와 다름 — 사람이 확인 필요")


def build() -> WorkflowDefinition:
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        version="1",
        description="네이버 메인: 뉴스스탠드 '언론사편집' 기사 N개(기본 10) 수집 → JSON·Markdown 저장 (웹, 사이트 변경 없음)",
        inputs=(
            InputField("request_id", pattern=r"[A-Za-z0-9_-]{1,64}", description="요청 식별자 (결과 파일 이름)"),
            InputField("count", required=False, pattern=r"[1-9]|[1-3][0-9]|40", description="수집할 기사 수 1~40 (기본 10)"),
        ),
        business_key_field="request_id",
        source_files=(__file__, str(SELECTORS_PATH)),
        environment_requirements=("browser:msedge", "internet:www.naver.com"),
        settings={"selectors": _sel()},
        steps=(
            StepDefinition("open_naver", open_naver, verify_open,
                           retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=2.0), timeout=120, action_timeout=40),
            StepDefinition("collect_news", collect_news, verify_collect,
                           retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=2.0), timeout=240, action_timeout=180),
            StepDefinition("save_news", save_news, verify_save, effect_kind=EffectKind.LOCAL_CHANGE,
                           operation_key="naver_news_save:{business_key}", reconcile=reconcile_save,
                           retry_policy=RetryPolicy(max_attempts=2), timeout=60, action_timeout=30),
        ),
    )


registry.register(WORKFLOW_ID, build, make_adapters)
