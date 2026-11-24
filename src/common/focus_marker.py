"""Visual proof of focus for the two-PC keyboard test window."""

from binascii import crc_hqx
from dataclasses import dataclass


MARKER_TTL = .50
_MAGIC = b'AM'
_START_X, _START_Y = .08, .08
_CELL_W, _CELL_H = .021, .036


def cells(width, height):
    for i in range(64):
        x = _START_X + (i % 16) * _CELL_W
        y = _START_Y + (i // 16) * _CELL_H
        yield (round(x * width), round(y * height),
               round((x + _CELL_W) * width), round((y + _CELL_H) * height))


def _checksum(data):
    return crc_hqx(data, 0xffff) & 255


def marker_bits(code, counter):
    data = _MAGIC + bytes.fromhex(code) + bytes((counter,))
    return [(byte >> bit) & 1 for byte in (*data, _checksum(data)) for bit in range(7, -1, -1)]


def read_marker(pixels):
    height, width = pixels.shape[:2]
    data = bytearray(8)
    for i, (left, top, right, bottom) in enumerate(cells(width, height)):
        rgb = pixels[(top + bottom) // 2, (left + right) // 2]
        if min(rgb) >= 195:
            bit = 1
        elif max(rgb) <= 60:
            bit = 0
        else:
            return None
        data[i // 8] = (data[i // 8] << 1) | bit
        if i == 15 and data[:2] != _MAGIC:
            return None
    if data[-1] != _checksum(data[:-1]):
        return None
    return data[2:6].hex(), data[6]


@dataclass(frozen=True)
class FocusProof:
    code: str
    epoch: int
    advanced: float


class FocusMarker:
    def __init__(self):
        self._marker = None
        self._epoch = 0
        self._advanced = None

    def reset(self):
        self._marker = self._advanced = None
        self._epoch += 1

    def update(self, pixels, received):
        read = read_marker(pixels)
        previous = self._marker
        self._marker = read
        if read is None or previous is None or read[0] != previous[0]:
            self._epoch += 1
            self._advanced = None
            return None
        code, count = read
        delta = (count - previous[1]) % 256
        if delta:
            if delta > 12 or (self._advanced is not None and received - self._advanced >= MARKER_TTL):
                self._epoch += 1
                self._advanced = None
                return None
            self._advanced = received
        if self._advanced is not None and received - self._advanced < MARKER_TTL:
            return FocusProof(code, self._epoch, self._advanced)
        return None
