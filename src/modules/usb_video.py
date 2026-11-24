"""GC311G2 DirectShow capture with a killable reader and a single latest-frame slot."""

from dataclasses import dataclass
from math import isfinite
from multiprocessing import get_context
from time import monotonic
from uuid import uuid4

import cv2
import numpy as np

from src.modules.directshow_devices import directshow_paths


STALE_AFTER = .75
START_TIMEOUT = 6
GC311G2 = 'vid_07ca&pid_3112'


@dataclass(frozen=True)
class VideoSettings:
    index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    fourcc: str = ''
    roi: tuple = ()
    reference: str = ''

    def __post_init__(self):
        fields = (self.index, self.width, self.height, self.fps)
        if any(type(item) is not int for item in fields) or not (
                0 <= self.index < 256 and 32 <= self.width <= 4096
                and 242 <= self.height <= 2160 and 1 <= self.fps <= 240):
            raise ValueError('Enter a valid index, frame size, and FPS.')
        if self.fourcc not in ('', 'MJPG', 'YUY2', 'NV12'):
            raise ValueError('Select Device default, MJPG, YUY2, or NV12.')
        if not isinstance(self.reference, str):
            raise ValueError('Enter a PNG file path.')
        if not isinstance(self.roi, (tuple, list)):
            raise ValueError('Enter four ROI values: x, y, width, height.')
        object.__setattr__(self, 'roi', tuple(self.roi))
        if not self.roi:
            return
        if len(self.roi) != 4 or any(type(item) is not int for item in self.roi):
            raise ValueError('Enter four ROI values: x, y, width, height.')
        x, y, w, h = self.roi
        if x < 0 or y < 0 or w < 32 or h < 242 or x + w > self.width or y + h > self.height:
            raise ValueError('ROI must fit inside the video and be at least 32 × 242.')


@dataclass(frozen=True)
class VideoFrame:
    session: str
    number: int
    received: float
    pixels: np.ndarray


@dataclass(frozen=True)
class VideoStatus:
    state: str
    detail: str = ''
    actual: tuple = ()
    measured_fps: float = 0


class VideoError(Exception):
    pass


def _mode(capture, pixels):
    fourcc = capture.get(cv2.CAP_PROP_FOURCC)
    if isfinite(fourcc) and 0 < fourcc <= 0xffffffff:
        text = int(fourcc).to_bytes(4, 'little').decode('ascii', errors='replace')
        fourcc = text if all(' ' <= char <= '~' for char in text) else None
    else:
        fourcc = None
    fps = capture.get(cv2.CAP_PROP_FPS)
    return pixels.shape[1], pixels.shape[0], fourcc, fps if isfinite(fps) and fps > 0 else None


def _signal_reference(settings):
    if not settings.reference:
        return None
    with open(settings.reference, 'rb') as file:
        data = file.read(32 * 1024 * 1024 + 1)
    size = settings.width.to_bytes(4, 'big') + settings.height.to_bytes(4, 'big')
    if (len(data) > 32 * 1024 * 1024
            or data[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or data[16:24] != size):
        raise VideoError('INVALID_REFERENCE', 'Use a PNG with the requested frame dimensions.')
    reference = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if reference is None or np.all(reference == reference[0, 0]):
        raise VideoError('INVALID_REFERENCE', 'Use a non-uniform PNG of the card’s no-signal screen.')
    return reference


def _device_at(index, paths):
    devices = paths()
    if devices is None:
        raise VideoError('DEVICE_ENUMERATION_FAILED', 'Windows could not enumerate DirectShow video inputs.')
    if index >= len(devices) or not devices[index] or GC311G2 not in devices[index]:
        indices = ', '.join(str(i) for i, path in enumerate(devices) if path and GC311G2 in path)
        raise VideoError('DEVICE_NOT_FOUND', f'GC311G2 DirectShow indices: {indices or "none"}.')
    return devices[index]


def _read_video(settings, capture_factory, paths, messages, stopped, slot, meta):
    capture = None
    try:
        path = _device_at(settings.index, paths)
        messages.send(('device', path))
        reference = _signal_reference(settings)
        capture = capture_factory(settings.index, cv2.CAP_DSHOW)
        if not capture.isOpened() or capture.getBackendName() != 'DSHOW':
            raise VideoError('OPEN_FAILED', 'Could not open the selected GC311G2 with DirectShow.')
        if _device_at(settings.index, paths) != path:
            raise VideoError('SOURCE_CHANGED', 'DirectShow device order changed during open.')
        capture.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        if settings.fourcc:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*settings.fourcc))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, settings.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.height)
        capture.set(cv2.CAP_PROP_FPS, settings.fps)
        shape = settings.height, settings.width, 3
        target = np.frombuffer(slot, dtype=np.uint8).reshape(shape)
        actual = first = previous = None
        count = generation = 0
        last_unique = 0
        had_signal = True
        while not stopped.is_set():
            ok, pixels = capture.read()
            received = monotonic()
            if not ok:
                stopped.wait(.02)
                continue
            if not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or (
                    pixels.ndim != 3 or pixels.shape[2] != 3):
                raise VideoError('FORMAT_UNSUPPORTED', 'DirectShow did not produce uint8 BGR pixels.')
            mode = _mode(capture, pixels)
            if actual is None:
                messages.send(('mode', mode))
                if pixels.shape != shape or settings.fourcc and settings.fourcc != mode[2]:
                    raise VideoError('MODE_UNSUPPORTED', 'Requested frame size or FourCC was not negotiated.')
                actual, first = mode, received
            elif mode != actual:
                raise VideoError('SOURCE_CHANGED', 'USB video mode changed. Open a new preview session.')
            count += 1
            no_signal = reference is not None and np.array_equal(pixels, reference)
            unique = not no_signal and (previous is None or not np.array_equal(pixels, previous))
            if unique:
                if not last_unique or received - last_unique >= STALE_AFTER:
                    generation += 1
                last_unique = received
                previous = pixels.copy()
            elif no_signal:
                previous = None
                if had_signal:
                    generation += 1
            had_signal = not no_signal
            with meta.get_lock():
                meta[2:] = (count, first, received, no_signal, generation)
                if unique:
                    np.copyto(target, pixels)
                    meta[:2] = (count, received)
                elif no_signal:
                    meta[:2] = (0, 0)
    except VideoError as error:
        messages.send(('error', error.args))
    except (OSError, ValueError, cv2.error) as error:
        messages.send(('error', ('CAPTURE_ERROR', str(error))))
    finally:
        if capture is not None:
            capture.release()
        messages.close()


