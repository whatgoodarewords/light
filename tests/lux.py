"""Read the MacBook's ambient light sensor (a brightness number, no image).

Test aid only: lets a test confirm that the room actually got brighter or
dimmer when the bulb was told to change. Uses the same IOKit HID event calls
that display-brightness utilities use. Prints one reading per line.
Usage: python lux.py [seconds] [interval]
"""
import ctypes
import sys
import time

cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
V = ctypes.c_void_p
cf.CFStringCreateWithCString.restype = V
cf.CFStringCreateWithCString.argtypes = [V, ctypes.c_char_p, ctypes.c_uint32]
cf.CFNumberCreate.restype = V
cf.CFNumberCreate.argtypes = [V, ctypes.c_int, V]
cf.CFDictionaryCreate.restype = V
cf.CFDictionaryCreate.argtypes = [V, ctypes.POINTER(V), ctypes.POINTER(V), ctypes.c_long, V, V]
cf.CFArrayGetCount.restype = ctypes.c_long
cf.CFArrayGetCount.argtypes = [V]
cf.CFArrayGetValueAtIndex.restype = V
cf.CFArrayGetValueAtIndex.argtypes = [V, ctypes.c_long]
cf.CFRelease.argtypes = [V]
io.IOHIDEventSystemClientCreate.restype = V
io.IOHIDEventSystemClientCreate.argtypes = [V]
io.IOHIDEventSystemClientSetMatching.argtypes = [V, V]
io.IOHIDEventSystemClientCopyServices.restype = V
io.IOHIDEventSystemClientCopyServices.argtypes = [V]
io.IOHIDServiceClientCopyEvent.restype = V
io.IOHIDServiceClientCopyEvent.argtypes = [V, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]
io.IOHIDEventGetFloatValue.restype = ctypes.c_double
io.IOHIDEventGetFloatValue.argtypes = [V, ctypes.c_int32]

ALS = 12  # kIOHIDEventTypeAmbientLightSensor


def _num(n):
    v = ctypes.c_int32(n)
    return cf.CFNumberCreate(None, 3, ctypes.byref(v))  # kCFNumberSInt32Type


def open_sensor():
    keys = (V * 2)(cf.CFStringCreateWithCString(None, b"PrimaryUsagePage", 0x08000100),
                   cf.CFStringCreateWithCString(None, b"PrimaryUsage", 0x08000100))
    vals = (V * 2)(_num(0xFF00), _num(4))
    kcb = V.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    vcb = V.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    match = cf.CFDictionaryCreate(None, keys, vals, 2, ctypes.addressof(kcb), ctypes.addressof(vcb))
    client = io.IOHIDEventSystemClientCreate(None)
    io.IOHIDEventSystemClientSetMatching(client, match)
    services = io.IOHIDEventSystemClientCopyServices(client)
    if not services or cf.CFArrayGetCount(services) == 0:
        return None
    return cf.CFArrayGetValueAtIndex(services, 0)


def read(service):
    """(lux, [raw channels]) or None."""
    ev = io.IOHIDServiceClientCopyEvent(service, ALS, 0, 0)
    if not ev:
        return None
    lux = io.IOHIDEventGetFloatValue(ev, ALS << 16)
    chans = [io.IOHIDEventGetFloatValue(ev, (ALS << 16) | i) for i in range(1, 5)]
    cf.CFRelease(ev)
    return lux, chans


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 2
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
    s = open_sensor()
    if s is None:
        print("no ambient light sensor found")
        sys.exit(1)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r = read(s)
        print(f"{time.time():.2f}", "none" if r is None else f"lux={r[0]:.2f} ch={[round(c, 1) for c in r[1]]}", flush=True)
        time.sleep(interval)
