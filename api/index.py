"""
Vercel Python Serverless Entrypoint Bridge for DND Finance.
Memastikan Vercel selalu menemukan instance FastAPI `app` baik lewat `main.py` maupun `api/index.py`.
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from main import app  # noqa: E402, F401
