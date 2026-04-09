"""Repository-root paths shared by the API layer."""
import os

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(_ROOT_DIR, "dashboard")
