# AI Cursor: a second mouse for the AI

A purple **AI** cursor that clicks and types in Windows apps on its own,
while you keep using your real mouse and keyboard.

## Why it works this way

Windows only has **one** real cursor. Plug in two mice and they both move the
same arrow, so "use the second mouse" can't be done at the hardware level.
AI Cursor takes a different route:

1. It **draws its own cursor**: a click-through, always-on-top overlay. It
   never grabs focus and your clicks pass straight through it.
2. When the AI clicks or types, it **sends the mouse and keyboard messages
   directly to the window under the AI cursor** (`PostMessage`). It never
   moves your real cursor and never uses `SendInput`, which would hijack you.

So you can be in Ableton on one screen while the AI fills in a form in a
browser on the other.

## Setup (Windows 10/11)

1. Install Python 3.10 or newer from python.org. During install, tick
   **"Add python.exe to PATH"**.
2. Double-click **`start.bat`**, or run `python ai_cursor.py` in this folder.
   A purple arrow labelled **AI** appears in the middle of your main screen.
3. Leave that window open. Close it (or press Ctrl+C) to quit.

You don't need to `pip install` anything for the cursor itself; it only uses
the Python standard library.

### Try it

With the cursor running, open a second terminal and run:

```
python demo.py
```

The AI cursor draws a circle, opens Notepad, clicks into it and types a few
lines. Keep moving your own mouse while it runs.

## Letting Claude Desktop use it (MCP)

1. `pip install mcp`
2. In Claude Desktop, go to **Settings → Developer → Edit Config**. That
   opens `%APPDATA%\Claude\claude_desktop_config.json`. Add the following,
   using the real path to this folder:

   ```json
   {
     "mcpServers": {
       "ai-cursor": {
         "command": "python",
         "args": ["C:\\path\\to\\tools\\ai-cursor\\mcp_server.py"]
       }
     }
   }
   ```
3. Restart Claude Desktop. Make sure `ai_cursor.py` is running. Then ask
   something like: *"Take a screenshot, then use the AI cursor to open the
   Downloads folder in Explorer."*

Claude gets these tools: `screenshot`, `move`, `click`, `double_click`,
`drag`, `scroll`, `type_text` and `press_key`. It works in screenshot
coordinates, and the MCP server converts them to real screen pixels.

## Driving it from your own scripts

Use the HTTP API on `127.0.0.1:8765`, or the Python client:

```python
from client import AICursorClient
ai = AICursorClient()
ai.click(800, 400)          # screen pixels
ai.type("hello\n")          # goes to whatever the AI last clicked
ai.key("tab")
ai.scroll(-3)               # 3 notches down, at the AI cursor
ai.drag(100, 100, 400, 300)
png, scale, origin = ai.screenshot(max_width=1366)
```

| Method | Path | Body (JSON) |
|---|---|---|
| GET | `/status` | none. Returns the position, screen bounds and typing target |
| GET | `/screenshot?max_width=1366` | none. Returns a PNG; the `X-Scale` and `X-Origin-X/Y` headers map pixels back to the screen |
| POST | `/move` | `{"x", "y", "duration"?}` |
| POST | `/click` | `{"x"?, "y"?, "button"?: "left"\|"right"\|"middle", "double"?}` |
| POST | `/drag` | `{"x1", "y1", "x2", "y2", "duration"?}` |
| POST | `/scroll` | `{"amount", "x"?, "y"?}` (positive scrolls up) |
| POST | `/type` | `{"text"}` |
| POST | `/key` | `{"key", "times"?}` where `key` is one of enter, tab, escape, backspace, delete, space, the arrows, home, end, pageup, pagedown, f1–f12 |
| POST | `/show`, `/hide` | `{}` |

POST requests must send `Content-Type: application/json`. The server only
listens on 127.0.0.1, and it rejects any request that comes from a web page,
so a website you visit can't take control of your desktop.

Options: `--color "#22c55e"` changes the cursor colour, `--port 9000` changes
the port, and `--dry-run` fakes everything so you can test your agent code on
any OS.

## What works and what doesn't

Sending messages straight to a window is what makes two cursors possible, but
not every app listens to them.

| App type | How well it works |
|---|---|
| Classic Windows apps (Explorer, Notepad, Settings dialogs, most installers, older tools) | Works well |
| Browsers and Electron apps (Chrome, Edge, Discord, VS Code) | Mixed. Clicks and typing often work. Hover menus and drag-and-drop often don't |
| Games and DirectX/raw-input apps | Don't work, because they read the hardware directly |
| Apps running as Administrator | Blocked by Windows unless AI Cursor also runs as admin |
| Keyboard shortcuts (Ctrl+C, Alt+Tab and so on) | Not supported. Apps check the real keyboard state for modifier keys |

The AI types into **whatever it last clicked**, not whatever *you* last
clicked. That's what keeps your typing and its typing separate.

### If you need it to work in everything

Give the AI its own machine: a VM (Hyper-V, VirtualBox) or **Windows
Sandbox**. Inside it, the AI gets a real mouse and keyboard that are fully
separate from yours, and every app works. It costs more RAM, but it's the
only fully reliable option.
