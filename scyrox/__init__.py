"""Scyrox mouse: native, offline configuration library.

Reimplements the protocol the official web driver (scyrox.net) speaks over WebHID,
so the mouse can be read and configured from Linux/macOS without the website.
See PROTOCOL.md for the reverse-engineered protocol.
"""

from . import protocol, flash  # noqa: F401

__all__ = ["protocol", "flash"]
