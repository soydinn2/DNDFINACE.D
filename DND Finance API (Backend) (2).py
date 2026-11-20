from fastapi import FastAPI, BackgroundTasks, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
from urllib.parse import urlencode
import pandas as pd
import sqlite3
import os
import glob
import re
import json
import base64
import secrets
import urllib.request
import time
import ctypes
import subprocess

app = FastAPI(
    title="DND Finance Full-Stack API",
    description="Full-Stack Backend with C++17 Quantitative Engine, Direct Google OAuth 2.0 Auto-Detection, Receipt OCR/Vision & Financial Intelligence",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "dnd_finance.db"
DEFAULT_BANKS = ["Bank Mandiri", "Bank BCA", "Bank BNI", "Bank Jago", "Corporate Bank", "Gopay", "Cash"]
DEFAULT_CATEGORIES = ["Food", "Transport", "Trading", "Online Shopping", "Investment", "Salary", "Utilities"]


class _CppMetricsStruct(ctypes.Structure):
    _fields_ = [
        ("total_income", ctypes.c_double),
        ("total_expense", ctypes.c_double),
        ("total_withdrawal", ctypes.c_double),
        ("net_balance", ctypes.c_double),
        ("health_score", ctypes.c_int),
        ("vat_11_percent", ctypes.c_double),
        ("fee_1_5_percent", ctypes.c_double),
    ]


class _CppAutomationStruct(ctypes.Structure):
    _fields_ = [
        ("triggered", ctypes.c_int),
        ("allocated_amount", ctypes.c_double),
    ]


def _load_cpp_engine():
    """Kompilasi otomatis & muat pustaka C++17 (dnd_engine.cpp) jika compiler tersedia"""
    cpp_src = "dnd_engine.cpp"
    lib_name = "dnd_engine.dll" if os.name == "nt" else "./dnd_engine.so"
    if os.path.exists(cpp_src):
        need_build = not os.path.exists(lib_name) or (
            os.path.getmtime(cpp_src) > os.path.getmtime(lib_name)
        )
        if need_build:
            try:
                subprocess.run(
                    ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", cpp_src, "-o", lib_name],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
    if os.path.exists(lib_name):
        try:
            lib = ctypes.CDLL(lib_name)
            lib.dnd_compute_ledger_metrics.argtypes = [
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_int,
            ]
            lib.dnd_compute_ledger_metrics.restype = _CppMetricsStruct

            lib.dnd_compute_split_bill.argtypes = [
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_double),
            ]
            lib.dnd_compute_split_bill.restype = None

            lib.dnd_evaluate_automation_rule.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
            ]
            lib.dnd_evaluate_automation_rule.restype = _CppAutomationStruct
            return lib
        except Exception:
            return None
    return None


CPP_ENGINE = _load_cpp_engine()


