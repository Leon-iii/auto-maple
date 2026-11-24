"""A module for simulating low-level keyboard and mouse key presses."""

import ctypes
import json
import re
import threading
import time
import win32con
import win32api
import win32gui
from src.common import config, utils
from src.common.hid_keyboard import KeyboardOutputError, LeonardoKeyboard, key_usage
from src.common.focus_marker import MARKER_TTL
from time import monotonic, monotonic_ns, time_ns
from ctypes import wintypes
from random import random


user32 = ctypes.WinDLL('user32', use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0

# https://docs.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes?redirectedfrom=MSDN
KEY_MAP = {
    'left': 0x25,   # Arrow keys
    'up': 0x26,
    'right': 0x27,
    'down': 0x28,

    'backspace': 0x08,      # Special keys
    'tab': 0x09,
    'enter': 0x0D,
    'shift': 0x10,
    'ctrl': 0x11,
    'alt': 0x12,
    'caps lock': 0x14,
    'esc': 0x1B,
    'space': 0x20,
    'page up': 0x21,
    'page down': 0x22,
    'end': 0x23,
    'home': 0x24,
    'insert': 0x2D,
    'delete': 0x2E,

    '0': 0x30,      # Numbers
    '1': 0x31,
    '2': 0x32,
    '3': 0x33,
    '4': 0x34,
    '5': 0x35,
    '6': 0x36,
    '7': 0x37,
    '8': 0x38,
    '9': 0x39,

    'a': 0x41,      # Letters
    'b': 0x42,
    'c': 0x43,
    'd': 0x44,
    'e': 0x45,
    'f': 0x46,
    'g': 0x47,
    'h': 0x48,
    'i': 0x49,
    'j': 0x4A,
    'k': 0x4B,
    'l': 0x4C,
    'm': 0x4D,
    'n': 0x4E,
    'o': 0x4F,
    'p': 0x50,
    'q': 0x51,
    'r': 0x52,
    's': 0x53,
    't': 0x54,
    'u': 0x55,
    'v': 0x56,
    'w': 0x57,
    'x': 0x58,
    'y': 0x59,
    'z': 0x5A,

    'f1': 0x70,     # Functional keys
    'f2': 0x71,
    'f3': 0x72,
    'f4': 0x73,
    'f5': 0x74,
    'f6': 0x75,
    'f7': 0x76,
    'f8': 0x77,
    'f9': 0x78,
    'f10': 0x79,
    'f11': 0x7A,
    'f12': 0x7B,
    'num lock': 0x90,
    'scroll lock': 0x91,

    ';': 0xBA,      # Special characters
    '=': 0xBB,
    ',': 0xBC,
    '-': 0xBD,
    '.': 0xBE,
    '/': 0xBF,
    '`': 0xC0,
    '[': 0xDB,
    '\\': 0xDC,
    ']': 0xDD,
    "'": 0xDE
}


#################################
#     C Struct Definitions      #
#################################
wintypes.ULONG_PTR = wintypes.WPARAM


class KeyboardInput(ctypes.Structure):
    _fields_ = (('wVk', wintypes.WORD),
                ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD),
                ('dwExtraInfo', wintypes.ULONG_PTR))

    def __init__(self, *args, **kwargs):
        super(KeyboardInput, self).__init__(*args, **kwargs)
        if not self.dwFlags & KEYEVENTF_UNICODE:
            self.wScan = user32.MapVirtualKeyExW(self.wVk, MAPVK_VK_TO_VSC, 0)


class MouseInput(ctypes.Structure):
    _fields_ = (('dx', wintypes.LONG),
                ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD),
                ('dwExtraInfo', wintypes.ULONG_PTR))


class HardwareInput(ctypes.Structure):
    _fields_ = (('uMsg', wintypes.DWORD),
                ('wParamL', wintypes.WORD),
                ('wParamH', wintypes.WORD))


class Input(ctypes.Structure):
    class _Input(ctypes.Union):
        _fields_ = (('ki', KeyboardInput),
                    ('mi', MouseInput),
                    ('hi', HardwareInput))

    _anonymous_ = ('_input',)
    _fields_ = (('type', wintypes.DWORD),
                ('_input', _Input))


LPINPUT = ctypes.POINTER(Input)


def err_check(result, _, args):
    if result == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    else:
        return args


user32.SendInput.errcheck = err_check
user32.SendInput.argtypes = (wintypes.UINT, LPINPUT, ctypes.c_int)


