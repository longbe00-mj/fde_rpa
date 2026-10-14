"""LocalRPA 웹 어댑터 테스트용 업무 포털 (표준 라이브러리 HTTP 서버).

실제 업무 사이트 대신 쓰는 테스트 대상. 접근성 이름(label/role/test id)을 갖춘 페이지와 장애 모드를 제공한다.

페이지
  /login                      로그인 (rpa / test). 로그인 상태 유지 쿠키(Max-Age) 발급
  /customers?q=               고객 조회 (표 aria-label="고객 목록")
  /customers/<id>             고객 상세 (data-testid, 메모 iframe title="메모", 내보내기 다운로드, 인쇄용 보기 새 창)
  /export/<id>.csv            CSV 다운로드 (UTF-8 BOM, Content-Disposition)
  /records/new?customer_id=   접수 등록 폼 (첨부 업로드)
  /records?customer_id=       접수 목록 (재조회용, aria-label="접수 목록")
  /records/<rid>              접수 상세
장애 모드 (/__admin/...): expire(세션 만료), mode?name=value (search_error, register_lost_response,
  ui_v2, slow_search_ms, duplicate_button), reset
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

CUSTOMERS = {
    "DEMO-001": {"customer_id": "DEMO-001", "name": "홍길동", "phone": "010-0012-3456", "memo": "VIP 고객 · 0012 확인"},
    "DEMO-002": {"customer_id": "DEMO-002", "name": "김철수 & Co. <특수>", "phone": "0001", "memo": "특수문자 <b>태그</b> 아님"},
    "DEMO-010": {"customer_id": "DEMO-010", "name": "이영희", "phone": "02-000-1000", "memo": ""},
}


class PortalState:
    def __init__(self, store_path: Path) -> None:
        self.store_path = Path(store_path)
        self.lock = threading.Lock()
        self.sessions: set[str] = set()
        self.mode: dict[str, str] = {}

    def records(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"records": {}}
        return json.loads(self.store_path.read_text(encoding="utf-8"))

    def add_record(self, values: dict[str, Any]) -> str:
        with self.lock:
            data = self.records()
            rid = f"W-{len(data['records']) + 1:04d}"
            data["records"][rid] = {**values, "record_id": rid, "created_at": time.time()}
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.store_path)
            return rid


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>{html.escape(title)}</title></head>"
            f"<body><main>{body}</main></body></html>").encode("utf-8")


def make_handler(state: PortalState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # 조용히
            pass

        # --- 공통 -------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
                  headers: Optional[dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to: str, headers: Optional[dict[str, str]] = None) -> None:
            self._send(303, b"", headers={"Location": to, **(headers or {})})

        def _sid(self) -> Optional[str]:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "sid":
                    return v
            return None

        def _authed(self) -> bool:
            return self._sid() in state.sessions

        def _require_login(self) -> bool:
            if self._authed():
                return True
            self._redirect("/login?next=" + quote(self.path))
            return False

        def _form(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                msg = BytesParser(policy=email_policy).parsebytes(
                    f"Content-Type: {ctype}\r\n\r\n".encode() + raw)
                out: dict[str, Any] = {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    filename = part.get_filename()
                    payload = part.get_payload(decode=True) or b""
                    out[name] = {"filename": filename, "size": len(payload)} if filename is not None \
                        else payload.decode("utf-8")
                return out
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}

        # --- GET --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            p = u.path
            if p.startswith("/__admin/"):
                return self._admin(p, q)
            if p == "/login":
                nxt = html.escape(q.get("next", "/customers"))
                return self._send(200, _page("로그인", f"""
                    <h1>로그인</h1>
                    <form method=post action="/login?next={nxt}">
                      <label for=user>아이디</label><input id=user name=user>
                      <label for=pw>비밀번호</label><input id=pw name=pw type=password>
                      <button type=submit>로그인</button>
                    </form>"""))
            if p == "/forbidden":
                return self._send(403, _page("권한 없음", "<h1>권한이 없습니다</h1>"))
            if not self._require_login():
                return
            if p == "/customers":
                if state.mode.get("search_error") and "q" in q:
                    return self._send(500, _page("서버 오류", "<h1>일시적인 서버 오류</h1>"))
                if state.mode.get("slow_search_ms") and "q" in q:
                    time.sleep(int(state.mode["slow_search_ms"]) / 1000)
                term = q.get("q", "")
                rows = "".join(
                    f"<tr><td><a href='/customers/{quote(c['customer_id'])}'>{html.escape(c['customer_id'])}</a></td>"
                    f"<td>{html.escape(c['name'])}</td><td>{html.escape(c['phone'])}</td></tr>"
                    for c in CUSTOMERS.values() if term and (term in c["customer_id"] or term in c["name"]))
                btn = "조회" if state.mode.get("ui_v2") else "검색"
                return self._send(200, _page("고객 조회", f"""
                    <h1>고객 조회</h1>
                    <form method=get action=/customers role=search>
                      <label for=q>고객 검색</label><input id=q name=q value="{html.escape(term)}">
                      <button type=submit>{btn}</button>
                    </form>
                    <table aria-label="고객 목록"><thead><tr><th>고객ID</th><th>이름</th><th>전화</th></tr></thead>
                    <tbody>{rows}</tbody></table>"""))
            if p.startswith("/customers/") and p.endswith("/memo"):
                cid = p.split("/")[2]
                c = CUSTOMERS.get(cid)
                return self._send(200, _page("메모", f"<p data-testid=memo>{html.escape(c['memo'] if c else '')}</p>"))
            if p.startswith("/customers/"):
                cid = p.split("/")[2]
                c = CUSTOMERS.get(cid)
                if not c:
                    return self._send(404, _page("없음", "<h1>고객이 없습니다</h1>"))
                return self._send(200, _page("고객 상세", f"""
                    <h1>고객 상세</h1>
                    <dl><dt>고객ID</dt><dd data-testid=customer-id>{html.escape(c['customer_id'])}</dd>
                        <dt>이름</dt><dd data-testid=customer-name>{html.escape(c['name'])}</dd>
                        <dt>전화</dt><dd data-testid=customer-phone>{html.escape(c['phone'])}</dd></dl>
                    <iframe title="메모" src="/customers/{quote(cid)}/memo" width=400 height=60></iframe>
                    <p><a href="/export/{quote(cid)}.csv" download>내보내기</a>
                       <a href="/customers/{quote(cid)}/print" target=_blank>인쇄용 보기</a>
                       <a href="/records/new?customer_id={quote(cid)}">접수 등록</a></p>"""))
            if p.startswith("/export/") and p.endswith(".csv"):
                cid = p[len("/export/"):-4]
                c = CUSTOMERS.get(cid)
                if not c:
                    return self._send(404, b"not found", "text/plain")
                body = ("﻿고객ID,이름,전화\r\n" + f"{c['customer_id']},\"{c['name']}\",{c['phone']}\r\n").encode("utf-8")
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="customer_{cid}.csv"'})
            if p == "/records/new":
                cid = html.escape(q.get("customer_id", ""))
                dup = "<button type=submit>등록</button>" if state.mode.get("duplicate_button") else ""
                return self._send(200, _page("접수 등록", f"""
                    <h1>접수 등록</h1>
                    <form method=post action=/records enctype=multipart/form-data>
                      <label for=f-cid>고객ID</label><input id=f-cid name=customer_id value="{cid}">
                      <label for=f-name>이름</label><input id=f-name name=name>
                      <label for=f-phone>전화</label><input id=f-phone name=phone>
                      <label for=f-memo>메모</label><textarea id=f-memo name=memo></textarea>
                      <label for=f-att>첨부</label><input id=f-att name=attachment type=file>
                      <button type=submit>등록</button>{dup}
                    </form>"""))
            if p == "/records":
                cid = q.get("customer_id", "")
                recs = [r for r in state.records()["records"].values() if r.get("customer_id") == cid]
                rows = "".join(
                    f"<tr><td><a href='/records/{r['record_id']}'>{r['record_id']}</a></td>"
                    f"<td>{html.escape(r['customer_id'])}</td><td>{html.escape(r['name'])}</td>"
                    f"<td>{html.escape(r['phone'])}</td><td>{html.escape(r.get('memo', ''))}</td>"
                    f"<td>{html.escape(r.get('attachment') or '')}</td></tr>" for r in recs)
                return self._send(200, _page("접수 목록", f"""
                    <h1>접수 목록</h1>
                    <table aria-label="접수 목록"><thead><tr><th>접수번호</th><th>고객ID</th><th>이름</th><th>전화</th>
                    <th>메모</th><th>첨부</th></tr></thead><tbody>{rows}</tbody></table>"""))
            if p.startswith("/records/"):
                rid = p.split("/")[2]
                r = state.records()["records"].get(rid)
                if not r:
                    return self._send(404, _page("없음", "<h1>접수가 없습니다</h1>"))
                return self._send(200, _page("접수 완료", f"""
                    <h1>접수 완료</h1><p>접수번호 <span data-testid=record-id>{rid}</span></p>"""))
            return self._send(404, _page("없음", "<h1>페이지가 없습니다</h1>"))

        # --- POST -------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/login":
                f = self._form()
                if f.get("user") == "rpa" and f.get("pw") == "test":
                    sid = secrets.token_hex(8)
                    state.sessions.add(sid)
                    return self._redirect(q.get("next", "/customers"),
                                          {"Set-Cookie": f"sid={sid}; Path=/; Max-Age=3600; HttpOnly"})
                return self._send(401, _page("로그인", "<h1>로그인</h1><p role=alert>아이디 또는 비밀번호 오류</p>"))
            if not self._require_login():
                return
            if u.path == "/records":
                f = self._form()
                att = f.get("attachment")
                values = {k: f.get(k, "") for k in ("customer_id", "name", "phone", "memo")}
                values["attachment"] = att["filename"] if isinstance(att, dict) and att["filename"] else ""
                values["attachment_size"] = att["size"] if isinstance(att, dict) else 0
                rid = state.add_record(values)
                if state.mode.pop("register_lost_response", None):
                    return self._send(502, _page("게이트웨이 오류", "<h1>응답을 받지 못했습니다</h1>"))
                return self._redirect(f"/records/{rid}")
            return self._send(404, b"")

        def _admin(self, p: str, q: dict[str, str]) -> None:
            if p == "/__admin/expire":
                state.sessions.clear()
            elif p == "/__admin/mode":
                state.mode.update(q)
            elif p == "/__admin/reset":
                state.mode.clear()
            return self._send(200, b"ok", "text/plain")

    return Handler


def start_portal(store_path: Path, port: int = 0) -> tuple[str, ThreadingHTTPServer, PortalState]:
    """백그라운드 스레드로 포털을 띄운다. (base_url, server, state) 반환. server.shutdown()으로 종료."""
    state = PortalState(store_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=server.serve_forever, name="portal", daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server, state


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    url, srv, _ = start_portal(Path(a.store), a.port)
    print(f"portal: {url}  (아이디 rpa / 비밀번호 test)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
