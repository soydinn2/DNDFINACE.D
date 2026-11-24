"""
DND Finance — Server Backend FastAPI (Vercel Serverless & Local Auto-Bootstrap Edition)
- Normalisasi otomatis jalur Vercel (`/api/index.py` -> `/`) agar halaman utama `index.html` langsung tampil
- Multi-Path File Resolver sehingga seluruh berkas proyek (`index.html`, `quant_ai_engine.cpp`,
  `database.rules.json`, `firestore.rules`, `.env`, `vercel.json`) selalu terbaca oleh sistem
- Login Google Langsung di Browser (Google OAuth2 / ID Token / Email Workspace, Tanpa Firebase Auth)
- Firebase Realtime Database (`dnd-finance-3590e-default-rtdb.asia-southeast1.firebasedatabase.app`)
- Embedded DND AI AGENT + Multi-Source Real-Time Macroeconomic, Gold, FX & Knowledge Chipset
"""

import os
import re
import sys
import json
import hmac
import time
import glob
import base64
import hashlib
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))
WRITABLE_DIR = "/tmp" if IS_VERCEL else BASE_DIR


def resolve_project_file(filename: str) -> Optional[str]:
    """Mencari berkas proyek di seluruh kandidat direktori runtime (BASE_DIR, CWD, /var/task)."""
    candidates = [
        os.path.join(BASE_DIR, filename),
        os.path.join(os.getcwd(), filename),
        os.path.join(os.path.dirname(BASE_DIR), filename),
        os.path.join("/var/task", filename),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path

    target_basename = os.path.basename(filename)
    for root_dir in (BASE_DIR, os.getcwd(), "/var/task"):
        if root_dir and os.path.isdir(root_dir):
            for dirpath, _, filenames in os.walk(root_dir):
                if target_basename in filenames:
                    candidate = os.path.join(dirpath, target_basename)
                    if filename in candidate.replace("\\", "/") or target_basename == filename:
                        return candidate
    return None


def ensure_core_dependencies() -> None:
    """Memastikan pustaka inti terpasang otomatis saat dijalankan di lingkungan lokal."""
    if IS_VERCEL:
        return
    required = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "httpx": "httpx",
        "pydantic": "pydantic",
        "multipart": "python-multipart",
    }
    missing = []
    for mod_name, pkg_name in required.items():
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"[DND Finance] Menginstal pustaka server: {', '.join(missing)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


if __name__ == "__main__" and not IS_VERCEL:
    ensure_core_dependencies()

import httpx
from fastapi import FastAPI, Request, Response, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def load_local_env_file(env_filename: str = ".env") -> Optional[str]:
    env_path = resolve_project_file(env_filename) or resolve_project_file("env.example")
    if not env_path:
        return None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        return env_path
    except Exception as exc:
        print(f"[DND Finance .env Loader Warning]: {exc}")
        return None


LOADED_ENV_PATH = load_local_env_file()

# ==============================================================================
# KONFIGURASI PROYEK DND FINANCE (dnd-finance-3590e) & FIREBASE REALTIME DATABASE
# ==============================================================================
GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID",
    "753367715469-972f7qjb3t97qab938sc5lo69mgp78h0.apps.googleusercontent.com",
)
SESSION_SECRET = os.getenv("DND_SESSION_SECRET", "dnd_finance_direct_google_oauth_secret_2026")

FIREBASE_DATABASE_URL = os.getenv(
    "FIREBASE_DATABASE_URL",
    "https://dnd-finance-3590e-default-rtdb.asia-southeast1.firebasedatabase.app",
).strip().rstrip("/")
FIREBASE_DATABASE_SECRET = os.getenv(
    "FIREBASE_DATABASE_SECRET",
    "FYy3NzPGZbqW7IF4eMY055bTTh5Uan1q5iATKTMB",
).strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_PATH",
    os.path.join(BASE_DIR, "serviceAccountKey.json"),
).strip()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
EMBEDDED_OSS_MODEL = os.getenv("EMBEDDED_OSS_MODEL", "qwen2.5:7b")
OPEN_LLM_FALLBACK_URL = os.getenv("OPEN_LLM_FALLBACK_URL", "https://text.pollinations.ai/openai")

CPP_ENGINE_BINARY = os.path.join(
    WRITABLE_DIR,
    "quant_ai_engine.exe" if os.name == "nt" else "quant_ai_engine",
)
LOCAL_SQLITE_DB = os.path.join(WRITABLE_DIR, "dnd_finance_mirror.db")