#################################
#           Functions           #
#################################
class _SendInput:
    @staticmethod
    def key_down(key):
        """
        Simulates a key-down action. Can be cancelled by Bot.toggle_enabled.
        :param key:     The key to press.
        :return:        None
        """

        key = key.lower()
        if key not in KEY_MAP.keys():
            print(f"Invalid keyboard input: '{key}'.")
        else:
            x = Input(type=INPUT_KEYBOARD, ki=KeyboardInput(wVk=KEY_MAP[key]))
            user32.SendInput(1, ctypes.byref(x), ctypes.sizeof(x))

    @staticmethod
    def key_up(key):
        """
        Simulates a key-up action. Cannot be cancelled by Bot.toggle_enabled.
        This is to ensure no keys are left in the 'down' state when the program pauses.
        :param key:     The key to press.
        :return:        None
        """

        key = key.lower()
        if key not in KEY_MAP.keys():
            print(f"Invalid keyboard input: '{key}'.")
        else:
            x = Input(type=INPUT_KEYBOARD, ki=KeyboardInput(wVk=KEY_MAP[key], dwFlags=KEYEVENTF_KEYUP))
            user32.SendInput(1, ctypes.byref(x), ctypes.sizeof(x))

    @staticmethod
    def click(position, button='left'):
        """
        Simulate a mouse click with BUTTON at POSITION.
        :param position:    The (x, y) position at which to click.
        :param button:      Either the left or right mouse button.
        :return:            None
        """

        if button not in ['left', 'right']:
            print(f"'{button}' is not a valid mouse button.")
        else:
            if button == 'left':
                down_event = win32con.MOUSEEVENTF_LEFTDOWN
                up_event = win32con.MOUSEEVENTF_LEFTUP
            else:
                down_event = win32con.MOUSEEVENTF_RIGHTDOWN
                up_event = win32con.MOUSEEVENTF_RIGHTUP
            win32api.SetCursorPos(position)
            win32api.mouse_event(down_event, position[0], position[1], 0, 0)
            win32api.mouse_event(up_event, position[0], position[1], 0, 0)


OUTPUT_MODE = 'SendInput'
_output = _SendInput()
input_lock = threading.RLock()
held_keys = set()
_leonardo = None
_probe = None
_dry_binding = None
_dry_held = set()
_dry_count = 0
dry_status = 'Dry run ready. No physical input.'


def external_keyboard():
    return _leonardo


def _video_context(observation):
    frame = observation.frame
    return {'capture_session': frame.session, 'frame': frame.number, 'captured_at': frame.received}


def _log(event, **fields):
    try:
        print(json.dumps({'event': event, 'utc_ns': time_ns(), 'monotonic_ns': monotonic_ns(), **fields}),
              flush=True)
    except OSError:
        pass


def _uart_observation(code):
    code = code.strip().lower()
    if not re.fullmatch(r'[0-9a-f]{8}', code) or config.capture_mode == config.CAPTURE_MODE:
        raise KeyboardOutputError('Open GC311G2 and enter the test window’s eight-digit code.')
    observation = config.capture.usb_observation()
    proof = observation.focus
    if (observation.status.state != 'STREAMING' or not observation.status.actual
            or observation.frame is None or proof is None or proof.code != code
            or monotonic() - proof.advanced >= MARKER_TTL):
        raise KeyboardOutputError('The confirmed test window marker is missing or stale.')
    binding = code, proof.epoch, observation.frame.session, observation.status.actual
    return binding, observation


def uart_approval(code):
    return _uart_observation(code)[0]


def plan_uart_probe(code, hold):
    with input_lock:
        if _leonardo is not None or not .05 <= hold <= 5:
            raise KeyboardOutputError('Select dry run and enter an F8 hold between 0.05 and 5 seconds.')
        approval, observed = _uart_observation(code)
        context = _video_context(observed)
        _log('planned_key', output='Dry run', code=approval[0], key='f8', down=True,
             offset_seconds=0, actual_output=0, **context)
        _log('planned_key', output='Dry run', code=approval[0], key='f8', down=False,
             offset_seconds=hold, actual_output=0, **context)


