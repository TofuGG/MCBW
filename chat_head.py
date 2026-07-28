"""
Messenger Chat Bubbles for Windows
- Floating bubble with unread badge + message preview popup
- Frameless pywebview window (hidden/shown, never destroyed)
- Scrollbar hidden via injected CSS
- Resizable with persistent memory
- System tray icon
- Notification grouping, hover preview, click-to-open
"""

import threading
import time
import os
import sys
import ctypes
import ctypes.wintypes
import json
import queue
import tkinter as tk
import atexit
import logging
import io
import webview


def _get_asset_path(filename):
    """Works both for .py and PyInstaller .exe"""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)

# ── Constants ──────────────────────────────────────────────────────────────────
BUBBLE_SIZE      = 58
BUBBLE_COLOR     = "#0084FF"
BUBBLE_HOVER     = "#005FBF"
CHAT_W           = 400
CHAT_H           = 780
SNAP_MARGIN      = 8
TASKBAR_H        = 52
POLL_INTERVAL    = 2.0
DRAG_THRESHOLD   = 8
POPUP_DURATION   = 5000
GROUP_WINDOW     = 10.0
HOVER_DELAY      = 1000
TOAST_DURATION   = 3000
MAX_CHAT_W       = 2400
MAX_CHAT_H       = 1600

STORAGE_DIR = os.path.join(os.environ.get("APPDATA", "."), "MCBW")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")

logger = logging.getLogger("MCBW")

# ── Shared state ───────────────────────────────────────────────────────────────
_state = {
    "sw": 0, "sh": 0,
    "bubble_x": 0, "bubble_y": 0,
    "chat_w": CHAT_W, "chat_h": CHAT_H,
    "chat_open":  False,
    "open_req":   False,
    "close_req":  False,
    "webview_ready": False,
    "unread": 0,
    "last_sender": "",
    "last_preview": "",
    "notify_req": False,
    "lift_bubble_req": False,
    "saved_bubble_y": -1,
    "bubble_move_req": -1,
    "group_sender": "",
    "group_time": 0.0,
    "group_count": 0,
    "popup_duration": POPUP_DURATION,
}
_lock = threading.Lock()
_shutdown_event = threading.Event()
_webview_win = None
_wv_queue = queue.Queue()
_toast_queue = queue.Queue()
_tray_hwnd = None
_bubble_root = None

# ── Windows helpers ────────────────────────────────────────────────────────────
GWL_EXSTYLE      = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW  = 0x00040000

def _set_toolwindow(hwnd):
    try:
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


def _create_temp_ico():
    """Convert logo.png to a temp ICO file, return path."""
    try:
        from PIL import Image
        icon_path = _get_asset_path("logo.png")
        if not os.path.exists(icon_path):
            return None
        with Image.open(icon_path) as img:
            img = img.convert("RGBA")
            ico_buf = io.BytesIO()
            img.save(ico_buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
            ico_buf.seek(0)
        ico_path = os.path.join(STORAGE_DIR, "taskbar_icon.ico")
        os.makedirs(STORAGE_DIR, exist_ok=True)
        with open(ico_path, "wb") as f:
            f.write(ico_buf.read())
        return ico_path
    except Exception:
        return None


def _set_taskbar_icon(hwnd):
    try:
        ico_path = _create_temp_ico()
        if not ico_path:
            return
        IMAGE_ICON      = 1
        LR_LOADFROMFILE = 0x00000010
        ICON_SMALL      = 0
        ICON_BIG        = 1
        WM_SETICON      = 0x0080
        hicon = ctypes.windll.user32.LoadImageW(
            None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
        )
        if hicon:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon)
    except Exception:
        pass


def _find_webview_hwnd():
    """Find webview HWND by matching our process ID and window title."""
    pid = os.getpid()
    found = [None]

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM
    )

    def callback(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        win_pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        if "MCBW" in buf.value:
            found[0] = hwnd
            return False
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0]


