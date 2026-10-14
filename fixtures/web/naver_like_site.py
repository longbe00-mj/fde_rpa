"""naver_news 업무 테스트용 가짜 네이버 메인 (표준 라이브러리 HTTP 서버).

실제 www.naver.com 대신 쓴다. 2026-10-14 실제 페이지 조사로 확인한 뉴스스탠드 영역의 구조만 흉내 낸다:
  region[aria-label=뉴스스탠드] > tab(뉴스스탠드/언론사편집) · tabpanel(기사 항목 4개씩) · 페이징("N 페이지 전체 /M") · 다음 페이지 버튼
기사 내용은 모두 합성 데이터이다 (실제 기사 아님).

모드 (start_site(mode=...)):
  normal          기사 4개씩 6페이지 (24개)
  overlap         페이지 사이에 같은 기사가 겹침 (중복 제거 확인)
  ui_v2           '언론사편집' 탭이 없음 (UI 변경 → 좌표 대체 없이 실패해야 함)
  short           기사가 6개뿐 (요청 수 미달 → 검증 실패해야 함)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRESS = ["가상일보", "테스트경제", "모의뉴스", "샘플방송", "예시신문", "합성타임즈"]


def articles(mode: str) -> list[dict]:
    n = 6 if mode == "short" else 24
    out = []
    for i in range(1, n + 1):
        out.append({"title": f"[합성] 테스트 기사 제목 {i:02d} · \"따옴표\" & <특수>",
                    "url": f"https://n.news.naver.com/article/{900 + i % 7:03d}/{1000000 + i:010d}",
                    "press": PRESS[i % len(PRESS)], "time": f"10월 14일 09:{i:02d}"})
    if mode == "overlap":  # 2페이지 첫 기사를 1페이지 마지막 기사와 같게
        out[4] = dict(out[3])
    return out


PAGE = r"""<!doctype html><html lang=ko><head><meta charset=utf-8><title>NAVER</title></head><body>
<h1>NAVER</h1>
<div id=newsstand role=region aria-label="뉴스스탠드" class="Layout-module__content_area___T3st1">
  <div class="ContentHeaderView-module__tab_list___aaaa1" role=tablist>
    <a href="#" role=tab aria-selected=true id=t_stand>뉴스스탠드</a>
    __EDIT_TAB__
    <a href="#" role=tab aria-selected=false>엔터</a>
  </div>
  <div role=tabpanel class="MediaView-module__media_area___T3st2" id=panel><p>언론사 로고 목록</p></div>
  <div class="ContentPagingView-module__content_paging___T3st3" id=paging>
    <button type=button>이전 페이지</button>
    <span>언론사 더보기<strong id=cur>1</strong> 페이지 전체 <span>/<span id=tot>1</span></span></span>
    <button type=button id=next>다음 페이지</button>
  </div>
</div>
<script>
const ARTS = __ARTS__;
const PER = 4;
let page = 0;
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
function render() {
  const items = ARTS.slice(page*PER, page*PER+PER).map(a =>
    `<li class="MediaContentsView-module__media_item___T3st4 MediaContentsView-module__type_news___T3st5">
       <a target=_blank href="${a.url}" class="MediaContentsView-module__link_thumb___T3st6"><img alt=""></a>
       <div class="MediaContentsView-module__media_info___T3st7">
         <div class="MediaContentsView-module__info_top___T3st8"><a target=_blank href="#press" class="MediaContentsView-module__link_press___T3st9">${esc(a.press)}</a><button type=button>구독</button></div>
         <a target=_blank href="${a.url}" class="MediaContentsView-module__info_title___T3stA">${esc(a.title)}</a>
         <span>${a.time}</span>
       </div></li>`).join('');
  document.getElementById('panel').innerHTML = `<ul class="MediaContentsView-module__media_list___T3stB">${items}</ul>`;
  document.getElementById('cur').textContent = page + 1;
  document.getElementById('tot').textContent = Math.ceil(ARTS.length / PER);
}
const edit = document.getElementById('t_edit');
if (edit) edit.addEventListener('click', ev => {
  ev.preventDefault();
  document.getElementById('t_stand').setAttribute('aria-selected', 'false');
  edit.setAttribute('aria-selected', 'true');
  setTimeout(render, 150);  // 실제 페이지처럼 비동기로 목록 표시
});
document.getElementById('next').addEventListener('click', () => {
  page = (page + 1) % Math.ceil(ARTS.length / PER);
  setTimeout(render, 150);
});
</script></body></html>"""

EDIT_TAB = '<a href="#" role=tab aria-selected=false id=t_edit>언론사편집</a>'


def make_handler(mode: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 조용히
            pass

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] != "/":
                self.send_error(404)
                return
            body = (PAGE.replace("__EDIT_TAB__", "" if mode == "ui_v2" else EDIT_TAB)
                    .replace("__ARTS__", json.dumps(articles(mode), ensure_ascii=False))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def start_site(mode: str = "normal", port: int = 0) -> tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(mode))
    threading.Thread(target=server.serve_forever, name="naver-like", daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--port", type=int, default=8766)
    a = ap.parse_args()
    url, _ = start_site(a.mode, a.port)
    print(f"naver-like: {url}  (mode={a.mode})", flush=True)
    while True:
        time.sleep(3600)
