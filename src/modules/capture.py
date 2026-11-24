"""A module for tracking useful in-game information."""

import time
import cv2
import threading
import ctypes
import mss
import mss.windows
import numpy as np
from dataclasses import dataclass, replace
from src.common import config, utils, vkeys
from src.common.focus_marker import FocusMarker
from src.modules.usb_video import STALE_AFTER, UsbVideo, VideoStatus
from ctypes import wintypes
user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


# The distance between the top of the minimap and the top of the screen
MINIMAP_TOP_BORDER = 5

# The thickness of the other three borders of the minimap
MINIMAP_BOTTOM_BORDER = 9

# Offset in pixels to adjust for windowed mode
WINDOWED_OFFSET_TOP = 36
WINDOWED_OFFSET_LEFT = 10

# The top-left and bottom-right corners of the minimap
MM_TL_TEMPLATE = cv2.imread('assets/minimap_tl_template.png', 0)
MM_BR_TEMPLATE = cv2.imread('assets/minimap_br_template.png', 0)

MMT_HEIGHT = max(MM_TL_TEMPLATE.shape[0], MM_BR_TEMPLATE.shape[0])
MMT_WIDTH = max(MM_TL_TEMPLATE.shape[1], MM_BR_TEMPLATE.shape[1])

# The player's symbol on the minimap
PLAYER_TEMPLATE = cv2.imread('assets/player_template.png', 0)
PT_HEIGHT, PT_WIDTH = PLAYER_TEMPLATE.shape


def minimap_bounds(tl, br):
    left, top = tl[0] + MINIMAP_BOTTOM_BORDER, tl[1] + MINIMAP_TOP_BORDER
    right = max(left + PT_WIDTH, br[0] - MINIMAP_BOTTOM_BORDER)
    bottom = max(top + PT_HEIGHT, br[1] - MINIMAP_BOTTOM_BORDER)
    return left, top, right, bottom


