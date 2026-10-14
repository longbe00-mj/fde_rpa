"""라이브 테스트용 방해 창: 입력 도중 사용자가 다른 창을 연 상황을 흉내 낸다."""

import ctypes
import tkinter as tk

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass

root = tk.Tk()
root.title("LocalRPA Distractor")
root.geometry("320x160+760+160")
tk.Label(root, text="방해 창 (라이브 테스트)").pack(expand=True)
root.mainloop()
