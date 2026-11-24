"""Read the DirectShow video input monikers in OpenCV's index order."""

from ctypes import POINTER, Structure, WINFUNCTYPE, WinDLL, byref, cast, c_long, c_ulong, c_void_p, wstring_at
from functools import cache
from uuid import UUID


class _Guid(Structure):
    _fields_ = [("data", c_ulong * 4)]


_SYSTEM_ENUMERATOR = _Guid.from_buffer_copy(UUID("62be5d10-60eb-11d0-bd3b-00a0c911ce86").bytes_le)
_DEVICE_ENUMERATOR = _Guid.from_buffer_copy(UUID("29840822-5b84-11d0-bd3b-00a0c911ce86").bytes_le)
_VIDEO_INPUT = _Guid.from_buffer_copy(UUID("860bb310-5d01-11d0-bd3b-00a0c911ce86").bytes_le)


def _call(pointer, slot, result_type, types=(), args=()):
    table = cast(pointer, POINTER(POINTER(c_void_p))).contents
    return WINFUNCTYPE(result_type, c_void_p, *types)(table[slot])(pointer, *args)


def _release(pointer):
    if pointer.value:
        _call(pointer, 2, c_ulong)


@cache
def _ole32():
    ole = WinDLL("ole32")
    ole.CoInitializeEx.argtypes = (c_void_p, c_ulong)
    ole.CoCreateInstance.argtypes = (POINTER(_Guid), c_void_p, c_ulong, POINTER(_Guid), POINTER(c_void_p))
    ole.CreateBindCtx.argtypes = (c_ulong, POINTER(c_void_p))
    ole.CoTaskMemFree.argtypes = (c_void_p,)
    return ole


def directshow_paths():
    """Return PnP monikers, preserving slots for other filters. None means enumeration failed."""
    ole = _ole32()
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE: usable existing apartment
        return None
    enumerator, devices, context = c_void_p(), c_void_p(), c_void_p()
    try:
        if ole.CoCreateInstance(byref(_SYSTEM_ENUMERATOR), None, 1, byref(_DEVICE_ENUMERATOR), byref(enumerator)) != 0:
            return None
        status = _call(enumerator, 3, c_long, (POINTER(_Guid), POINTER(c_void_p), c_ulong),
                       (byref(_VIDEO_INPUT), byref(devices), 0))
        if status == 1:
            return ()
        if status != 0 or ole.CreateBindCtx(0, byref(context)) != 0:
            return None
        paths = []
        for _ in range(256):
            moniker, name = c_void_p(), c_void_p()
            status = _call(devices, 3, c_long, (c_ulong, POINTER(c_void_p), POINTER(c_ulong)),
                           (1, byref(moniker), None))
            if status != 0:
                return tuple(paths) if status == 1 else None
            try:
                if _call(moniker, 20, c_long, (c_void_p, c_void_p, POINTER(c_void_p)),
                         (context, None, byref(name))) != 0 or not name.value:
                    return None
                path = wstring_at(name).casefold()
                paths.append(path if path.startswith("@device:pnp:") else None)
            finally:
                if name.value:
                    ole.CoTaskMemFree(name)
                _release(moniker)
        return None
    finally:
        _release(context)
        _release(devices)
        _release(enumerator)
        if initialized >= 0:
            ole.CoUninitialize()
