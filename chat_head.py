"""
Messenger Chat Head for Windows
- Floating bubble with unread badge + message preview popup
- Frameless pywebview window (hidden/shown, never destroyed)
- Scrollbar hidden via injected CSS
- Resizable with persistent memory
"""

import threading
import time
import os
import ctypes
import json
import tkinter as tk
import atexit
import webview

# ── Constants ──────────────────────────────────────────────────────────────────
BUBBLE_SIZE   = 58
BUBBLE_COLOR  = "#0084FF"
BUBBLE_HOVER  = "#005FBF"
CHAT_W        = 400  # Default starting width
CHAT_H        = 780  # Default starting height
SNAP_MARGIN   = 8
TASKBAR_H     = 52
POLL_INTERVAL = 2.0   # seconds between unread checks

STORAGE_DIR = os.path.join(os.environ.get("APPDATA", "."), "ChatHeadMessenger")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")

_state = {
    "sw": 0, "sh": 0,
    "bubble_x": 0, "bubble_y": 0,
    "chat_w": CHAT_W, "chat_h": CHAT_H, # Track dynamic size
    "chat_open":  False,
    "open_req":   False,
    "close_req":  False,
    "webview_ready": False,
    "unread": 0,
    "last_sender": "",
    "last_preview": "",
    "notify_req": False,   # bubble thread should show popup
    "lift_bubble_req": False,  # signal bubble to re-assert topmost after chat shows
    "saved_bubble_y": -1,  # restored bubble Y position from config
    "bubble_nudge_y": -1,  # signal to nudge bubble up if chat won't fit below
}
_lock = threading.Lock()

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

def _set_taskbar_icon(hwnd):
    """Set taskbar icon from logo.png using Windows API"""
    try:
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        if not os.path.exists(icon_path):
            return

        # Convert PNG to ICO in memory using Pillow
        from PIL import Image
        import io
        img = Image.open(icon_path).convert("RGBA")

        # Create both 16x16 and 32x32 sizes for proper taskbar rendering
        ico_buf = io.BytesIO()
        img.save(ico_buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        ico_buf.seek(0)

        # Write temp ICO file (LoadImage requires a file path)
        ico_path = os.path.join(STORAGE_DIR, "taskbar_icon.ico")
        os.makedirs(STORAGE_DIR, exist_ok=True)
        with open(ico_path, "wb") as f:
            f.write(ico_buf.read())

        IMAGE_ICON   = 1
        LR_LOADFROMFILE = 0x00000010
        ICON_SMALL   = 0
        ICON_BIG     = 1
        WM_SETICON   = 0x0080

        hicon = ctypes.windll.user32.LoadImageW(
            None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
        )
        if hicon:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon)
    except Exception:
        pass

# ── Config helpers ─────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "width" in data and "height" in data:
                    with _lock:
                        # Prevent setting absurdly small sizes from bugged hidden states
                        _state["chat_w"] = max(300, data["width"])
                        _state["chat_h"] = max(400, data["height"])
                if "bubble_y" in data:
                    with _lock:
                        _state["saved_bubble_y"] = data["bubble_y"]
        except Exception:
            pass