# ── System tray icon (Win32 via ctypes) ───────────────────────────────────────
def _tray_thread_main(on_quit_cb, on_open_cb):
    """Create system tray icon and run its message loop."""
    global _tray_hwnd

    user32  = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WM_TRAYICON   = 0x0400 + 20
    WM_RBUTTONUP  = 0x0205
    WM_LBUTTONDBLCLK = 0x0203
    NIF_MESSAGE   = 0x01
    NIF_ICON      = 0x02
    NIF_TIP       = 0x04
    NIM_ADD       = 0x00
    NIM_DELETE    = 0x02
    IMAGE_ICON    = 1
    LR_LOADFROMFILE = 0x10
    TPM_RETURNCMD = 0x0100
    TPM_RIGHTBUTTON = 0x02
    SW_HIDE       = 0
    WS_POPUP      = 0x80000000

    class _NID(ctypes.Structure):
        _fields_ = [
            ("cbSize",           ctypes.c_uint),
            ("hWnd",             ctypes.c_void_p),
            ("uID",              ctypes.c_uint),
            ("uFlags",           ctypes.c_uint),
            ("uCallbackMessage", ctypes.c_uint),
            ("hIcon",            ctypes.c_void_p),
            ("szTip",            ctypes.c_wchar * 128),
        ]

    @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.wintypes.HWND,
                         ctypes.wintypes.UINT, ctypes.wintypes.WPARAM,
                         ctypes.wintypes.LPARAM)
    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = lparam & 0xFFFF
            if event == WM_RBUTTONUP:
                pt = ctypes.wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                user32.SetForegroundWindow(hwnd)
                menu = user32.CreatePopupMenu()
                user32.AppendMenuW(menu, 0, 1002, ctypes.c_wchar_p("Open Messenger"))
                user32.AppendMenuW(menu, 0x800, 0, None)
                user32.AppendMenuW(menu, 0, 1001, ctypes.c_wchar_p("Quit"))
                screen_h = user32.GetSystemMetrics(1)
                if pt.y + 80 > screen_h:
                    pt.y -= 80
                cmd = user32.TrackPopupMenu(
                    menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                    pt.x, pt.y, 0, hwnd, None
                )
                user32.PostMessageW(hwnd, 0x0000, 0, 0)
                if cmd == 1001:
                    on_quit_cb()
                elif cmd == 1002:
                    on_open_cb()
                user32.DestroyMenu(menu)
                return 0
            elif event == WM_LBUTTONDBLCLK:
                on_open_cb()
                return 0
        elif msg == 0x0012:
            user32.DestroyWindow(hwnd)
            return 0
        elif msg == 0x0002:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _cb_ref = wnd_proc

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style",         ctypes.c_uint),
            ("lpfnWndProc",   ctypes.c_size_t),
            ("cbClsExtra",    ctypes.c_int),
            ("cbWndExtra",    ctypes.c_int),
            ("hInstance",     ctypes.c_size_t),
            ("hIcon",         ctypes.c_size_t),
            ("hCursor",       ctypes.c_size_t),
            ("hbrBackground", ctypes.c_size_t),
            ("lpszMenuName",  ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
        ]

    try:
        wc = WNDCLASS()
        ctypes.memset(ctypes.addressof(wc), 0, ctypes.sizeof(wc))
        wc.style         = 0
        wc.lpfnWndProc   = ctypes.cast(wnd_proc, ctypes.c_void_p).value
        wc.cbClsExtra    = 0
        wc.cbWndExtra    = 0
        wc.hInstance     = kernel32.GetModuleHandleW(None)
        wc.hIcon         = 0
        wc.hCursor       = 0
        wc.hbrBackground = 0
        wc.lpszMenuName  = None
        wc.lpszClassName = "MCBW_TrayIcon"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return

        hwnd = user32.CreateWindowExW(
            0, atom, "MCBW Tray", WS_POPUP,
            0, 0, 0, 0,
            None, 0, kernel32.GetModuleHandleW(None), None
        )
        if not hwnd:
            return
        user32.ShowWindow(hwnd, SW_HIDE)
        _tray_hwnd = hwnd

        ico_path = _create_temp_ico()
        hicon = None
        if ico_path:
            hicon = user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if not hicon:
            hicon = user32.LoadIconW(None, 32512)

        nid = _NID()
        nid.cbSize           = ctypes.sizeof(_NID)
        nid.hWnd             = hwnd
        nid.uID              = 1
        nid.uFlags           = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon            = hicon
        nid.szTip            = "Messenger Chat Bubbles"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        _tray_hwnd = None
    except Exception as e:
        _tray_hwnd = None


# ── Config helpers ─────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                with _lock:
                    if "width" in data:
                        _state["chat_w"] = max(300, min(MAX_CHAT_W, int(data["width"])))
                    if "height" in data:
                        _state["chat_h"] = max(400, min(MAX_CHAT_H, int(data["height"])))
                    if "bubble_y" in data:
                        _state["saved_bubble_y"] = data["bubble_y"]
                    if "popup_duration" in data:
                        _state["popup_duration"] = max(2000, min(15000, int(data["popup_duration"])))
        except Exception:
            pass


