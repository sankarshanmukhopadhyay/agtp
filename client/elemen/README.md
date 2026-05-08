# elemen

A desktop browser for the **AGTP** (Agent Transport Protocol).

elemen is to `agtp://` what a web browser is to `https://`. Type an
`agtp://<agent-id>` URI into the address bar, hit Go, and elemen
resolves the agent through the AGTP registry, opens a TLS connection,
issues a `DESCRIBE` request, and renders the response — JSON, YAML, or
HTML — in a multi-tab, dark-themed native window.

---

## Features

- **Address bar with format selector** — JSON, YAML, or HTML rendering.
- **Multi-tab** — open several agents at once. Each tab keeps its own
  URI, format, and response state. Tab labels show the resolved
  agent's `name` field once the document loads.
- **Sandboxed HTML rendering** — HTML responses render in a sandboxed
  iframe (no scripts, no network) so you can preview an agent's
  presentation document safely.
- **Persistent history** — every fetch is written to a per-user JSON
  file. Open the History panel from the `⋮` menu to revisit past
  resolutions; click an entry to load it into the current tab.
- **Configurable registry** — point at any AGTP registry from the
  Advanced panel. Defaults to `https://registry.agtp.io`.
- **TLS controls** — Plaintext and "skip cert verify" toggles for
  testing against local servers.
- **Cert / signature pane** — placeholder, grayed out until the
  Agent Document spec adds signature and certificate-chain fields.

---

## Requirements

- **Python 3.10+** on macOS and Linux. **Python 3.13** on Windows
  (newer Python versions don't yet have prebuilt `pythonnet` wheels,
  which pywebview needs for the Edge WebView2 backend).
- The **AGTP v1 reference library** — `agent_id.py`,
  `agent_document.py`, `wire_v2.py`. elemen imports these at
  runtime; point at them with the `AGTP_LIB_PATH` env var if they're
  not in the default location (see [Configuration](#configuration)).
- A platform-appropriate webview runtime (see per-OS sections below).

---

## Installation

### Windows

WebView2 ships with Windows 10+ already, so no separate runtime
download is needed.

```powershell
# from the repo root
py -3.13 -m pip install -r requirements.txt
```

If `pip` complains about building `pythonnet`, you're on a Python
version newer than the latest pythonnet wheel. Install Python 3.13
(`winget install Python.Python.3.13`) and re-run with `py -3.13`.

### macOS

Pywebview uses Cocoa + WebKit, which are built into macOS. The pip
install pulls in the `pyobjc` bindings automatically.

```bash
# from the repo root
python3 -m pip install -r requirements.txt
```

### Linux

Pywebview defaults to a GTK/WebKitGTK backend. Install the system
libraries first, then the Python package.

**Debian / Ubuntu:**
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
                 gir1.2-webkit2-4.1 libcairo2-dev
python3 -m pip install -r requirements.txt
```

**Fedora:**
```bash
sudo dnf install python3-gobject gtk3 webkit2gtk4.1
python3 -m pip install -r requirements.txt
```

**Qt backend alternative** (use this if WebKitGTK isn't available
on your distro):
```bash
python3 -m pip install -r requirements.txt PyQt5 PyQtWebEngine
# launch with: python3 app.py --backend qt
```

---

## Running

### Windows

```powershell
# console attached (handy for stack traces)
py -3.13 app.py

# no console window
pyw -3.13 app.py

# open with a URI prefilled and auto-fetched
py -3.13 app.py agtp://d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230
```

### macOS

```bash
python3 app.py

# or with a URI prefilled
python3 app.py "agtp://d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
```

To launch from the dock, build a one-liner shell wrapper:
```bash
cat > ~/elemen.command <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")/path/to/elemen" && exec python3 app.py
EOF
chmod +x ~/elemen.command
```

### Linux

```bash
python3 app.py

# launcher shortcut for GNOME/KDE
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/elemen.desktop <<EOF
[Desktop Entry]
Type=Application
Name=elemen
Exec=python3 $(pwd)/app.py
Icon=web-browser
Terminal=false
Categories=Network;
EOF
```

---

## Configuration

elemen reads two environment variables on startup:

| Variable | Purpose | Default |
|---|---|---|
| `AGTP_LIB_PATH` | Directory containing `agent_id.py`, `agent_document.py`, `wire_v2.py` | tries `<elemen>/../v1` (elemen as subdir of agtp), then `<elemen>/../agtp/v1` (elemen as sibling of agtp) |
| `ELEMEN_DATA_DIR` | Where to store `history.json` | OS-conventional dir (see below) |

**Default data directory:**
- Windows: `%APPDATA%\elemen\`
- macOS: `~/Library/Application Support/elemen/`
- Linux: `$XDG_CONFIG_HOME/elemen/` or `~/.config/elemen/`

Example:
```bash
export AGTP_LIB_PATH=/path/to/agtp/v1
export ELEMEN_DATA_DIR=/tmp/elemen-test
python3 app.py
```

In-app, the Advanced panel (`⋮` menu → Advanced) lets you override the
registry URL and toggle TLS settings per-fetch without restarting.

---

## Project layout

```
elemen/
├── app.py            # pywebview entry; exposes Api to JS, manages history
├── client.py         # AGTP client wrapper (resolve + fetch); locates AGTP lib
├── ui/
│   ├── index.html    # address bar, tab strip, response panes, history panel
│   ├── app.css       # dark theme, grid layout
│   └── app.js        # tab state, history, JSON highlighter, HTML iframe
├── requirements.txt
└── README.md
```

---

## Not yet implemented

- `agtp://` system-wide protocol handler registration (Windows
  registry / macOS LaunchServices / Linux `xdg-mime`). Click-through
  from `agtp://` links elsewhere in the OS isn't wired up yet.
- Cert / signature pane is a placeholder pending spec extensions.
- Methods other than `DESCRIBE`. The Agent Document spec defines a
  small set; elemen currently only issues `DESCRIBE`.
- HTTP-style "back / forward" within a tab.

---

## Troubleshooting

**"Could not locate AGTP v1 library"**: set `AGTP_LIB_PATH` to the
directory containing `agent_id.py`.

**Registry lookups fail with `getaddrinfo failed`**: DNS can't reach
the registry. Verify connectivity, or use the `agtp://<id>@host:port`
URI form to skip the registry entirely.

**Window opens then immediately closes (Windows)**: usually a
`pythonnet` install issue. Confirm with
`py -3.13 -c "import webview"` — if that errors, reinstall:
`py -3.13 -m pip install --force-reinstall pywebview`.

**Linux: `Namespace WebKit2 not available`**: install the WebKitGTK
GIR package (`gir1.2-webkit2-4.1` on Debian/Ubuntu; the version
suffix may differ on older distros — try `4.0`).
