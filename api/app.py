"""Modern Vercel Python Function entrypoint for the Flask application.

This wrapper keeps local ``run.py`` unchanged while allowing ``vercel.json`` to
use the current ``functions`` configuration instead of legacy ``builds``.
"""

from run import app

__all__ = ["app"]
