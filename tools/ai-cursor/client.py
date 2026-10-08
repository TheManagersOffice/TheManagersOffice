"""Tiny Python client for the AI Cursor HTTP API (standard library only).

    from client import AICursorClient
    ai = AICursorClient()
    ai.click(800, 400)
    ai.type("hello from the second mouse")
"""

import json
import urllib.error
import urllib.request


class AICursorError(RuntimeError):
    pass


class AICursorClient:
    def __init__(self, url="http://127.0.0.1:8765"):
        self.url = url.rstrip("/")

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise AICursorError(json.loads(e.read()).get("error", str(e))) from None
        except urllib.error.URLError as e:
            raise AICursorError(f"AI Cursor isn't running at {self.url} ({e.reason})") from None

    def status(self):
        return self._call("/status")

    def move(self, x, y, duration=0.25):
        return self._call("/move", {"x": x, "y": y, "duration": duration})

    def click(self, x=None, y=None, button="left", double=False):
        return self._call("/click", {"x": x, "y": y, "button": button, "double": double})

    def double_click(self, x=None, y=None):
        return self.click(x, y, double=True)

    def right_click(self, x=None, y=None):
        return self.click(x, y, button="right")

    def drag(self, x1, y1, x2, y2, duration=0.5):
        return self._call("/drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                    "duration": duration})

    def scroll(self, amount, x=None, y=None):
        """amount in wheel notches: positive = up, negative = down."""
        return self._call("/scroll", {"amount": amount, "x": x, "y": y})

    def type(self, text):
        return self._call("/type", {"text": text})

    def key(self, key, times=1):
        return self._call("/key", {"key": key, "times": times})

    def show(self):
        return self._call("/show", {})

    def hide(self):
        return self._call("/hide", {})

    def screenshot(self, max_width=None):
        """Returns (png_bytes, scale, (origin_x, origin_y)).
        Image pixel (px, py) is screen point (origin_x + px / scale, origin_y + py / scale)."""
        path = "/screenshot" + (f"?max_width={int(max_width)}" if max_width else "")
        try:
            with urllib.request.urlopen(self.url + path, timeout=30) as resp:
                h = resp.headers
                return (resp.read(), float(h["X-Scale"]),
                        (int(h["X-Origin-X"]), int(h["X-Origin-Y"])))
        except urllib.error.URLError as e:
            raise AICursorError(f"AI Cursor isn't running at {self.url} ({e})") from None
