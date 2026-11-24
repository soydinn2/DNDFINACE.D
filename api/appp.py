"""
Root ASGI Entrypoint Alias for DND Finance.
Memastikan perintah `uvicorn app:app`, `fastapi run`, maupun platform cloud langsung menemukan `app`.
"""
from main import app  # noqa: F401
