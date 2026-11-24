import os
from dataclasses import replace
from functools import partial
from pathlib import Path
from queue import Queue
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from src.modules.usb_video import UsbVideo, VideoSettings


DEVICE = '@device:pnp:usb#vid_07ca&pid_3112'
SETTINGS = VideoSettings(width=400, height=320, roi=(20, 30, 350, 260))


def wait_until(predicate, timeout=8):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = predicate()
        if result:
            return result
        sleep(.015)
    pytest.fail('Timed out waiting for the requested video state.')


def signal_image():
    image = np.zeros((320, 400, 3), np.uint8)
    image[50:80, 50:80] = (255, 100, 0)
    return image


def paths(control):
    return () if Path(control).read_text() == 'absent' else (DEVICE,)


def frame_with_target():
    frame = np.zeros((320, 400, 3), np.uint8)
    for x, y, asset in ((50, 50, 'minimap_tl'), (200, 170, 'minimap_br'), (100, 100, 'player')):
        pattern = cv2.imread(f'assets/{asset}_template.png', cv2.IMREAD_GRAYSCALE)
        h, w = pattern.shape
        frame[y:y + h, x:x + w] = pattern[:, :, None]
    frame[240:260, 310:330] = (23, 88, 211)
    return frame


class FakeDirectShow:
    def __init__(self, control, index, backend):
        assert index == 0 and backend == cv2.CAP_DSHOW
        self.control = Path(control)
        self.frame = frame_with_target()
        self.count = 0

    def isOpened(self):
        return True

    def getBackendName(self):
        return 'DSHOW'

    def set(self, prop, value):
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FOURCC:
            return cv2.VideoWriter_fourcc(*('YUY2' if self.control.read_text() == 'changed' else 'MJPG'))
        return 30

    def read(self):
        sleep(.02)
        self.count += 1
        mode = self.control.read_text()
        if mode == 'blocked':
            sleep(60)
        if mode != 'duplicate':
            self.frame[0, 0, 0] = self.count % 255
        if mode == 'no-signal':
            return True, signal_image()
        return True, self.frame

    def release(self):
        pass


def fake_source(control, settings):
    return UsbVideo(settings, paths=partial(paths, control),
                    capture_factory=partial(FakeDirectShow, control))


def test_usb_roi_and_invalidation_keep_output_zero(tmp_path, monkeypatch):
    from src.common import config, vkeys
    from src.modules.capture import Capture
    from src.modules.listener import Listener

    control = tmp_path / 'video-mode'
    control.write_text('streaming')
    reference = tmp_path / 'reference.png'
    assert cv2.imwrite(str(reference), signal_image())
    monkeypatch.setattr(config, 'capture', None)
    monkeypatch.setattr(config, 'capture_mode', config.CAPTURE_MODE)
    monkeypatch.setattr(config, 'enabled', False)
    monkeypatch.setattr(config, 'bot', SimpleNamespace(rune_active=False))
    monkeypatch.setattr(vkeys, 'held_keys', set())
    native = Mock()
    monkeypatch.setattr(vkeys, '_output', native)
    capture = Capture(usb_factory=partial(fake_source, control))
    capture.select_video(replace(SETTINGS, reference=str(reference)))
    capture.start()

    def observation(state):
        status = capture.usb_observation()
        return status if status.status.state == state else None

    try:
        first = wait_until(lambda: observation('STREAMING'))
        assert first.status.actual == (400, 320, 'MJPG', 30)
        assert first.minimap_box[:2] == (59, 55)
        assert first.player
        assert first.roi.shape == (260, 350, 3)
        assert tuple(first.frame.pixels[250, 320]) == (23, 88, 211)
        assert tuple(first.roi[220, 300]) == (23, 88, 211)
        with pytest.raises(ValueError):
            first.frame.pixels.setflags(write=True)

        Listener.toggle_enabled()
        assert not config.enabled
        config.enabled = True
        vkeys.press('a', 1, down_time=0, up_time=0)
        vkeys.key_down('a')
        vkeys.key_up('a')
        vkeys.click((1, 2))
        assert not native.mock_calls
        config.enabled = False

        control.write_text('duplicate')
        stale = wait_until(lambda: observation('STALE'))
        assert stale.frame is None and not stale.minimap_box
        wait_until(lambda: capture.usb_observation().status.measured_fps > 0)
        control.write_text('streaming')
        recovered = wait_until(lambda: observation('STREAMING'))
        assert recovered.frame.session != first.frame.session
        control.write_text('no-signal')
        assert wait_until(lambda: observation('NO_SIGNAL')).frame is None
        control.write_text('streaming')
        assert wait_until(lambda: observation('STREAMING')).frame.session != recovered.frame.session
        control.write_text('changed')
        assert wait_until(lambda: observation('SOURCE_CHANGED')).frame is None

        control.write_text('streaming')
        capture.select_video(SETTINGS)
        reopened = wait_until(lambda: observation('STREAMING'))
        assert reopened.frame.session != first.frame.session
        control.write_text('absent')
        assert wait_until(lambda: observation('DISCONNECTED')).frame is None
    finally:
        capture.close()


