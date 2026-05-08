"""
``client.elemen`` — Elemen, the AGTP client's graphical frontend.

A pywebview-hosted desktop browser that renders the same wire
protocol the CLI invokes. The Python bridge (``bridge.py``) is a
thin adapter from ``client.core_client.FetchResult`` to the JS-friendly
dict shapes the UI expects.

Launchable three ways:

  * After ``pip install -e .``::  elemen
  * As a module::                  python -m client.elemen.app
  * As a windowed module on Win::  pyw -3.13 -m client.elemen.app
"""

from __future__ import annotations
