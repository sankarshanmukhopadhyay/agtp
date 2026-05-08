"""
Elemen — desktop GUI frontend for the AGTP client.

Launches a native window via pywebview, hosting the HTML UI from
``client/elemen/ui/``. The UI calls into the Python ``Api`` class
exported by ``client.elemen.bridge``; this module only handles
window setup, command-line parsing, and the pywebview lifecycle.

Launchable three ways:

  * ``elemen``                                (after pip install)
  * ``python -m client.elemen.app``
  * ``pyw -3.13 -m client.elemen.app``        (Windows, no console)

Optional first argument prefills the URI bar::

  elemen agtp://agents.agtp.io
"""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from client.elemen.bridge import Api


HERE = Path(__file__).resolve().parent
UI_INDEX = HERE / "ui" / "index.html"


def main() -> int:
    initial_uri = ""
    if len(sys.argv) > 1:
        initial_uri = sys.argv[1].strip()

    api = Api(initial_uri=initial_uri)

    webview.create_window(
        title="elemen — AGTP Browser",
        url=str(UI_INDEX),
        js_api=api,
        width=1200,
        height=820,
        min_size=(820, 520),
    )
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
