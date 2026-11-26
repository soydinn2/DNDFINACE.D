"""
Vercel Python Serverless Entrypoint for DND Finance.
Mengekspor instance murni FastAPI (`app`) agar dideteksi secara native oleh Vercel Python Runtime.
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from main import app  # noqa: E402, F401
