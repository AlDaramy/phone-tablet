"""Input injection: Windows Ink pen (pressure, barrel button, eraser), mouse fallback,
keyboard shortcuts, cursor position and foreground-window lookup.

`geom` is (left, top, width, height) of the streamed monitor in physical pixels.
x / y arriving here are fractions (0..1) of that monitor.
"""
import sys

from common import (
    ACTIONS, DOWN, MOVE, UP, HOVER, LEAVE, clamp01, log,
)


class BaseInjector:
    def __init__(self, geom):
        self.geom = geom

    def set_geom(self, geom):
        self.geom = geom

    def handle(self, kind, x, y, pressure, barrel=False, eraser=False):
        raise NotImplementedError

    def release(self):
        pass

    def key(self, action):
        pass

    def cursor(self):
        """Cursor position as fractions of the monitor (may lie outside 0..1)."""
        return (-1.0, -1.0)

    def foreground_rect(self):
        """(left, top, right, bottom) of the foreground window in pixels, or None."""
        return None


class PrintInjector(BaseInjector):
    """--dry-run: records and prints what it receives (also used by the tests)."""

    def __init__(self, geom):
        super().__init__(geom)
        self.events = []
        self.keys = []
        self.last = (-1.0, -1.0)

    def handle(self, kind, x, y, pressure, barrel=False, eraser=False):
        names = {DOWN: "down", MOVE: "move", UP: "up", HOVER: "hover", LEAVE: "leave"}
        self.events.append((kind, round(x, 4), round(y, 4), round(pressure, 3), barrel, eraser))
        del self.events[:-2000]
        self.last = (x, y)
        extra = (" barrel" if barrel else "") + (" eraser" if eraser else "")
        log(f"{names.get(kind, kind):5s} x={x:.3f} y={y:.3f} p={pressure:.2f}{extra}")

    def key(self, action):
        self.keys.append(action)
        log(f"key   {ACTIONS[action][0]}")

    def cursor(self):
        return self.last

    def foreground_rect(self):
        l, t, w, h = self.geom
        return (l + w * 0.25, t + h * 0.25, l + w * 0.75, t + h * 0.75)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    DWORD, UINT32, INT32 = wintypes.DWORD, ctypes.c_uint32, ctypes.c_int32
    POINT, RECT = wintypes.POINT, wintypes.RECT

    class POINTER_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerType", DWORD),
            ("pointerId", UINT32),
            ("frameId", UINT32),
            ("pointerFlags", DWORD),
            ("sourceDevice", wintypes.HANDLE),
            ("hwndTarget", wintypes.HWND),
            ("ptPixelLocation", POINT),
            ("ptHimetricLocation", POINT),
            ("ptPixelLocationRaw", POINT),
            ("ptHimetricLocationRaw", POINT),
            ("dwTime", DWORD),
            ("historyCount", UINT32),
            ("InputData", INT32),
            ("dwKeyStates", DWORD),
            ("PerformanceCount", ctypes.c_uint64),
            ("ButtonChangeType", ctypes.c_int),
        ]

    class POINTER_PEN_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerInfo", POINTER_INFO),
            ("penFlags", UINT32),
            ("penMask", UINT32),
            ("pressure", UINT32),
            ("rotation", UINT32),
            ("tiltX", INT32),
            ("tiltY", INT32),
        ]

    class POINTER_TOUCH_INFO(ctypes.Structure):
        # Only here so the union has the correct (larger) size.
        _fields_ = [
            ("pointerInfo", POINTER_INFO),
            ("touchFlags", UINT32),
            ("touchMask", UINT32),
            ("rcContact", RECT),
            ("rcContactRaw", RECT),
            ("orientation", UINT32),
            ("pressure", UINT32),
        ]

    class _TypeUnion(ctypes.Union):
        _fields_ = [("penInfo", POINTER_PEN_INFO), ("touchInfo", POINTER_TOUCH_INFO)]

    class POINTER_TYPE_INFO(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", DWORD), ("u", _TypeUnion)]

    PT_PEN = 3
    POINTER_FEEDBACK_DEFAULT = 1
    PEN_MASK_PRESSURE = 0x1
    PEN_FLAG_BARREL, PEN_FLAG_INVERTED, PEN_FLAG_ERASER = 0x1, 0x2, 0x4

    F_NEW = 0x00000001
    F_INRANGE = 0x00000002
    F_INCONTACT = 0x00000004
    F_FIRSTBUTTON = 0x00000010
    F_SECONDBUTTON = 0x00000020
    F_DOWN = 0x00010000
    F_UPDATE = 0x00020000
    F_UP = 0x00040000

    VK = {
        "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
        "z": 0x5A, "y": 0x59, "e": 0x45, "b": 0x42, "s": 0x53,
        "[": 0xDB, "]": 0xDD,
    }

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

    class _WinBase(BaseInjector):
        def key(self, action):
            names = ACTIONS[action][1]
            codes = [VK[n] for n in names]
            for c in codes:
                user32.keybd_event(c, 0, 0, 0)
            for c in reversed(codes):
                user32.keybd_event(c, 0, 2, 0)  # KEYEVENTF_KEYUP

        def cursor(self):
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            l, t, w, h = self.geom
            return ((pt.x - l) / w, (pt.y - t) / h)

        def foreground_rect(self):
            hwnd = user32.GetForegroundWindow()
            if not hwnd or user32.IsIconic(hwnd):
                return None
            r = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return None
            return (r.left, r.top, r.right, r.bottom)

    class PenInjector(_WinBase):
        """Injects a real Windows Ink pen with pressure."""

        def __init__(self, geom):
            super().__init__(geom)
            user32.CreateSyntheticPointerDevice.restype = wintypes.HANDLE
            user32.CreateSyntheticPointerDevice.argtypes = [DWORD, ctypes.c_ulong, DWORD]
            user32.InjectSyntheticPointerInput.restype = wintypes.BOOL
            user32.InjectSyntheticPointerInput.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(POINTER_TYPE_INFO),
                UINT32,
            ]
            self.device = user32.CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
            if not self.device:
                raise OSError("CreateSyntheticPointerDevice failed (needs Windows 10 1809+)")
            self.in_range = False
            self.in_contact = False

        def _inject(self, flags, x, y, pressure, barrel, eraser):
            left, top, width, height = self.geom
            if barrel and not flags & F_UP:
                flags |= F_SECONDBUTTON
            info = POINTER_TYPE_INFO()
            info.type = PT_PEN
            pen = info.penInfo
            pi = pen.pointerInfo
            pi.pointerType = PT_PEN
            pi.pointerId = 0
            pi.pointerFlags = flags
            pi.ptPixelLocation.x = left + int(clamp01(x) * (width - 1))
            pi.ptPixelLocation.y = top + int(clamp01(y) * (height - 1))
            pen.penFlags = (PEN_FLAG_BARREL if barrel else 0) | (
                (PEN_FLAG_ERASER | PEN_FLAG_INVERTED) if eraser else 0
            )
            pen.penMask = PEN_MASK_PRESSURE
            pen.pressure = int(clamp01(pressure) * 1024)
            return user32.InjectSyntheticPointerInput(self.device, ctypes.byref(info), 1)

        def handle(self, kind, x, y, pressure, barrel=False, eraser=False):
            new = 0 if self.in_range else F_NEW
            if kind == DOWN:
                flags = F_DOWN | F_INRANGE | F_INCONTACT | F_FIRSTBUTTON | new
                self.in_range = self.in_contact = True
            elif kind == MOVE:
                if not self.in_contact:  # lost the DOWN packet; start a stroke
                    self.handle(DOWN, x, y, pressure, barrel, eraser)
                    return
                flags = F_UPDATE | F_INRANGE | F_INCONTACT | F_FIRSTBUTTON
            elif kind == UP:
                if not self.in_contact:
                    return
                flags = F_UP
                self.in_range = self.in_contact = False
            elif kind == HOVER:
                if self.in_contact:
                    return
                flags = F_UPDATE | F_INRANGE | new
                self.in_range = True
            elif kind == LEAVE:
                if not self.in_range or self.in_contact:
                    return
                flags = F_UPDATE
                self.in_range = False
            else:
                return
            self._inject(flags, x, y, pressure, barrel, eraser)

        def release(self):
            if self.in_contact:
                self._inject(F_UP, 0.5, 0.5, 0.0, False, False)
            self.in_range = self.in_contact = False

    class MouseInjector(_WinBase):
        """Fallback: moves the normal mouse cursor (no pressure; barrel = right button)."""

        def __init__(self, geom):
            super().__init__(geom)
            self.down = None  # None, or the mouse_event "up" flag to send

        def handle(self, kind, x, y, pressure, barrel=False, eraser=False):
            left, top, width, height = self.geom
            user32.SetCursorPos(
                left + int(clamp01(x) * (width - 1)), top + int(clamp01(y) * (height - 1))
            )
            if kind == DOWN and self.down is None:
                user32.mouse_event(0x0008 if barrel else 0x0002, 0, 0, 0, 0)
                self.down = 0x0010 if barrel else 0x0004
            elif kind in (UP, LEAVE) and self.down is not None:
                user32.mouse_event(self.down, 0, 0, 0, 0)
                self.down = None

        def release(self):
            if self.down is not None:
                user32.mouse_event(self.down, 0, 0, 0, 0)
                self.down = None


def make_injector(mouse, dry_run, geom):
    if dry_run:
        return PrintInjector(geom)
    if sys.platform != "win32":
        sys.exit("This server injects input on Windows only. Use --dry-run to test elsewhere.")
    if not mouse:
        try:
            inj = PenInjector(geom)
            log("Input mode: Windows Ink pen (pressure supported)")
            return inj
        except OSError as exc:
            log(f"Pen injection unavailable ({exc}); falling back to mouse.")
    log("Input mode: mouse (no pressure)")
    return MouseInjector(geom)