def save_config(w, h):
    if w > 100 and h > 100:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            with _lock:
                by = _state["bubble_y"]
                pd = _state["popup_duration"]
            with open(CONFIG_FILE, "w") as f:
                json.dump({"width": w, "height": h, "bubble_y": by,
                           "popup_duration": pd}, f)
        except Exception:
            pass


# ── CSS/JS injected into Messenger ────────────────────────────────────────────
HIDE_SCROLLBAR_CSS = """
(function() {
    var style = document.createElement('style');
    style.textContent = `
        * { scrollbar-width: none !important; }
        *::-webkit-scrollbar { display: none !important; width: 0 !important; }
    `;
    document.head.appendChild(style);
})();
"""

HIDE_SIDEBAR_CSS = """
(function() {
    var style = document.createElement('style');
    style.textContent = `
        [aria-label="Inbox switcher"] { display: none !important; }
        [aria-label="Thread list"] { margin-left: 0 !important; }
    `;
    document.head.appendChild(style);
})();
"""

FORCE_SELECT_CSS = """
(function() {
    var style = document.createElement('style');
    style.textContent = `
        *, *::before, *::after {
            -webkit-user-select: text !important;
            -moz-user-select: text !important;
            -ms-user-select: text !important;
            user-select: text !important;
        }
    `;
    document.head.appendChild(style);
})();
"""

UNREAD_JS = """
(function() {
    var result = { unread: 0, sender: '', preview: '' };
    try {
        var rows = document.querySelectorAll('[role="row"]');
        var unreadRows = [];
        rows.forEach(function(row) {
            var text = row.textContent || '';
            if (text.indexOf('Unread message:') !== -1) {
                unreadRows.push(row);
            }
        });
        result.unread = unreadRows.length;
        if (unreadRows.length > 0) {
            var first = unreadRows[0];
            var moreBtn = first.querySelector('[aria-label^="More options for"]');
            if (moreBtn) {
                var lbl = moreBtn.getAttribute('aria-label') || '';
                var m = lbl.match(/More options for (.+)/);
                if (m) result.sender = m[1].trim();
            }
            if (!result.sender) {
                var autoSpans = first.querySelectorAll('span[dir="auto"]');
                for (var i = 0; i < autoSpans.length; i++) {
                    var t = autoSpans[i].textContent.trim();
                    if (t && t.indexOf('Unread message:') === -1 &&
                        t.indexOf('\u00b7') === -1 && t.length > 1) {
                        result.sender = t;
                        break;
                    }
                }
            }
            var allSpans = first.querySelectorAll('span');
            for (var i = 0; i < allSpans.length; i++) {
                var st = allSpans[i].textContent || '';
                if (st.indexOf('Unread message:') !== -1) {
                    var msg = st.replace('Unread message:', '').trim();
                    if (msg) {
                        result.preview = msg.substring(0, 80);
                        break;
                    }
                }
            }
        }
    } catch(e) {}
    return JSON.stringify(result);
})();
"""

RESIZE_GRIP_JS = """
(function() {
    if (document.getElementById('__resize_grip_br__')) return;
    var grBR = document.createElement('div');
    grBR.id = '__resize_grip_br__';
    grBR.style.cssText = 'position:fixed;bottom:0;right:0;width:16px;height:16px;cursor:se-resize;z-index:999999;background:transparent;';
    grBR.addEventListener('mousedown', function(e) {
        e.preventDefault();
        var startX = e.screenX, startY = e.screenY;
        var startW = document.documentElement.clientWidth;
        var startH = document.documentElement.clientHeight;
        function onMove(e) {
            var w = Math.max(300, startW + (e.screenX - startX));
            var h = Math.max(400, startH + (e.screenY - startY));
            window.pywebview.api.resize(w, h);
        }
        function onUp() { document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
    document.body.appendChild(grBR);
    var grBL = document.createElement('div');
    grBL.id = '__resize_grip_bl__';
    grBL.style.cssText = 'position:fixed;bottom:0;left:0;width:16px;height:16px;cursor:sw-resize;z-index:999999;background:transparent;';
    grBL.addEventListener('mousedown', function(e) {
        e.preventDefault();
        var startX = e.screenX, startY = e.screenY;
        var startW = document.documentElement.clientWidth;
        var startH = document.documentElement.clientHeight;
        var startWinX = window.screenX;
        function onMove(e) {
            var dx = e.screenX - startX, dy = e.screenY - startY;
            var w = Math.max(300, startW - dx);
            var h = Math.max(400, startH + dy);
            window.pywebview.api.resize_and_move(w, h, startWinX + (startW - w));
        }
        function onUp() { document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
    document.body.appendChild(grBL);
})();
"""


