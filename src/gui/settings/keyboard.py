"""Select local input, video dry run, or an explicitly approved UART test."""

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from src.common import config, vkeys
from src.common.hid_keyboard import port_name


PORT_HINT = Path('.settings/leonardo-port')
UART_HINT = Path('.settings/leonardo-uart-port')


class KeyboardPanel(ttk.LabelFrame):
    def __init__(self, parent):
        super().__init__(parent, text='Keyboard output')
        self.port = ttk.Entry(self, width=12)
        self.label = ttk.Label(self, text='Leonardo CDC port')
        self.label.grid(row=0, column=0, sticky='w')
        self.port.grid(row=0, column=1, sticky='w')
        self._usb = None
        self.arm = ttk.Button(self, text='Confirm target and arm', command=self._arm)
        self.arm.grid(row=1, column=0, columnspan=2, sticky='w')
        self.stop = ttk.Button(self, text='Stop', command=self._stop)
        self.stop.grid(row=2, column=0, sticky='w')
        self.default = ttk.Button(self, text='Use SendInput', command=self._send_input)
        self.default.grid(row=2, column=1, sticky='w')
        self.status = ttk.Label(self, wraplength=235)
        self.status.grid(row=3, column=0, columnspan=2, sticky='w')
        self.notice = ttk.Label(self, wraplength=235)
        self.notice.grid(row=4, column=0, columnspan=2, sticky='w')
        self.remote = ttk.Frame(self)
        self.remote.grid(row=5, column=0, columnspan=2, sticky='w')
        ttk.Label(self.remote, text='Test target code').grid(row=0, column=0, sticky='w')
        self.code = ttk.Entry(self.remote, width=12)
        self.code.grid(row=0, column=1, sticky='w')
        ttk.Label(self.remote, text='F8 hold (seconds)').grid(row=1, column=0, sticky='w')
        self.hold = ttk.Entry(self.remote, width=12)
        self.hold.insert(0, '0.10')
        self.hold.grid(row=1, column=1, sticky='w')
        self.plan = ttk.Button(self.remote, text='Plan F8', command=lambda: self._perform(
            lambda: vkeys.plan_uart_probe(self.code.get(), float(self.hold.get()))))
        self.plan.grid(row=2, column=0, sticky='w')
        self.send = ttk.Button(self.remote, text='Send F8', command=lambda: self._perform(
            lambda: vkeys.send_uart_probe(float(self.hold.get()))))
        self.send.grid(row=2, column=1, sticky='w')
        self.dry = ttk.Button(self.remote, text='Run dry routine', command=lambda: self._perform(
            vkeys.start_dry_run))
        self.dry.grid(row=3, column=0, columnspan=2, sticky='w')
        self._refresh()

    def destroy(self):
        self.after_cancel(self._timer)
        super().destroy()

    def _arm(self):
        try:
            port = port_name(self.port.get())
            if self._usb:
                approval = vkeys.uart_approval(self.code.get())
                if not messagebox.askyesno(
                        'Approve UART test target',
                        f'Confirm that {approval[0]} is visible in the GC311G2 preview and that '
                        f'the Leonardo on {port} is connected by USB to that same target PC.\n\n'
                        'After arming, Send F8 sends only the test key.', parent=self):
                    return
                vkeys.arm_uart(port, approval)
            else:
                target = config.capture.local_target
                if target is None:
                    raise vkeys.KeyboardOutputError('Open MapleStory and wait for the MSS capture.')
                config.bot.validate_keyboard_only()
                if not messagebox.askyesno(
                        'Approve Leonardo target',
                        f'Use {port} to control the MapleStory window currently observed by MSS?\n\n'
                        'After confirming, focus that window within 12 seconds. '
                        'Use Start/stop in the approved window to start the routine.', parent=self):
                    return
                vkeys.arm_leonardo(port, target[0])
            self.notice.config(text='')
            try:
                (UART_HINT if self._usb else PORT_HINT).write_text(port, encoding='ascii')
            except OSError as error:
                self.notice.config(text=f'Could not save port hint: {error}')
        except (ValueError, vkeys.KeyboardOutputError) as error:
            self.notice.config(text=str(error))

    def _send_input(self):
        self._perform(vkeys.use_send_input)

    def _stop(self):
        vkeys.stop_dry_run()
        vkeys.disarm_leonardo()

    def _perform(self, action):
        try:
            action()
            self.notice.config(text='')
        except (ValueError, vkeys.KeyboardOutputError) as error:
            self.notice.config(text=str(error))

    def _refresh(self):
        usb = config.capture_mode != config.CAPTURE_MODE
        if self._usb != usb:
            self._usb = usb
            self.label.config(text='USB–TTL port' if usb else 'Leonardo CDC port')
            self.default.config(text='Use dry run' if usb else 'Use SendInput')
            self.port.delete(0, tk.END)
            try:
                hint = UART_HINT if usb else PORT_HINT
                self.port.insert(0, port_name(hint.read_text(encoding='ascii')))
            except (OSError, ValueError):
                pass
            if usb:
                self.remote.grid()
            else:
                self.code.delete(0, tk.END)
                self.remote.grid_remove()
        selected = vkeys.external_keyboard()
        busy = selected is not None and not selected.finished
        can_arm = not busy and not config.enabled
        self.arm.config(state=tk.NORMAL if can_arm else tk.DISABLED)
        self.stop.config(state=tk.NORMAL if busy or vkeys.dry_running() else tk.DISABLED)
        allow_plan = usb and selected is None and not config.enabled
        self.plan.config(state=tk.NORMAL if allow_plan else tk.DISABLED)
        self.dry.config(state=tk.NORMAL if allow_plan else tk.DISABLED)
        send_ready = usb and getattr(selected, 'uart', False) and selected.armed
        self.send.config(state=tk.NORMAL if send_ready else tk.DISABLED)
        if selected is not None:
            text = selected.status
        elif usb:
            text = vkeys.dry_status
        else:
            text = 'SendInput active'
        self.status.config(text=text)
        self._timer = self.after(100, self._refresh)
