"""Screen capture sources. mss / Pillow are imported lazily so --dry-run works anywhere."""
import sys

_DRY_SIZES = [(1920, 1080), (1280, 1024)]  # two fake monitors for --dry-run


def _mss_class():
    import mss
    return getattr(mss, "MSS", None) or mss.mss


def list_monitors(dry_run=False):
    """[(left, top, width, height), ...] for monitor 1..n in virtual-desktop pixels."""
    if dry_run:
        return [(0, 0, w, h) for (w, h) in _DRY_SIZES]
    try:
        cls = _mss_class()
    except ImportError:
        sys.exit("Missing packages. Run:  pip install -r requirements.txt")
    with cls() as sct:
        return [(m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]]


class ScreenSource:
    """Grabs one monitor. Create and use it from a single thread."""

    def __init__(self, index):
        from PIL import Image
        self._Image = Image
        self.sct = _mss_class()()
        self.mon = self.sct.monitors[index]

    def grab(self):
        """-> (change_key, make_image). make_image() builds the PIL image lazily."""
        shot = self.sct.grab(self.mon)
        raw, size, Image = shot.bgra, shot.size, self._Image
        return hash(raw), (lambda: Image.frombytes("RGB", size, raw, "raw", "BGRX"))

    def close(self):
        self.sct.close()


class PatternSource:
    """Animated test pattern for --dry-run (no screen capture needed)."""

    def __init__(self, index):
        from PIL import Image, ImageDraw
        self._Image, self._Draw = Image, ImageDraw
        self.index = index
        self.size = _DRY_SIZES[index - 1]
        self.n = 0

    def grab(self):
        self.n += 1
        n, size, idx = self.n, self.size, self.index

        def make():
            img = self._Image.new("RGB", size, (30, 60, 110) if idx == 1 else (90, 40, 110))
            d = self._Draw.Draw(img)
            x = (n * 12) % size[0]
            d.rectangle([x, 0, x + 80, size[1]], fill=(240, 200, 40))
            d.text((40, 40), f"test pattern  monitor {idx}  frame {n}", fill=(255, 255, 255))
            return img

        return n, make

    def close(self):
        pass


def open_source(index, dry_run=False):
    return PatternSource(index) if dry_run else ScreenSource(index)