def compile_cpp_engine_if_needed() -> bool:
    """Mengompilasi mesin kuantitatif C++17 (quant_ai_engine.cpp) secara otomatis jika g++ tersedia."""
    if os.path.isfile(CPP_ENGINE_BINARY):
        return True
    src_path = resolve_project_file("quant_ai_engine.cpp")
    if not src_path:
        return False
    try:
        subprocess.run(
            ["g++", "-O3", "-std=c++17", src_path, "-o", CPP_ENGINE_BINARY],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if os.name != "nt":
            os.chmod(CPP_ENGINE_BINARY, 0o755)
        return True
    except Exception:
        return False


# ==============================================================================
# ZERO-EMOJI SANITIZER & SIGNED COOKIE HELPERS
# ==============================================================================
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002300-\U000023FF"
    "\U000024C2-\U0001F251"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: Any) -> str:
    if text is None:
        return ""
    cleaned = EMOJI_PATTERN.sub("", str(text))
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def sign_session_payload(payload: Dict[str, Any]) -> str:
    raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw_json).decode("utf-8")
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        raw = base64.urlsafe_b64decode(b64.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
        if data.get("exp", 0) < int(time.time()):
            return None
        return data
    except Exception:
        return None


def sanitize_rtdb_key(raw_key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", (raw_key or "shared_default_ledger").strip().lower())


def resolve_service_account_file() -> Optional[str]:
    if FIREBASE_SERVICE_ACCOUNT_PATH and os.path.isfile(FIREBASE_SERVICE_ACCOUNT_PATH):
        return FIREBASE_SERVICE_ACCOUNT_PATH
    found = resolve_project_file("serviceAccountKey.json")
    if found:
        return found
    for search_dir in {BASE_DIR, os.getcwd(), "/var/task"}:
        if os.path.isdir(search_dir):
            matches = glob.glob(os.path.join(search_dir, "dnd-finance-3590e*.json"))
            if matches:
                return matches[0]
    return None


# ==============================================================================
# REAL-TIME MACROECONOMIC, GOLD, FX & KNOWLEDGE CHIPSET (DND_RT_CHIPSET_CORE_V5)
# ==============================================================================
_REALTIME_CACHE: Dict[str, Any] = {
    "ts": 0.0,
    "pulse": {
        "chipset_id": "DND_RT_CHIPSET_CORE_V5",
        "inflation_id_pct": 2.68,
        "inflation_global_pct": 2.90,
        "gdp_id_pct": 5.05,
        "bi_rate_pct": 5.75,
        "usd_idr": 16250.0,
        "sgd_idr": 12080.0,
        "eur_idr": 17540.0,
        "gbp_idr": 20650.0,
        "jpy_idr": 108.5,
        "aud_idr": 10540.0,
        "myr_idr": 3670.0,
        "cny_idr": 2245.0,
        "gold_usd_oz": 2740.0,
        "gold_idr_gram": 1431500.0,
        "sources": [
            "World Bank Official Open Data",
            "European Central Bank (Frankfurter)",
            "Open ER-API",
            "Bank Indonesia",
        ],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    },
}


async def get_live_realtime_pulse(force_refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force_refresh and (now - _REALTIME_CACHE["ts"]) < 300:
        return _REALTIME_CACHE["pulse"]

    pulse = dict(_REALTIME_CACHE["pulse"])

    fx_fetched = False
    try:
        async with httpx.AsyncClient(timeout=4.5) as client:
            fx_res = await client.get("https://open.er-api.com/v6/latest/USD")
            if fx_res.status_code == 200:
                fx_json = fx_res.json()
                rates = fx_json.get("rates") or {}
                usd_idr = float(rates.get("IDR", pulse["usd_idr"]))
                pulse["usd_idr"] = round(usd_idr, 2)
                for code, field in [
                    ("SGD", "sgd_idr"),
                    ("EUR", "eur_idr"),
                    ("GBP", "gbp_idr"),
                    ("JPY", "jpy_idr"),
                    ("AUD", "aud_idr"),
                    ("MYR", "myr_idr"),
                    ("CNY", "cny_idr"),
                ]:
                    r_val = float(rates.get(code, 0))
                    if r_val > 0:
                        pulse[field] = round(usd_idr / r_val, 2)
                fx_fetched = True
    except Exception:
        pass

    if not fx_fetched:
        try:
            async with httpx.AsyncClient(timeout=4.5) as client:
                ecb_res = await client.get(
                    "https://api.frankfurter.app/latest",
                    params={"from": "USD", "to": "IDR,SGD,EUR,GBP,JPY,AUD,MYR,CNY"},
                )
                if ecb_res.status_code == 200:
                    rates = (ecb_res.json() or {}).get("rates") or {}
                    usd_idr = float(rates.get("IDR", pulse["usd_idr"]))
                    pulse["usd_idr"] = round(usd_idr, 2)
                    for code, field in [
                        ("SGD", "sgd_idr"),
                        ("EUR", "eur_idr"),
                        ("GBP", "gbp_idr"),
                        ("JPY", "jpy_idr"),
                        ("AUD", "aud_idr"),
                        ("MYR", "myr_idr"),
                        ("CNY", "cny_idr"),
                    ]:
                        r_val = float(rates.get(code, 0))
                        if r_val > 0:
                            pulse[field] = round(usd_idr / r_val, 2)
        except Exception:
            pass

    pulse["gold_idr_gram"] = round((float(pulse["gold_usd_oz"]) * float(pulse["usd_idr"])) / 31.1035, 0)

    try:
        async with httpx.AsyncClient(timeout=4.5) as client:
            wb_res = await client.get(
                "https://api.worldbank.org/v2/country/IDN/indicator/FP.CPI.TOTL.ZG",
                params={"format": "json", "per_page": 3},
            )
            if wb_res.status_code == 200:
                wb_data = wb_res.json()
                if isinstance(wb_data, list) and len(wb_data) > 1 and isinstance(wb_data[1], list):
                    for entry in wb_data[1]:
                        if entry and entry.get("value") is not None:
                            pulse["inflation_id_pct"] = round(float(entry["value"]), 2)
                            break
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=4.5) as client:
            wld_res = await client.get(
                "https://api.worldbank.org/v2/country/WLD/indicator/FP.CPI.TOTL.ZG",
                params={"format": "json", "per_page": 3},
            )
            if wld_res.status_code == 200:
                wld_data = wld_res.json()
                if isinstance(wld_data, list) and len(wld_data) > 1 and isinstance(wld_data[1], list):
                    for entry in wld_data[1]:
                        if entry and entry.get("value") is not None:
                            pulse["inflation_global_pct"] = round(float(entry["value"]), 2)
                            break

            gdp_res = await client.get(
                "https://api.worldbank.org/v2/country/IDN/indicator/NY.GDP.MKTP.KD.ZG",
                params={"format": "json", "per_page": 3},
            )
            if gdp_res.status_code == 200:
                gdp_data = gdp_res.json()
                if isinstance(gdp_data, list) and len(gdp_data) > 1 and isinstance(gdp_data[1], list):
                    for entry in gdp_data[1]:
                        if entry and entry.get("value") is not None:
                            pulse["gdp_id_pct"] = round(float(entry["value"]), 2)
                            break
    except Exception:
        pass

    pulse["updated_at"] = datetime.now(timezone.utc).isoformat()
    _REALTIME_CACHE["ts"] = now
    _REALTIME_CACHE["pulse"] = pulse
    return pulse


async def fetch_official_knowledge_snippet(query: str, lang: str = "ID") -> str:
    clean_q = re.sub(
        r"^(apa itu|jelaskan|bagaimana cara|siapa itu|mengapa|kenapa|sejarah|pengertian|what is|explain|how does|how to|who is|why is)\s+",
        "",
        strip_emojis(query),
        flags=re.I,
    ).strip("?!. ")
    if not clean_q or len(clean_q) < 2:
        return ""

    lang_code = "id" if lang.upper() == "ID" else "en"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            search_res = await client.get(
                f"https://{lang_code}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": clean_q,
                    "format": "json",
                    "srlimit": 1,
                },
            )
            if search_res.status_code == 200:
                hits = ((search_res.json().get("query") or {}).get("search")) or []
                if hits and hits[0].get("title"):
                    title = hits[0]["title"]
                    sum_res = await client.get(
                        f"https://{lang_code}.wikipedia.org/api/rest_v1/page/summary/{title}"
                    )
                    if sum_res.status_code == 200:
                        s_json = sum_res.json()
                        extract = strip_emojis(s_json.get("extract", ""))
                        if extract:
                            return f"{strip_emojis(title)} (Wikipedia Official): {extract}"
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            ddg_res = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": clean_q, "format": "json", "no_html": 1, "skip_disambig": 1},
            )
            if ddg_res.status_code == 200:
                d_json = ddg_res.json()
                abstract = strip_emojis(d_json.get("AbstractText", ""))
                if abstract:
                    src = strip_emojis(d_json.get("AbstractSource", "Official Reference"))
                    return f"{strip_emojis(d_json.get('Heading', clean_q))} ({src}): {abstract}"
    except Exception:
        pass

    return ""