def arm_uart(port, approval):
    global _leonardo
    with input_lock:
        if config.enabled or getattr(config.bot, 'executing_step', False) or held_keys:
            raise KeyboardOutputError('Stop the routine before arming the UART test.')
        if _leonardo is not None and not _leonardo.finished:
            raise KeyboardOutputError('Disarm the current Leonardo session first.')
        code = approval[0]
        if uart_approval(code) != approval:
            raise KeyboardOutputError('The capture or test target has changed. Confirm it again.')

        def target_ok():
            try:
                current, observed = _uart_observation(code)
                if current == approval:
                    return _video_context(observed)
            except KeyboardOutputError:
                pass
            return False

        def audit(event, response, fields):
            if response is not None:
                fields = {**fields, 'seq': response.seq, 'hid_session': response.session_id}
            _log(event, output='Leonardo UART', code=code, **fields)

        _leonardo = LeonardoKeyboard(port, target_ok, lambda: True, uart=True, focus_timeout=.1, audit=audit)
        _log('target_confirmed', output='Leonardo UART', port=port, code=code,
             marker_epoch=approval[1], capture_session=approval[2], usb_mode=approval[3])
        _leonardo.start()


def send_uart_probe(hold):
    global _probe
    with input_lock:
        selected = _leonardo
        if selected is None or not selected.uart or not selected.armed or config.capture_mode == config.CAPTURE_MODE:
            raise KeyboardOutputError('Arm the GC311G2 test target first.')
        if not .05 <= hold <= 5:
            raise KeyboardOutputError('F8 hold must be between 0.05 and 5 seconds.')
        if _probe is not None and _probe.is_alive():
            raise KeyboardOutputError('The previous F8 probe is still running.')

        def probe():
            try:
                selected.key('f8', True)
                if not selected.wait_cancel(hold):
                    selected.key('f8', False)
            except KeyboardOutputError as error:
                _log('probe_interrupted', output='Leonardo UART', reason=str(error))

        _probe = threading.Thread(target=probe, name='Leonardo F8 probe', daemon=True)
        _probe.start()


def dry_running():
    return _dry_binding is not None and config.enabled


def _dry_observation():
    observation = config.capture.usb_observation()
    if observation.frame is None or not observation.player or not observation.minimap_box:
        return None, observation
    return (observation.frame.session, observation.minimap_box), observation


def start_dry_run():
    global _dry_binding, _dry_count, dry_status
    with input_lock:
        if config.capture_mode == config.CAPTURE_MODE or _leonardo is not None:
            raise KeyboardOutputError('Select GC311G2 dry run first.')
        if config.enabled or getattr(config.bot, 'executing_step', False):
            raise KeyboardOutputError('Wait for the current command to stop.')
        if not config.bot.ready:
            raise KeyboardOutputError('The Auto Maple routine worker is unavailable.')
        config.bot.validate_keyboard_only()
        binding, observation = _dry_observation()
        if binding is None:
            raise KeyboardOutputError('Wait for a fresh minimap and player position.')
        _dry_binding, _dry_count = binding, 0
        config.bot.rune_active = False
        config.stage_fright = False
        config.routine.index = 0
        _update_dry_position(observation)
        config.enabled = True
        dry_status = 'Dry routine running. Planned edges: 0. Actual output: 0.'
        _log('dry_run_started', **_video_context(observation), minimap=observation.minimap_box,
             player=observation.player, actual_output=0)


def refresh_dry_run():
    if _dry_binding is None:
        return None
    with input_lock:
        if _dry_binding is None:
            return None
        if not config.enabled:
            stop_dry_run('Routine paused.')
            return None
        binding, observation = _dry_observation()
        if binding != _dry_binding:
            stop_dry_run('Fresh video, minimap calibration, or player position was lost.')
            return None
        _update_dry_position(observation)
        return observation


def _update_dry_position(observation):
    config.player_pos = observation.player
    config.capture.minimap_ratio = observation.minimap_box[2] / observation.minimap_box[3]


def stop_dry_run(reason='Dry run stopped.'):
    global _dry_binding, _dry_count, dry_status
    with input_lock:
        if _dry_binding is None:
            return
        config.enabled = False
        config.bot.rune_active = False
        config.path = []
        for key in sorted(_dry_held):
            _log('planned_key', key=key, down=False, actual_output=0, reason=reason)
            _dry_count += 1
        _dry_held.clear()
        _dry_binding = None
        dry_status = f'{reason} Planned edges: {_dry_count}. Actual output: 0.'
        _log('dry_run_stopped', reason=reason, planned_edges=_dry_count, actual_output=0)


