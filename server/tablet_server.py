#!/usr/bin/env python3
"""
Phone-as-Graphics-Tablet  --  Windows server

* Streams the laptop screen to the phone (JPEG frames over TCP).
* Receives touch / stylus packets from the phone and injects them into
  Windows as a real pen with pressure (Pointer Injection API).
  Falls back to plain mouse control if pen injection is unavailable.

One TCP connection carries both directions.

phone -> server : 13-byte packets, big-endian  ">Bfff"
    type      0=down 1=move 2=up 3=hover 4=leave 10=resize
    x, y      0.0 .. 1.0 (fraction of the displayed screen image);
              for type 10: x = phone view width px, y = height px
    pressure  0.0 .. 1.0

server -> phone : frames, each  [4-byte big-endian length][JPEG bytes]

Usage:
    python tablet_server.py                 # pen injection, primary monitor
    python tablet_server.py --monitor 2     # another monitor
    python tablet_server.py --mouse         # force mouse mode
    python tablet_server.py --dry-run       # test pattern + print packets (any OS)
"""
import argparse
import io
import socket
import struct
import sys
import threading
import time

PACKET = struct.Struct(">Bfff")
DOWN, MOVE, UP, HOVER, LEAVE, RESIZE = 0, 1, 2, 3, 4, 10


def clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def enable_dpi_awareness():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Input injectors.  `geom` = (left, top, width, height) of the streamed
# monitor in physical pixels (virtual-desktop coordinates).
# --------------------------------------------------------------------------
class PrintInjector:
    """Debug injector: prints what it receives."""

    def handle(self, kind, x, y, pressure):
        names = {DOWN: "down", MOVE: "move", UP: "up", HOVER: "hover", LEAVE: "leave"}
        print(f"{names.get(kind, kind):5s} x={x:.3f} y={y:.3f} p={pressure:.2f}")

    def release(self):
        pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

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

    F_NEW = 0x00000001
    F_INRANGE = 0x00000002
    F_INCONTACT = 0x00000004
    F_FIRSTBUTTON = 0x00000010
    F_DOWN = 0x00010000
    F_UPDATE = 0x00020000
    F_UP = 0x00040000

    class PenInjector:
        """Injects a real Windows Ink pen with pressure."""

        def __init__(self, geom):
            self.left, self.top, self.width, self.height = geom
            user32 = ctypes.windll.user32
            self.user32 = user32

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

        def _inject(self, flags, x, y, pressure):
            info = POINTER_TYPE_INFO()
            info.type = PT_PEN
            pen = info.penInfo
            pi = pen.pointerInfo
            pi.pointerType = PT_PEN
            pi.pointerId = 0
            pi.pointerFlags = flags
            pi.ptPixelLocation.x = self.left + int(clamp01(x) * (self.width - 1))
            pi.ptPixelLocation.y = self.top + int(clamp01(y) * (self.height - 1))
            pen.penMask = PEN_MASK_PRESSURE
            pen.pressure = int(clamp01(pressure) * 1024)
            return self.user32.InjectSyntheticPointerInput(self.device, ctypes.byref(info), 1)

        def handle(self, kind, x, y, pressure):
            new = 0 if self.in_range else F_NEW
            if kind == DOWN:
                flags = F_DOWN | F_INRANGE | F_INCONTACT | F_FIRSTBUTTON | new
                self.in_range = self.in_contact = True
            elif kind == MOVE:
                if not self.in_contact:  # lost the DOWN packet; start a stroke
                    self.handle(DOWN, x, y, pressure)
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
            self._inject(flags, x, y, pressure)

        def release(self):
            if self.in_contact:
                self._inject(F_UP, 0.5, 0.5, 0.0)
            self.in_range = self.in_contact = False

    class MouseInjector:
        """Fallback: moves the normal mouse cursor (no pressure)."""

        def __init__(self, geom):
            self.left, self.top, self.width, self.height = geom
            self.user32 = ctypes.windll.user32
            self.down = False

        def handle(self, kind, x, y, pressure):
            px = self.left + int(clamp01(x) * (self.width - 1))
            py = self.top + int(clamp01(y) * (self.height - 1))
            self.user32.SetCursorPos(px, py)
            if kind == DOWN and not self.down:
                self.user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
                self.down = True
            elif kind in (UP, LEAVE) and self.down:
                self.user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
                self.down = False

        def release(self):
            if self.down:
                self.user32.mouse_event(0x0004, 0, 0, 0, 0)
                self.down = False


def make_injector(args, geom):
    if args.dry_run:
        return PrintInjector()
    if sys.platform != "win32":
        sys.exit("This server injects input on Windows only. Use --dry-run to test elsewhere.")
    if not args.mouse:
        try:
            inj = PenInjector(geom)
            print("Input mode: Windows Ink pen (pressure supported)")
            return inj
        except OSError as exc:
            print(f"Pen injection unavailable ({exc}); falling back to mouse.")
    print("Input mode: mouse (no pressure)")
    return MouseInjector(geom)


