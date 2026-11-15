import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import app  # noqa: E402,F401  (Vercel Python runtime serves this WSGI app)
