"""Demo: the AI cursor opens Notepad and writes in it while you keep using
your own mouse. Start ai_cursor.py first, then run:  python demo.py
"""

import ctypes
import math
import subprocess
import sys
import time
from ctypes import wintypes

from client import AICursorClient

if sys.platform != "win32":
    sys.exit("The demo opens Notepad, so it needs Windows.")

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # match ai_cursor.py's pixel coords
except OSError:
    pass

ai = AICursorClient()
status = ai.status()
print("AI cursor is at", status["x"], status["y"], "- move your own mouse around, it's yours.")

# 1. Show off: draw a circle with the AI cursor.
cx, cy = status["x"], status["y"]
for i in range(41):
    a = i / 40 * 2 * math.pi
    ai.move(cx + 150 * math.cos(a), cy + 150 * math.sin(a), duration=0.02)

# 2. Open Notepad and find its window.
subprocess.Popen(["notepad.exe"])
hwnd = None
for _ in range(50):
    hwnd = user32.FindWindowW("Notepad", None)
    if hwnd:
        break
    time.sleep(0.1)
if not hwnd:
    sys.exit("Couldn't find the Notepad window.")
time.sleep(0.8)  # let it finish drawing

rect = wintypes.RECT()
user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))

# 3. Click into the text area (middle of the window) and type.
x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
print("Clicking into Notepad at", x, y)
print(ai.click(x, y)["typing_target"])
ai.type("Hello from the second mouse.\n")
ai.type("I'm typing here while you keep using your own cursor.\n")
ai.key("enter")
ai.type("- Floating Castle / TMO")
print("Done.")
