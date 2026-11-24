"""Explicitly armed, keyboard-only Leonardo CDC or UART sessions."""

import re
import threading
from struct import pack
from time import monotonic


HEARTBEAT_INTERVAL = .25
HID_USAGES = {
    'enter': 0x28, 'esc': 0x29, 'backspace': 0x2A, 'tab': 0x2B, 'space': 0x2C,
    '-': 0x2D, '=': 0x2E, '[': 0x2F, ']': 0x30, '\\': 0x31, ';': 0x33,
    "'": 0x34, '`': 0x35, ',': 0x36, '.': 0x37, '/': 0x38, 'caps lock': 0x39,
    'scroll lock': 0x47, 'insert': 0x49, 'home': 0x4A, 'page up': 0x4B,
    'delete': 0x4C, 'end': 0x4D, 'page down': 0x4E, 'right': 0x4F,
    'left': 0x50, 'down': 0x51, 'up': 0x52, 'num lock': 0x53,
    'ctrl': 0xE0, 'shift': 0xE1, 'alt': 0xE2,
}
HID_USAGES.update((key, 0x04 + i) for i, key in enumerate('abcdefghijklmnopqrstuvwxyz'))
HID_USAGES.update((key, 0x1E + i) for i, key in enumerate('1234567890'))
HID_USAGES.update((f'f{i}', 0x39 + i) for i in range(1, 13))


class KeyboardOutputError(RuntimeError):
    pass


def key_usage(key):
    try:
        return HID_USAGES[key.lower()]
    except (AttributeError, KeyError) as error:
        raise KeyboardOutputError(f'Leonardo does not support key {key!r}.') from error


def port_name(value):
    port = value.strip().upper()
    if not re.fullmatch(r'COM[1-9][0-9]{0,4}', port):
        raise ValueError('Enter an explicit Windows COM port, such as COM4.')
    return port


def _open_link(port):
    try:
        from macro_studio.external_hid.serial_link import open_port
    except ImportError as error:
        raise KeyboardOutputError('Install Macro Studio with its [hid] extra to use Leonardo.') from error
    return open_port(port)


