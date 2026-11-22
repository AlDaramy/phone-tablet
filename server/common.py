"""Shared constants, wire-format helpers, config and logging for the Phone Tablet server.

Wire format (one TCP connection, default port 5555)

phone -> server, 13 bytes, big-endian ">Bfff": type, x, y, pressure
    type & 0x3F
        0..4  down / move / up / hover / leave
              x, y = position as a fraction (0..1) of the *monitor*, pressure 0..1
        10    resize       x, y = phone view size in px
        11    auth         x = PIN as a number (always the first packet)
        12    key          x = shortcut action id (see ACTIONS)
        13    view pos     x, y = left, top of the visible area (monitor fractions)
        14    view size    x, y = width, height of the visible area (monitor fractions)
        15    monitor      x = monitor number (1 = primary)
        16    snap window  show the foreground window full-size
    type & 0x40   stylus barrel button is pressed
    type & 0x80   eraser end of the stylus

server -> phone: [4-byte length N][1-byte kind][N-1 bytes body]
    kind 1  text   "AUTH_OK" / "AUTH_FAIL"
    kind 0  frame  FRAME_HEAD (31 bytes) + JPEG (JPEG may be empty = cursor-only update)
"""
import json
import os
import secrets
import struct
import sys

PACKET = struct.Struct(">Bfff")  # phone -> server
# region l,t,w,h | cursor x,y | monitor, monitor count | monitor w,h px | forced
FRAME_HEAD = struct.Struct(">ffffffBBHHB")

DOWN, MOVE, UP, HOVER, LEAVE = 0, 1, 2, 3, 4
RESIZE, AUTH, KEY, VIEW_POS, VIEW_SIZE, MONITOR, SNAP_WINDOW = 10, 11, 12, 13, 14, 15, 16
TYPE_MASK, FLAG_BARREL, FLAG_ERASER = 0x3F, 0x40, 0x80

MSG_FRAME, MSG_TEXT = 0, 1

DEFAULT_PORT = 5555
DISCOVERY_PORT = 5556
DISCOVERY_QUERY = b"PHONETABLET?"
DISCOVERY_REPLY = b"PHONETABLET!"

MIN_VIEW = 0.02  # smallest visible fraction of the monitor (50x zoom)

# Shortcut buttons on the phone: id -> (label, keys pressed together)
ACTIONS = {
    1: ("Undo", ("ctrl", "z")),
    2: ("Redo", ("ctrl", "y")),
    3: ("Redo (Ctrl+Shift+Z)", ("ctrl", "shift", "z")),
    4: ("Eraser", ("e",)),
    5: ("Brush", ("b",)),
    6: ("Save", ("ctrl", "s")),
    7: ("Smaller brush", ("[",)),
    8: ("Bigger brush", ("]",)),
}


def clamp01(v):
    """Clamp to 0..1; NaN becomes 0."""
    return 0.0 if not v > 0.0 else 1.0 if v > 1.0 else v


def sanitize_view(view):
    """Return a valid (left, top, width, height) inside the monitor."""
    l, t, w, h = view
    w = 1.0 if w != w else min(max(w, MIN_VIEW), 1.0)
    h = 1.0 if h != h else min(max(h, MIN_VIEW), 1.0)
    l = 0.0 if l != l else min(max(l, 0.0), 1.0 - w)
    t = 0.0 if t != t else min(max(t, 0.0), 1.0 - h)
    return (l, t, w, h)


def pack_message(kind, body=b""):
    return struct.pack(">IB", len(body) + 1, kind) + body


def pack_frame(region, cursor, monitor, count, mon_w, mon_h, forced, jpeg=b""):
    head = FRAME_HEAD.pack(
        region[0], region[1], region[2], region[3],
        cursor[0], cursor[1],
        monitor, count, mon_w, mon_h, 1 if forced else 0,
    )
    return pack_message(MSG_FRAME, head + jpeg)


# --------------------------------------------------------------------------
# Logging (the GUI swaps the sink so it works even without a console)
# --------------------------------------------------------------------------
_sink = None


def set_log_sink(fn):
    global _sink
    _sink = fn


def log(msg):
    if _sink is not None:
        _sink(str(msg))
        return
    out = sys.stdout
    if out is not None:
        try:
            print(msg, flush=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Config (PIN and settings survive restarts)
# --------------------------------------------------------------------------
def config_path():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "PhoneTablet", "config.json")
    return os.path.join(os.path.expanduser("~"), ".phone_tablet.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def new_pin():
    return "%06d" % secrets.randbelow(1_000_000)