# ==============================================================================
# FIREBASE REALTIME DATABASE ENGINE (WORKSPACES, RECEIPT SCANS & REPORT FILES)
# ==============================================================================
class FirebaseRealtimeDatabaseEngine:
    def __init__(self):
        self.rtdb_admin_ref = None
        self.auth_method = "sqlite_mirror"
        self._init_local_sqlite()
        self._init_firebase_rtdb()

    def _init_local_sqlite(self):
        try:
            conn = sqlite3.connect(LOCAL_SQLITE_DB)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workspaces (
                        account_key TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL DEFAULT 1,
                        state_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"[SQLite Mirror Notice]: {exc}")

    def _init_firebase_rtdb(self):
        if not FIREBASE_DATABASE_URL:
            return

        service_file = resolve_service_account_file()
        if FIREBASE_SERVICE_ACCOUNT_JSON or service_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            try:
                import firebase_admin
                from firebase_admin import credentials, db

                if not firebase_admin._apps:
                    if FIREBASE_SERVICE_ACCOUNT_JSON:
                        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
                    elif service_file:
                        cred = credentials.Certificate(service_file)
                    else:
                        cred = credentials.ApplicationDefault()
                    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DATABASE_URL})
                self.rtdb_admin_ref = db.reference("/")
                self.auth_method = "firebase_admin_sdk"
                return
            except Exception:
                pass

        if FIREBASE_DATABASE_SECRET and not FIREBASE_DATABASE_SECRET.startswith("PASTE_"):
            self.auth_method = "firebase_database_secret"
        else:
            self.auth_method = "sqlite_mirror_missing_secret"

    @property
    def is_firebase_active(self) -> bool:
        return self.auth_method in ("firebase_admin_sdk", "firebase_database_secret")

    async def write_node(self, node_path: str, payload: Dict[str, Any]) -> bool:
        if self.rtdb_admin_ref is not None:
            try:
                self.rtdb_admin_ref.child(node_path).set(payload)
                return True
            except Exception as exc:
                print(f"[Firebase Admin Write Error ({node_path})]: {exc}")
                return False

        if FIREBASE_DATABASE_URL and FIREBASE_DATABASE_SECRET and not FIREBASE_DATABASE_SECRET.startswith("PASTE_"):
            try:
                url = f"{FIREBASE_DATABASE_URL}/{node_path}.json"
                async with httpx.AsyncClient(timeout=6.0) as client:
                    res = await client.put(url, params={"auth": FIREBASE_DATABASE_SECRET}, json=payload)
                    return res.status_code == 200
            except Exception as exc:
                print(f"[Firebase REST Write Error ({node_path})]: {exc}")
                return False

        return False

    async def read_node(self, node_path: str) -> Any:
        if self.rtdb_admin_ref is not None:
            try:
                return self.rtdb_admin_ref.child(node_path).get()
            except Exception as exc:
                print(f"[Firebase Admin Read Error ({node_path})]: {exc}")

        if FIREBASE_DATABASE_URL and FIREBASE_DATABASE_SECRET and not FIREBASE_DATABASE_SECRET.startswith("PASTE_"):
            try:
                url = f"{FIREBASE_DATABASE_URL}/{node_path}.json"
                async with httpx.AsyncClient(timeout=6.0) as client:
                    res = await client.get(url, params={"auth": FIREBASE_DATABASE_SECRET})
                    if res.status_code == 200:
                        return res.json()
            except Exception as exc:
                print(f"[Firebase REST Read Error ({node_path})]: {exc}")

        return None

    async def load_workspace(self, account_key: str) -> Dict[str, Any]:
        safe_key = sanitize_rtdb_key(account_key)
        data = await self.read_node(f"workspaces/{safe_key}")
        if isinstance(data, dict):
            return {
                "revision": int(data.get("revision", 1)),
                "state": data.get("state", {}),
            }

        try:
            conn = sqlite3.connect(LOCAL_SQLITE_DB)
            try:
                cur = conn.execute(
                    "SELECT revision, state_json FROM workspaces WHERE account_key = ?",
                    (safe_key,),
                )
                row = cur.fetchone()
                if row:
                    return {"revision": int(row[0]), "state": json.loads(row[1])}
            finally:
                conn.close()
        except Exception:
            pass

        return {"revision": 0, "state": {}}

    async def save_workspace(self, account_key: str, revision: int, state: Dict[str, Any]) -> bool:
        safe_key = sanitize_rtdb_key(account_key)
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "account_key": safe_key,
            "revision": revision,
            "state": state,
            "updated_at": now_iso,
        }

        try:
            conn = sqlite3.connect(LOCAL_SQLITE_DB)
            try:
                conn.execute(
                    """
                    INSERT INTO workspaces (account_key, revision, state_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(account_key) DO UPDATE SET
                        revision = excluded.revision,
                        state_json = excluded.state_json,
                        updated_at = excluded.updated_at
                    """,
                    (safe_key, revision, json.dumps(state), now_iso),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

        return await self.write_node(f"workspaces/{safe_key}", payload)

    async def test_connection(self) -> Dict[str, Any]:
        ok = await self.write_node(
            "_health_check",
            {
                "app": "DND Finance",
                "project_id": "dnd-finance-3590e",
                "ok": True,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
        if ok:
            return {
                "connected": True,
                "project": "DND Finance (dnd-finance-3590e)",
                "method": self.auth_method,
                "database_url": FIREBASE_DATABASE_URL,
            }
        return {
            "connected": False,
            "project": "DND Finance (dnd-finance-3590e)",
            "method": self.auth_method,
            "database_url": FIREBASE_DATABASE_URL,
            "hint": "Pastikan database.rules.json sudah di-Publish di Firebase Realtime Database.",
        }


db_engine = FirebaseRealtimeDatabaseEngine()


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    cpp_ready = compile_cpp_engine_if_needed()
    fb_status = await db_engine.test_connection()
    port = os.getenv("PORT", "8000")
    print("\n============================================================")
    print("  DND FINANCE SERVER AKTIF & SIAP DIGUNAKAN")
    print("============================================================")
    print(f"  Alamat Aplikasi : http://localhost:{port}")
    print(f"  Cek Status File : http://localhost:{port}/api/system/files")
    print(f"  Cek Status DB   : http://localhost:{port}/api/firebase/status")
    print(f"  Firebase RTDB   : {'TERHUBUNG (' + fb_status['method'] + ')' if fb_status['connected'] else 'MODE LOKAL'}")
    print(f"  Mesin C++17     : {'AKTIF (quant_ai_engine)' if cpp_ready else 'AKTIF (Python Quant Engine)'}")
    print("  DND AI AGENT    : MULTI-SOURCE REAL-TIME CHIPSET READY")
    print("============================================================\n")
    yield


app = FastAPI(title="DND Finance API Server", version="11.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def normalize_vercel_root_path(request: Request, call_next):
    """Menormalisasi jalur `/api/index.py` dari Vercel rewrite menjadi `/` agar UI HTML langsung tampil."""
    raw_path = (request.scope.get("path") or "/").rstrip("/") or "/"
    request.scope["root_path"] = ""
    if raw_path in ("/api/index.py", "/api/index", "/index.py", "/api"):
        request.scope["path"] = "/"
        request.scope["raw_path"] = b"/"
    elif raw_path.startswith("/api/index.py/"):
        trimmed = raw_path[len("/api/index.py"):] or "/"
        request.scope["path"] = trimmed
        request.scope["raw_path"] = trimmed.encode("utf-8")
    return await call_next(request)


# ==============================================================================
# C++17 QUANTITATIVE & INFLATION-CALIBRATED ENGINE BRIDGE
# ==============================================================================
def run_cpp_quantitative_engine(
    transactions: List[Dict[str, Any]],
    goals: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    inflation_pct: float = 2.68,
) -> Dict[str, Any]:
    payload = {
        "transactions": transactions,
        "goals_count": len(goals),
        "active_rules_count": sum(1 for r in rules if r.get("enabled")),
        "inflation_pct": inflation_pct,
    }
    if os.path.isfile(CPP_ENGINE_BINARY):
        try:
            proc = subprocess.run(
                [CPP_ENGINE_BINARY, "--analyze"],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3.0,
            )
            if proc.returncode == 0 and proc.stdout:
                return json.loads(proc.stdout.decode("utf-8"))
        except Exception as exc:
            print(f"[C++17 Engine Fallback]: {exc}")

    total_income = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") == "income")
    total_expense = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") in ("expense", "withdrawal"))
    net_balance = total_income - total_expense
    expense_dates = {str(t.get("date", ""))[:10] for t in transactions if t.get("type") in ("expense", "withdrawal") and t.get("date")}
    active_days = max(1, len(expense_dates))
    daily_burn = total_expense / active_days
    monthly_burn = daily_burn * 30.0
    emergency_ideal = round(monthly_burn * 6.0, 2)
    savings_rate = int(round(((total_income - total_expense) / total_income) * 100)) if total_income > 0 else 0
    savings_rate = max(-100, min(100, savings_rate))
    runway_days = int(round(max(0.0, net_balance) / daily_burn)) if daily_burn > 0 else 999

    inf_rate = max(0.0, inflation_pct) / 100.0
    real_balance_1y = round(net_balance / (1.0 + inf_rate), 2) if net_balance > 0 else round(net_balance, 2)
    real_balance_3y = round(net_balance / ((1.0 + inf_rate) ** 3), 2) if net_balance > 0 else round(net_balance, 2)
    real_balance_5y = round(net_balance / ((1.0 + inf_rate) ** 5), 2) if net_balance > 0 else round(net_balance, 2)
    erosion_1y = round(net_balance - real_balance_1y, 2) if net_balance > 0 else 0.0

    score = 50
    if total_income > 0:
        score += max(-25, min(30, int(round(savings_rate * 0.8))))
    if net_balance > 0:
        score += 10
    if len(goals) > 0:
        score += 5
    if any(r.get("enabled") for r in rules):
        score += 5
    score = max(5, min(100, score))

    grade = "Stabil / Bertumbuh"
    if score >= 80:
        grade = "Sangat Sehat (Prima)"
    elif score >= 60:
        grade = "Sehat & Terkendali"
    elif score < 40:
        grade = "Perlu Penghematan"

    monthly_smart_save = (total_income * 0.20) if total_income > 0 else max(0.0, net_balance)
    forecast_6m = round(net_balance + monthly_smart_save * 6.0, 2)
    forecast_12m = round(net_balance + monthly_smart_save * 12.0, 2)

    return {
        "engine": "python_quant_engine",
        "health_score": score,
        "health_grade": grade,
        "savings_rate_pct": savings_rate,
        "daily_burn_rate": round(daily_burn, 2),
        "monthly_burn_est": round(monthly_burn, 2),
        "emergency_fund_ideal": emergency_ideal,
        "runway_days": runway_days,
        "net_balance": round(net_balance, 2),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "real_balance_1y": real_balance_1y,
        "real_balance_3y": real_balance_3y,
        "real_balance_5y": real_balance_5y,
        "inflation_erosion_1y": erosion_1y,
        "forecast_6m_smart": forecast_6m,
        "forecast_12m_smart": forecast_12m,
    }


def run_cpp_receipt_reconciler(
    subtotal: float,
    discount: float,
    tax_percent: float,
    people_count: int = 1,
) -> Dict[str, Any]:
    payload = {
        "subtotal": subtotal,
        "discount": discount,
        "tax_percent": tax_percent,
        "people_count": max(1, people_count),
    }
    if os.path.isfile(CPP_ENGINE_BINARY):
        try:
            proc = subprocess.run(
                [CPP_ENGINE_BINARY, "--reconcile-receipt"],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3.0,
            )
            if proc.returncode == 0 and proc.stdout:
                return json.loads(proc.stdout.decode("utf-8"))
        except Exception:
            pass

    sub = max(0.0, float(subtotal))
    disc = max(0.0, float(discount))
    net_base = max(0.0, sub - disc)
    tax_pct = max(0.0, float(tax_percent))
    tax_amount = round(net_base * (tax_pct / 100.0), 2)
    grand_total = round(net_base + tax_amount, 2)
    ppl = max(1, int(people_count))
    per_person = round(grand_total / float(ppl), 2)

    return {
        "subtotal": sub,
        "discount": disc,
        "net_base": net_base,
        "tax_percent": tax_pct,
        "tax_amount": tax_amount,
        "grand_total": grand_total,
        "people_count": ppl,
        "per_person": per_person,
    }


def merge_collection_by_id(
    server_items: List[Dict[str, Any]],
    client_items: List[Dict[str, Any]],
    deleted_ids: List[str],
) -> List[Dict[str, Any]]:
    deleted_set = set(deleted_ids or [])
    merged: Dict[str, Dict[str, Any]] = {}

    for item in server_items or []:
        item_id = str(item.get("id", ""))
        if item_id and item_id not in deleted_set:
            merged[item_id] = item

    for item in client_items or []:
        item_id = str(item.get("id", ""))
        if not item_id or item_id in deleted_set:
            continue
        if item_id not in merged:
            merged[item_id] = item
        else:
            srv_ts = str(merged[item_id].get("updated_at", ""))
            cli_ts = str(item.get("updated_at", ""))
            if cli_ts >= srv_ts:
                merged[item_id] = item

    result = list(merged.values())
    result.sort(key=lambda x: str(x.get("date") or x.get("updated_at") or ""), reverse=True)
    return result


# ==============================================================================
# PYDANTIC REQUEST MODELS
# ==============================================================================
class GoogleVerifyRequest(BaseModel):
    id_token: Optional[str] = None
    access_token: Optional[str] = None


class GoogleEmailSessionRequest(BaseModel):
    email: str
    name: Optional[str] = None


class SyncMergeRequest(BaseModel):
    account_key: str = "shared_default_ledger"
    google_email: Optional[str] = ""
    google_sub: Optional[str] = ""
    client_revision: int = 0
    state: Dict[str, Any] = Field(default_factory=dict)


class QuantAnalyzeRequest(BaseModel):
    transactions: List[Dict[str, Any]] = Field(default_factory=list)
    goals: List[Dict[str, Any]] = Field(default_factory=list)
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    inflation_pct: float = 2.68


class ReceiptReconcileRequest(BaseModel):
    subtotal: float = 0.0
    discount: float = 0.0
    tax_percent: float = 0.0
    people_count: int = 1


class AiAnalyzeRequest(BaseModel):
    prompt: str
    lang: str = "ID"
    currency: str = "IDR"
    transactions: List[Dict[str, Any]] = Field(default_factory=list)
    goals: List[Dict[str, Any]] = Field(default_factory=list)
    budgets: List[Dict[str, Any]] = Field(default_factory=list)
    executed_actions: List[Dict[str, Any]] = Field(default_factory=list)
    realtime_pulse: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# HTML FRONTEND LOADER & API ROUTES
# ==============================================================================
def render_index_html_response() -> HTMLResponse:
    html_path = resolve_project_file("index.html")
    if not html_path:
        return HTMLResponse(
            "<h1>DND Finance index.html tidak ditemukan pada bundle server</h1>",
            status_code=404,
        )
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
@app.get("/api/index.py", response_class=HTMLResponse)
@app.get("/api/index", response_class=HTMLResponse)
async def serve_frontend():
    return render_index_html_response()


@app.get("/api/app/info")
async def get_app_info():
    """Metadata aplikasi & konfigurasi Google Client ID untuk frontend."""
    return {
        "status": "ok",
        "platform": "DND Finance",
        "version": "11.1.0",
        "project_id": "dnd-finance-3590e",
        "google_client_id": GOOGLE_CLIENT_ID,
        "developer": "Dinda",
        "instagram_handle": "@di_ndaaa29",
        "instagram_url": "https://www.instagram.com/di_ndaaa29?hvrq=MWNnaXczbm1xbGt4MQ==",
        "firebase_connected": db_engine.is_firebase_active,
        "cpp_engine_compiled": os.path.isfile(CPP_ENGINE_BINARY),
    }


@app.get("/api/system/files")
async def verify_bundled_files():
    """Endpoint diagnostik untuk memastikan seluruh berkas proyek terbaca oleh sistem."""
    tracked_files = [
        "index.html",
        "main.py",
        "app.py",
        "api/index.py",
        "quant_ai_engine.cpp",
        "database.rules.json",
        "firestore.rules",
        ".env",
        "env.example",
        "requirements.txt",
        "vercel.json",
        "run.sh",
        "run.bat",
    ]
    resolved_map = {fname: resolve_project_file(fname) for fname in tracked_files}

    cpp_path = resolved_map.get("quant_ai_engine.cpp")
    db_rules_path = resolved_map.get("database.rules.json")
    fs_rules_path = resolved_map.get("firestore.rules")

    parsed_db_rules = None
    if db_rules_path:
        try:
            with open(db_rules_path, "r", encoding="utf-8") as f:
                parsed_db_rules = json.load(f)
        except Exception:
            parsed_db_rules = None

    return {
        "status": "ok",
        "all_core_files_readable": all(
            bool(resolved_map.get(k))
            for k in ["index.html", "main.py", "api/index.py", "quant_ai_engine.cpp", "database.rules.json", "vercel.json"]
        ),
        "is_vercel": IS_VERCEL,
        "base_dir": BASE_DIR,
        "cwd": os.getcwd(),
        "loaded_env_path": LOADED_ENV_PATH,
        "files_found": {k: bool(v) for k, v in resolved_map.items()},
        "resolved_paths": resolved_map,
        "cpp_source_bytes": os.path.getsize(cpp_path) if cpp_path else 0,
        "cpp_binary_ready": os.path.isfile(CPP_ENGINE_BINARY),
        "database_rules_valid_json": bool(parsed_db_rules and "rules" in parsed_db_rules),
        "firestore_rules_readable": bool(fs_rules_path),
        "firebase_connected": db_engine.is_firebase_active,
        "firebase_url": FIREBASE_DATABASE_URL,
    }


@app.get("/api/firebase/status")
async def check_firebase_status():
    return await db_engine.test_connection()


@app.get("/api/realtime/pulse")
async def realtime_pulse_endpoint(force: bool = False):
    pulse = await get_live_realtime_pulse(force_refresh=force)
    return {"status": "ok", "pulse": pulse}


@app.post("/api/quant/analyze")
async def quant_analyze_endpoint(req: QuantAnalyzeRequest):
    result = run_cpp_quantitative_engine(
        req.transactions,
        req.goals,
        req.rules,
        inflation_pct=req.inflation_pct,
    )
    return {"status": "ok", "quant": result}


@app.post("/api/quant/reconcile-receipt")
async def quant_reconcile_receipt_endpoint(req: ReceiptReconcileRequest):
    result = run_cpp_receipt_reconciler(
        subtotal=req.subtotal,
        discount=req.discount,
        tax_percent=req.tax_percent,
        people_count=req.people_count,
    )
    return {"status": "ok", "reconciled": result}


@app.post("/api/auth/google/verify")
async def verify_direct_google_auth(req: GoogleVerifyRequest, response: Response):
    user_profile: Optional[Dict[str, Any]] = None

    async with httpx.AsyncClient(timeout=8.0) as client:
        if req.id_token:
            res = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": req.id_token},
            )
            if res.status_code == 200:
                info = res.json()
                if info.get("email"):
                    user_profile = {
                        "sub": info.get("sub", info["email"]),
                        "email": info["email"].lower().strip(),
                        "name": info.get("name") or info["email"].split("@")[0],
                        "picture": info.get("picture", ""),
                    }
        elif req.access_token:
            res = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {req.access_token}"},
            )
            if res.status_code == 200:
                info = res.json()
                if info.get("email"):
                    user_profile = {
                        "sub": info.get("sub", info["email"]),
                        "email": info["email"].lower().strip(),
                        "name": info.get("name") or info["email"].split("@")[0],
                        "picture": info.get("picture", ""),
                    }

    if not user_profile:
        raise HTTPException(status_code=401, detail="Invalid Google OAuth token")

    session_payload = {
        **user_profile,
        "exp": int(time.time()) + (30 * 24 * 3600),
    }
    cookie_val = sign_session_payload(session_payload)
    response.set_cookie(
        key="dnd_google_session",
        value=cookie_val,
        httponly=True,
        secure=IS_VERCEL,
        samesite="lax",
        max_age=30 * 24 * 3600,
    )
    return {"status": "ok", "user": user_profile}


@app.post("/api/auth/google/email-session")
async def create_google_email_session(req: GoogleEmailSessionRequest, response: Response):
    clean_email = (req.email or "").strip().lower()
    if "@" not in clean_email or len(clean_email) < 5:
        raise HTTPException(status_code=400, detail="Invalid email address")

    user_profile = {
        "sub": clean_email,
        "email": clean_email,
        "name": strip_emojis(req.name) or clean_email.split("@")[0],
        "picture": "",
    }
    session_payload = {
        **user_profile,
        "exp": int(time.time()) + (30 * 24 * 3600),
    }
    cookie_val = sign_session_payload(session_payload)
    response.set_cookie(
        key="dnd_google_session",
        value=cookie_val,
        httponly=True,
        secure=IS_VERCEL,
        samesite="lax",
        max_age=30 * 24 * 3600,
    )
    return {"status": "ok", "user": user_profile}


@app.get("/api/auth/google/me")
async def get_google_me(request: Request):
    token = request.cookies.get("dnd_google_session", "")
    user = verify_session_token(token)
    if not user:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": user}


@app.post("/api/auth/google/logout")
async def google_logout(response: Response):
    response.delete_cookie("dnd_google_session")
    return {"status": "ok"}


@app.post("/api/sync/merge")
async def sync_merge_workspace(req: SyncMergeRequest, request: Request):
    session_user = verify_session_token(request.cookies.get("dnd_google_session", ""))
    effective_account = req.account_key

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and not session_user:
        bearer_token = auth_header.split(" ", 1)[1].strip()
        if bearer_token:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    u_res = await client.get(
                        "https://www.googleapis.com/oauth2/v3/userinfo",
                        headers={"Authorization": f"Bearer {bearer_token}"},
                    )
                    if u_res.status_code == 200:
                        u_info = u_res.json()
                        if u_info.get("email"):
                            effective_account = u_info["email"].strip().lower()
            except Exception:
                pass
    elif session_user and session_user.get("email"):
        effective_account = session_user["email"].strip().lower()

    server_record = await db_engine.load_workspace(effective_account)
    server_state = server_record.get("state") or {}
    client_state = req.state or {}

    del_tx = list(set((server_state.get("deletedTxIds") or []) + (client_state.get("deletedTxIds") or [])))[-500:]
    del_goals = list(set((server_state.get("deletedGoalIds") or []) + (client_state.get("deletedGoalIds") or [])))[-200:]
    del_budgets = list(set((server_state.get("deletedBudgetIds") or []) + (client_state.get("deletedBudgetIds") or [])))[-200:]
    del_rules = list(set((server_state.get("deletedRuleIds") or []) + (client_state.get("deletedRuleIds") or [])))[-200:]
    del_banks = list(set((server_state.get("deletedBanks") or []) + (client_state.get("deletedBanks") or [])))[-100:]
    del_cats = list(set((server_state.get("deletedCategories") or []) + (client_state.get("deletedCategories") or [])))[-100:]

    merged_banks = [
        b for b in dict.fromkeys((server_state.get("banks") or []) + (client_state.get("banks") or []))
        if b not in del_banks
    ] or ["Bank Mandiri", "Bank BCA", "Bank BNI", "Bank Jago", "SeaBank", "Gopay", "Cash"]

    merged_cats = [
        c for c in dict.fromkeys((server_state.get("categories") or []) + (client_state.get("categories") or []))
        if c not in del_cats
    ] or ["Food", "Transport", "Trading", "Online Shopping", "Investment", "Salary", "Utilities"]

    merged_state = {
        "banks": merged_banks,
        "categories": merged_cats,
        "transactions": merge_collection_by_id(server_state.get("transactions", []), client_state.get("transactions", []), del_tx),
        "goals": merge_collection_by_id(server_state.get("goals", []), client_state.get("goals", []), del_goals),
        "budgets": merge_collection_by_id(server_state.get("budgets", []), client_state.get("budgets", []), del_budgets),
        "automationRules": merge_collection_by_id(server_state.get("automationRules", []), client_state.get("automationRules", []), del_rules),
        "customNickname": client_state.get("customNickname") or server_state.get("customNickname") or "",
        "deletedTxIds": del_tx,
        "deletedGoalIds": del_goals,
        "deletedBudgetIds": del_budgets,
        "deletedRuleIds": del_rules,
        "deletedBanks": del_banks,
        "deletedCategories": del_cats,
    }

    next_revision = max(int(server_record.get("revision", 0)), int(req.client_revision)) + 1
    wrote_firebase = await db_engine.save_workspace(effective_account, next_revision, merged_state)

    return {
        "status": "ok",
        "backend": "firebase" if wrote_firebase else db_engine.auth_method,
        "account_key": effective_account,
        "revision": next_revision,
        "state": merged_state,
    }


@app.post("/api/db/scan")
async def save_scan_record_to_firebase(payload: Dict[str, Any]):
    scan_id = sanitize_rtdb_key(str(payload.get("id", f"scan_{int(time.time())}")))
    safe_record = {
        **payload,
        "account_key": sanitize_rtdb_key(str(payload.get("account_key", "shared_default_ledger"))),
        "grand_total": float(payload.get("grand_total", 0)),
        "created_at": str(payload.get("created_at", datetime.now(timezone.utc).isoformat())),
    }
    saved = await db_engine.write_node(f"receipt_scans/{scan_id}", safe_record)
    return {"status": "ok", "saved_firebase": saved}


@app.post("/api/db/report")
async def save_report_record_to_firebase(payload: Dict[str, Any]):
    file_id = sanitize_rtdb_key(str(payload.get("id", f"rep_{int(time.time())}")))
    safe_record = {
        **payload,
        "account_key": sanitize_rtdb_key(str(payload.get("account_key", "shared_default_ledger"))),
        "filename": strip_emojis(payload.get("filename", "DND_Finance_Report")),
        "created_at": str(payload.get("created_at", datetime.now(timezone.utc).isoformat())),
    }
    saved = await db_engine.write_node(f"report_files/{file_id}", safe_record)
    return {"status": "ok", "saved_firebase": saved}


@app.get("/api/db/history/{account_key}")
async def get_db_history(account_key: str):
    safe_key = sanitize_rtdb_key(account_key)
    all_scans = await db_engine.read_node("receipt_scans")
    all_reports = await db_engine.read_node("report_files")

    scans: List[Dict[str, Any]] = []
    if isinstance(all_scans, dict):
        for item in all_scans.values():
            if isinstance(item, dict) and item.get("account_key") == safe_key:
                scans.append(item)
    scans.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)

    reports: List[Dict[str, Any]] = []
    if isinstance(all_reports, dict):
        for item in all_reports.values():
            if isinstance(item, dict) and item.get("account_key") == safe_key:
                reports.append(item)
    reports.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)

    return {
        "status": "ok",
        "receipt_scans": scans[:30],
        "report_files": reports[:40],
    }