def _compute_metrics_with_cpp(tx_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Menghitung metrik buku besar via C++17 native library dengan fallback Python"""
    n = len(tx_rows)
    if CPP_ENGINE is not None and n > 0:
        amounts = (ctypes.c_double * n)()
        types = (ctypes.c_int * n)()
        type_map = {"income": 1, "expense": 2, "withdrawal": 3, "transfer": 4}
        for i, row in enumerate(tx_rows):
            amounts[i] = float(row.get("amount") or 0.0)
            types[i] = type_map.get(str(row.get("type") or "").lower(), 2)
        res = CPP_ENGINE.dnd_compute_ledger_metrics(amounts, types, n)
        return {
            "engine": "cpp17_native",
            "total_income": res.total_income,
            "total_expense": res.total_expense,
            "total_withdrawal": res.total_withdrawal,
            "net_balance": res.net_balance,
            "health_score": res.health_score,
            "vat_11_percent": res.vat_11_percent,
            "fee_1_5_percent": res.fee_1_5_percent,
        }

    inc = sum(float(t.get("amount") or 0.0) for t in tx_rows if t.get("type") == "income")
    exp = sum(float(t.get("amount") or 0.0) for t in tx_rows if t.get("type") == "expense")
    wd = sum(float(t.get("amount") or 0.0) for t in tx_rows if t.get("type") == "withdrawal")
    outflow = exp + wd
    bal = inc - outflow
    score = max(0, min(100, round(((inc - outflow) / inc) * 100))) if inc > 0 else 0
    return {
        "engine": "python_fallback",
        "total_income": inc,
        "total_expense": exp,
        "total_withdrawal": wd,
        "net_balance": bal,
        "health_score": score,
        "vat_11_percent": inc * 0.11,
        "fee_1_5_percent": (inc + outflow) * 0.015,
    }


def _load_google_oauth_credentials() -> tuple[str, str]:
    """Deteksi otomatis GOOGLE_CLIENT_ID & GOOGLE_CLIENT_SECRET dari env, .env, atau file client_secret*.json"""
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    csec = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    if (not cid or not csec) and os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "GOOGLE_CLIENT_ID" and not cid:
                        cid = v
                    elif k == "GOOGLE_CLIENT_SECRET" and not csec:
                        csec = v
        except Exception:
            pass

    if not cid:
        for json_file in sorted(glob.glob("client_secret*.json") + glob.glob("google_oauth*.json")):
            try:
                with open(json_file, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
                    block = data.get("web") or data.get("installed") or data
                    if block.get("client_id"):
                        cid = str(block["client_id"]).strip()
                        csec = str(block.get("client_secret", csec)).strip()
                        break
            except Exception:
                continue

    return cid, csec


def _get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS transactions
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         amount REAL, type TEXT, category TEXT, account TEXT, to_account TEXT DEFAULT '',
         admin_fee REAL DEFAULT 0, note TEXT, currency TEXT DEFAULT 'IDR', date TEXT)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS users
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         google_sub TEXT UNIQUE, email TEXT, name TEXT, nickname TEXT DEFAULT '',
         picture TEXT, last_login TEXT, session_token TEXT DEFAULT '')
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS user_workspace_state
        (email TEXT PRIMARY KEY,
         state_json TEXT,
         updated_at TEXT)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS custom_banks
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         name TEXT UNIQUE)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS custom_categories
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         name TEXT UNIQUE)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS automation_rules
        (id TEXT PRIMARY KEY,
         name TEXT,
         action_type TEXT,
         config_json TEXT,
         enabled INTEGER DEFAULT 1,
         run_count INTEGER DEFAULT 0)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS savings_goals
        (id TEXT PRIMARY KEY,
         name TEXT,
         target REAL,
         current REAL)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS category_budgets
        (id TEXT PRIMARY KEY,
         category TEXT,
         limit_amount REAL)
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS employees
        (id TEXT PRIMARY KEY,
         name TEXT,
         role TEXT,
         salary REAL)
        '''
    )

    c.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in c.fetchall()}
    if "nickname" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT ''")
    if "session_token" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN session_token TEXT DEFAULT ''")

    c.execute("PRAGMA table_info(transactions)")
    tx_cols = {row[1] for row in c.fetchall()}
    if "to_account" not in tx_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN to_account TEXT DEFAULT ''")
    if "admin_fee" not in tx_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN admin_fee REAL DEFAULT 0")
    if "currency" not in tx_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN currency TEXT DEFAULT 'IDR'")

    for bank_name in DEFAULT_BANKS:
        c.execute("INSERT OR IGNORE INTO custom_banks (name) VALUES (?)", (bank_name,))

    c.execute("DELETE FROM custom_categories WHERE LOWER(name) IN ('otomotif', 'automotive')")
    for cat_name in DEFAULT_CATEGORIES:
        c.execute("INSERT OR IGNORE INTO custom_categories (name) VALUES (?)", (cat_name,))

    conn.commit()
    conn.close()


init_db()


def _upsert_google_user(sub: str, email: str, name: str, picture: str, nickname: Optional[str] = None) -> tuple[str, str]:
    """Menyimpan atau memperbarui profil akun Google terverifikasi dan mengembalikan (nickname, session_token)"""
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT nickname FROM users WHERE google_sub = ? OR LOWER(email) = ?", (sub, email.lower()))
    existing = c.fetchone()
    final_nickname = (
        nickname.strip()
        if nickname is not None
        else (existing["nickname"] if existing and existing["nickname"] else "")
    )
    session_token = secrets.token_urlsafe(32)
    c.execute(
        '''
        INSERT INTO users (google_sub, email, name, nickname, picture, last_login, session_token)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(google_sub) DO UPDATE SET
            email = excluded.email,
            name = excluded.name,
            nickname = excluded.nickname,
            picture = excluded.picture,
            last_login = excluded.last_login,
            session_token = excluded.session_token
        ''',
        (sub, email, name, final_nickname, picture, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_token),
    )
    conn.commit()
    conn.close()
    return final_nickname, session_token


def _call_gemini_backend(parts: List[Dict[str, Any]], system_text: str = "", json_mode: bool = False) -> Optional[str]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload: Dict[str, Any] = {"contents": [{"parts": parts}]}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


class Transaction(BaseModel):
    amount: float
    type: str
    category: str
    account: str
    to_account: Optional[str] = ""
    admin_fee: Optional[float] = 0.0
    note: Optional[str] = ""
    currency: Optional[str] = "IDR"
    date: Optional[str] = None


class BankAccount(BaseModel):
    name: str


class CategoryItem(BaseModel):
    name: str


class GoogleAuthSession(BaseModel):
    access_token: str
    sub: Optional[str] = ""
    email: Optional[str] = ""
    name: Optional[str] = ""
    nickname: Optional[str] = ""
    picture: Optional[str] = ""


class GoogleIdTokenPayload(BaseModel):
    id_token: str


class NicknameUpdatePayload(BaseModel):
    email: str
    nickname: str


class WorkspaceSyncPayload(BaseModel):
    email: str
    state: Dict[str, Any]


class ReceiptScanRequest(BaseModel):
    image_base64: str
    mime_type: Optional[str] = "image/jpeg"


class AIAnalysisRequest(BaseModel):
    prompt: str
    lang: Optional[str] = "ID"
    currency: Optional[str] = "IDR"
    financial_context: Optional[str] = ""
    transactions: Optional[List[Dict[str, Any]]] = None


class AutomationRulePayload(BaseModel):
    id: str
    name: str
    actionType: str
    minAmount: Optional[float] = 0
    percent: Optional[float] = 20
    targetGoalName: Optional[str] = ""
    sourceWallet: Optional[str] = ""
    destWallet: Optional[str] = ""
    thresholdAmount: Optional[float] = 500000
    enabled: Optional[bool] = True
    runCount: Optional[int] = 0


class SplitBillPayload(BaseModel):
    subtotal: float
    tax_percent: Optional[float] = 0.0
    people: int = 2


@app.get("/")
async def serve_frontend():
    """Menyajikan antarmuka HTML + CSS + JS langsung dari server Python"""
    if os.path.exists("index.html"):
        return FileResponse("index.html", media_type="text/html")
    return {"status": "ok", "message": "DND Finance Full-Stack API Running"}


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "DND Finance Full-Stack API",
        "cpp_engine_active": CPP_ENGINE is not None,
    }


@app.get("/api/engine/metrics")
async def get_cpp_engine_metrics():
    """Endpoint metrik finansial yang dihitung oleh mesin C++17"""
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT amount, type FROM transactions")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return _compute_metrics_with_cpp(rows)


@app.post("/api/engine/split-bill")
async def compute_split_bill_api(payload: SplitBillPayload):
    """Kalkulasi patungan menggunakan mesin C++17"""
    sub = max(0.0, float(payload.subtotal))
    tax = max(0.0, float(payload.tax_percent or 0.0))
    ppl = max(1, int(payload.people))

    if CPP_ENGINE is not None:
        grand_out = ctypes.c_double(0.0)
        per_out = ctypes.c_double(0.0)
        CPP_ENGINE.dnd_compute_split_bill(sub, tax, ppl, ctypes.byref(grand_out), ctypes.byref(per_out))
        return {"engine": "cpp17_native", "grand_total": grand_out.value, "per_person": per_out.value}

    grand = sub + (sub * (tax / 100.0))
    return {"engine": "python_fallback", "grand_total": grand, "per_person": grand / ppl}


@app.get("/api/auth/config")
async def get_google_oauth_config():
    client_id, _ = _load_google_oauth_credentials()
    return {"google_client_id": client_id, "configured": bool(client_id)}


@app.get("/api/auth/google/auto-detect")
async def auto_detect_google_account(response: Response):
    """Deteksi otomatis profil akun Google aktif yang sudah terverifikasi di database server"""
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT google_sub, email, name, nickname, picture FROM users WHERE email != '' ORDER BY last_login DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if not row or not row["email"]:
        return {"authenticated": False, "user": None}
    _, session_token = _upsert_google_user(
        row["google_sub"] or row["email"],
        row["email"],
        row["name"] or row["email"].split("@")[0],
        row["picture"] or "",
    )
    response.set_cookie(key="dnd_google_session", value=session_token, httponly=True, samesite="lax", max_age=2592000)
    return {
        "authenticated": True,
        "user": {
            "sub": row["google_sub"] or row["email"],
            "email": row["email"],
            "name": row["name"] or row["email"].split("@")[0],
            "nickname": row["nickname"] or "",
            "picture": row["picture"] or "",
        },
    }


@app.get("/api/auth/google/me")
async def get_current_google_user(request: Request):
    """Deteksi otomatis sesi akun Google aktif dari cookie server"""
    session_token = request.cookies.get("dnd_google_session", "").strip()
    conn = _get_db_connection()
    c = conn.cursor()
    row = None
    if session_token:
        c.execute("SELECT google_sub, email, name, nickname, picture FROM users WHERE session_token = ?", (session_token,))
        row = c.fetchone()
    conn.close()
    if not row or not row["email"]:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": True,
        "user": {
            "sub": row["google_sub"] or row["email"],
            "email": row["email"],
            "name": row["name"] or row["email"].split("@")[0],
            "nickname": row["nickname"] or "",
            "picture": row["picture"] or "",
        },
    }


@app.get("/api/auth/google/login")
async def google_oauth_redirect(request: Request):
    client_id, _ = _load_google_oauth_credentials()
    if not client_id:
        return RedirectResponse(url="/?google_auth=unconfigured")
    redirect_uri = str(request.base_url).rstrip("/") + "/api/auth/google/callback"
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    })
    return RedirectResponse(url=f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/api/auth/google/callback")
async def google_oauth_callback(request: Request, code: Optional[str] = None):
    if not code:
        return RedirectResponse(url="/?google_auth=cancelled")
    client_id, client_secret = _load_google_oauth_credentials()
    redirect_uri = str(request.base_url).rstrip("/") + "/api/auth/google/callback"

    try:
        token_data = urlencode({
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode("utf-8")
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=12) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))
        access_token = tokens.get("access_token", "")

        info_req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        with urllib.request.urlopen(info_req, timeout=10) as uresp:
            g_info = json.loads(uresp.read().decode("utf-8"))

        sub = g_info.get("sub", "")
        email = g_info.get("email", "")
        name = g_info.get("name", "DND User")
        picture = g_info.get("picture", "")
        _, session_token = _upsert_google_user(sub, email, name, picture)

        qs = urlencode({
            "google_auth": "success",
            "sub": sub,
            "email": email,
            "name": name,
            "picture": picture,
            "access_token": access_token,
        })
        redirect_resp = RedirectResponse(url=f"/?{qs}")
        redirect_resp.set_cookie(key="dnd_google_session", value=session_token, httponly=True, samesite="lax", max_age=2592000)
        return redirect_resp
    except Exception:
        return RedirectResponse(url="/?google_auth=error")


@app.post("/api/auth/google/id-token")
async def verify_google_id_token_endpoint(payload: GoogleIdTokenPayload, response: Response):
    """Verifikasi Google One Tap JWT ID Token langsung via Google TokenInfo API"""
    token = (payload.id_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="id_token required")
    req = urllib.request.Request(
        f"https://oauth2.googleapis.com/tokeninfo?id_token={token}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            g_info = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Google ID token tidak valid: {exc}")

    email = g_info.get("email", "")
    if not email:
        raise HTTPException(status_code=401, detail="Email Google tidak ditemukan")
    sub = g_info.get("sub", email)
    name = (g_info.get("name") or email.split("@")[0]).strip()
    picture = g_info.get("picture", "")
    saved_nickname, session_token = _upsert_google_user(sub, email, name, picture)
    response.set_cookie(key="dnd_google_session", value=session_token, httponly=True, samesite="lax", max_age=2592000)
    return {
        "status": "authenticated",
        "user": {
            "sub": sub,
            "email": email,
            "name": name,
            "nickname": saved_nickname,
            "picture": picture,
        },
    }


@app.post("/api/auth/google")
async def verify_google_auth(session: GoogleAuthSession, response: Response):
    """Verifikasi OAuth Access Token langsung dengan server Google"""
    if not session.access_token or not session.access_token.strip():
        raise HTTPException(status_code=401, detail="Google OAuth access_token required")

    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {session.access_token.strip()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            g_info = json.loads(resp.read().decode("utf-8"))
            sub = g_info.get("sub", "")
            email = g_info.get("email", "")
            name = g_info.get("name", "")
            picture = g_info.get("picture", "")
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token Google tidak valid: {exc}")

    if not email:
        raise HTTPException(status_code=401, detail="Email akun Google tidak ditemukan")

    final_name = (name or email.split("@")[0]).strip()
    saved_nickname, session_token = _upsert_google_user(sub or email, email, final_name, picture, session.nickname or None)
    response.set_cookie(key="dnd_google_session", value=session_token, httponly=True, samesite="lax", max_age=2592000)
    return {
        "status": "authenticated",
        "user": {
            "sub": sub or email,
            "email": email,
            "name": final_name,
            "nickname": saved_nickname,
            "picture": picture,
        },
    }


@app.post("/api/auth/google/logout")
async def logout_google_session(request: Request, response: Response):
    session_token = request.cookies.get("dnd_google_session", "").strip()
    if session_token:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET session_token = '' WHERE session_token = ?", (session_token,))
        conn.commit()
        conn.close()
    response.delete_cookie("dnd_google_session")
    return {"status": "logged_out"}


@app.post("/api/auth/google/nickname")
async def update_google_nickname(payload: NicknameUpdatePayload):
    """Memperbarui nama panggilan kustom tanpa mengubah identitas Google terverifikasi"""
    clean_email = payload.email.strip().lower()
    if not clean_email:
        raise HTTPException(status_code=400, detail="Email required")
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET nickname = ? WHERE LOWER(email) = ?", (payload.nickname.strip(), clean_email))
    conn.commit()
    conn.close()
    return {"status": "updated", "email": clean_email, "nickname": payload.nickname.strip()}


@app.post("/api/sync/push")
async def push_workspace_state(payload: WorkspaceSyncPayload):
    """Menyimpan seluruh snapshot data keuangan per akun Google di SQLite"""
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    conn = _get_db_connection()
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        '''
        INSERT INTO user_workspace_state (email, state_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
        ''',
        (email, json.dumps(payload.state), now_str),
    )
    conn.commit()
    conn.close()
    return {"status": "synced", "email": email, "updated_at": now_str}


@app.get("/api/sync/pull")
async def pull_workspace_state(email: str):
    """Mengambil seluruh data keuangan tersimpan milik akun Google tertentu"""
    clean_email = email.strip().lower()
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT state_json, updated_at FROM user_workspace_state WHERE email = ?", (clean_email,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {"status": "empty", "state": None}
    try:
        return {"status": "ok", "state": json.loads(row[0]), "updated_at": row[1]}
    except Exception:
        return {"status": "empty", "state": None}


@app.get("/api/banks")
async def get_banks():
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name FROM custom_banks ORDER BY id ASC")
    banks = [row[0] for row in c.fetchall()]
    conn.close()
    return {"banks": banks or DEFAULT_BANKS}


@app.post("/api/banks")
async def add_bank(bank: BankAccount):
    name = bank.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nama wallet/bank tidak boleh kosong")
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO custom_banks (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
        return {"status": "success", "name": name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan wallet/bank: {exc}")


@app.delete("/api/banks/{bank_name}")
async def delete_bank(bank_name: str):
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM custom_banks WHERE name = ?", (bank_name,))
        conn.commit()
        conn.close()
        return {"status": "deleted", "name": bank_name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus wallet/bank: {exc}")


@app.get("/api/categories")
async def get_categories():
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name FROM custom_categories ORDER BY id ASC")
    cats = [row[0] for row in c.fetchall() if row[0].lower() not in ("otomotif", "automotive")]
    conn.close()
    return {"categories": cats or DEFAULT_CATEGORIES}


@app.post("/api/categories")
async def add_category(cat: CategoryItem):
    name = cat.name.strip()
    if not name or name.lower() in ("otomotif", "automotive"):
        raise HTTPException(status_code=400, detail="Nama kategori tidak valid")
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO custom_categories (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
        return {"status": "success", "name": name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan kategori: {exc}")


@app.delete("/api/categories/{cat_name}")
async def delete_category(cat_name: str):
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM custom_categories WHERE name = ?", (cat_name,))
        conn.commit()
        conn.close()
        return {"status": "deleted", "name": cat_name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus kategori: {exc}")


@app.get("/api/transactions")
async def get_transactions():
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC")
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return {"transactions": rows}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal mengambil transaksi: {exc}")


@app.post("/api/transactions")
async def create_transaction(tx: Transaction):
    if tx.amount <= 0:
        raise HTTPException(status_code=400, detail="Nominal transaksi harus lebih dari 0")
    tx_date = tx.date or datetime.now().strftime("%Y-%m-%d %H:%M")
    clean_cat = "Transport" if tx.category.strip().lower() in ("otomotif", "automotive") else tx.category
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO transactions (amount, type, category, account, to_account, admin_fee, note, currency, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                tx.amount,
                tx.type,
                clean_cat,
                tx.account,
                tx.to_account or "",
                tx.admin_fee or 0.0,
                tx.note or "",
                tx.currency or "IDR",
                tx_date,
            ),
        )
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return {"status": "success", "id": new_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan transaksi: {exc}")


@app.delete("/api/transactions/{tx_id}")
async def delete_transaction(tx_id: int):
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        conn.commit()
        conn.close()
        return {"status": "deleted", "id": tx_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus transaksi: {exc}")


@app.get("/api/macros")
async def get_macros():
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, action_type, config_json, enabled, run_count FROM automation_rules ORDER BY rowid ASC")
    rules = []
    for row in c.fetchall():
        cfg = json.loads(row["config_json"] or "{}")
        cfg.update(
            {
                "id": row["id"],
                "name": row["name"],
                "actionType": row["action_type"],
                "enabled": bool(row["enabled"]),
                "runCount": int(row["run_count"] or 0),
            }
        )
        rules.append(cfg)
    conn.close()
    return {"macros": rules}


@app.post("/api/macros")
async def upsert_macro(rule: AutomationRulePayload):
    conn = _get_db_connection()
    c = conn.cursor()
    cfg = rule.model_dump() if hasattr(rule, "model_dump") else rule.dict()
    c.execute(
        '''
        INSERT INTO automation_rules (id, name, action_type, config_json, enabled, run_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            action_type = excluded.action_type,
            config_json = excluded.config_json,
            enabled = excluded.enabled,
            run_count = excluded.run_count
        ''',
        (rule.id, rule.name, rule.actionType, json.dumps(cfg), 1 if rule.enabled else 0, rule.runCount or 0),
    )
    conn.commit()
    conn.close()
    return {"status": "saved", "id": rule.id}


@app.delete("/api/macros/{rule_id}")
async def delete_macro_rule(rule_id: str):
    conn = _get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": rule_id}


@app.post("/api/ai/analyze")
async def analyze_financial_data(req: AIAnalysisRequest):
    """Endpoint Analisis Keuangan Lengkap berbasis data transaksi, wallet, anggaran & target"""
    tx_rows = req.transactions if req.transactions is not None else []
    metrics = _compute_metrics_with_cpp(tx_rows)
    inc = metrics["total_income"]
    exp = metrics["total_expense"] + metrics["total_withdrawal"]
    bal = metrics["net_balance"]
    ratio = metrics["health_score"]

    ai_text = _call_gemini_backend(
        [
            {"text": f"Pertanyaan/Instruksi: {req.prompt}\n\nData Keuangan:\n{req.financial_context or ''}"},
        ],
        system_text=(
            "Anda adalah analis keuangan profesional DND Finance. Jawab secara ringkas, terstruktur, "
            "berbasis data angka aktual, dan gunakan bahasa yang sama dengan pengguna."
        ),
    )

    if ai_text:
        return {"status": "success", "source": "gemini_api", "analysis": ai_text}

    if len(tx_rows) == 0:
        fallback_text = (
            "### Status Data: 0 Transaksi\n"
            "- Catatan keuangan Anda saat ini belum memiliki transaksi.\n"
            "- Catat Pemasukan dan Pengeluaran agar analisis rasio kas, saldo wallet, dan target tabungan dapat dihitung."
            if req.lang == "ID"
            else "### Data Status: 0 Transactions\n"
            "- Your ledger currently has no recorded transactions.\n"
            "- Log Income and Expenses so cash flow ratios, wallet balances, and savings projections can be calculated."
        )
    else:
        df = pd.DataFrame(tx_rows)
        exp_df = df[df["type"] == "expense"] if "type" in df.columns else pd.DataFrame()
        cat_series = (
            exp_df.groupby("category")["amount"].sum().sort_values(ascending=False)
            if not exp_df.empty and "category" in exp_df.columns
            else pd.Series(dtype=float)
        )
        top_cat_name = str(cat_series.index[0]) if len(cat_series) > 0 else "-"
        top_cat_val = float(cat_series.iloc[0]) if len(cat_series) > 0 else 0.0
        curr_prefix = "Rp " if (req.currency or "IDR") == "IDR" else f"{req.currency} "

        if req.lang == "ID":
            fallback_text = (
                f"### Audit Keuangan Menyeluruh ({metrics['engine']})\n"
                f"- **Total Saldo Bersih:** {curr_prefix}{bal:,.0f} (Rasio Simpanan: **{ratio}%**)\n"
                f"- **Pemasukan vs Pengeluaran:** {curr_prefix}{inc:,.0f} vs {curr_prefix}{exp:,.0f}\n"
                f"- **Pos Pengeluaran Tertinggi:** {top_cat_name} ({curr_prefix}{top_cat_val:,.0f})\n"
                f"### Rekomendasi Tindakan\n"
                f"- Pertahankan rasio simpanan minimal **20%** setiap bulan dan aktifkan aturan **Otomasi** untuk menyisihkan dana secara otomatis."
            )
        else:
            fallback_text = (
                f"### Executive Financial Audit ({metrics['engine']})\n"
                f"- **Net Total Balance:** {curr_prefix}{bal:,.0f} (Savings Ratio: **{ratio}%**)\n"
                f"- **Income vs Expenses:** {curr_prefix}{inc:,.0f} vs {curr_prefix}{exp:,.0f}\n"
                f"- **Top Expense Category:** {top_cat_name} ({curr_prefix}{top_cat_val:,.0f})\n"
                f"### Actionable Recommendations\n"
                f"- Maintain a savings ratio above **20%** and activate **Automation** rules to allocate funds automatically."
            )

    return {"status": "success", "source": metrics["engine"], "analysis": fallback_text}


@app.post("/api/scan/receipt")
async def scan_receipt_endpoint(req: ReceiptScanRequest):
    """Endpoint Full-Stack Python untuk ekstraksi kamera/struk belanja (Vision & OCR)"""
    prompt_text = (
        'Ekstrak total pembayaran akhir, kategori (pilih satu: Food, Transport, Utilities, Online Shopping, Health & Lifestyle, Shopping, Office Rent, SaaS/Software), '
        'dan nama toko/merchant singkat dari gambar struk ini dalam JSON: {"amount": 0, "category": "Food", "note": "..."}'
    )
    gemini_json = _call_gemini_backend(
        [
            {"text": prompt_text},
            {"inlineData": {"mimeType": req.mime_type or "image/jpeg", "data": req.image_base64}},
        ],
        json_mode=True,
    )
    if gemini_json:
        try:
            parsed = json.loads(gemini_json)
            if float(parsed.get("amount", 0)) > 0:
                cat = parsed.get("category", "Food")
                if str(cat).lower() in ("otomotif", "automotive"):
                    cat = "Transport"
                return {
                    "status": "success",
                    "amount": float(parsed["amount"]),
                    "category": cat,
                    "note": parsed.get("note", "Pindai Struk"),
                }
        except Exception:
            pass

    try:
        import io
        from PIL import Image
        import pytesseract

        img_bytes = base64.b64decode(req.image_base64)
        img = Image.open(io.BytesIO(img_bytes))
        raw_text = pytesseract.image_to_string(img)
        matches = re.findall(r"\b\d{1,3}(?:[.,]\d{3})+\b", raw_text)
        nums = [int(re.sub(r"[.,]", "", m)) for m in matches]
        valid_nums = [n for n in nums if n >= 1000]
        if valid_nums:
            lines = [ln.strip() for ln in raw_text.splitlines() if len(ln.strip()) > 3 and not ln.strip().isdigit()]
            merchant = lines[0][:40] if lines else "Pindai Struk"
            return {
                "status": "success",
                "amount": max(valid_nums),
                "category": "Food",
                "note": merchant,
            }
    except Exception:
        pass

    return {"status": "fallback", "amount": 0}


@app.get("/api/export/excel")
async def export_excel_report(background_tasks: BackgroundTasks, lang: str = "ID", email: str = ""):
    """Export Excel Lengkap (Overview Analysis + Wallet Balances + All Transactions)"""
    is_id = lang.upper() == "ID"
    conn = _get_db_connection()
    df = pd.read_sql_query("SELECT * FROM transactions ORDER BY date DESC, id DESC", conn)
    banks_df = pd.read_sql_query("SELECT name FROM custom_banks ORDER BY id ASC", conn)
    if email.strip():
        c = conn.cursor()
        c.execute("SELECT state_json FROM user_workspace_state WHERE email = ?", (email.strip().lower(),))
        st_row = c.fetchone()
        if st_row and st_row[0]:
            try:
                st_data = json.loads(st_row[0])
                if isinstance(st_data.get("transactions"), list) and len(st_data["transactions"]) > 0:
                    df = pd.DataFrame(st_data["transactions"])
                    if "toAccount" in df.columns and "to_account" not in df.columns:
                        df["to_account"] = df["toAccount"]
                    if "adminFee" in df.columns and "admin_fee" not in df.columns:
                        df["admin_fee"] = df["adminFee"]
            except Exception:
                pass
    conn.close()

    bank_names = banks_df["name"].tolist() if not banks_df.empty else DEFAULT_BANKS
    wallet_balances = {b: 0.0 for b in bank_names}

    if not df.empty:
        for _, r in df.iterrows():
            amt = float(r.get("amount") or 0.0)
            fee = float(r.get("admin_fee") or 0.0)
            src = r.get("account") or bank_names[0]
            wallet_balances.setdefault(src, 0.0)
            if r["type"] == "income":
                wallet_balances[src] += amt
            elif r["type"] in ("expense", "withdrawal"):
                wallet_balances[src] -= amt
            elif r["type"] == "transfer":
                dst = r.get("to_account") or ""
                wallet_balances[src] -= amt + fee
                if dst:
                    wallet_balances.setdefault(dst, 0.0)
                    wallet_balances[dst] += amt
    else:
        df = pd.DataFrame(
            [
                {
                    "id": 0,
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "category": "Belum Ada Data" if is_id else "No Data",
                    "type": "income",
                    "account": "-",
                    "to_account": "",
                    "note": "Belum ada transaksi tercatat" if is_id else "No transactions recorded yet",
                    "amount": 0.0,
                }
            ]
        )

    export_path = f"DND_Financial_Report_{lang.upper()}_{int(time.time())}.xlsx"
    writer = pd.ExcelWriter(export_path, engine="xlsxwriter")
    workbook = writer.book

    sheet_overview = "Ringkasan Analisis" if is_id else "Overview Analysis"
    sheet_wallets = "Saldo Wallet" if is_id else "Wallet Balances"
    sheet_tx = "Semua Transaksi" if is_id else "All Transactions"

    ws_overview = workbook.add_worksheet(sheet_overview)
    ws_wallets = workbook.add_worksheet(sheet_wallets)
    ws_tx = workbook.add_worksheet(sheet_tx)

    hdr_fmt = workbook.add_format({"bold": True, "bg_color": "#1D4ED8", "font_color": "#FFFFFF", "border": 1})
    tbl_hdr = workbook.add_format({"bold": True, "bg_color": "#E2E8F0", "font_color": "#0F172A", "border": 1})
    cell_fmt = workbook.add_format({"border": 1})
    curr_fmt = workbook.add_format({"num_format": '"Rp "#,##0', "border": 1})
    kpi_lbl = workbook.add_format({"bold": True, "font_color": "#475569", "bg_color": "#F8FAFC", "border": 1})
    kpi_val = workbook.add_format({"bold": True, "font_size": 14, "num_format": '"Rp "#,##0', "bg_color": "#FFFFFF", "border": 1})

    w_headers = ["Nama Wallet / Bank", "Saldo Saat Ini"] if is_id else ["Wallet / Bank Name", "Current Balance"]
    for col_idx, text in enumerate(w_headers):
        ws_wallets.write(0, col_idx, text, hdr_fmt)
        ws_wallets.set_column(col_idx, col_idx, 26)
    for idx, (w_name, w_bal) in enumerate(wallet_balances.items()):
        ws_wallets.write(idx + 1, 0, w_name, cell_fmt)
        ws_wallets.write_number(idx + 1, 1, float(w_bal), curr_fmt)

    headers = (
        ["ID", "Tanggal & Jam", "Kategori", "Tipe", "Wallet / Rute", "Catatan", "Nominal", "Biaya (1.5%)", "Pajak (11%)"]
        if is_id
        else ["ID", "Date & Time", "Category", "Type", "Wallet / Route", "Note", "Amount", "Fees (1.5%)", "Tax (11%)"]
    )
    for col_idx, text in enumerate(headers):
        ws_tx.write(0, col_idx, text, hdr_fmt)
        ws_tx.set_column(col_idx, col_idx, 20)

    for idx, row in df.iterrows():
        r = idx + 1
        acc_display = f"{row['account']} -> {row.get('to_account', '')}" if row["type"] == "transfer" else row["account"]
        ws_tx.write(r, 0, row["id"], cell_fmt)
        ws_tx.write(r, 1, str(row["date"]), cell_fmt)
        ws_tx.write(r, 2, row["category"], cell_fmt)
        ws_tx.write(r, 3, row["type"], cell_fmt)
        ws_tx.write(r, 4, acc_display, cell_fmt)
        ws_tx.write(r, 5, row.get("note") or "-", cell_fmt)
        ws_tx.write_number(r, 6, float(row["amount"]), curr_fmt)
        ws_tx.write_formula(r, 7, f"=G{r+1}*0.015", curr_fmt)
        ws_tx.write_formula(r, 8, f"=G{r+1}*0.11", curr_fmt)

    last_row = len(df) + 1

    ws_overview.merge_range("D5:F5", "Total Nominal" if is_id else "Total Amount", kpi_lbl)
    ws_overview.merge_range("D6:F6", f"=SUM('{sheet_tx}'!G2:G{last_row})", kpi_val)

    ws_overview.merge_range("H5:J5", "Total Transaksi" if is_id else "Total Transactions", kpi_lbl)
    ws_overview.merge_range("H6:J6", f"=COUNTA('{sheet_tx}'!A2:A{last_row})", kpi_val)

    ws_overview.merge_range("L5:N5", "Rata-rata Transaksi" if is_id else "Avg Transaction Value", kpi_lbl)
    ws_overview.merge_range("L6:N6", f"=IFERROR(AVERAGE('{sheet_tx}'!G2:G{last_row}), 0)", kpi_val)

    summary = df.groupby("category")["amount"].sum().reset_index()
    ws_overview.write("K22", "Kategori" if is_id else "Category", tbl_hdr)
    ws_overview.write("L22", "Nominal" if is_id else "Amount", tbl_hdr)
    ws_overview.write("M22", "Biaya (1.5%)" if is_id else "Fees (1.5%)", tbl_hdr)
    ws_overview.write("N22", "Pajak (11%)" if is_id else "Tax (11%)", tbl_hdr)

    for i, srow in summary.iterrows():
        r = 23 + i
        ws_overview.write(r, 10, srow["category"], cell_fmt)
        ws_overview.write_number(r, 11, float(srow["amount"]), curr_fmt)
        ws_overview.write_formula(r, 12, f"=L{r+1}*0.015", curr_fmt)
        ws_overview.write_formula(r, 13, f"=L{r+1}*0.11", curr_fmt)

    end_mat = 23 + len(summary)
    if len(summary) > 0:
        ws_overview.conditional_format(f"L24:L{end_mat}", {"type": "data_bar", "bar_color": "#3B82F6"})
        ws_overview.conditional_format(f"M24:M{end_mat}", {"type": "data_bar", "bar_color": "#8B5CF6"})
        ws_overview.conditional_format(f"N24:N{end_mat}", {"type": "data_bar", "bar_color": "#F59E0B"})

    bar_chart = workbook.add_chart({"type": "bar"})
    bar_chart.add_series(
        {
            "name": "Total per Kategori" if is_id else "Total Amount by Category",
            "categories": [sheet_overview, 23, 10, end_mat - 1, 10],
            "values": [sheet_overview, 23, 11, end_mat - 1, 11],
            "fill": {"color": "#1D4ED8"},
        }
    )
    bar_chart.set_title({"name": "Total per Kategori" if is_id else "Total Amount by Category"})
    bar_chart.set_legend({"none": True})
    ws_overview.insert_chart("D22", bar_chart, {"x_scale": 1.15, "y_scale": 1.05})

    donut_chart = workbook.add_chart({"type": "doughnut"})
    donut_chart.add_series(
        {
            "name": "Distribusi Kategori" if is_id else "Category Breakdown",
            "categories": [sheet_overview, 23, 10, end_mat - 1, 10],
            "values": [sheet_overview, 23, 11, end_mat - 1, 11],
        }
    )
    donut_chart.set_title({"name": "Proporsi Kategori" if is_id else "Category Proportion"})
    ws_overview.insert_chart("K9", donut_chart, {"x_scale": 0.95, "y_scale": 0.9})

    writer.close()

    def remove_file(path: str):
        time.sleep(2)
        if os.path.exists(path):
            os.remove(path)

    background_tasks.add_task(remove_file, export_path)
    return FileResponse(
        export_path,
        filename=export_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)