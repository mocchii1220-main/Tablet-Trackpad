
import ctypes
import json
import secrets
import socket
from ctypes import wintypes
from pathlib import Path

from aiohttp import WSMsgType, web

PORT = 8765
BASE = Path(__file__).parent
KEY_FILE = BASE / "key.txt"

# ---------- 認証キー ----------
if KEY_FILE.exists():
    KEY = KEY_FILE.read_text().strip()
else:
    KEY = secrets.token_urlsafe(6)
    KEY_FILE.write_text(KEY)

# ---------- Windows SendInput ----------
MOVE, LDOWN, LUP, RDOWN, RUP = 0x0001, 0x0002, 0x0004, 0x0008, 0x0010
WHEEL, HWHEEL = 0x0800, 0x1000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32 = ctypes.windll.user32


def send(flags, dx=0, dy=0, data=0):
    inp = INPUT()
    inp.type = 0  # INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# ---------- モニター情報 ----------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, MonitorEnumProc, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL


def get_monitors():
    mons = []

    def cb(hmon, hdc, rect, lparam):
        r = rect.contents
        mons.append({"l": r.left, "t": r.top, "r": r.right, "b": r.bottom})
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    mons.sort(key=lambda m: (m["l"], m["t"]))  # 左から右の順に並べる
    return mons


def monitors_payload():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    mons = get_monitors()
    cur = next((i for i, m in enumerate(mons)
                if m["l"] <= pt.x < m["r"] and m["t"] <= pt.y < m["b"]), 0)
    return {"t": "monitors", "list": mons, "cur": cur}


def teleport(i):
    # 指定モニターの中央へカーソルを移動
    mons = get_monitors()
    if 0 <= i < len(mons):
        m = mons[i]
        user32.SetCursorPos((m["l"] + m["r"]) // 2, (m["t"] + m["b"]) // 2)


BUTTONS = {"left": (LDOWN, LUP), "right": (RDOWN, RUP)}


def handle(m, held):
    t = m.get("t")
    if t == "move":
        send(MOVE, int(m["dx"]), int(m["dy"]))
    elif t == "scroll":
        send(WHEEL, data=int(m["dy"]))
    elif t == "hscroll":
        send(HWHEEL, data=int(m["dx"]))
    elif t in ("down", "up", "click"):
        down, up = BUTTONS[m.get("b", "left")]
        if t in ("down", "click"):
            send(down)
            held.add(m.get("b", "left"))
        if t in ("up", "click"):
            send(up)
            held.discard(m.get("b", "left"))


# ---------- Web ----------
async def index(request):
    if request.query.get("key") != KEY:
        return web.Response(status=403, text="Forbidden")
    return web.FileResponse(BASE / "index.html")


async def ws_handler(request):
    if request.query.get("key") != KEY:
        return web.Response(status=403, text="Forbidden")
    ws = web.WebSocketResponse(compress=False)  # 小さなメッセージの圧縮は遅延の元
    await ws.prepare(request)
    try: 
        sock = request.transport.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    held = set()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                    t = m.get("t")
                    if t == "ping":  # 往復遅延の測定用
                        await ws.send_str(msg.data)
                    elif t == "monitors":  # モニター配置の問い合わせ
                        await ws.send_json(monitors_payload())
                    elif t == "teleport":  # 指定モニターへカーソル移動
                        teleport(int(m["i"]))
                    else:
                        handle(m, held)
                except Exception as e:  # 不正なメッセージは無視
                    print("bad message:", e)
    finally:
        # 切断時にボタンが押しっぱなしにならないよう解放
        for b in held:
            send(BUTTONS[b][1])
    return ws


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # 実際には送信されません
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler)])
    print("\nタブレットで次のURLを開いてください:\n")
    print(f"  http://{local_ip()}:{PORT}/?key={KEY}\n")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)