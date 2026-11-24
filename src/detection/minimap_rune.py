"""Shared minimap rune recognition for MSS and GC311G2."""

import cv2

from src.common import utils


RUNE_RANGES = (((141, 148, 245), (146, 158, 255)),)
_filtered = utils.filter_color(cv2.imread('assets/rune_template.png'), RUNE_RANGES)
RUNE_TEMPLATE = cv2.cvtColor(_filtered, cv2.COLOR_BGR2GRAY)


def find_runes(minimap):
    filtered = utils.filter_color(minimap, RUNE_RANGES)
    return utils.multi_match(filtered, RUNE_TEMPLATE, threshold=.9)