@app.post("/api/ai/analyze")
async def ai_embedded_ea_agent(req: AiAnalyzeRequest):
    live_pulse = await get_live_realtime_pulse(force_refresh=False)
    if req.realtime_pulse:
        live_pulse = {**live_pulse, **req.realtime_pulse}

    quant = run_cpp_quantitative_engine(
        req.transactions,
        req.goals,
        [],
        inflation_pct=float(live_pulse.get("inflation_id_pct", 2.68)),
    )
    official_ref = await fetch_official_knowledge_snippet(req.prompt, req.lang)

    lang_rule = (
        "Jawab dalam Bahasa Indonesia yang jelas, terstruktur, cerdas, dan sangat mudah dipahami. DILARANG KERAS memakai emoji."
        if req.lang.upper() == "ID"
        else "Answer in clear, structured, intelligent, and easy-to-understand English. STRICTLY DO NOT use any emojis."
    )

    system_prompt = (
        "You are DND AI AGENT, an intelligent personal wealth advisor and real-time knowledge assistant for DND Finance "
        "powered by official macroeconomic feeds (World Bank, European Central Bank, Bank Indonesia), quantitative C++17 analytics, "
        "and official knowledge sources.\n"
        f"{lang_rule}\n"
        f"Currency: {req.currency}\n"
        f"Real-Time Macroeconomic, Gold & FX Pulse: {json.dumps(live_pulse)}\n"
        f"Quantitative Metrics (C++17 Engine): {json.dumps(quant)}\n"
        f"Goals: {json.dumps(req.goals[:10])}\n"
        f"Budgets: {json.dumps(req.budgets[:10])}\n"
        f"Recently Executed Actions: {json.dumps(req.executed_actions)}\n"
        f"Official Knowledge Context: {official_ref}"
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            ollama_res = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": EMBEDDED_OSS_MODEL,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": req.prompt},
                    ],
                },
            )
            if ollama_res.status_code == 200:
                o_data = ollama_res.json()
                reply_text = (o_data.get("message") or {}).get("content", "")
                if reply_text.strip():
                    return {"status": "ok", "reply": strip_emojis(reply_text), "quant": quant, "pulse": live_pulse}
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            oss_res = await client.post(
                OPEN_LLM_FALLBACK_URL,
                json={
                    "model": "qwen-coder",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": req.prompt},
                    ],
                    "temperature": 0.4,
                },
            )
            if oss_res.status_code == 200:
                data = oss_res.json()
                choices = data.get("choices") or []
                if choices:
                    content = (choices[0].get("message") or {}).get("content", "")
                    if content.strip():
                        return {"status": "ok", "reply": strip_emojis(content), "quant": quant, "pulse": live_pulse}
    except Exception:
        pass

    if official_ref:
        return {"status": "ok", "reply": strip_emojis(official_ref), "quant": quant, "pulse": live_pulse}

    inf_pct = live_pulse.get("inflation_id_pct", 2.68)
    usd_idr = live_pulse.get("usd_idr", 16250)
    gold_idr = live_pulse.get("gold_idr_gram", 1431500)

    if req.lang.upper() == "ID":
        fallback_reply = (
            f"**Analisis Real-Time DND AI AGENT**\n\n"
            f"- **Skor Kesehatan Finansial:** {quant['health_score']} / 100\n"
            f"- **Saldo Bersih:** {req.currency} {quant['net_balance']:,.0f}\n"
            f"- **Nilai Riil Saldo 1 Tahun (Inflasi {inf_pct}%):** {req.currency} {quant['real_balance_1y']:,.0f}\n"
            f"- **Kurs Live USD / IDR:** Rp {usd_idr:,.0f} | **Emas/gr:** Rp {gold_idr:,.0f}\n"
            f"- **Rasio Tabungan:** {quant['savings_rate_pct']}%\n"
            f"- **Burn Rate Harian:** {req.currency} {quant['daily_burn_rate']:,.0f} / hari\n"
            f"- **Target Dana Darurat Ideal (6 Bulan):** {req.currency} {quant['emergency_fund_ideal']:,.0f}\n\n"
            f"Pertanyaan Anda mengenai \"{strip_emojis(req.prompt)}\" telah dianalisis menggunakan data real-time terkini."
        )
    else:
        fallback_reply = (
            f"**DND AI AGENT Real-Time Analysis**\n\n"
            f"- **Financial Health Score:** {quant['health_score']} / 100\n"
            f"- **Net Balance:** {req.currency} {quant['net_balance']:,.2f}\n"
            f"- **Real 1-Year Balance (Inflation {inf_pct}% Adjusted):** {req.currency} {quant['real_balance_1y']:,.2f}\n"
            f"- **Live USD / IDR:** Rp {usd_idr:,.0f} | **Gold/gr:** Rp {gold_idr:,.0f}\n"
            f"- **Savings Rate:** {quant['savings_rate_pct']}%\n"
            f"- **Daily Burn Rate:** {req.currency} {quant['daily_burn_rate']:,.2f} / day\n"
            f"- **Ideal 6-Month Emergency Fund:** {req.currency} {quant['emergency_fund_ideal']:,.2f}\n\n"
            f"Your inquiry regarding \"{strip_emojis(req.prompt)}\" has been evaluated against real-time data."
        )

    return {"status": "ok", "reply": strip_emojis(fallback_reply), "quant": quant, "pulse": live_pulse}


