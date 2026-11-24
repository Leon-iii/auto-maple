import ctypes
import gc
import os
import shutil
import tkinter as tk
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import mss
import numpy as np
import pytest

from src.common import config, vkeys
from src.common.interfaces import Configurable
from src.modules.capture import Capture, UsbObservation
from src.modules.gui import GUI
from src.modules.listener import Listener, winsound
from src.modules.usb_video import VideoFrame, VideoSettings, VideoStatus
from src.routine.components import Wait
from src.routine.routine import Routine
from src.routine.layout import Layout


class LegacyBindings(Configurable):
    DEFAULT_CONFIG = {'Interact': 'space'}
    submodules = {}
    command_book = {'wait': Wait}
    usb_rune_result = None


def test_mss_capture_uses_existing_grab_and_window(monkeypatch):
    monkeypatch.setattr(config, 'capture', None)
    capture = Capture()
    assert capture._mss_factory is mss.mss
    pixels = np.arange(16, dtype=np.uint8).reshape(2, 2, 4)
    capture.sct = Mock()
    capture.sct.grab.return_value = pixels
    np.testing.assert_array_equal(capture.screenshot(), pixels)
    capture.sct.grab.assert_called_once_with(capture.window)


def test_sendinput_press_and_release_after_pause(monkeypatch):
    sent = []

    def send_input(count, pointer, size):
        assert count == 1 and size == ctypes.sizeof(vkeys.Input)
        entry = pointer._obj
        assert entry.type == vkeys.INPUT_KEYBOARD
        sent.append((entry.ki.wVk, entry.ki.dwFlags))

    monkeypatch.setattr(vkeys.user32, 'SendInput', send_input)
    monkeypatch.setattr(vkeys.time, 'sleep', Mock())
    monkeypatch.setattr(winsound, 'Beep', Mock())
    monkeypatch.setattr(config, 'enabled', True)
    monkeypatch.setattr(config, 'bot', SimpleNamespace(rune_active=True))
    vkeys.press('A', 2)
    Listener.toggle_enabled()
    assert not config.enabled
    assert not config.bot.rune_active
    vkeys.key_down('a')
    vkeys.press('a', 1)
    vkeys.key_up('a')
    down, up = (vkeys.KEY_MAP['a'], 0), (vkeys.KEY_MAP['a'], vkeys.KEYEVENTF_KEYUP)
    assert sent == [down, up, down, up, up]


def test_sendinput_absolute_mouse_click(monkeypatch):
    cursor = Mock()
    mouse_event = Mock()
    monkeypatch.setattr(vkeys.win32api, 'SetCursorPos', cursor)
    monkeypatch.setattr(vkeys.win32api, 'mouse_event', mouse_event)
    monkeypatch.setattr(config, 'enabled', True)
    vkeys.click((120, 300), 'right')
    cursor.assert_called_once_with((120, 300))
    assert [call.args[0] for call in mouse_event.call_args_list] == [
        vkeys.win32con.MOUSEEVENTF_RIGHTDOWN, vkeys.win32con.MOUSEEVENTF_RIGHTUP,
    ]
    config.enabled = False
    vkeys.click((400, 500))
    assert cursor.call_count == 1 and mouse_event.call_count == 2


