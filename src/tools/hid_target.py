"""Full-screen, focus-scoped Leonardo test target. Run with python -m src.tools.hid_target."""

import ctypes
import json
import secrets
import tkinter as tk
from time import monotonic_ns, time_ns

from src.common.focus_marker import cells, marker_bits


def main():
    root = tk.Tk()
    root.title('Auto Maple HID test target')
    root.attributes('-fullscreen', True)
    canvas = tk.Canvas(root, background='#303840', highlightthickness=0)
    canvas.pack(fill='both', expand=True)
    root.update_idletasks()
    user32 = ctypes.WinDLL('user32')
    foreground = user32.GetForegroundWindow
    foreground.restype = ctypes.c_void_p
    key_state = user32.GetAsyncKeyState
    key_state.argtypes = (ctypes.c_int,)
    key_state.restype = ctypes.c_short
    ancestor = user32.GetAncestor
    ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    ancestor.restype = ctypes.c_void_p
    target = ancestor(root.winfo_id(), 2)
    code = ''
    counter = 0
    last_tick = 0
    focused = False
    held = bool(key_state(0x77) & 0x8000)

    def emit(event):
        print(json.dumps({'event': event, 'code': code, 'utc_ns': time_ns(),
                          'monotonic_ns': monotonic_ns(), 'focused': focused}), flush=True)

    def paint():
        canvas.delete('proof')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if focused:
            for rect, bit in zip(cells(width, height), marker_bits(code, counter)):
                canvas.create_rectangle(*rect, fill='white' if bit else 'black', width=0, tags='proof')
        text = f'Auto Maple HID test · {code} · {"FOCUSED" if focused else "UNFOCUSED"}'
        canvas.create_text(width / 2, height * .35, text=text, fill='white',
                           font=('Segoe UI', 26), tags='proof')
        canvas.create_text(width / 2, height * .47, text='F8 records a test edge · Esc exits',
                           fill='white', font=('Segoe UI', 18), tags='proof')
        canvas.create_text(width / 2, height * .59, text='F8 DOWN' if held else 'F8 UP',
                           fill='#ffd44d', font=('Segoe UI', 20), tags='proof')

    def check_focus():
        nonlocal code, counter, focused, last_tick
        active = foreground() == target and root.focus_displayof() is not None
        if active != focused:
            focused = active
            if focused:
                code = secrets.token_hex(4)
                counter = 0
            emit('focus_in' if focused else 'focus_out')
            paint()
        now = monotonic_ns()
        if focused and now - last_tick >= 125_000_000:
            last_tick = now
            counter = (counter + 1) % 256
            paint()

    def tick():
        nonlocal held
        check_focus()
        down = bool(key_state(0x77) & 0x8000)
        if down != held:
            held = down
            emit('f8_state_down' if down else 'f8_state_up')
            paint()
        root.after(25, tick)

    def edge(down):
        check_focus()
        if focused:
            emit('f8_down' if down else 'f8_up')

    root.bind('<FocusIn>', lambda _: check_focus())
    root.bind('<FocusOut>', lambda _: check_focus())
    canvas.bind('<Configure>', lambda _: paint())
    root.bind('<KeyPress-F8>', lambda _: edge(True))
    root.bind('<KeyRelease-F8>', lambda _: edge(False))
    root.bind('<Escape>', lambda _: root.destroy())
    paint()
    root.after(25, tick)
    root.mainloop()


if __name__ == '__main__':
    main()
