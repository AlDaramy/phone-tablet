"""The Phone Tablet server: streams the screen to one phone and injects its input."""
import io
import socket
import threading
import time

import capture
import injectors
from common import (
    ACTIONS, AUTH, DEFAULT_PORT, DISCOVERY_PORT, DISCOVERY_QUERY, DISCOVERY_REPLY,
    DOWN, FLAG_BARREL, FLAG_ERASER, HOVER, KEY, LEAVE, MONITOR, MOVE, MSG_TEXT,
    PACKET, RESIZE, SNAP_WINDOW, TYPE_MASK, UP, VIEW_POS, VIEW_SIZE,
    clamp01, log, pack_frame, pack_message, sanitize_view,
)

INPUT_KINDS = (DOWN, MOVE, UP, HOVER, LEAVE)


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


class Client:
    """One connected phone."""

    def __init__(self, server, conn, addr):
        self.server, self.conn, self.addr = server, conn, addr
        self.stop = threading.Event()
        self.lock = threading.Lock()  # protects the view state below
        self.send_lock = threading.Lock()
        self.size = (0, 0)
        self.size_known = threading.Event()
        self.monitor = server.monitor
        self.view = [0.0, 0.0, 1.0, 1.0]
        self.force_seq = 0
        self.activated = False

    # ---- plumbing -------------------------------------------------------
    def send(self, kind, body):
        with self.send_lock:
            self.conn.sendall(pack_message(kind, body))

    def close(self):
        self.stop.set()
        self.size_known.set()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    # ---- lifecycle ------------------------------------------------------
    def run(self):
        conn = self.conn
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            if not self._authenticate():
                return
            self.server._activate(self)
            self.activated = True
            log(f"Phone connected: {self.addr[0]}")
            threading.Thread(target=self._stream, daemon=True).start()
            self._read_loop()
        except OSError:
            pass
        finally:
            self.close()
            self.server._deactivate(self)
            if self.activated:
                log("Phone disconnected")

    def _authenticate(self):
        self.conn.settimeout(5)
        kind, x, _y, _p = PACKET.unpack(self._recv_exact(PACKET.size))
        if kind & TYPE_MASK != AUTH:
            return False
        need = self.server.pin
        if need and int(round(x)) != int(need):
            log(f"Rejected {self.addr[0]}: wrong PIN")
            self.send(MSG_TEXT, b"AUTH_FAIL")
            time.sleep(1.0)  # slow down guessing
            return False
        self.send(MSG_TEXT, b"AUTH_OK")
        self.conn.settimeout(None)
        return True

    # ---- phone -> server ------------------------------------------------
    def _read_loop(self):
        buf = b""
        while not self.stop.is_set():
            chunk = self.conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= PACKET.size:
                raw, buf = buf[: PACKET.size], buf[PACKET.size:]
                try:
                    self._handle(*PACKET.unpack(raw))
                except OSError:
                    raise
                except Exception as exc:  # never let one bad packet kill the session
                    log(f"Bad packet ignored: {exc}")

    def _handle(self, raw_kind, x, y, p):
        srv = self.server
        kind = raw_kind & TYPE_MASK
        if kind in INPUT_KINDS:
            barrel = bool(raw_kind & FLAG_BARREL)
            eraser = bool(raw_kind & FLAG_ERASER)
            with srv.input_lock:
                if srv.active is self:
                    srv.injector.handle(kind, clamp01(x), clamp01(y), clamp01(p), barrel, eraser)
        elif kind == RESIZE:
            if x == x and y == y:
                self.size = (max(0, int(x)), max(0, int(y)))
            self.size_known.set()
        elif kind == KEY:
            action = int(x) if x == x else 0
            if action in ACTIONS:
                with srv.input_lock:
                    if srv.active is self:
                        srv.injector.key(action)
        elif kind == VIEW_POS:
            with self.lock:
                self.view[0], self.view[1] = x, y
        elif kind == VIEW_SIZE:
            with self.lock:
                self.view[2], self.view[3] = x, y
        elif kind == MONITOR:
            self._set_monitor(int(x) if x == x else 0)
        elif kind == SNAP_WINDOW:
            self._snap_window()

    def _force_view(self, view):
        with self.lock:
            self.view = list(view)
            self.force_seq += 1

    def _set_monitor(self, idx):
        srv = self.server
        if not 1 <= idx <= len(srv.monitors):
            return
        with self.lock:
            if idx == self.monitor:
                return
            self.monitor = idx
            self.view = [0.0, 0.0, 1.0, 1.0]
            self.force_seq += 1
        with srv.input_lock:
            if srv.active is self:
                srv.injector.set_geom(srv.monitors[idx - 1])
        log(f"Streaming monitor {idx}")

    def _snap_window(self):
        srv = self.server
        gl, gt, gw, gh = srv.monitors[self.monitor - 1]
        with srv.input_lock:
            rect = srv.injector.foreground_rect()
        if not rect:
            return
        l, t, r, b = rect
        fl, ft = clamp01((l - gl) / gw), clamp01((t - gt) / gh)
        fr, fb = clamp01((r - gl) / gw), clamp01((b - gt) / gh)
        if fr - fl < 0.05 or fb - ft < 0.05:
            return
        self._force_view((fl, ft, fr - fl, fb - ft))

    # ---- server -> phone ------------------------------------------------
    def _stream(self):
        from PIL import Image

        srv = self.server
        source = None
        source_idx = None
        last_state = None
        last_cursor = None
        last_region = (0.0, 0.0, 1.0, 1.0)
        sent_seq = 0
        self.size_known.wait(1.5)  # let the phone report its screen size first
        try:
            while not self.stop.is_set():
                started = time.perf_counter()
                with self.lock:
                    idx = self.monitor
                    view = sanitize_view(self.view)
                    seq = self.force_seq
                size = self.size
                if source is None or source_idx != idx:
                    if source is not None:
                        source.close()
                    source = capture.open_source(idx, srv.dry_run)
                    source_idx = idx
                    last_state = None
                geom = srv.monitors[idx - 1]
                key, make_image = source.grab()
                with srv.input_lock:
                    cx, cy = srv.injector.cursor()
                cursor = (round(cx, 4), round(cy, 4)) if 0 <= cx <= 1 and 0 <= cy <= 1 else (-1.0, -1.0)
                state = (key, view, size, idx)
                count = len(srv.monitors)
                if state != last_state:
                    img = make_image()
                    full_w, full_h = img.size
                    x0, y0 = int(view[0] * full_w), int(view[1] * full_h)
                    x1 = max(x0 + 1, min(full_w, int(round((view[0] + view[2]) * full_w))))
                    y1 = max(y0 + 1, min(full_h, int(round((view[1] + view[3]) * full_h))))
                    if (x0, y0, x1, y1) != (0, 0, full_w, full_h):
                        img = img.crop((x0, y0, x1, y1))
                    last_region = (x0 / full_w, y0 / full_h, (x1 - x0) / full_w, (y1 - y0) / full_h)
                    tw, th = size
                    if tw > 0 and th > 0:
                        scale = min(tw / img.width, th / img.height, 1.0)
                        if scale < 1.0:
                            img = img.resize(
                                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                                Image.BILINEAR,
                            )
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=srv.quality)
                    with self.send_lock:
                        self.conn.sendall(pack_frame(
                            last_region, cursor, idx, count, geom[2], geom[3],
                            seq != sent_seq, buf.getvalue()))
                    sent_seq = seq
                    last_state, last_cursor = state, cursor
                elif cursor != last_cursor:  # cheap cursor-only update
                    with self.send_lock:
                        self.conn.sendall(pack_frame(
                            last_region, cursor, idx, count, geom[2], geom[3], False))
                    last_cursor = cursor
                delay = 1.0 / max(1, srv.fps) - (time.perf_counter() - started)
                if delay > 0:
                    time.sleep(delay)
        except OSError:
            pass
        except Exception as exc:
            log(f"Streaming stopped: {exc}")
        finally:
            if source is not None:
                source.close()
            self.close()