def _dry_key(key, down):
    global _dry_count, dry_status
    observation = refresh_dry_run()
    if observation is None:
        return
    usage = key_usage(key)
    key = key.lower()
    if not down and key not in _dry_held:
        return
    if down:
        if key not in _dry_held and usage < 0xE0:
            if sum(key_usage(held) < 0xE0 for held in _dry_held) >= 6:
                raise KeyboardOutputError('Leonardo supports up to six held non-modifier keys.')
        _dry_held.add(key)
    else:
        _dry_held.remove(key)
    _dry_count += 1
    _log('planned_key', key=key, down=down, **_video_context(observation), actual_output=0)
    dry_status = f'Dry routine running. Planned edges: {_dry_count}. Actual output: 0.'


def arm_leonardo(port, target):
    global _leonardo
    with input_lock:
        if (config.capture_mode != config.CAPTURE_MODE or config.enabled or held_keys
                or getattr(config.bot, 'executing_step', False)):
            raise KeyboardOutputError('Pause input and select MSS before arming Leonardo.')
        if _leonardo is not None and not _leonardo.finished:
            raise KeyboardOutputError('Disarm the current Leonardo session first.')
        current = config.capture.local_target
        if current is None or current[0] != target or monotonic() - current[1] >= .75:
            raise KeyboardOutputError('Open MapleStory and wait for the MSS capture.')

        def target_ok():
            current = config.capture.local_target
            return (config.capture_mode == config.CAPTURE_MODE and current is not None
                    and current[0] == target and monotonic() - current[1] < .75
                    and win32gui.GetForegroundWindow() == target)

        def routine_running():
            return config.enabled and config.bot.thread.is_alive()

        def stop_routine():
            config.enabled = False

        _leonardo = LeonardoKeyboard(port, target_ok, routine_running, on_disarm=stop_routine)
        _leonardo.start()


def disarm_leonardo(*, wait=False):
    with input_lock:
        selected = _leonardo
        if selected is not None:
            config.enabled = False
            selected.stop()
    if wait and selected is not None:
        selected.wait(2)


def use_send_input():
    global _leonardo
    with input_lock:
        if _leonardo is not None and not _leonardo.finished:
            raise KeyboardOutputError('Disarm Leonardo and wait for the session to close.')
        if _leonardo is not None:
            if getattr(config.bot, 'executing_step', False):
                raise KeyboardOutputError('Wait for the stopped routine to finish its current command.')
            config.enabled = False
        _leonardo = None


def _external_key(key, down):
    try:
        _leonardo.key(key, down)
    except KeyboardOutputError:
        disarm_leonardo()
        raise


@utils.run_if_enabled
def key_down(key):
    with input_lock:
        if config.capture_mode != config.CAPTURE_MODE:
            return _dry_key(key, True)
        if config.enabled:
            if _leonardo is not None:
                return _external_key(key, True)
            _output.key_down(key)
            if key.lower() in KEY_MAP:
                held_keys.add(key.lower())


def key_up(key):
    with input_lock:
        if config.capture_mode != config.CAPTURE_MODE:
            return _dry_key(key, False)
        if _leonardo is not None:
            return _external_key(key, False)
        _output.key_up(key)
        held_keys.discard(key.lower())


@utils.run_if_enabled
def click(position, button='left'):
    with input_lock:
        if dry_running():
            stop_dry_run('Routine requires mouse input.')
            raise KeyboardOutputError('This command requires mouse output.')
        if config.enabled and config.capture_mode == config.CAPTURE_MODE:
            if _leonardo is not None:
                disarm_leonardo()
                raise KeyboardOutputError('This command requires mouse output. Leonardo is keyboard only.')
            return _output.click(position, button)


@utils.run_if_enabled
def press(key, n, down_time=0.05, up_time=0.1):
    """
    Presses KEY N times, holding it for DOWN_TIME seconds, and releasing for UP_TIME seconds.
    :param key:         The keyboard input to press.
    :param n:           Number of times to press KEY.
    :param down_time:   Duration of down-press (in seconds).
    :param up_time:     Duration of release (in seconds).
    :return:            None
    """

    for _ in range(n):
        key_down(key)
        time.sleep(down_time * (0.8 + 0.4 * random()))
        key_up(key)
        time.sleep(up_time * (0.8 + 0.4 * random()))
