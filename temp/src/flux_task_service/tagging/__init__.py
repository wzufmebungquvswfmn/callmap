"""Engineering-only test evidence tags and configurable task admission."""

from .analyzer import analyze_task
from .models import TaggingPolicy

__all__ = ["TaggingPolicy", "analyze_task"]
