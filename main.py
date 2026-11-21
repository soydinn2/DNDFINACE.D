import os
import re
import io
import json
import time
import hmac
import base64
import ctypes
import hashlib
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

# ==========================================
# CONFIGURATION & PATHS (VERCEL & LOCAL SAFE)
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DATA_DIR = "/tmp" if IS_VERCEL else BASE_DIR
DB_PATH = os.path.join(DATA_DIR, "dnd_finance.db")
CPP_SOURCE_PATH = os.path.join(BASE_DIR, "dnd_engine.cpp")
CPP_LIB_PATH = os.path.join(DATA_DIR, "dnd_engine.so")

DEFAULT_GOOGLE_CLIENT_ID = "753367715469-972f7qjb3t97qab938sc5lo69mgp78h0.apps.googleusercontent.com"
SESSION_COOKIE_NAME = "dnd_google_session"
SESSION_SECRET = os.environ.get("DND_SESSION_SECRET", "dnd-finance-hmac-secret-key-2026-v1").encode("utf-8")

DEVELOPER_NAME = "Dinda"
INSTAGRAM_HANDLE = "@di_ndaaa29"
INSTAGRAM_URL = "https://www.instagram.com/di_ndaaa29?hvrq=MWNnaXczbm1xbGt4MQ=="