# --------------------------------------------------------------------------
# Frame sources.  grab() returns (change_key, make_image) where make_image()
# builds a PIL RGB image only if the key changed (saves CPU on a still screen).
# --------------------------------------------------------------------------
class ScreenSource:
    def __init__(self, monitor_index):
        import mss
        from PIL import Image
        self._Image = Image
        self.sct = mss.mss()
        self.mon = self.sct.monitors[monitor_index]

    def grab(self):
        shot = self.sct.grab(self.mon)
        raw, size, Image = shot.bgra, shot.size, self._Image
        return hash(raw), (lambda: Image.frombytes("RGB", size, raw, "raw", "BGRX"))

    def close(self):
        self.sct.close()


class PatternSource:
    """Animated test pattern for --dry-run (no screen capture needed)."""

    def __init__(self, size=(1920, 1080)):
        from PIL import Image, ImageDraw
        self._Image, self._Draw = Image, ImageDraw
        self.size = size
        self.n = 0

    def grab(self):
        self.n += 1
        n = self.n

        def make():
            img = self._Image.new("RGB", self.size, (30, 60, 110))
            d = self._Draw.Draw(img)
            x = (n * 12) % self.size[0]
            d.rectangle([x, 0, x + 80, self.size[1]], fill=(240, 200, 40))
            d.text((40, 40), f"test pattern  frame {n}", fill=(255, 255, 255))
            return img

        return n, make

    def close(self):
        pass


def monitor_geometry(args):
    if args.dry_run:
        return (0, 0, 1920, 1080)
    try:
        import mss
    except ImportError:
        sys.exit("Missing packages. Run:  pip install -r requirements.txt")
    with mss.mss() as sct:
        if not 1 <= args.monitor < len(sct.monitors):
            sys.exit(f"--monitor must be 1..{len(sct.monitors) - 1}")
        m = sct.monitors[args.monitor]
        return (m["left"], m["top"], m["width"], m["height"])


def stream_frames(conn, stop, target, size_known, args):
    """Capture -> downscale to the phone's size -> JPEG -> send, until stop is set."""
    from PIL import Image
    try:
        source = PatternSource() if args.dry_run else ScreenSource(args.monitor)
    except Exception as exc:  # e.g. mss missing
        print(f"Screen capture failed: {exc}")
        stop.set()
        return
    size_known.wait(1.5)  # give the phone a moment to report its screen size
    interval = 1.0 / args.fps
    last_key = None
    try:
        while not stop.is_set():
            started = time.perf_counter()
            key, make_image = source.grab()
            size = target["size"]
            if (key, size) != last_key:
                img = make_image()
                tw, th = size
                if tw > 0 and th > 0:
                    scale = min(tw / img.width, th / img.height, 1.0)
                    if scale < 1.0:
                        img = img.resize(
                            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                            Image.BILINEAR,
                        )
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=args.quality)
                data = buf.getvalue()
                conn.sendall(struct.pack(">I", len(data)) + data)
                last_key = (key, size)
            delay = interval - (time.perf_counter() - started)
            if delay > 0:
                time.sleep(delay)
    except OSError:
        pass
    finally:
        source.close()
        stop.set()


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no traffic is sent
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def serve_client(conn, addr, injector, lock, args):
    print(f"Phone connected: {addr[0]}")
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    stop = threading.Event()
    size_known = threading.Event()
    target = {"size": (0, 0)}
    threading.Thread(
        target=stream_frames, args=(conn, stop, target, size_known, args), daemon=True
    ).start()

    buf = b""
    try:
        while not stop.is_set():
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= PACKET.size:
                kind, x, y, p = PACKET.unpack_from(buf)
                buf = buf[PACKET.size:]
                if kind == RESIZE:
                    target["size"] = (int(x), int(y))
                    size_known.set()
                else:
                    with lock:
                        injector.handle(kind, x, y, p)
    except OSError:
        pass
    finally:
        stop.set()
        size_known.set()
        with lock:
            injector.release()
        conn.close()
        print("Phone disconnected")


def main():
    parser = argparse.ArgumentParser(description="Phone graphics tablet server")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--monitor", type=int, default=1, help="monitor to stream (1 = primary)")
    parser.add_argument("--fps", type=int, default=30, help="max frames per second")
    parser.add_argument("--quality", type=int, default=70, help="JPEG quality 1-95")
    parser.add_argument("--mouse", action="store_true", help="force mouse mode (no pen)")
    parser.add_argument("--dry-run", action="store_true", help="test pattern, print packets")
    args = parser.parse_args()

    enable_dpi_awareness()
    geom = monitor_geometry(args)
    injector = make_injector(args, geom)
    if args.dry_run:
        print("Dry-run mode: streaming a test pattern, input is only printed.")
    lock = threading.Lock()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.port))
    server.listen(2)

    print(f"Streaming monitor {args.monitor}: {geom[2]}x{geom[3]}")
    print(f"\nListening on port {args.port}")
    for ip in local_ips():
        print(f"  Wi-Fi address for the phone: {ip}")
    print(f"  USB: run  adb reverse tcp:{args.port} tcp:{args.port}  then use 127.0.0.1")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(
                target=serve_client, args=(conn, addr, injector, lock, args), daemon=True
            ).start()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        injector.release()
        server.close()


if __name__ == "__main__":
    main()
