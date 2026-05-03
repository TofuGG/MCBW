# Messenger Chat Head for Windows

A floating Messenger chat head for Windows — just like the mobile bubbles, but on your desktop.

---

## Requirements

- **Windows 10 or 11**
- **Python 3.9+** — https://python.org/downloads

---

## Setup (run once)

Open a terminal (CMD or PowerShell) in this folder and run:

```
pip install -r requirements.txt
```

---

## Run

```
python chat_head.py
```

---

## How it works

| Feature | Detail |
|---|---|
| 🔵 Floating bubble | Always-on-top, draggable circle — snaps to left or right screen edge |
| 💬 Chat panel | 430×700 panel with real Messenger (messenger.com) embedded via pywebview |
| 🔐 Stay logged in | Your Messenger session is saved in `%APPDATA%\ChatHeadMessenger` |
| 🚫 No taskbar clutter | Window is hidden from taskbar and Alt-Tab |

---

## Usage

1. **Drag** the bubble anywhere on screen — it snaps to the nearest edge when released.
2. **Click** the bubble to open/close the Messenger chat panel.
3. **Log in** to Messenger the first time — stays logged in afterward.
4. **Close panel** using the ✕ button on the panel's title bar.
5. To **quit** fully, right-click the bubble or close from Task Manager.

---

## Notes

- pywebview uses your system's WebView2 runtime (comes with Windows 11 / Edge).
  If it's missing on Windows 10, download it from:
  https://developer.microsoft.com/en-us/microsoft-edge/webview2/
- The chat panel is resizable by editing `CHAT_W` and `CHAT_H` at the top of `chat_head.py`.