def _open_capture(index, backend):
    return cv2.VideoCapture(index, backend)


class UsbVideo:
    """Owned by the capture thread. The device is opened only in its child process."""

    def __init__(self, settings, *, capture_factory=_open_capture, paths=directshow_paths):
        self.status = VideoStatus('OPENING')
        self.frame = None
        self._settings = settings
        self._paths = paths
        self._path = None
        self._session = None
        self._number = 0
        self._generation = 0
        self._next_check = 0
        context = get_context('spawn')
        self._messages, sender = context.Pipe(duplex=False)
        self._stop = context.Event()
        self._slot = context.RawArray('B', settings.width * settings.height * 3)
        self._meta = context.Array('d', 7)
        self._process = context.Process(
            target=_read_video,
            args=(settings, capture_factory, paths, sender, self._stop, self._slot, self._meta),
            daemon=True)
        try:
            self._process.start()
        except Exception:
            self._messages.close()
            raise
        finally:
            sender.close()
        self._deadline = monotonic() + START_TIMEOUT

    def poll(self):
        if self._process is None:
            return
        actual = self.status.actual
        now = monotonic()
        if self._path and now >= self._next_check:
            self._next_check = now + .25
            paths = self._paths()
            if paths is not None and self._path not in paths:
                self._fail('DISCONNECTED', 'GC311G2 USB connection was lost.')
                return
        try:
            while self._messages.poll():
                kind, value = self._messages.recv()
                if kind == 'error':
                    self._fail(*value)
                    return
                if kind == 'mode':
                    actual = value
                    self.status = VideoStatus('OPENING', actual=actual)
                if kind == 'device':
                    self._path = value
        except (EOFError, OSError):
            self._fail('WORKER_EXITED', 'DirectShow reader exited.')
            return
        if not self._meta.get_lock().acquire(False):
            return
        try:
            number, received, count, first, last, no_signal, generation = self._meta[:]
            if actual and number > self._number and now - received < STALE_AFTER:
                pixels = np.frombuffer(self._slot, np.uint8).reshape(
                    self._settings.height, self._settings.width, 3)
                if self.frame is None or generation != self._generation:
                    self._session = uuid4().hex
                self._generation = generation
                # Backed by immutable bytes, including when NumPy flags are reset by a caller.
                self.frame = VideoFrame(self._session, int(number), received,
                                        np.frombuffer(pixels.tobytes(), np.uint8).reshape(pixels.shape))
                self._number = number
        finally:
            self._meta.get_lock().release()
        measured = (count - 1) / (last - first) if last > first else 0
        if no_signal:
            self.frame = None
            self.status = VideoStatus('NO_SIGNAL', 'Matches the HDMI no-signal PNG.', actual, measured)
        elif self.frame and now - self.frame.received < STALE_AFTER:
            self.status = VideoStatus('STREAMING', actual=actual, measured_fps=measured)
        elif count:
            self.frame = None
            self.status = VideoStatus('STALE', 'No changed frame received. Check HDMI or move the target.',
                                      actual, measured)
        elif now >= self._deadline:
            self._fail('TIMEOUT', 'Timed out opening DirectShow or waiting for the first frame.')

    def _fail(self, state, detail):
        self.status = VideoStatus(state, detail, self.status.actual, self.status.measured_fps)
        self.close()

    def close(self):
        self.frame = None
        if self._process is None:
            return
        self._stop.set()
        self._process.join(.1)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(.5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(.5)
        self._process.close()
        self._messages.close()
        self._process = None
