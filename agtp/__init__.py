"""
``agtp`` — public API surface for AGTP handler authors.

This is the package handler-writing developers ``import`` from. The
goal is a small, stable, documented surface: the only types you
need to know to bind a Python function to an AGTP endpoint live
here.

::

    from agtp.handlers import EndpointContext, EndpointResponse, EndpointError

    def book_room(ctx: EndpointContext):
        ...
        return EndpointResponse(body={...})

The internal server / client modules (``server.*``, ``client.*``,
``core.*``) are not part of this public surface and may move
between releases.
"""

from __future__ import annotations
