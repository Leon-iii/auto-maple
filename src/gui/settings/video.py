"""The GC311G2 preview and recognition controls in the existing Settings tab."""

from dataclasses import asdict
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

import cv2
from PIL import Image, ImageTk

from src.common import config, vkeys
from src.modules.usb_video import VideoSettings


PREFERENCES = Path('.settings/video.json')


def load_video_settings():
    try:
        saved = json.loads(PREFERENCES.read_text(encoding='utf-8'))
        return VideoSettings(**saved)
    except (OSError, TypeError, ValueError):
        return VideoSettings()


class VideoPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._active = False
        self._previewed = None
        self._diagnostic = None
        self._photo = None
        self._pending = None
        self._settings = load_video_settings()
        self._entries = {}

        self.source = ttk.Label(self, text=f'Video source: {config.CAPTURE_MODE}')
        self.source.grid(row=0, column=0, columnspan=3, sticky='w')
        self.output = ttk.Label(self, text=f'Keyboard output: {vkeys.OUTPUT_MODE}')
        self.output.grid(row=1, column=0, columnspan=3, sticky='w')

        for row, (name, title) in enumerate((('index', 'DirectShow index'), ('width', 'Width'),
                                             ('height', 'Height'), ('fps', 'Requested FPS'),
                                             ('fourcc', 'FourCC'), ('roi', 'ROI x,y,w,h'),
                                             ('reference', 'No-signal PNG')), start=2):
            ttk.Label(self, text=title).grid(row=row, column=0, sticky='w', padx=(0, 6))
            value = getattr(self._settings, name)
            if name == 'roi':
                value = ','.join(map(str, value))
            if name == 'fourcc':
                entry = ttk.Combobox(self, values=('Device default', 'MJPG', 'YUY2', 'NV12'),
                                     width=18, state='readonly')
                entry.set(value or 'Device default')
            else:
                entry = ttk.Entry(self, width=21)
                entry.insert(0, value)
            entry.grid(row=row, column=1, sticky='ew')
            self._entries[name] = entry
        ttk.Button(self, text='Browse…', command=self._choose_png).grid(row=8, column=2, padx=3)

        actions = ttk.Frame(self)
        actions.grid(row=9, column=0, columnspan=3, sticky='w', pady=4)
        ttk.Button(actions, text='Open GC311G2', command=self._open).pack(side='left')
        ttk.Button(actions, text='Use MSS', command=self._use_mss).pack(side='left', padx=4)
        self.rune = ttk.Button(actions, text='Read rune', command=self._read_rune, state='disabled')
        self.rune.pack(side='left')

        self.notice = ttk.Label(self, wraplength=365)
        self.notice.grid(row=10, column=0, columnspan=3, sticky='w')
        self.info = ttk.Label(self, wraplength=365)
        self.info.grid(row=11, column=0, columnspan=3, sticky='w')
        self.preview = ttk.Label(self)
        self.preview.grid(row=12, column=0, columnspan=3, sticky='w')
        self.recognition = ttk.Label(self, wraplength=365)
        self.recognition.grid(row=13, column=0, columnspan=3, sticky='w')
        self._timer = self.after(100, self._refresh)

    def destroy(self):
        self.after_cancel(self._timer)
        super().destroy()

    def _choose_png(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[('PNG image', '*.png')])
        if path:
            entry = self._entries['reference']
            entry.delete(0, tk.END)
            entry.insert(0, path)

    def _open(self):
        try:
            values = {name: entry.get().strip() for name, entry in self._entries.items()}
            for name in ('index', 'width', 'height', 'fps'):
                values[name] = int(values[name])
            values['roi'] = tuple(map(int, values['roi'].split(','))) if values['roi'] else ()
            if values['fourcc'] == 'Device default':
                values['fourcc'] = ''
            settings = VideoSettings(**values)
            config.capture.select_video(settings)
        except ValueError as error:
            self.notice.config(text=str(error))
            return
        self._settings = settings
        self._active = True
        self._diagnostic = None
        self._pending = None
        self.source.config(text='Video source: GC311G2 / DirectShow')
        self.output.config(text='Keyboard output: Dry run (no input)')
        self.notice.config(text='')
        config.gui.edit.minimap.clear()
        try:
            PREFERENCES.parent.mkdir(exist_ok=True)
            pending = PREFERENCES.with_suffix('.tmp')
            pending.write_text(json.dumps(asdict(settings)), encoding='utf-8')
            pending.replace(PREFERENCES)
        except OSError as error:
            self.notice.config(text=f'Could not save video settings: {error}')
        self._update_preview()

    def _use_mss(self):
        if not self._active:
            return
        try:
            config.capture.select_video(None)
        except ValueError as error:
            self.notice.config(text=str(error))
            return
        self._active = False
        self._pending = None
        self.source.config(text=f'Video source: {config.CAPTURE_MODE}')
        self.output.config(text=f'Keyboard output: {vkeys.OUTPUT_MODE}')
        for label in (self.notice, self.info, self.recognition):
            label.config(text='')
        self._clear_preview()

    def _read_rune(self):
        if vkeys.dry_running():
            return
        observation = config.capture.usb_observation()
        if observation.frame is None:
            return
        config.bot.usb_rune_result = None
        try:
            config.bot.inspect_video_rune(observation)
            self._pending = observation.frame.session, observation.frame.number
            self.notice.config(text=f'Reading frame {observation.frame.number}…')
        except ValueError as error:
            self.notice.config(text=str(error))

    def _clear_preview(self):
        self._previewed = None
        self.preview.config(image='')
        self._photo = None
        self.rune.config(state='disabled')

    def _refresh(self):
        if self._active:
            selected = vkeys.external_keyboard()
            text = 'Leonardo UART – test window' if getattr(selected, 'uart', False) else 'Dry run (no input)'
            self.output.config(text=f'Keyboard output: {text}')
            self._update_preview()
        elif vkeys.external_keyboard() is not None:
            self.output.config(text='Keyboard output: Leonardo USB CDC')
        else:
            self.output.config(text=f'Keyboard output: {vkeys.OUTPUT_MODE}')
        self._timer = self.after(100, self._refresh)

    def _update_preview(self):
        observation = config.capture.usb_observation()
        state = observation.status
        settings = self._settings
        requested = f'{settings.width}×{settings.height} / {settings.fps} fps / {settings.fourcc or "device default"}'
        reported = 'unknown'
        if state.actual:
            width, height, fourcc, fps = state.actual
            reported = f'{width}×{height} / {fps or "?"} fps / {fourcc or "?"}'
        diagnostic = (state.state, state.detail, requested, reported, state.measured_fps > 0)
        if diagnostic != self._diagnostic:
            print(f'[GC311G2] {state.state} | request {requested} | DirectShow {reported} '
                  f'| measured {state.measured_fps:.2f} fps {state.detail}')
            self._diagnostic = diagnostic
        self.info.config(text=f'{state.state}  {state.detail}\nRequested {requested}\n'
                              f'DirectShow {reported}\nMeasured {state.measured_fps:.1f} fps')
        frame = observation.frame
        if self._pending and (frame is None or frame.session != self._pending[0]):
            self._pending = None
            self.notice.config(text='Video interrupted. Read the rune again after video returns.')
        if frame is None:
            self._clear_preview()
            self.recognition.config(text='')
            return
        if self._previewed != (frame.session, frame.number):
            rgb = cv2.cvtColor(frame.pixels, cv2.COLOR_BGR2RGB)
            for box, color in ((settings.roi, (255, 190, 40)), (observation.minimap_box, (40, 255, 120))):
                if box:
                    x, y, width, height = box
                    cv2.rectangle(rgb, (x, y), (x + width - 1, y + height - 1), color, 2)
            image = Image.fromarray(rgb)
            image.thumbnail((360, 202), Image.Resampling.BILINEAR)
            self._photo = ImageTk.PhotoImage(image, master=self)
            self.preview.config(image=self._photo)
            self._previewed = frame.session, frame.number
        self.rune.config(state='disabled' if vkeys.dry_running() else 'normal')
        player = ', '.join(f'{value:.3f}' for value in observation.player) or 'not found'
        position = f'Minimap {observation.minimap_box or "not found"}   Player {player}'
        if observation.focus is not None:
            position += f'\nFocused test code {observation.focus.code}'
        result = config.bot.usb_rune_result
        if result and result[0] == frame.session:
            position += f'\nRune at frame {result[1]}  {result[2]}'
            if self._pending == result[:2]:
                self._pending = None
                self.notice.config(text='')
        self.recognition.config(text=position)