def test_blocked_read_can_be_closed(tmp_path):
    control = tmp_path / 'video-mode'
    control.write_text('streaming')
    source = fake_source(control, SETTINGS)
    try:
        def streaming():
            source.poll()
            return source.frame

        wait_until(streaming)
        control.write_text('blocked')
        sleep(.1)
        started = monotonic()
        source.close()
        assert monotonic() - started < 2
        assert source.frame is None and source._process is None
    finally:
        source.close()


def test_rune_analysis_uses_roi_and_drops_result_after_source_loss(monkeypatch):
    from src.common import config
    from src.modules.bot import Bot, detection
    from src.modules.capture import UsbObservation
    from src.modules.usb_video import VideoFrame, VideoStatus

    pixels = frame_with_target()
    observation = UsbObservation(VideoStatus('STREAMING'), VideoFrame('current', 1, monotonic(), pixels),
                                 pixels[30:290, 20:370])
    bot = Bot.__new__(Bot)
    bot.usb_rune_requests = Queue()
    bot.usb_rune_result = None
    capture = SimpleNamespace(usb_observation=lambda: observation)
    monkeypatch.setattr(config, 'capture_mode', 'GC311G2')
    monkeypatch.setattr(config, 'capture', capture)
    model = object()
    arrows = ['up', 'left', 'down', 'right']
    analyze = Mock(return_value=arrows)
    monkeypatch.setattr(detection, 'detect_rune', analyze)

    bot.usb_rune_requests.put(observation)
    bot._inspect_video_rune(model)
    assert analyze.call_args.args[0] is model
    assert analyze.call_args.args[1] is observation.roi
    assert bot.usb_rune_result == ('current', 1, 'up, left, down, right')

    def lose_source(model, image):
        capture.usb_observation = lambda: UsbObservation(VideoStatus('NO_SIGNAL'))
        return arrows

    monkeypatch.setattr(detection, 'detect_rune', lose_source)
    bot.usb_rune_result = None
    bot.usb_rune_requests.put(observation)
    bot._inspect_video_rune(model)
    assert bot.usb_rune_result is None


@pytest.mark.skipif(os.getenv('AUTO_MAPLE_TEST_GC311G2') != '1', reason='requires a connected GC311G2')
def test_native_gc311g2_reception():
    settings = VideoSettings(fourcc=os.getenv('AUTO_MAPLE_GC311G2_FOURCC', ''))
    source = UsbVideo(settings)
    try:
        def received():
            source.poll()
            if source.status.state not in ('OPENING', 'STREAMING', 'STALE'):
                pytest.fail(str(source.status))
            return source.status if source.status.measured_fps > 0 else None

        status = wait_until(received)
        frame = source.frame
        assert frame is not None and frame.pixels.shape == (720, 1280, 3)
        assert frame.pixels.dtype == np.uint8
        deadline = monotonic() + 3
        while monotonic() < deadline:
            source.poll()
            sleep(.015)
        print(f'720p30 request, {settings.fourcc or "device default"}; first frame {frame.number}; '
              f'final {source.status}')
        assert source.status.state in ('STREAMING', 'STALE')
    finally:
        source.close()
    if os.getenv('AUTO_MAPLE_TEST_RUNE_MODEL') == '1':
        from src.detection import detection

        arrows = detection.detect_rune(detection.load_model(), frame.pixels)
        assert set(arrows) <= {'up', 'down', 'left', 'right'}
        print(f'SavedModel on GC311G2 frame {frame.number}: {arrows}')