# ── pywebview JS bridge ────────────────────────────────────────────────────────
class Api:
    def __init__(self, win_ref=None):
        self._win = win_ref

    def resize(self, w, h):
        if self._win:
            self._win.resize(int(w), int(h))

    def resize_and_move(self, w, h, x):
        if self._win:
            self._win.resize(int(w), int(h))
            self._win.move(int(x), self._win.y)


# ── Rounded-rect helper for Canvas ─────────────────────────────────────────────
def _draw_rounded_rect(canvas, x1, y1, x2, y2, r, fill, outline=""):
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=outline)
    canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=outline)
    canvas.create_arc(x1,     y1,     x1 + 2*r, y1 + 2*r, start=90,  extent=90,  fill=fill, outline=outline)
    canvas.create_arc(x2-2*r, y1,     x2,       y1 + 2*r, start=0,   extent=90,  fill=fill, outline=outline)
    canvas.create_arc(x1,     y2-2*r, x1 + 2*r, y2,       start=180, extent=90,  fill=fill, outline=outline)
    canvas.create_arc(x2-2*r, y2-2*r, x2,       y2,       start=270, extent=90,  fill=fill, outline=outline)


# ── Bubble thread ──────────────────────────────────────────────────────────────
class BubbleThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.root   = None
        self.canvas = None
        self.popup  = None
        self._popup_canvas = None
        self._popup_sender_id = None
        self._popup_preview_id = None
        self._popup_after_id = None
        self._popup_fade_id = None
        self._hover_timer = None
        self._quit_func = None

    def run(self):
        global _bubble_root
        self.root = root = tk.Tk()
        _bubble_root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-toolwindow", True)
        root.config(bg="#010101")
        root.attributes("-transparentcolor", "#010101")

        try:
            from PIL import Image, ImageTk
            logo_path = _get_asset_path("logo.png")
            if os.path.exists(logo_path):
                with Image.open(logo_path) as img:
                    icon_photo = ImageTk.PhotoImage(img.convert("RGBA"))
                root.iconphoto(True, icon_photo)
                root._icon_photo = icon_photo
        except Exception:
            pass

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        with _lock:
            _state["sw"] = sw
            _state["sh"] = sh

        x = sw - BUBBLE_SIZE - SNAP_MARGIN
        y = sh // 6
        with _lock:
            saved_y = _state.get("saved_bubble_y", -1)
        if saved_y != -1:
            y = max(0, min(saved_y, sh - TASKBAR_H - BUBBLE_SIZE))

        with _lock:
            _state["bubble_x"] = x
            _state["bubble_y"] = y

        root.geometry(f"{BUBBLE_SIZE}x{BUBBLE_SIZE}+{x}+{y}")

        self.canvas = canvas = tk.Canvas(root, width=BUBBLE_SIZE, height=BUBBLE_SIZE,
                                         bg="#010101", highlightthickness=0)
        canvas.pack()

        self._unread = 0
        self._logo_img = None
        try:
            from PIL import Image, ImageTk
            logo_path = _get_asset_path("logo.png")
            if os.path.exists(logo_path):
                with Image.open(logo_path) as img:
                    icon_size = BUBBLE_SIZE - 16
                    self._logo_img = ImageTk.PhotoImage(
                        img.convert("RGBA").resize((icon_size, icon_size), Image.LANCZOS)
                    )
        except Exception:
            self._logo_img = None

        def draw(color, unread=0):
            canvas.delete("all")
            if self._logo_img:
                canvas.create_image(BUBBLE_SIZE // 2, BUBBLE_SIZE // 2,
                                    image=self._logo_img, anchor="center")
            else:
                pad = 3
                canvas.create_oval(pad, pad, BUBBLE_SIZE - pad, BUBBLE_SIZE - pad,
                                   fill=color, outline="", width=0)
            if unread > 0:
                bx, by = BUBBLE_SIZE - 14, 6
                canvas.create_oval(bx - 9, by - 9, bx + 9, by + 9,
                                   fill="#ff3b30", outline="white", width=1.5)
                label = str(unread) if unread < 100 else "99+"
                canvas.create_text(bx, by, text=label,
                                   font=("Helvetica", 8, "bold"), fill="white")

        self._draw = draw
        draw(BUBBLE_COLOR)

        bubble_save_timer = [None]

        def schedule_bubble_save():
            if bubble_save_timer[0]:
                bubble_save_timer[0].cancel()
            def _do_save():
                with _lock:
                    w, h = _state["chat_w"], _state["chat_h"]
                save_config(w, h)
            t = threading.Timer(1.0, _do_save)
            t.daemon = True
            bubble_save_timer[0] = t
            t.start()

        drag = {"sx": 0, "sy": 0, "moved": False, "press_x": 0, "press_y": 0}

        def on_press(e):
            drag["sx"] = e.x_root - x
            drag["sy"] = e.y_root - y
            drag["moved"] = False
            drag["press_x"] = e.x_root
            drag["press_y"] = e.y_root

        def on_motion(e):
            nonlocal x, y
            if not drag["moved"]:
                dx = abs(e.x_root - drag["press_x"])
                dy = abs(e.y_root - drag["press_y"])
                if dx < DRAG_THRESHOLD and dy < DRAG_THRESHOLD:
                    return
                drag["moved"] = True
                with _lock:
                    if _state["chat_open"]:
                        _state["close_req"] = True
            x = e.x_root - drag["sx"]
            y = e.y_root - drag["sy"]
            y = max(0, min(y, sh - TASKBAR_H - BUBBLE_SIZE))
            root.geometry(f"+{x}+{y}")
            with _lock:
                _state["bubble_x"] = x
                _state["bubble_y"] = y
            schedule_bubble_save()

        def on_release(e):
            if drag["moved"]:
                snap()
            else:
                toggle()

        def snap():
            with _lock:
                cur_sw = _state["sw"]
            target = SNAP_MARGIN if x < cur_sw // 2 else cur_sw - BUBBLE_SIZE - SNAP_MARGIN
            dx = (target - x) / 8
            def step(n):
                nonlocal x
                if n == 0:
                    with _lock:
                        if _state["chat_open"]:
                            _state["close_req"] = True
                    return
                x = int(x + dx)
                root.geometry(f"+{x}+{y}")
                with _lock:
                    _state["bubble_x"] = x
                root.after(14, lambda: step(n - 1))
            step(8)

        def toggle():
            self._dismiss_popup(animate=False)
            with _lock:
                is_open = _state["chat_open"]
            if is_open:
                with _lock:
                    _state["close_req"] = True
            else:
                with _lock:
                    _state["open_req"] = True

        def _quit():
            if _shutdown_event.is_set():
                return
            def _do_quit():
                if _shutdown_event.is_set():
                    return
                _shutdown_event.set()
                try:
                    with _lock:
                        w = _state.get("chat_w", CHAT_W)
                        h = _state.get("chat_h", CHAT_H)
                    save_config(w, h)
                except Exception:
                    pass
                self._dismiss_popup(animate=False)
                try:
                    if _bubble_root:
                        _bubble_root.after(0, _bubble_root.quit)
                except Exception:
                    pass
                try:
                    if _webview_win:
                        _webview_win.destroy()
                except Exception:
                    pass
            root.after(500, _do_quit)

        canvas.bind("<ButtonPress-1>",   on_press)
        canvas.bind("<B1-Motion>",       on_motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<ButtonPress-3>", lambda e: _quit())
        self._quit_func = _quit

        def on_hover_enter(e):
            draw(BUBBLE_HOVER, self._unread)
            def _show():
                with _lock:
                    unread = _state["unread"]
                if unread > 0:
                    with _lock:
                        sender  = _state["last_sender"]
                        preview = _state["last_preview"]
                    self._show_popup(sender, preview, x, y,
                                     root.winfo_screenwidth())
            self._hover_timer = root.after(HOVER_DELAY, _show)

        def on_hover_leave(e):
            draw(BUBBLE_COLOR, self._unread)
            if self._hover_timer:
                try:
                    root.after_cancel(self._hover_timer)
                except Exception:
                    pass
                self._hover_timer = None

        canvas.bind("<Enter>", on_hover_enter)
        canvas.bind("<Leave>", on_hover_leave)

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        _set_toolwindow(hwnd)

        def _smooth_move_bubble(target_y):
            steps = 8
            dy = (target_y - y) / steps
            def step(n):
                nonlocal y
                if n == 0:
                    y = target_y
                    root.geometry(f"+{x}+{y}")
                    with _lock:
                        _state["bubble_y"] = y
                    schedule_bubble_save()
                    return
                y = int(y + dy)
                root.geometry(f"+{x}+{y}")
                with _lock:
                    _state["bubble_y"] = y
                root.after(14, lambda: step(n - 1))
            step(steps)

        def poll_notify():
            if _shutdown_event.is_set():
                return
            if not self.canvas:
                root.after(500, poll_notify)
                return

            while not _wv_queue.empty():
                try:
                    action = _wv_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if action[0] == "show":
                        _webview_win.move(action[1], action[2])
                        _webview_win.show()
                    elif action[0] == "hide":
                        _webview_win.hide()
                except Exception:
                    pass

            while not _toast_queue.empty():
                try:
                    text = _toast_queue.get_nowait()
                except queue.Empty:
                    break
                self._show_toast(text)

            with _lock:
                req         = _state.get("notify_req", False)
                unread      = _state["unread"]
                sender      = _state["last_sender"]
                preview     = _state["last_preview"]
                lift        = _state.get("lift_bubble_req", False)
                move_target = _state.get("bubble_move_req", -1)
                if req:
                    _state["notify_req"] = False
                if lift:
                    _state["lift_bubble_req"] = False
                if move_target != -1:
                    _state["bubble_move_req"] = -1

            if move_target != -1:
                _smooth_move_bubble(move_target)

            if lift:
                root.lift()
                root.attributes("-topmost", True)

            if unread != self._unread:
                self._unread = unread
                draw(BUBBLE_COLOR, unread)

            if req and sender:
                now = time.time()
                with _lock:
                    gs = _state.get("group_sender", "")
                    gt = _state.get("group_time", 0.0)
                    gc = _state.get("group_count", 0)
                    if sender == gs and now - gt < GROUP_WINDOW:
                        gc += 1
                    else:
                        gc = 1
                        gs = sender
                        gt = now
                    _state["group_sender"] = gs
                    _state["group_time"] = gt
                    _state["group_count"] = gc
                    count = gc

                if count > 1:
                    disp_sender  = f"{count} new messages"
                    disp_preview = f"From {sender}: {preview or 'Sent a message'}"
                else:
                    disp_sender  = sender
                    disp_preview = preview
                self._show_popup(disp_sender, disp_preview, x, y,
                                 root.winfo_screenwidth())

            root.after(500, poll_notify)

        root.after(500, poll_notify)
        root.mainloop()

    # ── Notification popup ─────────────────────────────────────────────────────
    _POPUP_TRANSPARENT = "#f0f0f0"
    _POPUP_BG = "#2a2a2a"
    _POPUP_RADIUS = 16
    _POPUP_W, _POPUP_H = 244, 74

    def _get_popup_duration(self):
        with _lock:
            return _state.get("popup_duration", POPUP_DURATION)

    def _show_popup(self, sender, preview, bx, by, sw):
        if preview and len(preview) > 40:
            preview = preview[:37].rsplit(" ", 1)[0] + "..."

        if self.popup and self._popup_canvas:
            try:
                if self._popup_fade_id:
                    try:
                        self.popup.after_cancel(self._popup_fade_id)
                    except Exception:
                        pass
                    self._popup_fade_id = None
                self.popup.attributes("-alpha", 1.0)
                self._popup_canvas.itemconfigure(self._popup_sender_id, text=sender)
                self._popup_canvas.itemconfigure(self._popup_preview_id,
                                                 text=preview or "Sent a message")
                if self._popup_after_id:
                    self.popup.after_cancel(self._popup_after_id)
                self._popup_after_id = self.popup.after(
                    self._get_popup_duration(), self._dismiss_popup
                )
                return
            except Exception:
                self._destroy_popup()

        PW, PH = self._POPUP_W, self._POPUP_H
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.config(bg=self._POPUP_TRANSPARENT)
        popup.attributes("-transparentcolor", self._POPUP_TRANSPARENT)

        on_right = bx > sw // 2
        px = bx - PW - 6 if on_right else bx + BUBBLE_SIZE + 6
        py = by - 6
        vx = ctypes.windll.user32.GetSystemMetrics(76)
        vy = ctypes.windll.user32.GetSystemMetrics(77)
        vw = ctypes.windll.user32.GetSystemMetrics(78)
        vh = ctypes.windll.user32.GetSystemMetrics(79)
        px = max(vx, min(px, vx + vw - PW))
        py = max(vy, min(py, vy + vh - PH))
        popup.geometry(f"{PW}x{PH}+{px}+{py}")

        canvas = tk.Canvas(popup, width=PW, height=PH, bg=self._POPUP_TRANSPARENT,
                           highlightthickness=0, bd=0)
        canvas.pack()
        _draw_rounded_rect(canvas, 0, 0, PW, PH, self._POPUP_RADIUS,
                           fill=self._POPUP_BG)

        sender_id = canvas.create_text(14, 14, text=sender, anchor="nw",
                                       fill="#ffffff", font=("Segoe UI", 9, "bold"),
                                       width=PW - 28)
        preview_id = canvas.create_text(14, 36, text=preview or "Sent a message",
                                        anchor="nw", fill="#aaaaaa",
                                        font=("Segoe UI", 9), width=PW - 28)

        def on_popup_click(e):
            self._dismiss_popup(animate=False)
            with _lock:
                _state["open_req"] = True

        canvas.bind("<Button-1>", on_popup_click)
        canvas.config(cursor="hand2")

        popup.attributes("-alpha", 0.0)
        def fade_in(alpha=0.0):
            alpha = min(alpha + 0.1, 1.0)
            try:
                popup.attributes("-alpha", alpha)
            except Exception:
                return
            if alpha < 1.0:
                popup.after(30, lambda: fade_in(alpha))
        fade_in()

        self._popup_after_id = popup.after(
            self._get_popup_duration(), self._dismiss_popup
        )
        self.popup = popup
        self._popup_canvas = canvas
        self._popup_sender_id = sender_id
        self._popup_preview_id = preview_id

    def _dismiss_popup(self, animate=True):
        if not self.popup:
            return
        if self._popup_after_id:
            try:
                self.popup.after_cancel(self._popup_after_id)
            except Exception:
                pass
            self._popup_after_id = None
        if self._popup_fade_id:
            try:
                self.popup.after_cancel(self._popup_fade_id)
            except Exception:
                pass
            self._popup_fade_id = None

        if animate:
            self._start_fade_out()
        else:
            self._destroy_popup()

    def _start_fade_out(self):
        def step(alpha=1.0):
            alpha = max(alpha - 0.15, 0.0)
            try:
                self.popup.attributes("-alpha", alpha)
            except Exception:
                alpha = 0.0
            if alpha > 0.0:
                self._popup_fade_id = self.popup.after(20, lambda: step(alpha))
            else:
                self._destroy_popup()
        step()

    def _destroy_popup(self):
        try:
            if self.popup:
                self.popup.destroy()
        except Exception:
            pass
        self.popup = None
        self._popup_canvas = None
        self._popup_sender_id = None
        self._popup_preview_id = None
        self._popup_after_id = None
        self._popup_fade_id = None

    # ── Download toast (uses BubbleThread's Tk instance) ───────────────────────
    def _show_toast(self, text):
        try:
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.attributes("-topmost", True)
            toast.config(bg="#2a2a2a")

            lbl = tk.Label(toast, text=text, bg="#2a2a2a", fg="#ddd",
                           font=("Segoe UI", 11), padx=14, pady=8)
            lbl.pack()

            toast.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            tw = toast.winfo_width()
            th = toast.winfo_height()
            toast.geometry(f"+{sw - tw - 16}+{sh - th - 48}")

            toast.after(TOAST_DURATION, lambda: self._safe_destroy(toast))
        except Exception:
            pass

    @staticmethod
    def _safe_destroy(win):
        try:
            win.destroy()
        except Exception:
            pass


# ── Main-thread webview logic ──────────────────────────────────────────────────
def _on_loaded(window):
    try:
        window.evaluate_js(HIDE_SCROLLBAR_CSS)
        window.evaluate_js(HIDE_SIDEBAR_CSS)
        window.evaluate_js(FORCE_SELECT_CSS)
    except Exception:
        pass


def _poll_unread(window):
    prev_unread = 0
    prev_sender = ""
    prev_preview = ""
    first_poll = True
    while not _shutdown_event.is_set():
        time.sleep(POLL_INTERVAL)
        if _shutdown_event.is_set():
            break
        with _lock:
            ready = _state["webview_ready"]
        if not ready:
            continue
        try:
            raw = window.evaluate_js(UNREAD_JS)
            if raw:
                data = json.loads(raw)
                unread  = int(data.get("unread", 0))
                sender  = data.get("sender", "")
                preview = data.get("preview", "")

                with _lock:
                    _state["unread"] = unread
                    open_ = _state["chat_open"]
                    new_msgs = unread > prev_unread
                    content_changed = (unread > 0 and sender and
                        (sender != prev_sender or preview != prev_preview))
                    if not first_poll and (new_msgs or content_changed):
                        _state["last_sender"]  = sender
                        _state["last_preview"] = preview
                        if not open_:
                            _state["notify_req"] = True
                first_poll = False
                prev_unread = unread
                prev_sender = sender
                prev_preview = preview
        except Exception as e:
            logger.debug("unread poll error: %s", e)


def run_webview():
    global _webview_win
    os.environ["WEBVIEW2_USER_DATA_FOLDER"] = STORAGE_DIR

    with _lock:
        chat_w = _state["chat_w"]
        chat_h = _state["chat_h"]

    resize_timer = [None]
    api = Api()

    win = webview.create_window(
        "MCBW - Messenger",
        "https://www.messenger.com",
        x=-9999, y=-9999,
        width=chat_w, height=chat_h,
        frameless=True,
        resizable=True,
        on_top=True,
        shadow=True,
        easy_drag=False,
        hidden=True,
        js_api=api,
    )

    api._win = win
    _webview_win = win

    _poll_started = [False]
    _download_hooked = [False]

    def on_loaded():
        _on_loaded(win)
        win.evaluate_js(RESIZE_GRIP_JS)

        if not _download_hooked[0]:
            _download_hooked[0] = True
            try:
                download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
                os.makedirs(download_dir, exist_ok=True)

                def on_download_starting(sender, args):
                    try:
                        filename = os.path.basename(args.ResultFilePath)
                        args.ResultFilePath = os.path.join(download_dir, filename)
                        _toast_queue.put(f"Downloaded: {filename}")
                    except Exception:
                        pass

                win.webview.CoreWebView2.DownloadStarting += on_download_starting
            except Exception:
                pass

        with _lock:
            _state["webview_ready"] = True
        if not _poll_started[0]:
            _poll_started[0] = True
            threading.Thread(target=_poll_unread, args=(win,), daemon=True).start()

            def attach_icon():
                time.sleep(0.5)
                hwnd = _find_webview_hwnd()
                if hwnd:
                    _set_taskbar_icon(hwnd)
            threading.Thread(target=attach_icon, daemon=True).start()

    def on_resized(width, height):
        with _lock:
            _state["chat_w"] = width
            _state["chat_h"] = height
            _state["lift_bubble_req"] = True
        if resize_timer[0]:
            resize_timer[0].cancel()
        t = threading.Timer(1.0, lambda: save_config(width, height))
        t.daemon = True
        resize_timer[0] = t
        t.start()

    win.events.loaded += on_loaded
    win.events.resized += on_resized

    def watcher():
        while not _shutdown_event.is_set():
            time.sleep(0.15)
            with _lock:
                open_req  = _state["open_req"]
                close_req = _state["close_req"]
                if open_req:
                    _state["open_req"]  = False
                if close_req:
                    _state["close_req"] = False

            if open_req:
                try:
                    with _lock:
                        bx2 = _state["bubble_x"]
                        by2 = _state["bubble_y"]
                        sw2 = _state["sw"]
                        sh2 = _state["sh"]
                        current_w = _state["chat_w"]
                        current_h = _state["chat_h"]

                    if bx2 > sw2 // 2:
                        cx2 = bx2 - current_w + BUBBLE_SIZE
                    else:
                        cx2 = bx2
                    cx2 = max(0, min(cx2, sw2 - current_w))

                    cy2 = by2 + BUBBLE_SIZE + 8
                    if cy2 + current_h > sh2 - TASKBAR_H:
                        new_by2 = max(0, sh2 - TASKBAR_H - current_h - BUBBLE_SIZE - 8)
                        cy2 = new_by2 + BUBBLE_SIZE + 8
                        with _lock:
                            _state["bubble_move_req"] = new_by2

                    cy2 = max(0, cy2)

                    _wv_queue.put(("show", cx2, cy2))
                    with _lock:
                        _state["chat_open"] = True
                        _state["lift_bubble_req"] = True
                except Exception:
                    pass

            if close_req:
                try:
                    _wv_queue.put(("hide",))
                    with _lock:
                        _state["chat_open"] = False
                except Exception:
                    pass

    threading.Thread(target=watcher, daemon=True).start()

    def _save_on_exit():
        with _lock:
            w, h = _state["chat_w"], _state["chat_h"]
        save_config(w, h)
    atexit.register(_save_on_exit)

    webview.settings['ALLOW_DOWNLOADS'] = True
    webview.start(
        storage_path=STORAGE_DIR,
        private_mode=False,
    )


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    load_config()

    bubble = BubbleThread()
    bubble.start()

    while not _shutdown_event.is_set():
        with _lock:
            if _state["sw"]:
                break
        time.sleep(0.05)

    def _open_chat():
        with _lock:
            if not _state["chat_open"]:
                _state["open_req"] = True

    def _on_quit():
        if bubble._quit_func and _bubble_root:
            _bubble_root.after(0, bubble._quit_func)

    tray = threading.Thread(target=_tray_thread_main,
                            args=(_on_quit, _open_chat), daemon=True)
    tray.start()

    print("Chat Bubble running — click the blue bubble!")
    print("Use tray icon or right-click bubble to quit.\n")

    run_webview()

    _shutdown_event.set()
    if _tray_hwnd:
        try:
            ctypes.windll.user32.PostMessageW(_tray_hwnd, 0x0012, 0, 0)
        except Exception:
            pass
    time.sleep(0.5)


if __name__ == "__main__":
    main()
