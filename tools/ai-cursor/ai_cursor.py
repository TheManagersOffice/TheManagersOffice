"""
AI Cursor - a second, independent "ghost" mouse for Windows.

Windows only has ONE real cursor. Plug in two mice and they both push the
same arrow around. So this doesn't fight over the real cursor at all:

  * It draws its own cursor (a purple arrow tagged "AI") in a click-through,
    always-on-top overlay window.
  * When the AI "clicks" or "types", it sends the mouse/keyboard messages
    straight to the window under the AI cursor (PostMessage), instead of
    moving the real mouse or using SendInput.

Result: you keep full control of your real mouse and keyboard while the AI
cursor does its own thing somewhere else on screen.

Anything (an AI agent, a script, the MCP server in this folder) drives it via
a tiny local HTTP API on 127.0.0.1 - see README.md.

Run:   python ai_cursor.py
Test:  python ai_cursor.py --dry-run     (no Windows needed, just logs)
"""

import argparse
import ctypes
import json
import queue
import signal
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IS_WINDOWS = sys.platform == "win32"
DEFAULT_PORT = 8765
CURSOR_COLOR = "#8b5cf6"  # purple, so it's obviously not your cursor


# --------------------------------------------------------------------------
# PNG encoding (pure Python, so screenshots need no Pillow)
# --------------------------------------------------------------------------

def encode_png(bgra, width, height):
    """Encode raw top-down BGRA pixels (what Windows GDI gives us) as PNG."""
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[r * stride:(r + 1) * stride] for r in range(height))

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    HWND, HDC, UINT, INT = wintypes.HWND, wintypes.HDC, wintypes.UINT, ctypes.c_int

    def _sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)

    _sig(user32.WindowFromPoint, HWND, wintypes.POINT)
    _sig(user32.ScreenToClient, wintypes.BOOL, HWND, ctypes.POINTER(wintypes.POINT))
    _sig(user32.PostMessageW, wintypes.BOOL, HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.GetAncestor, HWND, HWND, UINT)
    _sig(user32.SetWindowPos, wintypes.BOOL, HWND, HWND, INT, INT, INT, INT, UINT)
    _sig(user32.ShowWindow, wintypes.BOOL, HWND, INT)
    _sig(user32.GetWindowLongW, wintypes.LONG, HWND, INT)
    _sig(user32.SetWindowLongW, wintypes.LONG, HWND, INT, wintypes.LONG)
    _sig(user32.MapVirtualKeyW, UINT, UINT, UINT)
    _sig(user32.GetClassNameW, INT, HWND, wintypes.LPWSTR, INT)
    _sig(user32.GetWindowTextW, INT, HWND, wintypes.LPWSTR, INT)
    _sig(user32.GetSystemMetrics, INT, INT)
    _sig(user32.GetDC, HDC, HWND)
    _sig(user32.ReleaseDC, INT, HWND, HDC)
    _sig(gdi32.CreateCompatibleDC, HDC, HDC)
    _sig(gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, HDC, INT, INT)
    _sig(gdi32.SelectObject, wintypes.HGDIOBJ, HDC, wintypes.HGDIOBJ)
    _sig(gdi32.SetStretchBltMode, INT, HDC, INT)
    _sig(gdi32.SetBrushOrgEx, wintypes.BOOL, HDC, INT, INT, ctypes.c_void_p)
    _sig(gdi32.StretchBlt, wintypes.BOOL, HDC, INT, INT, INT, INT,
         HDC, INT, INT, INT, INT, wintypes.DWORD)
    _sig(gdi32.GetDIBits, INT, HDC, wintypes.HBITMAP, UINT, UINT,
         ctypes.c_void_p, ctypes.c_void_p, UINT)
    _sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
    _sig(gdi32.DeleteDC, wintypes.BOOL, HDC)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

# Window messages
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010

BUTTONS = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, MK_MBUTTON),
}