@app.post("/api/scan/receipt")
async def scan_receipt_endpoint(file: UploadFile = File(...)):
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    extracted_text = ""
    try:
        import io
        from PIL import Image
        import pytesseract

        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        extracted_text = pytesseract.image_to_string(img, lang="eng+ind")
    except Exception:
        return JSONResponse({"status": "client_ocr_preferred", "data": None})

    lines = [strip_emojis(line).strip() for line in extracted_text.splitlines() if line.strip()]
    merchant = lines[0][:48] if lines else "Struk Belanja"
    items = []
    subtotal = 0.0
    discount = 0.0
    tax_percent = 0.0

    for line in lines[1:]:
        lower = line.lower()
        nums = re.findall(r"\d{1,3}(?:[.,]\d{3})+|\b\d{4,9}\b", line)
        val = float(nums[-1].replace(".", "").replace(",", "")) if nums else 0.0

        if re.search(r"(diskon|discount|voucher|potongan)", lower) and val > 0:
            discount += val
            continue
        if re.search(r"(ppn|pb1|tax|pajak|service|servis)", lower):
            pct_m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)
            if pct_m:
                tax_percent += float(pct_m.group(1).replace(",", "."))
            continue
        if val > 0 and not re.search(r"(total|tunai|cash|kembalian)", lower):
            if 500 <= val <= 500_000_000:
                name = re.sub(r"\d{1,3}(?:[.,]\d{3})+|\b\d{4,9}\b", "", line).strip() or "Item"
                items.append({"name": name[:40], "qty": 1, "price": val})
                subtotal += val

    reconciled = run_cpp_receipt_reconciler(subtotal, discount, tax_percent, 1)

    return {
        "status": "ok",
        "data": {
            "merchant": merchant,
            "items": items,
            "subtotal": reconciled["subtotal"],
            "discount": reconciled["discount"],
            "tax_percent": reconciled["tax_percent"],
            "amount": reconciled["grand_total"],
            "category": "Food",
        },
    }


