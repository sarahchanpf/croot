"""Vercel Python entrypoint.

Re-exports the Flask app from the project root so Vercel's Python runtime
picks it up as a WSGI handler. The vercel.json rewrite at the project root
routes every incoming path to /api/index, and Flask then does its own routing
(/, /api/search, /api/history, /static/*).
"""

import os
import sys

# api/ sits one level below the project root; make sure `import app` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401  (Vercel reads `app` as the WSGI callable)
