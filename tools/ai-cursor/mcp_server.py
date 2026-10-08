"""MCP server so Claude Desktop (or any MCP client) can use the AI Cursor.

The AI gets a screenshot tool plus mouse/keyboard tools that act through the
second, purple cursor - never your real one.

Coordinates: the AI works in *screenshot* pixels; this server converts them to
real screen pixels using the scale of the last screenshot, so the AI never
has to do that maths.

Needs:  pip install mcp      (and ai_cursor.py running)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import AICursorClient, AICursorError  # noqa: E402

try:  # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer, Image
    from mcp.server.fastmcp.exceptions import ToolError

MAX_WIDTH = int(os.environ.get("AI_CURSOR_MAX_WIDTH", "1366"))

ai = AICursorClient(os.environ.get("AI_CURSOR_URL", "http://127.0.0.1:8765"))
server = MCPServer(
    "ai-cursor",
    instructions=(
        "You control a SECOND mouse cursor (purple, labelled AI). The user keeps "
        "using their own mouse at the same time, so never assume where their "
        "cursor is and don't fight them for focus. Always take a screenshot "
        "first, then use coordinates from the latest screenshot. Typing goes to "
        "whatever you last clicked, so click a text field before typing."),
)

_view = {"scale": 1.0, "origin": (0, 0)}  # updated by every screenshot


def _to_screen(x, y):
    s, (ox, oy) = _view["scale"], _view["origin"]
    return round(ox + x / s), round(oy + y / s)


def _do(action, *args, **kwargs):
    """Run a client call; turn failures into a clean message for the AI."""
    try:
        r = action(*args, **kwargs)
    except AICursorError as e:
        raise ToolError(str(e)) from None
    t = r.get("typing_target") or {}
    where = f" Typing target: {t.get('class')} in '{t.get('window_title')}'." if t else ""
    return f"OK.{where}"


@server.tool()
def screenshot() -> Image:
    """Capture the whole screen (all monitors). The purple 'AI' arrow is your cursor.
    Use pixel coordinates from this image for every other tool."""
    try:
        png, scale, origin = ai.screenshot(MAX_WIDTH)
    except AICursorError as e:
        raise ToolError(str(e)) from None
    _view.update(scale=scale, origin=origin)
    return Image(data=png, format="png")


@server.tool()
def move(x: int, y: int) -> str:
    """Glide the AI cursor to (x, y) in screenshot pixels (hover, no click)."""
    return _do(ai.move, *_to_screen(x, y))


@server.tool()
def click(x: int, y: int, button: str = "left") -> str:
    """Click at (x, y) in screenshot pixels. button: left, right or middle."""
    return _do(ai.click, *_to_screen(x, y), button=button)


@server.tool()
def double_click(x: int, y: int) -> str:
    """Double-click at (x, y) in screenshot pixels."""
    return _do(ai.double_click, *_to_screen(x, y))


@server.tool()
def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Press at (x1, y1), drag to (x2, y2), release. Screenshot pixels."""
    return _do(ai.drag, *_to_screen(x1, y1), *_to_screen(x2, y2))


@server.tool()
def scroll(x: int, y: int, amount: int) -> str:
    """Scroll the mouse wheel over (x, y). amount in notches: positive = up, negative = down."""
    return _do(ai.scroll, amount, *_to_screen(x, y))


@server.tool()
def type_text(text: str) -> str:
    """Type text into whatever the AI cursor last clicked. Newlines press Enter."""
    return _do(ai.type, text)


@server.tool()
def press_key(key: str, times: int = 1) -> str:
    """Press a key in whatever the AI cursor last clicked: enter, tab, escape,
    backspace, delete, space, up, down, left, right, home, end, pageup,
    pagedown, f1-f12."""
    return _do(ai.key, key, times)


if __name__ == "__main__":
    server.run()