class LeonardoKeyboard:
    def __init__(self, port, target_ok, running, *, on_disarm=lambda: None,
                 link_factory=_open_link, focus_timeout=12, uart=False, audit=None):
        self.port = port_name(port)
        self._target_ok = target_ok
        self._running = running
        self._on_disarm = on_disarm
        self._link_factory = link_factory
        self._focus_timeout = focus_timeout
        self.uart = uart
        self._audit = audit
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._finished = threading.Event()
        self._held = set()
        self._link = None
        self._run_started = False
        self._failure = None
        self.status = ('Checking the approved video marker…' if uart
                       else 'Focus the approved MSS window within 12 seconds.')
        self.armed = False
        self.acknowledged_edges = 0
        self.release_acknowledged = False
        self._worker = threading.Thread(target=self._work, name='Leonardo HID', daemon=True)

    @property
    def finished(self):
        return self._finished.is_set()

    def start(self):
        self._worker.start()

    def stop(self):
        was_stopping = self._cancel.is_set()
        self._cancel.set()
        if not was_stopping:
            self._record('stop_requested')

    def wait(self, timeout):
        return self._finished.wait(timeout)

    def wait_cancel(self, timeout):
        return self._cancel.wait(timeout)

    def _record(self, event, response=None, **fields):
        if self._audit is not None:
            self._audit(event, response, fields)

    def _require_target(self):
        target = self._target_ok()
        if not target:
            source = 'HDMI test marker' if self.uart else 'MSS window'
            raise KeyboardOutputError(f'The approved {source} lost focus or fresh capture.')
        return target

    def _work(self):
        link = None
        try:
            deadline = monotonic() + self._focus_timeout
            while not self._cancel.is_set() and not self._target_ok():
                if monotonic() >= deadline:
                    raise KeyboardOutputError('Target confirmation expired. Confirm and arm again.')
                self._cancel.wait(.05)
            if self._cancel.is_set():
                return
            self.status = f'Checking Leonardo on {self.port}…'
            link = self._link_factory(self.port)
            from macro_studio.external_hid.codec import Opcode

            capabilities = link.inspect()
            board = 'Leonardo-UART' if self.uart else 'Leonardo'
            self._record('hello_caps', board=capabilities['board'], watchdog_ms=capabilities['watchdog_ms'])
            if (capabilities['board'] != board or not capabilities['keyboard']
                    or capabilities['max_payload'] < 6 or capabilities['watchdog_ms'] < 750):
                raise KeyboardOutputError('Leonardo keyboard capabilities or watchdog are incompatible.')
            with self._lock:
                if self._cancel.is_set():
                    return
                self._require_target()
                response = link.exchange(Opcode.ARM_SESSION)
                self._link = link
                self._op = Opcode
                self.armed = True
                self.status = (f'Armed on {self.port}. Send F8 to the test window.' if self.uart
                               else f'Armed on {self.port}. Use Start/stop in the approved window.')
                self._record('arm_ack', response)
            heartbeat_due = monotonic() + HEARTBEAT_INTERVAL
            while not self._cancel.wait(.04):
                with self._lock:
                    if self._cancel.is_set() or (self._run_started and not self._running()):
                        break
                    self._require_target()
                    if monotonic() >= heartbeat_due:
                        heartbeat = link.exchange(Opcode.HEARTBEAT)
                        self._record('heartbeat_ack', heartbeat)
                        heartbeat_due = monotonic() + HEARTBEAT_INTERVAL
        except (RuntimeError, OSError, ValueError, ImportError) as error:
            self._failure = str(error)
            self._record('session_fault', reason=self._failure)
        finally:
            with self._lock:
                self._cancel.set()
                self.armed = False
                self._on_disarm()
                if link is not None:
                    try:
                        # Explicit exchange returns only after a matching release ACK.
                        from macro_studio.external_hid.codec import Opcode
                        response = link.exchange(Opcode.RELEASE_ALL)
                        self.release_acknowledged = True
                        self._record('release_ack', response)
                    except (RuntimeError, OSError, ValueError, ImportError) as error:
                        if self._failure is None:
                            self._failure = f'Release ACK unavailable: {error}'
                        try:
                            link.close()
                        except (RuntimeError, OSError):
                            pass
                self._link = None
                self._held.clear()
                outcome = self._failure or 'Disarmed.'
                self.status = (f'{outcome} Key ACKs: {self.acknowledged_edges}. '
                               f'Release ACK: {"yes" if self.release_acknowledged else "no"}.')
                self._record('disarmed', reason=outcome, release_ack=self.release_acknowledged)
                self._finished.set()

    def start_run(self):
        with self._lock:
            if not self.armed or self._cancel.is_set():
                raise KeyboardOutputError('Arm Leonardo for the approved MSS window first.')
            self._require_target()
            self._run_started = True

    def key(self, key, down):
        with self._lock:
            if not self.armed or self._cancel.is_set():
                if not down:
                    return
                raise KeyboardOutputError('Leonardo is disarmed.')
            try:
                observed = self._require_target()
                usage = key_usage(key)
                if down and usage not in self._held and usage < 0xE0:
                    if sum(held < 0xE0 for held in self._held) >= 6:
                        raise KeyboardOutputError('Leonardo supports up to six held non-modifier keys.')
                if not down and usage not in self._held:
                    return
                action = 1 if down else 2
                evidence = observed if isinstance(observed, dict) else {}
                self._record('key_request', key=key, down=down, **evidence)
                if self.uart:
                    self._require_target()
                    if self._cancel.is_set():
                        raise KeyboardOutputError('The UART test was stopped.')
                response = self._link.exchange(self._op.INPUT_EVENT, pack('<BBHH', 1, action, 7, usage))
                self._record('key_ack', response, key=key, down=down, **evidence)
                self.acknowledged_edges += 1
                if down:
                    self._held.add(usage)
                else:
                    self._held.remove(usage)
            except (RuntimeError, OSError, ValueError) as error:
                self._failure = str(error)
                self.stop()
                raise KeyboardOutputError(str(error)) from error