# Virtual key codes; the bool marks "extended" keys (needed in lParam)
KEYS = {
    "enter": (0x0D, False), "return": (0x0D, False), "tab": (0x09, False),
    "escape": (0x1B, False), "esc": (0x1B, False), "backspace": (0x08, False),
    "space": (0x20, False), "delete": (0x2E, True), "insert": (0x2D, True),
    "home": (0x24, True), "end": (0x23, True), "pageup": (0x21, True),
    "pagedown": (0x22, True), "left": (0x25, True), "up": (0x26, True),
    "right": (0x27, True), "down": (0x28, True),
}
KEYS.update({f"f{i}": (0x6F + i, False) for i in range(1, 13)})

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
SWP_NOSIZE, SWP_NOACTIVATE = 0x0001, 0x0010
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SRCCOPY, CAPTUREBLT, HALFTONE = 0x00CC0020, 0x40000000, 4


def make_lparam(x, y):
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def enable_dpi_awareness():
    """So our coordinates are real pixels, not Windows' scaled guesses."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # per-monitor v2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


# --------------------------------------------------------------------------
# Overlay - the visible purple AI cursor
# --------------------------------------------------------------------------

class Overlay:
    """A tiny transparent, click-through, topmost window holding the arrow.

    Tk owns the window (it must live on the main thread), but moving/hiding is
    done with raw Win32 calls so the HTTP threads can do it directly.
    """

    W, H = 76, 60
    TIP = 12  # where the arrow tip sits inside the window
    KEY = "#ff00fe"  # colour that becomes see-through

    def __init__(self, color=CURSOR_COLOR):
        import tkinter as tk

        self._effects = queue.Queue()
        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=self.KEY)
        root.attributes("-transparentcolor", self.KEY)

        c = self.canvas = tk.Canvas(root, width=self.W, height=self.H, bg=self.KEY,
                                    highlightthickness=0)
        c.pack()
        t = self.TIP
        arrow = [(0, 0), (0, 24), (6, 18), (11, 28), (15, 26), (10, 16), (18, 16)]
        c.create_polygon([(x + t, y + t) for x, y in arrow], fill=color,
                         outline="white", width=2)
        c.create_rectangle(t + 14, t + 26, t + 40, t + 42, fill=color, outline="white")
        c.create_text(t + 27, t + 34, text="AI", fill="white", font=("Segoe UI", 9, "bold"))
        self.ring = c.create_oval(t - 10, t - 10, t + 10, t + 10, outline=color,
                                  width=3, state="hidden")

        root.update()
        self.hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT)
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
                              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        root.after(15, self._pump)

    def place(self, x, y):
        user32.SetWindowPos(self.hwnd, ctypes.c_void_p(-1),  # HWND_TOPMOST
                            x - self.TIP, y - self.TIP, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)

    def set_visible(self, visible):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)

    def flash(self):
        self._effects.put("flash")  # Tk calls must happen on the Tk thread

    def _pump(self):
        try:
            while True:
                if self._effects.get_nowait() == "flash":
                    self.canvas.itemconfigure(self.ring, state="normal")
                    self.root.after(160, lambda: self.canvas.itemconfigure(self.ring, state="hidden"))
        except queue.Empty:
            pass
        self.root.after(15, self._pump)

    def run(self):
        self.root.mainloop()

    def stop(self):
        self.root.after(0, self.root.destroy)


# --------------------------------------------------------------------------
# Backends - Win32 does the real work, DryRun just logs (for testing anywhere)
# --------------------------------------------------------------------------

class Win32Backend:
    def __init__(self, overlay):
        self.overlay = overlay

    def screen_bounds(self):
        return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),  # left, top
                user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))  # width, height

    def primary_center(self):
        return user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2

    def place_overlay(self, x, y):
        self.overlay.place(x, y)

    def set_visible(self, visible):
        self.overlay.set_visible(visible)

    def flash(self):
        self.overlay.flash()

    def window_at(self, x, y):
        """Deepest window under a screen point + the point in its client coords."""
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if hwnd and user32.GetAncestor(hwnd, GA_ROOT) == self.overlay.hwnd:
            # Should never happen (overlay is click-through), but just in case.
            self.overlay.set_visible(False)
            hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
            self.overlay.set_visible(True)
        if not hwnd:
            raise RuntimeError(f"no window at ({x}, {y})")
        pt = wintypes.POINT(x, y)
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        return hwnd, pt.x, pt.y

    def describe(self, hwnd):
        if not hwnd:
            return None
        cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        user32.GetWindowTextW(user32.GetAncestor(hwnd, GA_ROOT), title, 256)
        return {"hwnd": hwnd, "class": cls.value, "window_title": title.value}

    def _post(self, hwnd, msg, wparam, lparam):
        if not user32.PostMessageW(hwnd, msg, wparam, lparam):
            raise RuntimeError(f"PostMessage failed (error {ctypes.get_last_error()}) "
                               "- the window may be gone or running as admin")

    def hover(self, x, y):
        hwnd, cx, cy = self.window_at(x, y)
        self._post(hwnd, WM_MOUSEMOVE, 0, make_lparam(cx, cy))

    def click(self, x, y, button, double):
        down, up, dbl, mk = BUTTONS[button]
        hwnd, cx, cy = self.window_at(x, y)
        lp = make_lparam(cx, cy)
        self._post(hwnd, WM_MOUSEMOVE, 0, lp)
        self._post(hwnd, down, mk, lp)
        self._post(hwnd, up, 0, lp)
        if double:
            self._post(hwnd, dbl, mk, lp)
            self._post(hwnd, up, 0, lp)
        return hwnd

    def drag(self, path):
        """Press at path[0], move through the points, release at path[-1].
        Messages all go to the window under the start point (like real mouse capture)."""
        hwnd, _, _ = self.window_at(*path[0])
        for i, (x, y) in enumerate(path):
            pt = wintypes.POINT(x, y)
            user32.ScreenToClient(hwnd, ctypes.byref(pt))
            lp = make_lparam(pt.x, pt.y)
            if i == 0:
                self._post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
            self._post(hwnd, WM_MOUSEMOVE, MK_LBUTTON, lp)
            if i == len(path) - 1:
                self._post(hwnd, WM_LBUTTONUP, 0, lp)
        return hwnd

    def scroll(self, x, y, notches):
        hwnd, _, _ = self.window_at(x, y)
        delta = int(notches * 120) & 0xFFFF
        self._post(hwnd, WM_MOUSEWHEEL, delta << 16, make_lparam(x, y))  # wheel uses screen coords
        return hwnd

    def type_text(self, hwnd, text):
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                self.press_key(hwnd, "enter")
                continue
            data = ch.encode("utf-16-le")  # emoji etc. are two UTF-16 units
            for i in range(0, len(data), 2):
                self._post(hwnd, WM_CHAR, int.from_bytes(data[i:i + 2], "little"), 1)

    def press_key(self, hwnd, name):
        vk, extended = KEYS[name]
        scan = user32.MapVirtualKeyW(vk, 0)
        lp = 1 | (scan << 16) | (1 << 24 if extended else 0)
        # The app's own message loop turns this into WM_CHAR where needed.
        self._post(hwnd, WM_KEYDOWN, vk, lp)
        self._post(hwnd, WM_KEYUP, vk, lp | 0xC0000000)

    def screenshot(self, max_width=None):
        left, top, width, height = self.screen_bounds()
        scale = min(1.0, max_width / width) if max_width else 1.0
        ow, oh = max(1, round(width * scale)), max(1, round(height * scale))
        sdc = user32.GetDC(None)
        mdc = gdi32.CreateCompatibleDC(sdc)
        bmp = gdi32.CreateCompatibleBitmap(sdc, ow, oh)
        try:
            old = gdi32.SelectObject(mdc, bmp)
            gdi32.SetStretchBltMode(mdc, HALFTONE)
            gdi32.SetBrushOrgEx(mdc, 0, 0, None)
            gdi32.StretchBlt(mdc, 0, 0, ow, oh, sdc, left, top, width, height,
                             SRCCOPY | CAPTUREBLT)
            gdi32.SelectObject(mdc, old)  # GetDIBits wants the bitmap deselected
            bmi = BITMAPINFO()
            h = bmi.bmiHeader
            h.biSize, h.biWidth, h.biHeight = ctypes.sizeof(BITMAPINFOHEADER), ow, -oh  # top-down
            h.biPlanes, h.biBitCount, h.biCompression = 1, 32, 0
            buf = ctypes.create_string_buffer(ow * oh * 4)
            gdi32.GetDIBits(mdc, bmp, 0, oh, buf, ctypes.byref(bmi), 0)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(None, sdc)
        return encode_png(buf.raw, ow, oh), scale, (left, top)


class DryRunBackend:
    """Pretends to be Windows. Lets you test the API/agent wiring on any OS."""

    def __init__(self, log=print):
        self.log = log
        self._next = 1000

    def screen_bounds(self):
        return 0, 0, 1920, 1080

    def primary_center(self):
        return 960, 540

    def place_overlay(self, x, y):
        pass

    def set_visible(self, visible):
        self.log(f"[dry-run] overlay {'shown' if visible else 'hidden'}")

    def flash(self):
        pass

    def describe(self, hwnd):
        return hwnd and {"hwnd": hwnd, "class": "DryRunWindow", "window_title": "dry run"}

    def hover(self, x, y):
        self.log(f"[dry-run] hover ({x}, {y})")

    def click(self, x, y, button, double):
        self._next += 1
        self.log(f"[dry-run] {'double-' if double else ''}{button} click at ({x}, {y})")
        return self._next

    def drag(self, path):
        self.log(f"[dry-run] drag {path[0]} -> {path[-1]}")
        return 1

    def scroll(self, x, y, notches):
        self.log(f"[dry-run] scroll {notches} at ({x}, {y})")
        return 1

    def type_text(self, hwnd, text):
        self.log(f"[dry-run] type {text!r} into window {hwnd}")

    def press_key(self, hwnd, name):
        self.log(f"[dry-run] key {name} into window {hwnd}")

    def screenshot(self, max_width=None):
        w, h = 1920, 1080
        scale = min(1.0, max_width / w) if max_width else 1.0
        ow, oh = round(w * scale), round(h * scale)
        row = bytes(b for x in range(ow) for b in (x * 255 // ow, 60, 140, 255))
        return encode_png(row * oh, ow, oh), scale, (0, 0)


# --------------------------------------------------------------------------
# The AI cursor itself
# --------------------------------------------------------------------------

class AICursor:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()  # one action at a time
        self.x, self.y = backend.primary_center()
        self.target = None  # window the AI last clicked; typing goes here
        self.visible = True
        backend.place_overlay(self.x, self.y)

    def status(self):
        left, top, width, height = self.backend.screen_bounds()
        return {"x": self.x, "y": self.y, "visible": self.visible,
                "screen": {"left": left, "top": top, "width": width, "height": height},
                "typing_target": self.backend.describe(self.target)}

    def move(self, x, y, duration=0.25):
        with self.lock:
            x, y = int(x), int(y)
            duration = max(0.0, min(float(duration), 3.0))
            steps = max(1, int(duration * 60))
            sx, sy = self.x, self.y
            for i in range(1, steps + 1):
                t = i / steps
                t = t * t * (3 - 2 * t)  # ease in/out so it looks like a hand
                self.backend.place_overlay(round(sx + (x - sx) * t), round(sy + (y - sy) * t))
                if i < steps:
                    time.sleep(duration / steps)
            self.x, self.y = x, y
            self.backend.hover(x, y)

    def _maybe_move(self, x, y):
        if x is not None and y is not None:
            self.move(x, y)

    def click(self, x=None, y=None, button="left", double=False):
        if button not in BUTTONS:
            raise ValueError(f"button must be one of {list(BUTTONS)}")
        with self.lock:
            self._maybe_move(x, y)
            self.target = self.backend.click(self.x, self.y, button, bool(double))
            self.backend.flash()
            return self.backend.describe(self.target)

    def drag(self, x1, y1, x2, y2, duration=0.5):
        with self.lock:
            self.move(x1, y1)
            steps = max(2, int(float(duration) * 30))
            path = [(round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
                    for i in range(steps + 1)]
            for px, py in path:
                self.backend.place_overlay(px, py)
                time.sleep(float(duration) / steps)
            self.target = self.backend.drag(path)
            self.x, self.y = int(x2), int(y2)

    def scroll(self, amount, x=None, y=None):
        with self.lock:
            self._maybe_move(x, y)
            self.backend.scroll(self.x, self.y, float(amount))

    def _need_target(self):
        if not self.target:
            raise RuntimeError("click on something with the AI cursor first - typing goes "
                               "to whatever the AI cursor last clicked")

    def type(self, text):
        with self.lock:
            self._need_target()
            self.backend.type_text(self.target, str(text))

    def key(self, name, times=1):
        name = str(name).lower()
        if name not in KEYS:
            raise ValueError(f"unknown key {name!r}; try one of {sorted(KEYS)}")
        with self.lock:
            self._need_target()
            for _ in range(max(1, int(times))):
                self.backend.press_key(self.target, name)

    def show(self, visible=True):
        with self.lock:
            self.visible = bool(visible)
            self.backend.set_visible(self.visible)


# --------------------------------------------------------------------------
# Local HTTP API
# --------------------------------------------------------------------------

def make_handler(cursor, port, verbose=False):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    routes = {
        "/move": lambda b: cursor.move(b["x"], b["y"], b.get("duration", 0.25)),
        "/click": lambda b: cursor.click(b.get("x"), b.get("y"), b.get("button", "left"),
                                         b.get("double", False)),
        "/drag": lambda b: cursor.drag(b["x1"], b["y1"], b["x2"], b["y2"],
                                       b.get("duration", 0.5)),
        "/scroll": lambda b: cursor.scroll(b["amount"], b.get("x"), b.get("y")),
        "/type": lambda b: cursor.type(b["text"]),
        "/key": lambda b: cursor.key(b["key"], b.get("times", 1)),
        "/show": lambda b: cursor.show(True),
        "/hide": lambda b: cursor.show(False),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        def _reply(self, code, payload=None, body=None, ctype="application/json", headers=()):
            body = body if body is not None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _safe(self):
            # Block web pages from driving your desktop: browsers always send
            # Origin on cross-site POSTs, and DNS-rebinding attacks show up as
            # a foreign Host header.
            if "Origin" in self.headers or self.headers.get("Host") not in allowed_hosts:
                self._reply(403, {"ok": False, "error": "forbidden"})
                return False
            return True

        def do_GET(self):
            if not self._safe():
                return
            url = urlparse(self.path)
            if url.path == "/status":
                self._reply(200, {"ok": True, **cursor.status()})
            elif url.path == "/screenshot":
                q = parse_qs(url.query)
                max_width = int(q["max_width"][0]) if "max_width" in q else None
                png, scale, (ox, oy) = cursor.backend.screenshot(max_width)
                # image pixel (px, py) == screen (ox + px / scale, oy + py / scale)
                self._reply(200, body=png, ctype="image/png",
                            headers=[("X-Scale", str(scale)), ("X-Origin-X", str(ox)),
                                     ("X-Origin-Y", str(oy))])
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._safe():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._reply(415, {"ok": False, "error": "send Content-Type: application/json"})
                return
            route = routes.get(urlparse(self.path).path)
            if not route:
                self._reply(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                result = route(body)
                self._reply(200, {"ok": True, "result": result, **cursor.status()})
            except KeyError as e:
                self._reply(400, {"ok": False, "error": f"missing field {e}"})
            except Exception as e:  # report anything else back to the caller
                self._reply(400, {"ok": False, "error": str(e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="A second, independent AI mouse cursor.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--color", default=CURSOR_COLOR, help="AI cursor colour, e.g. #22c55e")
    ap.add_argument("--dry-run", action="store_true", help="fake backend, works on any OS")
    ap.add_argument("--verbose", action="store_true", help="log every HTTP request")
    args = ap.parse_args()

    if not args.dry_run and not IS_WINDOWS:
        sys.exit("AI Cursor drives Windows apps. Use --dry-run to test it on this OS.")

    overlay = None
    if args.dry_run:
        backend = DryRunBackend()
    else:
        enable_dpi_awareness()
        overlay = Overlay(args.color)
        backend = Win32Backend(overlay)

    cursor = AICursor(backend)
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(cursor, args.port, args.verbose))
    print(f"AI Cursor ready on http://127.0.0.1:{args.port}  (Ctrl+C to quit)")

    if overlay is None:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Tk swallows Ctrl+C, so catch it ourselves and keep Python ticking so
    # the signal handler actually gets a chance to run.
    signal.signal(signal.SIGINT, lambda *_: overlay.stop())

    def tick():
        overlay.root.after(200, tick)
    tick()
    overlay.run()
    server.shutdown()


if __name__ == "__main__":
    main()