def save_config(w, h):
    # Only save valid, visible dimensions
    if w > 100 and h > 100:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            with _lock:
                by = _state["bubble_y"]
            with open(CONFIG_FILE, "w") as f:
                json.dump({"width": w, "height": h, "bubble_y": by}, f)
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
        /* Target only the narrow icon sidebar by its exact aria-label */
        [aria-label="Inbox switcher"] {
            display: none !important;
        }
    `;
    document.head.appendChild(style);
})();
"""

UNREAD_JS = """
(function() {
    var result = { unread: 0, sender: '', preview: '' };
    try {
        var badges = document.querySelectorAll('[aria-label*="unread"]');
        if (badges.length > 0) {
            result.unread = badges.length;
        } else {
            var nums = document.querySelectorAll('span[aria-label]');
            var count = 0;
            nums.forEach(function(el) {
                var lbl = el.getAttribute('aria-label') || '';
                if (/^\\d+$/.test(lbl.trim())) count += parseInt(lbl.trim());
            });
            if (count > 0) result.unread = count;
        }

        var rows = document.querySelectorAll('a[href*="/t/"]');
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var bold = row.querySelector('span[style*="700"], span[style*="bold"]');
            if (bold) {
                var spans = row.querySelectorAll('span');
                var texts = [];
                spans.forEach(function(s) {
                    if (s.children.length === 0 && s.textContent.trim())
                        texts.push(s.textContent.trim());
                });
                if (texts.length >= 2) {
                    result.sender  = texts[0];
                    result.preview = texts.slice(1).join(' ').substring(0, 60);
                }
                break;
            }
        }
    } catch(e) {}
    return JSON.stringify(result);
})();
"""

RESIZE_GRIP_JS = """
(function() {
    if (document.getElementById('__resize_grip_br__')) return;

    // ── Bottom-right grip ──────────────────────────────────────────────────
    var grBR = document.createElement('div');
    grBR.id = '__resize_grip_br__';
    grBR.style.cssText = `
        position: fixed;
        bottom: 0;
        right: 0;
        width: 16px;
        height: 16px;
        cursor: se-resize;
        z-index: 999999;
        background: transparent;
    `;
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
        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
    document.body.appendChild(grBR);

    // ── Bottom-left grip ───────────────────────────────────────────────────
    var grBL = document.createElement('div');
    grBL.id = '__resize_grip_bl__';
    grBL.style.cssText = `
        position: fixed;
        bottom: 0;
        left: 0;
        width: 16px;
        height: 16px;
        cursor: sw-resize;
        z-index: 999999;
        background: transparent;
    `;
    grBL.addEventListener('mousedown', function(e) {
        e.preventDefault();
        var startX = e.screenX, startY = e.screenY;
        var startW = document.documentElement.clientWidth;
        var startH = document.documentElement.clientHeight;
        var startWinX = window.screenX;
        function onMove(e) {
            var dx = e.screenX - startX;
            var dy = e.screenY - startY;
            var w = Math.max(300, startW - dx);
            var h = Math.max(400, startH + dy);
            var newX = startWinX + (startW - w);
            window.pywebview.api.resize_and_move(w, h, newX);
        }
        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
    document.body.appendChild(grBL);
})();
"""

# ── Bubble thread ──────────────────────────────────────────────────────────────
class Api:
    """Expose Python methods to JS via window.pywebview.api"""
    def __init__(self, win_ref=None):
        self._win = win_ref
    
    def resize(self, w, h):
        """Called by JS resize grip to trigger window resize"""
        if self._win:
            self._win.resize(int(w), int(h))
    
    def resize_and_move(self, w, h, x):
        """Resize and reposition window (for left-side drag)"""
        if self._win:
            self._win.resize(int(w), int(h))
            self._win.move(int(x), self._win.y)


class BubbleThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.root   = None
        self.canvas = None
        self.popup  = None   

    def run(self):
        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.config(bg="#010101")  
        root.attributes("-transparentcolor", "#010101")

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        with _lock:
            _state["sw"] = sw
            _state["sh"] = sh

        x = sw - BUBBLE_SIZE - SNAP_MARGIN
        y = (sh - BUBBLE_SIZE) // 2
        # Restore saved bubble Y position if available
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
            logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
            if os.path.exists(logo_path):
                img = Image.open(logo_path).convert("RGBA")
                icon_size = BUBBLE_SIZE - 16
                img = img.resize((icon_size, icon_size), Image.LANCZOS)
                self._logo_img = ImageTk.PhotoImage(img)
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

        bubble_save_timer = [None]  # mutable container for debounced bubble save

        def schedule_bubble_save():
            if bubble_save_timer[0]:
                bubble_save_timer[0].cancel()
            with _lock:
                w = _state["chat_w"]
                h = _state["chat_h"]
            bubble_save_timer[0] = threading.Timer(1.0, lambda: save_config(w, h))
            bubble_save_timer[0].start()

        drag = {"sx": 0, "sy": 0, "moved": False}

        def on_press(e):
            drag["sx"] = e.x_root - x
            drag["sy"] = e.y_root - y
            drag["moved"] = False

        def on_motion(e):
            nonlocal x, y
            if not drag["moved"]:
                with _lock:
                    if _state["chat_open"]:
                        _state["close_req"] = True
            drag["moved"] = True
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
            nonlocal x
            target = SNAP_MARGIN if x < sw // 2 else sw - BUBBLE_SIZE - SNAP_MARGIN
            dx = (target - x) / 8
            def step(n):
                nonlocal x
                if n == 0:
                    # Close chat window when snap completes
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
            self._dismiss_popup()
            with _lock:
                is_open = _state["chat_open"]
            if is_open:
                with _lock:
                    _state["close_req"] = True
            else:
                with _lock:
                    _state["open_req"] = True

        canvas.bind("<ButtonPress-1>",   on_press)
        canvas.bind("<B1-Motion>",       on_motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<Enter>", lambda e: draw(BUBBLE_HOVER, self._unread))
        canvas.bind("<Leave>", lambda e: draw(BUBBLE_COLOR, self._unread))

        def _quit():
            # Force close: request webview close, persist size, then kill process
            with _lock:
                _state["close_req"] = True
                w = _state.get("chat_w", CHAT_W)
                h = _state.get("chat_h", CHAT_H)
            try:
                save_config(w, h)
            except Exception:
                pass
            root.after(200, lambda: os._exit(0))

        # Right-click to quit
        canvas.bind("<ButtonPress-3>", lambda e: _quit())

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        _set_toolwindow(hwnd)

        def poll_notify():
            if not self.canvas:  # not ready yet
                root.after(500, poll_notify)
                return
            with _lock:
                req     = _state.get("notify_req", False)
                unread  = _state["unread"]
                sender  = _state["last_sender"]
                preview = _state["last_preview"]
                lift    = _state.get("lift_bubble_req", False)
                nudge_y = _state.get("bubble_nudge_y", -1)
                if req:
                    _state["notify_req"] = False
                if lift:
                    _state["lift_bubble_req"] = False
                if nudge_y != -1:
                    _state["bubble_nudge_y"] = -1

            if nudge_y != -1:
                nonlocal y
                y = nudge_y
                root.geometry(f"+{x}+{y}")

            if lift:
                root.lift()
                root.attributes("-topmost", True)

            if unread != self._unread:
                self._unread = unread
                draw(BUBBLE_COLOR, unread)

            if req and sender:
                self._show_popup(sender, preview, x, y, sw)

            root.after(500, poll_notify)

        root.after(500, poll_notify)
        root.mainloop()

    # ── Notification popup ─────────────────────────────────────────────────────
    def _show_popup(self, sender, preview, bx, by, sw):
        self._dismiss_popup()

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.config(bg="#1e1e1e")

        PW, PH = 240, 70
        on_right = bx > sw // 2
        px = bx - PW - 6 if on_right else bx + BUBBLE_SIZE + 6
        py = by - 6
        # Clamp popup position for multi-monitor setups
        px = max(0, min(px, sw - PW))
        py = max(0, py)

        popup.geometry(f"{PW}x{PH}+{px}+{py}")

        inner = tk.Frame(popup, bg="#2a2a2a", padx=10, pady=8)
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        tk.Label(inner, text=sender, bg="#2a2a2a", fg="#ffffff",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
        tk.Label(inner, text=preview or "Sent a message", bg="#2a2a2a", fg="#aaaaaa",
                 font=("Segoe UI", 9), anchor="w", wraplength=210,
                 justify="left").pack(fill="x")

        popup.attributes("-alpha", 0.0)
        def fade_in(alpha=0.0):
            alpha = min(alpha + 0.1, 1.0)
            popup.attributes("-alpha", alpha)
            if alpha < 1.0:
                popup.after(30, lambda: fade_in(alpha))
        fade_in()

        popup.after(5000, self._dismiss_popup)
        self.popup = popup

    def _dismiss_popup(self):
        if self.popup:
            try:
                self.popup.destroy()
            except Exception:
                pass
            self.popup = None

# ── Main-thread webview logic ──────────────────────────────────────────────────
def _on_loaded(window):
    try:
        window.evaluate_js(HIDE_SCROLLBAR_CSS)
        window.evaluate_js(HIDE_SIDEBAR_CSS)
    except Exception:
        pass

def _poll_unread(window):
    prev_unread = 0
    first_poll = True
    while True:
        time.sleep(POLL_INTERVAL)
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
                    # Don't notify on first poll to avoid spurious popup on startup
                    if not first_poll and unread > prev_unread and sender:
                        _state["last_sender"]  = sender
                        _state["last_preview"] = preview
                        if not open_:
                            _state["notify_req"] = True
                first_poll = False
                prev_unread = unread
        except Exception:
            pass


def run_webview():
    # Force Edge WebView2 to use our AppData folder for user data (cookies/session)
    os.environ["WEBVIEW2_USER_DATA_FOLDER"] = STORAGE_DIR

    load_config() # Load saved dimensions before creating window

    with _lock:
        chat_w = _state["chat_w"]
        chat_h = _state["chat_h"]

    resize_timer = None  # nonlocal instead of global
    api = Api()  # placeholder, set reference after window creation

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
        js_api=api,  # expose Python API to JS
    )
    
    api._win = win  # set window reference after creation

    def on_loaded():
        _on_loaded(win)
        win.evaluate_js(HIDE_SIDEBAR_CSS)
        win.evaluate_js(RESIZE_GRIP_JS)  # inject resize grip for frameless window
        with _lock:
            _state["webview_ready"] = True
        threading.Thread(target=_poll_unread, args=(win,), daemon=True).start()
        
        def attach_icon():
            """Attach taskbar icon after HWND settles"""
            time.sleep(0.5)
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool,
                ctypes.wintypes.HWND,
                ctypes.wintypes.LPARAM
            )
            found = [None]
            def callback(hwnd, _):
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                if "MCBW" in buf.value or "Messenger" in buf.value:
                    found[0] = hwnd
                    return False
                return True
            ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
            if found[0]:
                _set_taskbar_icon(found[0])
        
        threading.Thread(target=attach_icon, daemon=True).start()

    def on_resized(width, height):
        nonlocal resize_timer  # cleaner than global
        # Update state immediately
        with _lock:
            _state["chat_w"] = width
            _state["chat_h"] = height
            _state["lift_bubble_req"] = True  # re-lift bubble above resizing window
        # Debounce config saves to avoid disk hammering during drag resizes
        if resize_timer:
            resize_timer.cancel()
        resize_timer = threading.Timer(1.0, lambda: save_config(width, height))
        resize_timer.start()

    win.events.loaded += on_loaded
    win.events.resized += on_resized # Bind resize listener

    def watcher():
        while True:
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
                        current_w = _state["chat_w"] # Get dynamic width
                        current_h = _state["chat_h"] # Get dynamic height
                    
                    # Calculate X position: snap to left or right
                    if bx2 > sw2 // 2:  # bubble on right
                        cx2 = bx2 - current_w + BUBBLE_SIZE
                    else:  # bubble on left
                        cx2 = bx2
                    cx2 = max(0, min(cx2, sw2 - current_w))
                    
                    # Calculate Y position: prefer below bubble, above if it won't fit
                    cy2 = by2 + BUBBLE_SIZE + 8
                    if cy2 + current_h > sh2 - TASKBAR_H:
                        # Push bubble up so chat fits below it
                        new_by2 = sh2 - TASKBAR_H - current_h - BUBBLE_SIZE - 8
                        new_by2 = max(0, new_by2)
                        cy2 = new_by2 + BUBBLE_SIZE + 8
                        # Signal bubble thread to move up
                        with _lock:
                            _state["bubble_y"] = new_by2
                            _state["bubble_nudge_y"] = new_by2
                    
                    cy2 = max(0, cy2)
                    
                    win.move(cx2, cy2)
                    win.show()
                    with _lock:
                        _state["chat_open"] = True
                        _state["lift_bubble_req"] = True
                except Exception:
                    pass

            if close_req:
                try:
                    win.hide()
                    with _lock:
                        _state["chat_open"] = False
                except Exception:
                    pass

    threading.Thread(target=watcher, daemon=True).start()

    atexit.register(lambda: save_config(_state["chat_w"], _state["chat_h"]))

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

    bubble = BubbleThread()
    bubble.start()

    while True:
        with _lock:
            if _state["sw"]:
                break
        time.sleep(0.05)

    print("Chat Head running — click the blue bubble!")
    print("Close this terminal to quit.\n")

    run_webview()   


if __name__ == "__main__":
    main()
