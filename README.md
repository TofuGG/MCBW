# MCBW — Messenger Chat Bubbles for Windows

A floating Messenger chat bubble for Windows — just like the mobile bubbles, but on your desktop.

---

## Preview
![Preview](https://github.com/TofuGG/MCBW/blob/main/Preview.png)

---

## Requirements

- **Windows 10 or 11**
- **Python 3.9+** — https://python.org/downloads
- **Microsoft Edge WebView2 Runtime**
  - Comes pre-installed on Windows 11 and most Windows 10 systems
  - If missing, download from: https://developer.microsoft.com/en-us/microsoft-edge/webview2/

---

### Dependencies (`requirements.txt`)
| Package | Version | Purpose |
|---|---|---|
| pywebview | >=4.4.1 | Embeds Messenger in a frameless window |
| pywin32 | >=306 | Windows API access for window management |
| Pillow | >=10.0.0 | Logo/icon loading and ICO conversion |

## Setup (run once)

Open a terminal (CMD or PowerShell) in this folder and run:

```
pip install -r requirements.txt
```

## Run

```
python chat_head.py
```

---

## Build as .exe

To package the app into a standalone executable (no Python required to run):

**1. Install PyInstaller:**
```
pip install pyinstaller
```

**2. Build:**
```
pyinstaller --noconfirm --onefile --windowed --icon=logo.png --add-data "logo.png;." --name "MCBW-Messenger" chat_head.py
```

**3. Find your exe:**
The finished executable will be in the `dist/` folder as `MCBW-Messenger.exe`.

> **Note:** The `logo.png` file must be in the same folder as `chat_head.py` when building.

---

## Features

| Feature | Detail |
|---|---|
| 🔵 Floating bubble | Always-on-top draggable circle — snaps to left or right screen edge |
| 💬 Chat panel | Frameless Messenger (messenger.com) window embedded via pywebview |
| 🔔 Unread badge | Red badge on the bubble shows unread message count |
| 📩 Message preview | Popup notification shows sender name and message preview |
| 🔐 Stay logged in | Session and cookies saved in `%APPDATA%\MCBW` |
| 💾 Persistent size | Chat window size is remembered across restarts |
| 📍 Persistent position | Bubble position is remembered across restarts |
| 🖱️ Resizable window | Drag the bottom-right or bottom-left corner to resize the chat panel |
| 🚫 Clean taskbar | Bubble is hidden from taskbar — only the chat panel shows as "MCBW - Messenger" |
| 🖼️ Custom icon | logo.png used for both the bubble and the taskbar icon |
| 🧹 No sidebar | Messenger's left icon sidebar is hidden for a cleaner look |

---

## How to Use

1. **Launch** — run `chat_head.py` or `MCBW-Messenger.exe`. A blue bubble appears on the right side of your screen.

2. **Drag** — click and drag the bubble anywhere on screen. It snaps to the nearest edge (left or right) when released.

3. **Open chat** — single click the bubble to open the Messenger panel below it. Click again to close it.

4. **Log in** — on first launch, log into Messenger inside the panel. Your session is saved automatically and you won't need to log in again.

5. **Resize** — drag the bottom-right or bottom-left corner of the chat panel to resize it. The new size is saved automatically.

6. **Notifications** — when the chat panel is closed and a new message arrives, a small popup appears next to the bubble showing the sender and a message preview. (Might not work)

7. **Unread count** — the red badge on the bubble shows how many unread messages you have.

8. **Quit** — right-click the bubble to fully close the app and save your settings.

---

## File Structure

```
📁 project folder
├── chat_head.py       ← main script
├── logo.png           ← bubble and taskbar icon
├── requirements.txt   ← dependencies
└── README.md          ← this file

📁 %APPDATA%\MCBW\   ← created automatically on first run
├── config.json                    ← saved window size and bubble position
└── taskbar_icon.ico               ← generated from logo.png on first run
```

---

## Notes

- The chat panel auto-adjusts its spawn position if the bubble is too low on screen — it nudges the bubble up to make room.
- The panel always opens below the bubble when possible, and above it if there isn't enough space.
- The bubble always stays on top of the chat panel.
- If the Messenger UI changes (Meta updates their web app), the sidebar hiding or unread detection may need a selector update in the script.

---

## Disclaimer

This project is an independent hobby project and is in no way affiliated with, endorsed by, or connected to Meta Platforms, Inc., Facebook, or Messenger. All Messenger content is loaded directly from messenger.com and belongs to Meta. This tool simply provides a convenient desktop wrapper around the existing web interface.

This software is provided as-is, for personal use only. Use it responsibly and in accordance with Messenger's terms of service.