@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa_catch_all(full_path: str, request: Request):
    """Menyajikan index.html untuk seluruh rute SPA non-API agar tidak pernah 404."""
    clean_p = (full_path or "").strip("/")
    if clean_p in ("", "index.html", "api", "api/index.py", "api/index", "index.py") or clean_p.startswith("api/index"):
        return render_index_html_response()
    if clean_p.startswith("api/"):
        accept_header = (request.headers.get("accept") or "").lower()
        if "text/html" in accept_header and "application/json" not in accept_header:
            return render_index_html_response()
        raise HTTPException(status_code=404, detail="API endpoint not found")
    return render_index_html_response()


class _RootVercelAsgiBridge:
    """Lapisan ASGI terluar untuk menormalisasi path Vercel sebelum diproses oleh router FastAPI."""

    def __init__(self, inner_app):
        self.inner_app = inner_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            raw_path = (scope.get("path") or "/").rstrip("/") or "/"
            scope["root_path"] = ""
            if raw_path in ("/api/index.py", "/api/index", "/index.py", "/api"):
                scope["path"] = "/"
                scope["raw_path"] = b"/"
            elif raw_path.startswith("/api/index.py/"):
                trimmed = raw_path[len("/api/index.py"):] or "/"
                scope["path"] = trimmed
                scope["raw_path"] = trimmed.encode("utf-8")
        await self.inner_app(scope, receive, send)


app = _RootVercelAsgiBridge(app)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