class TabletServer:
    def __init__(self, port=DEFAULT_PORT, pin="", monitor=1, fps=30, quality=70,
                 mouse=False, dry_run=False, discovery_port=DISCOVERY_PORT):
        self.port = port
        self.pin = pin  # "" = no PIN required
        self.monitor = monitor
        self.fps = fps
        self.quality = quality
        self.mouse = mouse
        self.dry_run = dry_run
        self.discovery_port = discovery_port
        self.monitors = []
        self.injector = None
        self.active = None
        self.running = False
        self.input_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sock = None
        self.dsock = None

    @property
    def active_addr(self):
        client = self.active
        return client.addr[0] if client else None

    def start(self):
        self.monitors = capture.list_monitors(self.dry_run)
        if not 1 <= self.monitor <= len(self.monitors):
            self.monitor = 1
        self.injector = injectors.make_injector(self.mouse, self.dry_run, self.monitors[self.monitor - 1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.listen(2)
        sock.settimeout(0.5)
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self._start_discovery()
        g = self.monitors[self.monitor - 1]
        log(f"Streaming monitor {self.monitor} of {len(self.monitors)}: {g[2]}x{g[3]}")
        log(f"Listening on port {self.port}  (PIN {'required' if self.pin else 'off'})")

    def stop(self):
        self.running = False
        client = self.active
        if client:
            client.close()
        for s in (self.sock, self.dsock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if self.injector is not None:
            with self.input_lock:
                self.injector.release()

    # ---- internals ------------------------------------------------------
    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=Client(self, conn, addr).run, daemon=True).start()

    def _activate(self, client):
        with self.state_lock:
            old, self.active = self.active, client
        if old is not None:
            old.close()  # a new phone (or a reconnect) replaces the old connection
        with self.input_lock:
            self.injector.set_geom(self.monitors[client.monitor - 1])

    def _deactivate(self, client):
        with self.state_lock:
            if self.active is not client:
                return
            self.active = None
        with self.input_lock:
            self.injector.release()

    def _start_discovery(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.discovery_port))
            s.settimeout(0.5)
        except OSError as exc:
            log(f"Auto-discovery unavailable: {exc}")
            return
        self.dsock = s
        self.discovery_port = s.getsockname()[1]
        threading.Thread(target=self._discovery_loop, args=(s,), daemon=True).start()

    def _discovery_loop(self, s):
        name = socket.gethostname().replace("|", " ")
        while self.running:
            try:
                data, addr = s.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(DISCOVERY_QUERY):
                reply = DISCOVERY_REPLY + f"{name}|{self.port}|{1 if self.pin else 0}".encode("utf-8")
                try:
                    s.sendto(reply, addr)
                except OSError:
                    pass
