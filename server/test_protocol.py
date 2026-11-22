#!/usr/bin/env python3
"""End-to-end protocol test. No screen and no Windows needed:

    python server/test_protocol.py
"""
import io
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

import common as C  # noqa: E402
from server_core import TabletServer  # noqa: E402

PIN = "123456"


def close(a, b, tol=0.002):
    return abs(a - b) <= tol


class Phone:
    def __init__(self, port, pin=PIN):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.send(C.AUTH, float(int(pin)))

    def send(self, kind, x=0.0, y=0.0, p=0.0, flags=0):
        self.s.sendall(C.PACKET.pack(kind | flags, x, y, p))

    def _exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.s.recv(n - len(data))
            if not chunk:
                raise EOFError("server closed the connection")
            data += chunk
        return data

    def message(self, timeout=5.0):
        self.s.settimeout(timeout)
        n = struct.unpack(">I", self._exact(4))[0]
        data = self._exact(n)
        return data[0], data[1:]

    def text(self):
        kind, body = self.message()
        assert kind == C.MSG_TEXT, f"expected text, got kind {kind}"
        return body.decode()

    def frame(self, timeout=5.0):
        kind, body = self.message(timeout)
        assert kind == C.MSG_FRAME, f"expected frame, got kind {kind}"
        h = C.FRAME_HEAD.unpack(body[: C.FRAME_HEAD.size])
        jpeg = body[C.FRAME_HEAD.size:]
        return dict(
            region=h[0:4], cursor=h[4:6], monitor=h[6], count=h[7],
            mon_w=h[8], mon_h=h[9], forced=bool(h[10]), jpeg=jpeg,
            size=Image.open(io.BytesIO(jpeg)).size if jpeg else None,
        )

    def frame_until(self, pred, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            f = self.frame(timeout=max(0.1, end - time.time()))
            if pred(f):
                return f
        raise AssertionError("expected frame never arrived")


def main():
    srv = TabletServer(port=0, pin=PIN, fps=30, dry_run=True, discovery_port=0)
    srv.start()
    port = srv.port
    try:
        # 1. wrong PIN is rejected
        bad = Phone(port, pin="000000")
        assert bad.text() == "AUTH_FAIL"
        print("ok  wrong PIN rejected")

        # 2. correct PIN, first frame is the whole monitor, scaled to the phone size
        ph = Phone(port)
        assert ph.text() == "AUTH_OK"
        ph.send(C.RESIZE, 800.0, 400.0)
        f = ph.frame()
        assert f["monitor"] == 1 and f["count"] == 2, f
        assert (f["mon_w"], f["mon_h"]) == (1920, 1080), f
        assert all(close(a, b) for a, b in zip(f["region"], (0, 0, 1, 1))), f["region"]
        assert f["size"] == (711, 400) and not f["forced"], f
        print("ok  PIN accepted, full frame", f["size"])

        # 3. zoom: the server crops the requested area
        ph.send(C.VIEW_POS, 0.25, 0.25)
        ph.send(C.VIEW_SIZE, 0.5, 0.5)
        f = ph.frame_until(lambda f: close(f["region"][2], 0.5))
        assert all(close(a, b) for a, b in zip(f["region"], (0.25, 0.25, 0.5, 0.5))), f["region"]
        print("ok  zoomed region", tuple(round(v, 3) for v in f["region"]))

        # 4. pen input incl. barrel button and eraser flags
        ph.send(C.HOVER, 0.1, 0.1, 0.0)
        ph.send(C.DOWN, 0.30, 0.55, 0.7, C.FLAG_BARREL)
        ph.send(C.MOVE, 0.31, 0.56, 0.8, C.FLAG_BARREL)
        ph.send(C.UP, 0.31, 0.56, 0.0, C.FLAG_BARREL)
        ph.send(C.DOWN, 0.2, 0.2, 0.5, C.FLAG_ERASER)
        ph.send(C.UP, 0.2, 0.2, 0.0, C.FLAG_ERASER)
        time.sleep(0.3)
        ev = srv.injector.events
        assert (C.HOVER, 0.1, 0.1, 0.0, False, False) in ev, ev
        assert (C.DOWN, 0.3, 0.55, 0.7, True, False) in ev, ev
        assert (C.MOVE, 0.31, 0.56, 0.8, True, False) in ev, ev
        assert (C.DOWN, 0.2, 0.2, 0.5, False, True) in ev, ev
        print("ok  pen input, barrel button, eraser")

        # 5. the cursor shows up in the stream
        f = ph.frame_until(lambda f: close(f["cursor"][0], 0.2) and close(f["cursor"][1], 0.2))
        print("ok  cursor in stream", tuple(round(v, 3) for v in f["cursor"]))

        # 6. shortcut buttons
        ph.send(C.KEY, 1.0)
        ph.send(C.KEY, 4.0)
        ph.send(C.KEY, 99.0)  # unknown -> ignored
        time.sleep(0.3)
        assert srv.injector.keys == [1, 4], srv.injector.keys
        print("ok  shortcut keys", srv.injector.keys)

        # 7. switch monitor -> forced full frame of the other screen
        ph.send(C.MONITOR, 2.0)
        f = ph.frame_until(lambda f: f["monitor"] == 2)
        assert f["forced"] and (f["mon_w"], f["mon_h"]) == (1280, 1024), f
        assert all(close(a, b) for a, b in zip(f["region"], (0, 0, 1, 1))), f["region"]
        assert srv.injector.geom == srv.monitors[1]
        print("ok  monitor switch", (f["mon_w"], f["mon_h"]))

        # 8. snap to the foreground window -> forced zoomed frame
        ph.send(C.SNAP_WINDOW)
        f = ph.frame_until(lambda f: f["forced"] and close(f["region"][2], 0.5))
        assert all(close(a, b) for a, b in zip(f["region"], (0.25, 0.25, 0.5, 0.5))), f["region"]
        print("ok  snap to window")

        # 9. phone discovery over UDP
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(3)
        u.sendto(C.DISCOVERY_QUERY, ("127.0.0.1", srv.discovery_port))
        data, _ = u.recvfrom(256)
        assert data.startswith(C.DISCOVERY_REPLY), data
        name, rport, needs_pin = data[len(C.DISCOVERY_REPLY):].decode().split("|")
        assert int(rport) == port and needs_pin == "1", data
        print("ok  discovery reply:", data.decode())

        # 10. a second phone replaces the first (needed for auto-reconnect)
        ph2 = Phone(port)
        assert ph2.text() == "AUTH_OK"
        try:
            ph.s.settimeout(3)
            while ph.s.recv(65536):
                pass
        except (ConnectionError, socket.timeout):
            pass
        ph2.send(C.RESIZE, 800.0, 400.0)
        ph2.frame()
        assert srv.active_addr == "127.0.0.1"
        print("ok  second phone replaces the first")
    finally:
        srv.stop()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
