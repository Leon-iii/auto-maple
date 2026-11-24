import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from struct import pack
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.common.hid_keyboard import KeyboardOutputError, LeonardoKeyboard


protocol = pytest.importorskip('macro_studio.external_hid.codec')
from macro_studio.external_hid.serial_link import SerialLink
from macro_studio.external_hid.simulator import McuSimulator


def wait_until(check):
    deadline = monotonic() + 4
    while monotonic() < deadline:
        if check():
            return
        sleep(.01)
    pytest.fail('Timed out waiting for keyboard session state.')


class SimulatorPort:
    def __init__(self):
        self.device = McuSimulator()
        self.incoming = bytearray()
        self.packets = []
        self.closed = False
        self.drop_input_ack = False

    @property
    def in_waiting(self):
        return len(self.incoming)

    def write(self, data):
        frame = protocol.decode(data.lstrip(b'\0'))
        self.packets.append(frame)
        reply = self.device.receive(data)
        if frame.opcode == protocol.Opcode.HELLO:
            # The actual CDC firmware advertises Leonardo. Its wire replies are checked below.
            response = protocol.decode(reply)
            reply = protocol.encode(replace(response, payload=response.payload[:6] + b'Leonardo'))
        if not (self.drop_input_ack and frame.opcode == protocol.Opcode.INPUT_EVENT):
            self.incoming.extend(reply)
        return len(data)

    def read(self, size):
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        if not chunk:
            sleep(.005)
        return chunk

    def close(self):
        self.closed = True


def test_vkeys_routes_chord_stop_and_focus_loss_without_sendinput(monkeypatch):
    from src.common import config, vkeys

    port = SimulatorPort()
    state = SimpleNamespace(focused=True, running=True)
    keyboard = LeonardoKeyboard('COM7', lambda: state.focused, lambda: state.running,
                                link_factory=lambda name: SerialLink(port, session_id=101))
    monkeypatch.setattr(vkeys, '_leonardo', keyboard)
    monkeypatch.setattr(config, 'capture_mode', config.CAPTURE_MODE)
    monkeypatch.setattr(config, 'enabled', True)
    native = Mock()
    monkeypatch.setattr(vkeys, '_output', native)
    keyboard.start()
    try:
        wait_until(lambda: keyboard.armed)
        keyboard.start_run()
        vkeys.key_down('Ctrl')
        vkeys.key_down('left')
        assert port.device.pressed_keys == {(7, 0xE0), (7, 0x50)}
        wait_until(lambda: any(frame.opcode == protocol.Opcode.HEARTBEAT for frame in port.packets))
        vkeys.key_up('left')
        assert port.device.pressed_keys == {(7, 0xE0)}
        state.focused = False
        assert keyboard.wait(2)
        assert keyboard.release_acknowledged and not port.device.pressed_keys
        assert keyboard.acknowledged_edges == 3 and port.closed
        assert 'lost focus' in keyboard.status
        with pytest.raises(KeyboardOutputError, match='disarmed'):
            vkeys.key_down('up')
        assert not config.enabled and not native.mock_calls
    finally:
        keyboard.stop()
        keyboard.wait(2)


@pytest.mark.parametrize('case', ['unsupported', 'rollover', 'ack_loss', 'stop'])
def test_rejected_or_stopped_session_neutralizes_and_never_retries(case):
    port = SimulatorPort()
    keyboard = LeonardoKeyboard('COM3', lambda: True, lambda: True,
                                link_factory=lambda name: SerialLink(port, session_id=102, timeout_ms=80))
    keyboard.start()
    try:
        wait_until(lambda: keyboard.armed)
        keyboard.key('shift', True)
        if case == 'unsupported':
            with pytest.raises(KeyboardOutputError, match='does not support'):
                keyboard.key('emoji', True)
        elif case == 'rollover':
            for key in 'abcdef':
                keyboard.key(key, True)
            with pytest.raises(KeyboardOutputError, match='six held'):
                keyboard.key('g', True)
        elif case == 'ack_loss':
            port.drop_input_ack = True
            with pytest.raises(KeyboardOutputError, match='timed out'):
                keyboard.key('down', True)
        else:
            keyboard.stop()
        assert keyboard.wait(2)
        assert not port.device.pressed_keys and port.closed
        keys = [frame for frame in port.packets if frame.opcode == protocol.Opcode.INPUT_EVENT]
        assert len(keys) == {'unsupported': 1, 'rollover': 7, 'ack_loss': 2, 'stop': 1}[case]
        assert keyboard.release_acknowledged == (case != 'ack_loss')
    finally:
        keyboard.stop()
        keyboard.wait(2)


def test_requires_target_before_port_opening():
    factory = Mock()
    keyboard = LeonardoKeyboard('COM2', lambda: False, lambda: False,
                                link_factory=factory, focus_timeout=.05)
    keyboard.start()
    assert keyboard.wait(2)
    factory.assert_not_called()
    assert 'confirmation expired' in keyboard.status