def test_tk_tabs_and_legacy_settings_and_routine(tmp_path, monkeypatch):
    assets = tmp_path / 'assets'
    assets.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'assets/icon.png', assets / 'icon.png')
    monkeypatch.chdir(tmp_path)
    bindings = LegacyBindings('bot')
    bindings.config['Interact'] = 'shift'
    bindings.save_config()
    monkeypatch.setattr(config, 'bot', LegacyBindings('bot'))
    monkeypatch.setattr(config, 'listener', LegacyBindings('listener'))
    capture = SimpleNamespace(minimap_sample=None, minimap={}, select_video=Mock(),
                              usb_observation=Mock(return_value=UsbObservation(VideoStatus('DEVICE_NOT_FOUND'))))
    monkeypatch.setattr(config, 'capture', capture)
    monkeypatch.setattr(config, 'routine', Routine())
    monkeypatch.setattr(config, 'gui', None)
    monkeypatch.setattr(config, 'layout', None)
    monkeypatch.setattr(Layout, 'load', lambda path: None)
    routine_path = tmp_path / 'source.csv'
    routine_path.write_text('*, x=0.2, y=0.4\n    wait, duration=0.25\n', encoding='ascii')
    gui = GUI()
    try:
        gui.root.withdraw()
        assert [gui.navigation.tab(tab, 'text') for tab in gui.navigation.tabs()] == ['View', 'Edit', 'Settings']
        assert gui.settings.video.source['text'] == 'Video source: MSS'
        assert gui.settings.video.output['text'] == 'Keyboard output: SendInput'
        video = gui.settings.video
        capture.select_video.assert_not_called()
        video._open()
        capture.select_video.assert_called_once_with(VideoSettings())
        assert 'DEVICE_NOT_FOUND' in video.info['text']
        assert video.output['text'] == 'Keyboard output: Dry run (no input)'
        assert video.rune.instate(('disabled',))
        frame = VideoFrame('test-session', 1, monotonic(), np.zeros((720, 1280, 3), np.uint8))
        capture.usb_observation.return_value = UsbObservation(
            VideoStatus('STREAMING', actual=(1280, 720, 'YUY2', 30), measured_fps=29.9), frame, frame.pixels)
        video._update_preview()
        assert 350 < video._photo.width() <= 360
        assert 'Measured 29.9 fps' in video.info['text']
        assert video.rune.instate(('!disabled',))
        capture.usb_observation.return_value = UsbObservation(VideoStatus('STALE'))
        video._update_preview()
        assert video._photo is None and video.rune.instate(('disabled',))
        video._use_mss()
        capture.select_video.assert_called_with(None)
        assert video.output['text'] == 'Keyboard output: SendInput'
        assert gui.settings.common_bindings.forward['Interact'] == 'shift'
        config.routine.load(str(routine_path))
        target = tmp_path / 'roundtrip.csv'
        config.routine.save(str(target))
        config.routine.load(str(target))
        point, = config.routine.sequence
        command, = point.commands
        assert point.location == (0.2, 0.4)
        assert isinstance(command, Wait) and command.duration == .25
    finally:
        gui.root.destroy()
        config.gui = None
        del gui
        gc.collect()


@pytest.mark.skipif(os.environ.get('AUTO_MAPLE_TEST_NATIVE_INPUT') != '1',
                    reason='requires an opt-in Windows foreground test window')
def test_native_mss_and_sendinput_in_dedicated_tk_window(monkeypatch):
    root = tk.Tk()
    root.title('Auto Maple Stage 0 keyboard test')
    root.configure(background='#28486c')
    root.geometry('200x120')
    root.attributes('-topmost', True)
    events = []
    root.bind('<KeyPress-F8>', lambda event: events.append('down'))
    root.bind('<KeyRelease-F8>', lambda event: events.append('up'))
    user32 = vkeys.user32
    foreground = user32.GetForegroundWindow
    foreground.restype = ctypes.c_void_p
    ancestor = user32.GetAncestor
    ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    ancestor.restype = ctypes.c_void_p
    try:
        root.update()
        root.focus_force()
        root.update()
        hwnd = ancestor(root.winfo_id(), 2)
        deadline = monotonic() + 45
        while foreground() != hwnd and monotonic() < deadline:
            root.update()
            sleep(.05)
        assert hwnd and foreground() == hwnd, 'Select the dedicated test window to run this test'
        root.focus_set()
        root.update()
        monkeypatch.setattr(config, 'capture', None)
        capture = Capture()
        capture.window.update(left=root.winfo_rootx() + 32, top=root.winfo_rooty() + 32,
                              width=16, height=16)
        with mss.mss() as capture.sct:
            pixels = capture.screenshot()
        assert pixels.shape == (16, 16, 4)
        np.testing.assert_array_equal(pixels[8, 8, :3], [108, 72, 40])
        monkeypatch.setattr(config, 'enabled', True)
        assert root.focus_get() == root and foreground() == hwnd
        try:
            vkeys.key_down('f8')
            deadline = monotonic() + .4
            while not events and monotonic() < deadline:
                root.update()
                sleep(.01)
            assert events == ['down']
            config.enabled = False
            assert foreground() == hwnd
            vkeys.key_down('f8')
        finally:
            vkeys.key_up('f8')
        deadline = monotonic() + .4
        while len(events) < 2 and monotonic() < deadline:
            root.update()
            sleep(.01)
        assert events == ['down', 'up']
    finally:
        root.destroy()
