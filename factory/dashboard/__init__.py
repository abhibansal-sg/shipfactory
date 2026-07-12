"""Local, token-authenticated Hermes Factory dashboard."""

from .server import create_server, serve

__all__ = ["create_server", "serve"]