def test_approves_one_mss_window_and_preserves_selected_mode_on_stop(monkeypatch):
    from src.common import config, vkeys

    capture = SimpleNamespace(local_target=(12, monotonic()))
    monkeypatch.setattr(config, 'capture', capture)
    monkeypatch.setattr(config, 'capture_mode', config.CAPTURE_MODE)
    monkeypatch.setattr(config, 'enabled', False)
    monkeypatch.setattr(vkeys, 'held_keys', set())
    monkeypatch.setattr(vkeys, '_leonardo', None)
    selected = SimpleNamespace(finished=False, start=Mock(), stop=Mock())
    factory = Mock(return_value=selected)
    monkeypatch.setattr(vkeys, 'LeonardoKeyboard', factory)
    monkeypatch.setattr(vkeys.win32gui, 'GetForegroundWindow', lambda: 12)

    with pytest.raises(KeyboardOutputError):
        vkeys.arm_leonardo('COM7', 13)
    factory.assert_not_called()
    vkeys.arm_leonardo('COM7', 12)
    selected.start.assert_called_once_with()
    target_ok = factory.call_args.args[1]
    assert target_ok()
    capture.local_target = (12, monotonic() - 1)
    assert not target_ok()
    vkeys.disarm_leonardo()
    selected.stop.assert_called_once_with()
    assert vkeys.external_keyboard() is selected
    with pytest.raises(KeyboardOutputError):
        vkeys.use_send_input()
    selected.finished = True
    vkeys.use_send_input()
    assert vkeys.external_keyboard() is None


def test_preflight_rejects_mouse_and_unsupported_bindings(monkeypatch):
    from src.common import config
    from src.modules import bot as bot_module

    bot = bot_module.Bot.__new__(bot_module.Bot)
    bot.command_book = SimpleNamespace(config={'Jump': 'space'}, module=object())
    bot.config = {'Interact': 'y'}
    monkeypatch.setattr(config, 'routine', [object()])
    source = Mock(return_value="from src.common.vkeys import press\npress('left', 1)")
    monkeypatch.setattr(bot_module.inspect, 'getsource', source)
    bot.validate_keyboard_only()
    source.return_value = 'from src.common.vkeys import click as pointer'
    with pytest.raises(KeyboardOutputError, match='mouse output'):
        bot.validate_keyboard_only()
    source.return_value = "press('emoji', 1)"
    with pytest.raises(KeyboardOutputError, match='does not support'):
        bot.validate_keyboard_only()


def test_auto_maple_packets_match_existing_cdc_firmware(tmp_path):
    compiler = shutil.which('g++')
    if not compiler:
        pytest.skip('g++ is required for the existing CDC firmware stub')
    studio = Path(protocol.__file__).parents[3]
    stubs = studio / 'tests/firmware_stub'
    sketch = studio / 'firmware/MacroStudioLeonardo/streaming.cpp'
    program = tmp_path / 'cdc-firmware.exe'
    subprocess.run([compiler, '-std=c++11', '-I', stubs, stubs / 'main.cpp', sketch, '-o', program],
                   check=True, capture_output=True, timeout=30)

    port = SimulatorPort()
    keyboard = LeonardoKeyboard('COM5', lambda: True, lambda: True,
                                link_factory=lambda name: SerialLink(port, session_id=103))
    keyboard.start()
    try:
        wait_until(lambda: keyboard.armed)
        keyboard.key('alt', True)
        keyboard.key('right', True)
        keyboard.key('right', False)
    finally:
        keyboard.stop()
        assert keyboard.wait(2)

    key_frame = next(frame for frame in port.packets if frame.opcode == protocol.Opcode.INPUT_EVENT)
    frames = []
    for frame in port.packets:
        frames.append(frame)
        if frame is key_frame:
            frames.extend((frame, replace(frame, payload=pack('<BBHH', 1, 1, 7, 4))))
    result = subprocess.run(
        [program], input=''.join(f'0 1 {protocol.encode(frame).hex()}\n' for frame in frames),
        check=True, capture_output=True, text=True, timeout=10)
    rows = [line.split() for line in result.stdout.splitlines()]
    replies = [protocol.decode(bytes.fromhex(row[0])) for row in rows]
    idx = frames.index(key_frame)
    assert replies[0].payload[6:] == b'Leonardo'
    assert replies[1].payload[:2] == b'\x0f\x40'
    assert replies[idx + 1] == replies[idx]
    assert replies[idx + 2].opcode == protocol.Opcode.NACK
    assert replies[idx + 2].payload == bytes((protocol.Opcode.INPUT_EVENT, 5, 0))
    edges = [int(row[1]) for row, frame in zip(rows[idx:], frames[idx:])
             if frame.opcode != protocol.Opcode.HEARTBEAT]
    assert edges == [1, 1, 1, 2, 1, 0]
    assert replies[-1].payload == bytes((protocol.Opcode.RELEASE_ALL,))
