"""Generic network I/O used by adapters (a minimal WebSocket client). The core never imports it."""

from project100c.netio.ws import WsClosed, WsConnection, WsError, ws_connect

__all__ = ["WsClosed", "WsConnection", "WsError", "ws_connect"]
