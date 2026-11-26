"""
Vercel Python Serverless Entrypoint for DND Finance.
Menormalisasi jalur rewrite Vercel (`/api/index.py` -> `/`) di level ASGI sebelum diteruskan ke FastAPI.
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from main import app as _dnd_fastapi_app  # noqa: E402


class VercelEntrypointASGI:
    """Wrapper ASGI untuk memastikan rute utama Vercel selalu mengarah ke `/` (index.html)."""

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            raw_path = (scope.get("path") or "/").rstrip("/") or "/"
            clean_path = raw_path.rstrip("/") or "/"

            scope["root_path"] = ""

            if clean_path in ("/api/index.py", "/api/index", "/index.py", "/api"):
                scope["path"] = "/"
                scope["raw_path"] = b"/"
            elif clean_path.startswith("/api/index.py/"):
                trimmed = clean_path[len("/api/index.py"):] or "/"
                scope["path"] = trimmed
                scope["raw_path"] = trimmed.encode("utf-8")
            elif clean_path.startswith("/api/index/"):
                trimmed = clean_path[len("/api/index"):] or "/"
                scope["path"] = trimmed
                scope["raw_path"] = trimmed.encode("utf-8")

        await self.asgi_app(scope, receive, send)


app = VercelEntrypointASGI(_dnd_fastapi_app)
