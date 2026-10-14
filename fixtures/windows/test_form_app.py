"""LocalRPA 라이브 테스트용 Windows 폼 (tkinter).

실제 업무 앱 대신 사용자가 허용한 테스트 대상이다. 저장 버튼을 누르면 --store JSON 파일에
레코드를 기록한다 (업무 시스템의 '재조회 가능한 저장소' 역할).

레이아웃은 Per-Monitor DPI v2 + 고정 픽셀 배치이므로, 배율과 관계없이 클라이언트 영역 좌표가 같다.
selector: src/rpa/workflows/demo_test_form/selectors.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from pathlib import Path

TITLE = "LocalRPA Test Form"
VERSION = "1.0"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--save-delay", type=float, default=0.0, help="저장 처리 지연(초)")
    ap.add_argument("--x", type=int, default=120)
    ap.add_argument("--y", type=int, default=120)
    args = ap.parse_args()

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass

    import tkinter as tk

    store = Path(args.store)
    root = tk.Tk()
    root.title(TITLE)
    root.geometry(f"520x300+{args.x}+{args.y}")
    root.resizable(False, False)
    font = ("Malgun Gothic", 11)

    entries: dict[str, tk.Entry] = {}
    for i, (key, label) in enumerate((("customer_id", "고객ID"), ("name", "이름"), ("phone", "전화"))):
        y = 20 + i * 50
        tk.Label(root, text=label, font=font, anchor="w").place(x=20, y=y, width=90, height=32)
        e = tk.Entry(root, font=font)
        e.place(x=120, y=y, width=360, height=32)
        # 표준 편집 컨트롤처럼 Ctrl+A = 전체 선택
        e.bind("<Control-a>", lambda ev: (ev.widget.select_range(0, "end"), ev.widget.icursor("end"), "break")[-1])
        entries[key] = e

    status = tk.Label(root, text="대기", font=font, anchor="w")
    status.place(x=20, y=240, width=480, height=32)

    def save() -> None:
        values = {k: e.get() for k, e in entries.items()}
        if not values["customer_id"]:
            status.config(text="고객ID 없음")
            return
        status.config(text="저장 중...")
        root.update_idletasks()
        if args.save_delay:
            time.sleep(args.save_delay)
        data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {"records": {}}
        rec_id = f"TF-{len(data['records']) + 1:04d}"
        data["records"][values["customer_id"]] = {**values, "record_id": rec_id, "saved_at": time.time()}
        store.parent.mkdir(parents=True, exist_ok=True)
        tmp = store.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(store)
        status.config(text=f"저장됨 {rec_id}")

    tk.Button(root, text="저장", font=font, command=save).place(x=120, y=180, width=120, height=40)
    root.mainloop()


if __name__ == "__main__":
    main()