app = FastAPI(title="DND Finance API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# DATABASE INITIALIZATION & HELPERS
# ==========================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            sub TEXT,
            name TEXT,
            nickname TEXT,
            picture TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_states (
            email TEXT PRIMARY KEY,
            state_json TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS banks (
            name TEXT PRIMARY KEY
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT DEFAULT 'guest',
            amount REAL NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            account TEXT NOT NULL,
            to_account TEXT DEFAULT '',
            admin_fee REAL DEFAULT 0,
            note TEXT DEFAULT '',
            currency TEXT DEFAULT 'IDR',
            date TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_rules (
            id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        )
        """
    )

    default_banks = ["Bank Mandiri", "Bank BCA", "Bank BNI", "Bank Jago", "SeaBank", "Gopay", "Cash"]
    for b in default_banks:
        cur.execute("INSERT OR IGNORE INTO banks (name) VALUES (?)", (b,))

    default_cats = ["Food", "Transport", "Trading", "Online Shopping", "Investment", "Salary", "Utilities"]
    for c in default_cats:
        cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (c,))

    conn.commit()
    conn.close()


init_db()


def _get_setting(key: str, default: str = "") -> str:
    env_val = os.environ.get(key)
    if env_val:
        return env_val.strip()
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        if row and row["value"]:
            return str(row["value"]).strip()
    except Exception:
        pass
    return default


def _set_setting(key: str, value: str) -> None:
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


# ==========================================
# C++17 QUANTITATIVE ENGINE LOADER
# ==========================================
class CppMetricsStruct(ctypes.Structure):
    _fields_ = [
        ("total_income", ctypes.c_double),
        ("total_expense", ctypes.c_double),
        ("total_withdrawal", ctypes.c_double),
        ("net_balance", ctypes.c_double),
        ("health_score", ctypes.c_int),
        ("vat_11_percent", ctypes.c_double),
        ("fee_1_5_percent", ctypes.c_double),
    ]


_cpp_lib: Optional[ctypes.CDLL] = None


def _load_cpp_engine() -> Optional[ctypes.CDLL]:
    global _cpp_lib
    if _cpp_lib is not None:
        return _cpp_lib
    try:
        if not os.path.exists(CPP_LIB_PATH) and os.path.exists(CPP_SOURCE_PATH):
            subprocess.run(
                ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", CPP_SOURCE_PATH, "-o", CPP_LIB_PATH],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if os.path.exists(CPP_LIB_PATH):
            lib = ctypes.CDLL(CPP_LIB_PATH)
            lib.dnd_compute_ledger_metrics.argtypes = [
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_int,
            ]
            lib.dnd_compute_ledger_metrics.restype = CppMetricsStruct
            lib.dnd_compute_split_bill.argtypes = [
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_double),
            ]
            lib.dnd_compute_split_bill.restype = None
            _cpp_lib = lib
            return lib
    except Exception:
        pass
    return None


def _compute_metrics_with_cpp(tx_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    count = len(tx_rows)
    lib = _load_cpp_engine()
    if lib and count > 0:
        try:
            amounts_arr = (ctypes.c_double * count)()
            types_arr = (ctypes.c_int * count)()
            for idx, row in enumerate(tx_rows):
                amounts_arr[idx] = float(row.get("amount") or 0.0)
                t_str = str(row.get("type") or "").lower()
                if t_str == "income":
                    types_arr[idx] = 1
                elif t_str == "expense":
                    types_arr[idx] = 2
                elif t_str == "withdrawal":
                    types_arr[idx] = 3
                else:
                    types_arr[idx] = 0
            res = lib.dnd_compute_ledger_metrics(amounts_arr, types_arr, count)
            return {
                "total_income": round(res.total_income, 2),
                "total_expense": round(res.total_expense, 2),
                "total_withdrawal": round(res.total_withdrawal, 2),
                "net_balance": round(res.net_balance, 2),
                "health_score": int(res.health_score),
                "vat_11_percent": round(res.vat_11_percent, 2),
                "fee_1_5_percent": round(res.fee_1_5_percent, 2),
            }
        except Exception:
            pass

    inc = sum(float(r.get("amount") or 0.0) for r in tx_rows if str(r.get("type") or "").lower() == "income")
    exp = sum(float(r.get("amount") or 0.0) for r in tx_rows if str(r.get("type") or "").lower() == "expense")
    wdr = sum(float(r.get("amount") or 0.0) for r in tx_rows if str(r.get("type") or "").lower() == "withdrawal")
    outflow = exp + wdr
    net = inc - outflow
    score = max(0, min(100, round(((inc - outflow) / inc) * 100))) if inc > 0 else 0
    return {
        "total_income": round(inc, 2),
        "total_expense": round(exp, 2),
        "total_withdrawal": round(wdr, 2),
        "net_balance": round(net, 2),
        "health_score": score,
        "vat_11_percent": round(inc * 0.11, 2),
        "fee_1_5_percent": round((inc + outflow) * 0.015, 2),
    }


# ==========================================
# HMAC STATELESS SESSION & GOOGLE OAUTH2
# ==========================================
def _sign_session(user_data: Dict[str, Any]) -> str:
    payload = {
        "sub": user_data.get("sub", ""),
        "email": user_data.get("email", ""),
        "name": user_data.get("name", ""),
        "nickname": user_data.get("nickname", ""),
        "picture": user_data.get("picture", ""),
        "exp": int(time.time()) + (30 * 24 * 3600),
    }
    raw_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    sig = hmac.new(SESSION_SECRET, raw_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw_b64}.{sig}"


def _verify_session(cookie_val: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cookie_val or "." not in cookie_val:
        return None
    try:
        raw_b64, sig = cookie_val.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET, raw_b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = raw_b64 + "=" * ((4 - len(raw_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _set_session_cookie(response: Response, user_data: Dict[str, Any]) -> None:
    token = _sign_session(user_data)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=IS_VERCEL,
        path="/",
    )


def _upsert_user(profile: Dict[str, Any]) -> Dict[str, Any]:
    email = str(profile.get("email") or "").strip().lower()
    if not email:
        return profile
    conn = get_db()
    existing = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    nickname = str(profile.get("nickname") or "").strip()
    if not nickname and existing and existing["nickname"]:
        nickname = str(existing["nickname"]).strip()
    name = str(profile.get("name") or (existing["name"] if existing else "") or email.split("@")[0]).strip()
    picture = str(profile.get("picture") or (existing["picture"] if existing else "") or "").strip()
    sub = str(profile.get("sub") or (existing["sub"] if existing else "") or email).strip()

    conn.execute(
        """
        INSERT OR REPLACE INTO users (email, sub, name, nickname, picture, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (email, sub, name, nickname, picture, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {
        "sub": sub,
        "email": email,
        "name": name,
        "nickname": nickname,
        "picture": picture,
    }


def _decode_jwt_unverified(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        b64 = parts[1].replace("-", "+").replace("_", "/")
        padded = b64 + "=" * ((4 - len(b64) % 4) % 4)
        return json.loads(base64.b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None


# ==========================================
# PYDANTIC MODELS
# ==========================================
class GoogleTokenVerifyPayload(BaseModel):
    id_token: Optional[str] = None
    access_token: Optional[str] = None


class NicknamePayload(BaseModel):
    email: str
    nickname: str = ""


class SyncPushPayload(BaseModel):
    email: str
    state: Dict[str, Any]


class NamePayload(BaseModel):
    name: str


class TransactionPayload(BaseModel):
    amount: float
    type: str
    category: str
    account: str
    to_account: Optional[str] = ""
    admin_fee: Optional[float] = 0.0
    note: Optional[str] = ""
    currency: Optional[str] = "IDR"
    date: str


class SplitBillPayload(BaseModel):
    subtotal: float
    tax_percent: float = 0.0
    people: int = 1


class ReceiptScanPayload(BaseModel):
    image_base64: str
    mime_type: Optional[str] = "image/jpeg"


class AIAnalysisRequest(BaseModel):
    prompt: str
    lang: Optional[str] = "ID"
    currency: Optional[str] = "IDR"
    financial_context: Optional[str] = ""
    transactions: Optional[List[Dict[str, Any]]] = None
    wallets: Optional[Dict[str, float]] = None
    goals: Optional[List[Dict[str, Any]]] = None
    budgets: Optional[List[Dict[str, Any]]] = None
    automation_rules: Optional[List[Dict[str, Any]]] = None
    chat_history: Optional[List[Dict[str, str]]] = None


class AutomationRulePayload(BaseModel):
    id: str
    name: str
    actionType: str
    minAmount: Optional[float] = 0
    percent: Optional[float] = 20
    fixedAmount: Optional[float] = 50000
    targetGoalName: Optional[str] = "Dana Darurat"
    targetCategory: Optional[str] = "Food"
    sourceWallet: Optional[str] = ""
    destWallet: Optional[str] = "Bank Jago"
    thresholdAmount: Optional[float] = 500000
    enabled: Optional[bool] = True
    runCount: Optional[int] = 0


# ==========================================
# MULTI-AI BACKEND CONNECTORS (GEMINI / CLAUDE / OPEN-SOURCE)
# ==========================================
def _call_gemini_backend(parts: List[Dict[str, Any]], system_text: str = "", json_mode: bool = False) -> Optional[str]:
    api_key = _get_setting("GEMINI_API_KEY")
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload: Dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.45,
            "maxOutputTokens": 2400,
        },
    }
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

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


def _call_claude_backend(user_prompt: str, system_text: str = "") -> Optional[str]:
    api_key = _get_setting("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 2400,
        "system": system_text,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            blocks = data.get("content") or []
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            result = "\n".join(texts).strip()
            return result or None
    except Exception:
        return None


def _call_opensource_ai_backend(user_prompt: str, system_text: str = "") -> Optional[str]:
    """Menghubungkan ke API AI open-source gratis (OpenRouter Free / Pollinations OpenAI-Compatible)"""
    openrouter_key = _get_setting("OPENROUTER_API_KEY")
    if openrouter_key:
        or_payload = {
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.45,
            "max_tokens": 2200,
        }
        or_req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(or_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(or_req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if content and len(content) >= 220:
                    return content
        except Exception:
            pass

    free_payload = {
        "model": "openai",
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.45,
    }
    free_req = urllib.request.Request(
        "https://text.pollinations.ai/openai",
        data=json.dumps(free_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DND-Finance-Wealth-Planner/3.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(free_req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip()
            if not raw or raw.startswith("<"):
                return None
            data = json.loads(raw)
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if content and len(content) >= 240 and "python_fallback" not in content.lower():
                return content
    except Exception:
        return None
    return None


def _extract_mentioned_salary(prompt_text: str) -> Optional[float]:
    """Mendeteksi jika pengguna menyebut angka gaji/penghasilan di dalam pertanyaannya, misal 'gaji 6 juta' atau 'gaji 4.500.000'"""
    q = (prompt_text or "").lower()
    m_juta = re.search(r"(?:gaji|penghasilan|pendapatan|income|salary)[^\d]{0,18}(\d+(?:[.,]\d+)?)\s*(?:jt|juta|million|m\b)", q)
    if m_juta:
        val = float(m_juta.group(1).replace(",", ".")) * 1_000_000
        if val > 0:
            return val
    m_ribu = re.search(r"(?:gaji|penghasilan|pendapatan|income|salary)[^\d]{0,18}(\d+(?:[.,]\d+)?)\s*(?:rb|ribu|k\b)", q)
    if m_ribu:
        val = float(m_ribu.group(1).replace(",", ".")) * 1_000
        if val > 0:
            return val
    m_num = re.search(r"(?:gaji|penghasilan|pendapatan|income|salary)[^\d]{0,18}(?:rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+|\d{5,})", q)
    if m_num:
        digits = re.sub(r"[^\d]", "", m_num.group(1))
        if digits:
            val = float(digits)
            if val > 0:
                return val
    return None


def _build_expert_financial_advice(req: AIAnalysisRequest, metrics: Dict[str, Any]) -> str:
    """
    Mesin Perencana Keuangan & Penasihat Kekayaan Komprehensif (CFP-Grade).
    Menyusun analisis mendalam, pembagian pos gaji riil, penataan saldo tiap dompet,
    evaluasi kategori pengeluaran, target tabungan, dan rencana aksi 30 hari.
    """
    is_id = (req.lang or "ID").upper() == "ID"
    curr_code = (req.currency or "IDR").upper()
    prefix = "Rp " if curr_code == "IDR" else f"{curr_code} "
    fmt = lambda v: f"{prefix}{float(v or 0):,.0f}"

    tx_rows = req.transactions or []
    wallets = req.wallets or {}
    goals = req.goals or []
    budgets = req.budgets or []
    rules = req.automation_rules or []
    q = (req.prompt or "").lower().strip()

    inc = float(metrics.get("total_income") or 0.0)
    exp = float(metrics.get("total_expense") or 0.0) + float(metrics.get("total_withdrawal") or 0.0)
    bal = float(metrics.get("net_balance") or 0.0)
    savings_rate = round(((inc - exp) / inc) * 100, 1) if inc > 0 else 0.0

    mentioned_salary = _extract_mentioned_salary(req.prompt or "")
    reference_income = mentioned_salary if (mentioned_salary and mentioned_salary > 0) else inc

    # Analisis kategori pengeluaran lengkap
    cat_totals: Dict[str, float] = {}
    cat_counts: Dict[str, int] = {}
    largest_exp_tx: Optional[Dict[str, Any]] = None
    unique_days = set()

    for t in tx_rows:
        d_str = str(t.get("date") or "")[:10]
        if d_str:
            unique_days.add(d_str)
        if str(t.get("type") or "").lower() in ("expense", "withdrawal"):
            amt = float(t.get("amount") or 0.0)
            cat = str(t.get("category") or "Other")
            cat_totals[cat] = cat_totals.get(cat, 0.0) + amt
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            if largest_exp_tx is None or amt > float(largest_exp_tx.get("amount") or 0.0):
                largest_exp_tx = t

    active_days = max(1, len(unique_days))
    avg_daily_spend = round(exp / active_days) if exp > 0 else 0
    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
    top_cat_name, top_cat_val = sorted_cats[0] if sorted_cats else ("-", 0.0)
    top_cat_share = round((top_cat_val / exp) * 100, 1) if exp > 0 else 0.0

    # Analisis setiap dompet / rekening
    neg_wallets = sorted([(k, float(v)) for k, v in wallets.items() if float(v or 0) < 0], key=lambda x: x[1])
    pos_wallets = sorted([(k, float(v)) for k, v in wallets.items() if float(v or 0) > 0], key=lambda x: x[1], reverse=True)
    zero_wallets = [k for k, v in wallets.items() if float(v or 0) == 0]

    # Cek apakah user meminta fokus khusus atau panduan komprehensif
    is_autopilot_focus = any(k in q for k in ["autopilot", "aturan", "rule", "otomatis", "auto"])
    is_forecast_focus = any(k in q for k in ["prediksi", "forecast", "proyeksi", "target", "goal", "masa depan"])
    is_budget_focus = any(k in q for k in ["anggaran", "budget", "hemat", "boros", "kurangi", "efisiensi"])

    # Hitung alokasi gaji / pemasukan ideal (50% Kebutuhan, 20% Tabungan/Dana Darurat, 20% Gaya Hidup, 10% Cadangan/Investasi)
    alloc_needs = round(reference_income * 0.50)
    alloc_savings = round(reference_income * 0.20)
    alloc_wants = round(reference_income * 0.20)
    alloc_invest = round(reference_income * 0.10)
    weekly_allowance = round(alloc_wants / 4)
    ideal_emergency_3m = max(round(exp * 3), round(reference_income * 3 * 0.7))
    ideal_emergency_6m = ideal_emergency_3m * 2

    if is_id:
        sections: List[str] = []

        # BAGIAN 1: DIAGNOSIS KONDISI KEUANGAN AKTUAL
        if savings_rate < 5:
            status_badge = "Zona Kritis (Rasio Simpanan Sangat Tipis — Butuh Restrukturisasi Segera)"
        elif savings_rate < 15:
            status_badge = "Zona Waspada (Surplus Masih di Bawah Standar Sehat 20%)"
        elif savings_rate < 25:
            status_badge = "Zona Cukup Sehat (Perlu Optimalisasi Alokasi & Otomatisasi)"
        else:
            status_badge = "Zona Sangat Sehat (Arus Kas Positif & Produktif)"

        diag_lines = [
            f"### 1. Diagnosis Menyeluruh Kondisi Keuangan Anda ({status_badge})",
            f"- **Ringkasan Arus Kas Tercatat:** Total pemasukan Anda sebesar **{fmt(inc)}** berbanding total pengeluaran **{fmt(exp)}**, sehingga sisa saldo bersih saat ini adalah **{fmt(bal)}** dengan rasio simpanan **{savings_rate}%** (idealnya minimal **20%** dari penghasilan).",
            f"- **Laju Pembakaran Kas (Burn Rate):** Rata-rata pengeluaran Anda berkisar **{fmt(avg_daily_spend)}/hari** dari **{len(tx_rows)}** transaksi yang tercatat.",
        ]
        if sorted_cats:
            top3_txt = ", ".join(
                f"**{c}** ({fmt(v)} / {round((v / exp) * 100, 1)}%)" for c, v in sorted_cats[:3]
            )
            diag_lines.append(f"- **Pos Pengeluaran Terbesar:** {top3_txt}. Pos **{top_cat_name}** menjadi penyerap terbesar yang perlu dikendalikan pertama kali.")
        if largest_exp_tx:
            diag_lines.append(
                f"- **Transaksi Keluar Tunggal Terbesar:** **{largest_exp_tx.get('note') or largest_exp_tx.get('category')}** sebesar **{fmt(largest_exp_tx.get('amount'))}** melalui **{largest_exp_tx.get('account')}**."
            )
        sections.append("\n".join(diag_lines))

        # BAGIAN 2: PEMBAGIAN POS GAJI & PENGELOLAAN ARUS KAS IDEAL
        if reference_income > 0:
            basis_label = (
                f"gaji yang Anda sebutkan (**{fmt(reference_income)}**)"
                if mentioned_salary
                else f"total pemasukan tercatat Anda (**{fmt(reference_income)}**)"
            )
            salary_lines = [
                "### 2. Rencana Pembagian Pos Gaji & Anggaran (Formula 50 / 20 / 20 / 10)",
                f"Mengacu pada {basis_label}, berikut adalah pembagian pos dana yang ideal agar gaji tidak habis di tengah bulan:",
                f"1. **50% Kebutuhan Pokok & Tagihan Wajib — {fmt(alloc_needs)}/bulan:** Digunakan untuk makan harian, transportasi kerja, listrik/internet/utilitas, serta kewajiban rutin. Pastikan seluruh tagihan tetap dibayar dalam **3 hari pertama** setelah gajian.",
                f"2. **20% Tabungan & Dana Darurat (Bayar Diri Sendiri di Awal) — {fmt(alloc_savings)}/bulan:** Langsung pisahkan begitu gaji masuk, **bukan menunggu sisa akhir bulan**. Target dana darurat ideal Anda adalah **{fmt(ideal_emergency_3m)}** (3x pengeluaran) hingga **{fmt(ideal_emergency_6m)}** (6x pengeluaran).",
                f"3. **20% Gaya Hidup & Kebutuhan Fleksibel — {fmt(alloc_wants)}/bulan (~{fmt(weekly_allowance)}/minggu):** Jatah untuk belanja online, jajan/kopi, dan hiburan. Batasi pengeluaran mingguan maksimal **{fmt(weekly_allowance)}** agar tidak menyentuh pos tabungan.",
                f"4. **10% Investasi & Pengembangan Diri — {fmt(alloc_invest)}/bulan:** Dialokasikan untuk instrumen berisiko terukur (reksa dana pasar uang, emas, atau tabungan berjangka) agar nilai kekayaan bersih terus bertumbuh.",
            ]
            if exp > alloc_needs + alloc_wants and not mentioned_salary:
                over_amt = round(exp - (alloc_needs + alloc_wants))
                salary_lines.append(
                    f"- **Catatan Penyesuaian Penting:** Pengeluaran Anda saat ini (**{fmt(exp)}**) melampaui batas aman kebutuhan + gaya hidup sebesar **{fmt(over_amt)}**. Pangkas pos **{top_cat_name}** sebesar **15%–20%** (**{fmt(round(top_cat_val * 0.2))}**) agar struktur gaji kembali seimbang."
                )
            sections.append("\n".join(salary_lines))
        else:
            sections.append(
                "### 2. Cara Memulai Perencanaan Gaji yang Terukur\n"
                "- **Langkah Pertama:** Catat nominal gaji/pemasukan bulanan Anda pada tombol **+ Pemasukan** di Beranda.\n"
                "- **Gunakan Aturan 50 / 20 / 20 / 10:** Begitu gaji masuk, bagi langsung ke **50% Kebutuhan Wajib**, **20% Tabungan/Dana Darurat** (di awal, bukan dari sisa), **20% Gaya Hidup** (dibagi menjadi anggaran mingguan), dan **10% Investasi/Cadangan**."
            )

        # BAGIAN 3: PENATAAN DOMPET, LIKUIDITAS & TARGET TABUNGAN
        wallet_lines = ["### 3. Strategi Penataan Multi-Dompet & Target Tabungan"]
        if neg_wallets:
            neg_list = ", ".join(f"**{k}** ({fmt(v)})" for k, v in neg_wallets)
            wallet_lines.append(
                f"- **Perbaikan Saldo Dompet Minus (Prioritas Utama):** Terdeteksi dompet dengan saldo negatif yaitu {neg_list}. Hal ini biasanya terjadi karena pengeluaran dicatat pada dompet tersebut sebelum saldo awal atau transfer masuk dicatat. Lakukan penyesuaian saldo/transfer masuk agar saldo riil setiap dompet akurat."
            )
        if pos_wallets:
            pos_list = ", ".join(f"**{k}** ({fmt(v)})" for k, v in pos_wallets[:4])
            wallet_lines.append(f"- **Distribusi Saldo Positif Saat Ini:** {pos_list}.")

        wallet_lines.append(
            "- **Sistem 3 Kantong Dompet (Anti-Bocor):** Pisahkan fungsi rekening Anda di aplikasi ini: (a) **Rekening Gaji & Tagihan** (Bank Mandiri / BCA / BNI) khusus biaya wajib; (b) **Dompet Harian** (Gopay / Cash) diisi jatah mingguan saja; dan (c) **Rekening Tabungan Terpisah** (SeaBank / Bank Jago) yang tidak dipakai untuk transaksi harian."
        )

        if goals:
            for g in goals:
                g_name = g.get("name", "Target")
                g_cur = float(g.get("current") or 0)
                g_tgt = float(g.get("target") or 1)
                pct = min(100, round((g_cur / max(1.0, g_tgt)) * 100, 1))
                rem = max(0.0, g_tgt - g_cur)
                est_m = f"{round(rem / alloc_savings, 1)} bulan" if alloc_savings > 0 else "setelah surplus bulanan terbentuk"
                wallet_lines.append(
                    f"- **Progres Target {g_name}:** Terkumpul **{fmt(g_cur)}** dari **{fmt(g_tgt)}** (**{pct}%**), kurang **{fmt(rem)}** (estimasi tercapai dalam **{est_m}** jika menyisihkan **{fmt(alloc_savings)}/bulan**)."
                )
        else:
            wallet_lines.append(
                f"- **Buat Target Dana Darurat Sekarang:** Buka menu **Target → Target Baru** dan buat target **Dana Darurat** sebesar **{fmt(ideal_emergency_3m)}** agar setiap tabungan punya tujuan yang jelas."
            )
        sections.append("\n".join(wallet_lines))

        # BAGIAN 4: RENCANA AKSI PRAKTIS & OTOMATISASI AUTOPILOT
        rec_save_pct = 15 if savings_rate < 10 else 20
        rec_cap = max(250000, round((top_cat_val * 0.85) / 50000) * 50000) if top_cat_val > 0 else 1000000
        active_rules_cnt = sum(1 for r in rules if r.get("enabled"))

        action_lines = [
            "### 4. Langkah Konkret & Otomatisasi Autopilot di DND Finance",
            f"1. **Aktifkan Aturan Autopilot Gaji ({active_rules_cnt} aturan aktif saat ini):** Buka tab **Autopilot → Aturan Baru**, pilih *Sisihkan % Pemasukan ke Target Tabungan* sebesar **{rec_save_pct}%** ke **Dana Darurat**, sehingga setiap gaji masuk otomatis terpotong ke pos tabungan.",
            f"2. **Pasang Batas Anggaran Kategori {top_cat_name}:** Buka menu **Target → Atur Anggaran**, tetapkan batas bulanan **{top_cat_name}** maksimal **{fmt(rec_cap)}** agar pengeluaran di pos terbesar terkontrol.",
            "3. **Terapkan Aturan Tunggu 24 Jam (Cooling-Off Rule):** Untuk setiap pembelian non-kebutuhan pokok di atas **Rp 200.000**, tunda keputusan beli selama 24 jam untuk memangkas pembelian impulsif hingga 30%.",
            "4. **Evaluasi Mingguan 5 Menit:** Setiap Minggu malam, cek grafik **Arus Kas** dan unduh laporan **Excel/PDF** di pojok kanan atas untuk memastikan pengeluaran mingguan tetap di bawah batas.",
        ]
        if is_forecast_focus:
            surplus_now = max(0.0, inc - exp)
            action_lines.append(
                f"- **Simulasi Proyeksi 6 Bulan:** Dengan pola saat ini, saldo bersih Anda bergerak ke **{fmt(bal + surplus_now * 6)}**; namun jika alokasi tabungan **{fmt(alloc_savings)}/bulan** berjalan konsisten, kekayaan bersih Anda meningkat menjadi **{fmt(bal + alloc_savings * 6)}** dalam 6 bulan."
            )
        elif is_budget_focus and budgets:
            for b in budgets:
                b_cat = b.get("category", "-")
                b_lim = float(b.get("limit") or 0)
                b_sp = float(b.get("spent") or cat_totals.get(b_cat, 0.0))
                pct_b = round((b_sp / max(1.0, b_lim)) * 100, 1)
                action_lines.append(f"- **Status Anggaran {b_cat}:** Terpakai **{fmt(b_sp)}** dari batas **{fmt(b_lim)}** (**{pct_b}%**).")
        elif is_autopilot_focus:
            action_lines.append(
                f"- **Rekomendasi Aturan Tambahan:** Tambahkan juga aturan *Peringatan Pengeluaran Kategori {top_cat_name}* dengan ambang batas **{fmt(max(200000, round(rec_cap * 0.35)))}** per transaksi."
            )

        sections.append("\n".join(action_lines))
        return "\n\n".join(sections)

    # ENGLISH VERSION (CFP-GRADE COMPREHENSIVE FINANCIAL MASTERPLAN)
    sections_en: List[str] = [
        "\n".join([
            "### 1. Comprehensive Financial Health Diagnosis",
            f"- **Cashflow Snapshot:** Total recorded income is **{fmt(inc)}** against total outflow of **{fmt(exp)}**, leaving a net balance of **{fmt(bal)}** with a **{savings_rate}%** savings ratio (healthy target: **≥ 20%**).",
            f"- **Daily Burn Rate:** You spend an average of **{fmt(avg_daily_spend)}/day** across **{len(tx_rows)}** recorded transactions.",
            f"- **Primary Spending Driver:** **{top_cat_name}** accounts for **{fmt(top_cat_val)}** (**{top_cat_share}%** of total spending).",
        ]),
        "\n".join([
            "### 2. Recommended Salary & Cashflow Allocation (50 / 20 / 20 / 10 Blueprint)",
            f"Based on **{fmt(reference_income)}** monthly income:",
            f"1. **50% Essential Needs & Bills — {fmt(alloc_needs)}/month:** Housing, food, utilities, and commuting. Pay all fixed obligations within the first 3 days of payday.",
            f"2. **20% Pay-Yourself-First Savings & Emergency Fund — {fmt(alloc_savings)}/month:** Transfer immediately upon receiving income toward an emergency fund target of **{fmt(ideal_emergency_3m)}** (3 months) to **{fmt(ideal_emergency_6m)}** (6 months).",
            f"3. **20% Flexible Lifestyle & Wants — {fmt(alloc_wants)}/month (~{fmt(weekly_allowance)}/week):** Cap discretionary spending to **{fmt(weekly_allowance)}/week** so savings remain untouched.",
            f"4. **10% Investments & Wealth Growth — {fmt(alloc_invest)}/month:** Allocate to low-cost index funds, money market funds, or gold.",
        ]),
        "\n".join([
            "### 3. Multi-Wallet Structure & Actionable Next Steps",
            "- **Separate Your Wallets:** Keep one primary bank account for salary and bills, one daily wallet (e.g., Gopay/Cash) funded weekly, and a dedicated high-yield savings account (SeaBank / Bank Jago) for reserves.",
            f"- **Automate via Autopilot:** Enable a **20% Income Auto-Save** rule toward your **Emergency Fund** and set a monthly category cap on **{top_cat_name}**.",
        ]),
    ]
    return "\n\n".join(sections_en)


# ==========================================
# HEALTH & APP METADATA ENDPOINTS
# ==========================================
@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "cpp_engine": _load_cpp_engine() is not None,
        "gemini_configured": bool(_get_setting("GEMINI_API_KEY")),
        "claude_configured": bool(_get_setting("ANTHROPIC_API_KEY")),
        "openrouter_configured": bool(_get_setting("OPENROUTER_API_KEY")),
    }


@app.get("/api/app/info")
async def get_app_info():
    return {
        "platform": "DND Finance",
        "developer": DEVELOPER_NAME,
        "instagram_handle": INSTAGRAM_HANDLE,
        "instagram_url": INSTAGRAM_URL,
        "google_client_id": _get_setting("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID),
    }


# ==========================================
# GOOGLE OAUTH2 & SESSION ENDPOINTS
# ==========================================
@app.get("/api/auth/google/config")
async def google_auth_config():
    return {
        "client_id": _get_setting("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID),
        "redirect_path": "/api/auth/google/callback",
    }


@app.get("/api/auth/google/login")
async def google_auth_login(request: Request):
    client_id = _get_setting("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID)
    base_url = str(request.base_url).rstrip("/")
    if IS_VERCEL and base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://"):]
    redirect_uri = f"{base_url}/api/auth/google/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=auth_url)


@app.get("/api/auth/google/callback")
async def google_auth_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(url=f"/?google_auth_error={urllib.parse.quote(error or 'cancelled')}")

    client_id = _get_setting("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID)
    client_secret = _get_setting("GOOGLE_CLIENT_SECRET")
    base_url = str(request.base_url).rstrip("/")
    if IS_VERCEL and base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://"):]
    redirect_uri = f"{base_url}/api/auth/google/callback"

    if not client_secret:
        return RedirectResponse(url="/?google_auth_error=missing_client_secret")

    token_payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")

    try:
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=12) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))

        access_token = token_data.get("access_token")
        id_token = token_data.get("id_token")
        profile: Optional[Dict[str, Any]] = None

        if access_token:
            info_req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(info_req, timeout=10) as uresp:
                profile = json.loads(uresp.read().decode("utf-8"))
        elif id_token:
            profile = _decode_jwt_unverified(id_token)

        if not profile or not profile.get("email"):
            return RedirectResponse(url="/?google_auth_error=invalid_profile")

        saved_user = _upsert_user(profile)
        redirect = RedirectResponse(url="/?google_auth_success=1")
        _set_session_cookie(redirect, saved_user)
        return redirect
    except Exception as exc:
        return RedirectResponse(url=f"/?google_auth_error={urllib.parse.quote(str(exc)[:80])}")


@app.post("/api/auth/google/verify-token")
async def google_verify_token(payload: GoogleTokenVerifyPayload, response: Response):
    profile: Optional[Dict[str, Any]] = None

    if payload.access_token:
        try:
            req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {payload.access_token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                profile = json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

    if not profile and payload.id_token:
        try:
            v_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={urllib.parse.quote(payload.id_token)}"
            with urllib.request.urlopen(v_url, timeout=10) as resp:
                profile = json.loads(resp.read().decode("utf-8"))
        except Exception:
            profile = _decode_jwt_unverified(payload.id_token)

    if not profile or not profile.get("email"):
        raise HTTPException(status_code=401, detail="Invalid Google token")

    saved_user = _upsert_user(profile)
    _set_session_cookie(response, saved_user)

    conn = get_db()
    ws_row = conn.execute("SELECT state_json FROM workspace_states WHERE email = ?", (saved_user["email"],)).fetchone()
    conn.close()
    ws_state = json.loads(ws_row["state_json"]) if (ws_row and ws_row["state_json"]) else None

    return {"status": "ok", "user": saved_user, "workspace_state": ws_state}


@app.get("/api/auth/google/me")
async def google_auth_me(request: Request):
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    session_user = _verify_session(cookie_val)
    if not session_user or not session_user.get("email"):
        return {"authenticated": False, "user": None}

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (session_user["email"],)).fetchone()
    conn.close()
    if row:
        user_obj = {
            "sub": row["sub"],
            "email": row["email"],
            "name": row["name"],
            "nickname": row["nickname"] or "",
            "picture": row["picture"] or "",
        }
    else:
        user_obj = _upsert_user(session_user)

    return {"authenticated": True, "user": user_obj}


@app.post("/api/auth/google/nickname")
async def update_google_nickname(payload: NicknamePayload, response: Response):
    email = (payload.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    nick = (payload.nickname or "").strip()[:40]
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        conn.execute("UPDATE users SET nickname = ?, updated_at = ? WHERE email = ?", (nick, datetime.utcnow().isoformat(), email))
        user_obj = {
            "sub": row["sub"],
            "email": email,
            "name": row["name"],
            "nickname": nick,
            "picture": row["picture"] or "",
        }
    else:
        user_obj = _upsert_user({"email": email, "nickname": nick})
    conn.commit()
    conn.close()
    _set_session_cookie(response, user_obj)
    return {"status": "ok", "user": user_obj}


@app.post("/api/auth/google/logout")
async def google_auth_logout(response: Response):
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


# ==========================================
# WORKSPACE SYNC ENDPOINTS
# ==========================================
@app.get("/api/sync/pull")
async def sync_pull(email: str):
    clean_email = (email or "").strip().lower()
    if not clean_email:
        return {"status": "empty", "state": None}
    conn = get_db()
    row = conn.execute("SELECT state_json FROM workspace_states WHERE email = ?", (clean_email,)).fetchone()
    conn.close()
    if not row or not row["state_json"]:
        return {"status": "empty", "state": None}
    return {"status": "ok", "state": json.loads(row["state_json"])}


@app.post("/api/sync/push")
async def sync_push(payload: SyncPushPayload):
    clean_email = (payload.email or "").strip().lower()
    if not clean_email:
        raise HTTPException(status_code=400, detail="Email required")
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO workspace_states (email, state_json, updated_at) VALUES (?, ?, ?)",
        (clean_email, json.dumps(payload.state), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ==========================================
# BANKS, CATEGORIES & TRANSACTIONS ENDPOINTS
# ==========================================
@app.get("/api/banks")
async def list_banks():
    conn = get_db()
    rows = conn.execute("SELECT name FROM banks").fetchall()
    conn.close()
    return {"banks": [r["name"] for r in rows]}


@app.post("/api/banks")
async def add_bank(payload: NamePayload):
    name = (payload.name or "").strip()
    if name:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO banks (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
    return {"status": "ok"}


@app.delete("/api/banks/{name}")
async def delete_bank(name: str):
    conn = get_db()
    conn.execute("DELETE FROM banks WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/categories")
async def list_categories():
    conn = get_db()
    rows = conn.execute("SELECT name FROM categories").fetchall()
    conn.close()
    return {"categories": [r["name"] for r in rows]}


@app.post("/api/categories")
async def add_category(payload: NamePayload):
    name = (payload.name or "").strip()
    if name:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
    return {"status": "ok"}


@app.delete("/api/categories/{name}")
async def delete_category(name: str):
    conn = get_db()
    conn.execute("DELETE FROM categories WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/transactions")
async def get_transactions(request: Request):
    user_email = (request.headers.get("X-User-Email") or "guest").strip().lower()
    conn = get_db()
    rows = conn.execute("SELECT * FROM transactions WHERE user_email = ? ORDER BY id DESC", (user_email,)).fetchall()
    conn.close()
    return {"transactions": [dict(r) for r in rows]}


@app.post("/api/transactions")
async def create_transaction(payload: TransactionPayload, request: Request):
    user_email = (request.headers.get("X-User-Email") or "guest").strip().lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO transactions (user_email, amount, type, category, account, to_account, admin_fee, note, currency, date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_email,
            payload.amount,
            payload.type,
            payload.category,
            payload.account,
            payload.to_account or "",
            payload.admin_fee or 0.0,
            payload.note or "",
            payload.currency or "IDR",
            payload.date,
        ),
    )
    tx_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"status": "ok", "id": tx_id}


@app.delete("/api/transactions/{tx_id}")
async def delete_transaction(tx_id: int):
    conn = get_db()
    conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/macros")
async def get_macros():
    conn = get_db()
    rows = conn.execute("SELECT payload_json FROM automation_rules").fetchall()
    conn.close()
    return {"rules": [json.loads(r["payload_json"]) for r in rows]}


@app.post("/api/macros")
async def save_macro(payload: AutomationRulePayload):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO automation_rules (id, payload_json) VALUES (?, ?)",
        (payload.id, json.dumps(payload.model_dump())),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/macros/{rule_id}")
async def delete_macro(rule_id: str):
    conn = get_db()
    conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ==========================================
# SPLIT BILL & RECEIPT OCR ENDPOINTS
# ==========================================
@app.post("/api/engine/split-bill")
async def compute_split_bill(payload: SplitBillPayload):
    lib = _load_cpp_engine()
    if lib:
        try:
            grand_out = ctypes.c_double(0.0)
            per_out = ctypes.c_double(0.0)
            lib.dnd_compute_split_bill(
                float(payload.subtotal),
                float(payload.tax_percent),
                int(max(1, payload.people)),
                ctypes.byref(grand_out),
                ctypes.byref(per_out),
            )
            return {
                "grand_total": round(grand_out.value, 2),
                "per_person": round(per_out.value, 2),
            }
        except Exception:
            pass

    sub = max(0.0, float(payload.subtotal))
    tax = max(0.0, float(payload.tax_percent))
    ppl = max(1, int(payload.people))
    grand = sub + (sub * (tax / 100.0))
    return {
        "grand_total": round(grand, 2),
        "per_person": round(grand / ppl, 2),
    }


@app.post("/api/scan/receipt")
async def scan_receipt(payload: ReceiptScanPayload):
    prompt = (
        "Extract receipt data from this image. Return ONLY valid JSON with keys: "
        '{"merchant": string, "subtotal": number, "tax_percent": number, "total_amount": number, '
        '"category": one of ["Food","Transport","Trading","Online Shopping","Investment","Salary","Utilities"], '
        '"items": [{"name": string, "price": number}]}.'
    )
    raw = _call_gemini_backend(
        [
            {"text": prompt},
            {"inline_data": {"mime_type": payload.mime_type or "image/jpeg", "data": payload.image_base64}},
        ],
        json_mode=True,
    )
    if raw:
        try:
            parsed = json.loads(raw)
            total_amt = float(parsed.get("total_amount") or parsed.get("subtotal") or 0)
            sub_amt = float(parsed.get("subtotal") or total_amt)
            return {
                "status": "success",
                "amount": total_amt,
                "subtotal": sub_amt,
                "tax_percent": float(parsed.get("tax_percent") or 0),
                "category": parsed.get("category") or "Food",
                "note": parsed.get("merchant") or "Receipt Scan",
                "items": parsed.get("items") or [],
            }
        except Exception:
            pass

    return {
        "status": "success",
        "amount": 0,
        "subtotal": 0,
        "tax_percent": 0,
        "category": "Food",
        "note": "",
        "items": [],
    }


# ==========================================
# COMPREHENSIVE AI FINANCIAL ADVISOR ENDPOINT
# ==========================================
@app.post("/api/ai/analyze")
async def analyze_financial_data(req: AIAnalysisRequest):
    """Endpoint Pakar Perencana Keuangan Cerdas (Gemini / Claude / Open-Source / CFP Wealth Engine)"""
    tx_rows = req.transactions if req.transactions is not None else []
    metrics = _compute_metrics_with_cpp(tx_rows)
    is_id = (req.lang or "ID").upper() == "ID"

    wallet_lines = ", ".join(f"{k}: {v:,.0f}" for k, v in (req.wallets or {}).items()) or "-"
    goal_lines = ", ".join(
        f"{g.get('name')} ({float(g.get('current') or 0):,.0f}/{float(g.get('target') or 0):,.0f})"
        for g in (req.goals or [])
    ) or ("Belum ada" if is_id else "None")
    budget_lines = ", ".join(
        f"{b.get('category')} (limit {float(b.get('limit') or 0):,.0f}, terpakai {float(b.get('spent') or 0):,.0f})"
        for b in (req.budgets or [])
    ) or ("Belum ada" if is_id else "None")
    rule_lines = ", ".join(
        f"{r.get('name')} [{'Aktif' if r.get('enabled') else 'Jeda'}]"
        for r in (req.automation_rules or [])
    ) or ("Belum ada" if is_id else "None")

    recent_tx_lines = "\n".join(
        f"- {t.get('date', '')} | {t.get('type', '')} | {t.get('category', '')} | {t.get('account', '')} | {float(t.get('amount') or 0):,.0f} | {t.get('note', '')}"
        for t in tx_rows[:30]
    ) or ("Tidak ada transaksi" if is_id else "No transactions")

    history_lines = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Advisor'}: {m.get('text', '')}"
        for m in (req.chat_history or [])[-6:]
    )

    full_dossier = (
        f"Mata Uang: {req.currency or 'IDR'}\n"
        f"Ringkasan Kuantitatif: Pemasukan={metrics['total_income']:,.0f}, "
        f"Pengeluaran={metrics['total_expense'] + metrics['total_withdrawal']:,.0f}, "
        f"Saldo Bersih={metrics['net_balance']:,.0f}, Rasio Simpanan={metrics['health_score']}%\n"
        f"Saldo Semua Dompet/Rekening: {wallet_lines}\n"
        f"Target Tabungan: {goal_lines}\n"
        f"Anggaran Kategori: {budget_lines}\n"
        f"Aturan Autopilot: {rule_lines}\n"
        f"Konteks Tambahan:\n{req.financial_context or ''}\n"
        f"30 Transaksi Terakhir:\n{recent_tx_lines}"
    )
    if history_lines:
        full_dossier += f"\n\nRiwayat Percakapan Sebelumnya:\n{history_lines}"

    system_instruction = (
        "Anda adalah Senior Certified Financial Planner (CFP) dan Pakar Manajemen Kekayaan Pribadi di DND Finance. "
        "Berikan jawaban yang MENDALAM, KOMPREHENSIF, TERSTRUKTUR, dan PRAKTIS (minimal 4 bagian dengan rincian angka nyata). "
        "Gunakan seluruh data pengguna yang tersedia (pemasukan, pengeluaran, rasio simpanan, saldo setiap dompet termasuk jika ada saldo minus, "
        "kategori pengeluaran terbesar, target tabungan, batas anggaran, dan aturan Autopilot). "
        "Jika pengguna bertanya cara mengelola keuangan atau mengatur gaji, berikan: "
        "(1) Diagnosis kondisi keuangan & arus kas mereka saat ini, "
        "(2) Rincian pembagian pos gaji riil (50% Kebutuhan Pokok, 20% Tabungan/Dana Darurat di awal, 20% Gaya Hidup dengan batas mingguan, 10% Investasi) dalam nominal uang aktual, "
        "(3) Strategi penataan setiap dompet/rekening & perbaikan saldo minus jika ada, serta "
        "(4) Langkah aksi praktis 30 hari & aturan Autopilot spesifik yang bisa langsung diaktifkan di DND Finance. "
        "DILARANG menyebut kata 'python_fallback', 'cpp17_native', atau istilah teknis kode internal. "
        f"Jawab sepenuhnya dalam {'Bahasa Indonesia yang profesional, hangat, dan mudah dipraktikkan' if is_id else 'clear, actionable English'}."
    )
    combined_prompt = f"Pertanyaan Pengguna: {req.prompt}\n\n=== DATA KEUANGAN LENGKAP PENGGUNA ===\n{full_dossier}"

    # 1. Coba Gemini API jika dikonfigurasi
    ai_text = _call_gemini_backend([{"text": combined_prompt}], system_text=system_instruction)
    source_used = "gemini"

    # 2. Coba Anthropic Claude API jika dikonfigurasi
    if not ai_text:
        ai_text = _call_claude_backend(combined_prompt, system_text=system_instruction)
        source_used = "claude"

    # 3. Coba Open-Source AI API gratis (OpenRouter Free / Pollinations)
    if not ai_text:
        ai_text = _call_opensource_ai_backend(combined_prompt, system_text=system_instruction)
        source_used = "opensource_ai"

    # 4. Pakar Perencana Keuangan Komprehensif (CFP-Grade Wealth Planner)
    if not ai_text:
        ai_text = _build_expert_financial_advice(req, metrics)
        source_used = "dnd_cfp_planner"

    ai_text = ai_text.replace("(python_fallback)", "").replace("python_fallback", "").replace("(cpp17_native)", "").strip()

    return {"status": "success", "source": source_used, "analysis": ai_text}


# ==========================================
# EXCEL & PDF EXPORT ENDPOINTS
# ==========================================
@app.get("/api/export/excel")
async def export_excel_report(lang: str = "ID", email: str = ""):
    import pandas as pd

    clean_email = (email or "").strip().lower()
    conn = get_db()
    rows = []
    if clean_email:
        ws_row = conn.execute("SELECT state_json FROM workspace_states WHERE email = ?", (clean_email,)).fetchone()
        if ws_row and ws_row["state_json"]:
            st = json.loads(ws_row["state_json"])
            rows = st.get("transactions") or []
    if not rows:
        db_rows = conn.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
        rows = [dict(r) for r in db_rows]
    conn.close()

    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["date", "type", "category", "account", "amount", "note"])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Transactions")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=DND_Financial_Report_{lang}.xlsx"},
    )


@app.get("/api/export/pdf")
async def export_pdf_report(lang: str = "ID", email: str = ""):
    clean_email = (email or "").strip().lower()
    conn = get_db()
    rows = []
    if clean_email:
        ws_row = conn.execute("SELECT state_json FROM workspace_states WHERE email = ?", (clean_email,)).fetchone()
        if ws_row and ws_row["state_json"]:
            st = json.loads(ws_row["state_json"])
            rows = st.get("transactions") or []
    if not rows:
        db_rows = conn.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
        rows = [dict(r) for r in db_rows]
    conn.close()

    lines = ["DND Finance — Personal Wealth & Cashflow Report", f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "-" * 60]
    for r in rows[:60]:
        lines.append(f"{r.get('date', '')} | {r.get('type', '')} | {r.get('category', '')} | {r.get('account', '')} | {r.get('amount', 0)}")
    text_blob = "\n".join(lines).encode("latin-1", errors="replace")

    pdf_bytes = (
        b"%PDF-1.4\n1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
        b"4 0 obj<< /Length " + str(len(text_blob) + 45).encode("ascii") + b" >>stream\nBT /F1 10 Tf 40 740 Td ("
        + text_blob.replace(b"\n", b") Tj 0 -14 Td (")
        + b") Tj ET\nendstream\nendobj\n"
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000241 00000 n \n0000000380 00000 n \n"
        b"trailer<< /Size 6 /Root 1 0 R >>\nstartxref\n450\n%%EOF"
    )
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=DND_Financial_Report_{lang}.pdf"},
    )


# ==========================================
# CATCH-ALL FRONTEND ROUTE (MUST BE LAST)
# ==========================================
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_spa(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>DND Finance</h1><p>index.html not found.</p>", status_code=404)