def usb_minimap_bounds(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners = []
    for template in (MM_TL_TEMPLATE, MM_BR_TEMPLATE):
        result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, position = cv2.minMaxLoc(result)
        if score < .8:
            return None
        corners.append(position)
    tl, (x, y) = corners
    left, top, right, bottom = minimap_bounds(
        tl, (x + MM_BR_TEMPLATE.shape[1], y + MM_BR_TEMPLATE.shape[0]))
    if x > left and y > top and right <= image.shape[1] and bottom <= image.shape[0]:
        return left, top, right, bottom
    return None


@dataclass(frozen=True)
class UsbObservation:
    status: VideoStatus
    frame: object = None
    roi: object = None
    minimap_box: tuple = ()
    player: tuple = ()
    focus: object = None

    def minimap_pixels(self):
        if self.frame is None or not self.minimap_box:
            return None
        x, y, width, height = self.minimap_box
        return self.frame.pixels[y:y + height, x:x + width]


class Capture:
    """
    A class that tracks player position and various in-game events. It constantly updates
    the config module with information regarding these events. It also annotates and
    displays the minimap in a pop-up window.
    """

    def __init__(self, *, mss_factory=mss.mss, usb_factory=UsbVideo):
        """Initializes this Capture object's main thread."""

        config.capture = self

        self.frame = None
        self.minimap = {}
        self.minimap_ratio = 1
        self.minimap_sample = None
        self.local_target = None
        self.sct = None
        self._mss_factory = mss_factory
        self._usb_factory = usb_factory
        self._selection = None
        self._observation = None, UsbObservation(VideoStatus('CLOSED'))
        self._stop = threading.Event()
        self.window = {
            'left': 0,
            'top': 0,
            'width': 1366,
            'height': 768
        }

        self.ready = False
        self.calibrated = False
        self.thread = threading.Thread(target=self._main)
        self.thread.daemon = True

    def select_video(self, settings):
        """Apply a new preview selection, or restore the default MSS source."""
        with vkeys.input_lock:
            if config.enabled or vkeys.held_keys:
                raise ValueError('Stop the routine and wait for its keys to release before changing video.')
            if vkeys.external_keyboard() is not None:
                raise ValueError('Disarm Leonardo and select the default output before changing capture.')
            if getattr(config.bot, 'executing_step', False):
                raise ValueError('Wait for the stopped routine to finish its current command.')
            config.capture_mode = 'GC311G2' if settings is not None else config.CAPTURE_MODE
            self._selection = (settings,) if settings is not None else None
            self.calibrated = False
            self.minimap_sample = None
            self.minimap = {}
            self.local_target = None

    def close(self):
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(2)

    def usb_observation(self):
        selection, observation = self._observation
        if selection is not self._selection:
            return UsbObservation(VideoStatus('OPENING'))
        frame = observation.frame
        if frame is not None and time.monotonic() - frame.received >= STALE_AFTER:
            status = observation.status
            return UsbObservation(VideoStatus('STALE', 'Waiting for a fresh frame.',
                                               status.actual, status.measured_fps))
        return observation

    def _run_usb(self, selection):
        settings, = selection
        source = None
        try:
            source = self._usb_factory(settings)
            session = None
            previous = None
            bounds = None
            next_calibration = 0
            observation = None
            marker = FocusMarker()
            while self._selection is selection and not self._stop.is_set():
                source.poll()
                frame = source.frame
                if frame is None:
                    if previous is not None:
                        marker.reset()
                    session = previous = bounds = None
                    next_calibration = 0
                    observation = UsbObservation(source.status)
                elif frame is not previous:
                    x, y, width, height = settings.roi or (0, 0, settings.width, settings.height)
                    roi = frame.pixels[y:y + height, x:x + width]
                    if frame.session != session:
                        marker.reset()
                    if frame.session != session or frame.received >= next_calibration:
                        session, bounds = frame.session, usb_minimap_bounds(roi)
                        next_calibration = frame.received + .25
                    box = player_pos = ()
                    if bounds:
                        left, top, right, bottom = bounds
                        minimap = roi[top:bottom, left:right]
                        player = utils.multi_match(minimap, PLAYER_TEMPLATE, threshold=.8)
                        if player:
                            mx, my = player[0]
                            player_pos = (mx / minimap.shape[1], my / minimap.shape[1])
                        box = (x + left, y + top, right - left, bottom - top)
                    focus = marker.update(frame.pixels, frame.received)
                    observation = UsbObservation(source.status, frame, roi, box, player_pos, focus)
                    previous = frame
                elif source.status != observation.status:
                    observation = replace(observation, status=source.status)
                self._observation = selection, observation
                vkeys.refresh_dry_run()
                self._stop.wait(.015)
        except (OSError, ValueError, cv2.error) as error:
            self._observation = selection, UsbObservation(VideoStatus('CAPTURE_ERROR', str(error)))
            vkeys.refresh_dry_run()
            while self._selection is selection and not self._stop.wait(.05):
                pass
        finally:
            vkeys.stop_dry_run()
            if source is not None:
                source.close()
            self.minimap_sample = None
            self.minimap = {}

    def start(self):
        """Starts this Capture's thread."""

        print('\n[~] Started video capture')
        self.thread.start()

    def _main(self):
        """Constantly monitors the player's position and in-game events."""

        mss.windows.CAPTUREBLT = 0
        while not self._stop.is_set():
            selection = self._selection
            if selection is not None:
                self._run_usb(selection)
                continue
            # Calibrate screen capture
            handle = user32.FindWindowW(None, 'MapleStory')
            rect = wintypes.RECT()
            if not handle or not user32.GetWindowRect(handle, ctypes.pointer(rect)):
                self.local_target = None
                self._stop.wait(.05)
                continue
            rect = (rect.left, rect.top, rect.right, rect.bottom)
            rect = tuple(max(0, x) for x in rect)

            self.window['left'] = rect[0]
            self.window['top'] = rect[1]
            self.window['width'] = max(rect[2] - rect[0], MMT_WIDTH)
            self.window['height'] = max(rect[3] - rect[1], MMT_HEIGHT)

            # Calibrate by finding the top-left and bottom-right corners of the minimap
            with self._mss_factory() as self.sct:
                self.frame = self.screenshot()
            if self.frame is None or self._selection is not None:
                continue
            tl, _ = utils.single_match(self.frame, MM_TL_TEMPLATE)
            _, br = utils.single_match(self.frame, MM_BR_TEMPLATE)
            left, top, right, bottom = minimap_bounds(tl, br)
            mm_tl, mm_br = (left, top), (right, bottom)
            self.minimap_ratio = (mm_br[0] - mm_tl[0]) / (mm_br[1] - mm_tl[1])
            self.minimap_sample = self.frame[mm_tl[1]:mm_br[1], mm_tl[0]:mm_br[0]]
            self.calibrated = True

            with self._mss_factory() as self.sct:
                while not self._stop.is_set():
                    if not self.calibrated or self._selection is not None:
                        break

                    # Take screenshot
                    self.frame = self.screenshot()
                    if self.frame is None:
                        continue
                    self.local_target = handle, time.monotonic()

                    # Crop the frame to only show the minimap
                    minimap = self.frame[mm_tl[1]:mm_br[1], mm_tl[0]:mm_br[0]]

                    # Determine the player's position
                    player = utils.multi_match(minimap, PLAYER_TEMPLATE, threshold=0.8)
                    if player:
                        config.player_pos = utils.convert_to_relative(player[0], minimap)

                    # Package display information to be polled by GUI
                    self.minimap = {
                        'minimap': minimap,
                        'rune_active': config.bot.rune_active,
                        'rune_pos': config.bot.rune_pos,
                        'path': config.path,
                        'player_pos': config.player_pos
                    }

                    if not self.ready:
                        self.ready = True
                    time.sleep(0.001)

    def screenshot(self, delay=1):
        try:
            return np.array(self.sct.grab(self.window))
        except mss.exception.ScreenShotError:
            print(f'\n[!] Error while taking screenshot, retrying in {delay} second'
                  + ('s' if delay != 1 else ''))
            time.sleep(delay)
