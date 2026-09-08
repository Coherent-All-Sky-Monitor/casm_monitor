"""FastAPI web service (status strip, events, series, jobs, SPA hosting)."""

from .app import create_app
from .status import ITEMS, build_status

__all__ = ["create_app", "build_status", "ITEMS"]
