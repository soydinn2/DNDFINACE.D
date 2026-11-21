import os
import re
import io
import json
import time
import hmac
import math
import base64
import ctypes
import hashlib
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC_SDK = True
except Exception:
    Anthropic = None
    HAS_ANTHROPIC_SDK = False

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

app = FastAPI(title="DND Finance API", version="5.1.0")

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


def _get_anthropic_api_key() -> str:
    return _get_setting("ANTHROPIC_API_KEY") or _get_setting("CLAUDE_API_KEY")


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
# BUILT-IN AUTOMATIC AI BRAIN (ZERO MANUAL KEY)
# ==========================================
def _call_official_anthropic(
    user_prompt: str,
    system_text: str = "",
    chat_history: Optional[List[Dict[str, str]]] = None,
    max_tokens: int = 3000,
    temperature: float = 0.35,
) -> Optional[str]:
    api_key = _get_anthropic_api_key()
    if not api_key:
        return None

    messages: List[Dict[str, str]] = []
    for item in (chat_history or [])[-6:]:
        role = "assistant" if item.get("role") == "ai" else "user"
        txt = str(item.get("text") or "").strip()
        if not txt:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += f"\n\n{txt}"
        else:
            messages.append({"role": role, "content": txt})

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = user_prompt
    else:
        messages.append({"role": "user", "content": user_prompt})

    models = ("claude-sonnet-4-20250514", "claude-3-7-sonnet-latest")

    if HAS_ANTHROPIC_SDK and Anthropic is not None:
        try:
            client = Anthropic(api_key=api_key)
            for model_id in models:
                try:
                    msg = client.messages.create(
                        model=model_id,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        system=system_text,
                        messages=messages,
                    )
                    parts = [
                        getattr(blk, "text", "")
                        for blk in (getattr(msg, "content", None) or [])
                        if getattr(blk, "type", "") == "text"
                    ]
                    out = "\n".join(parts).strip()
                    if out:
                        return out
                except Exception:
                    continue
        except Exception:
            pass

    for model_id in models:
        payload = {
            "model": model_id,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_text,
            "messages": messages,
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
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                blocks = data.get("content") or []
                texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                result = "\n".join(texts).strip()
                if result:
                    return result
        except Exception:
            continue

    return None


def _call_official_anthropic_vision(image_base64: str, mime_type: str, prompt_text: str) -> Optional[str]:
    api_key = _get_anthropic_api_key()
    if not api_key or not image_base64:
        return None
    safe_mime = mime_type if mime_type in ("image/jpeg", "image/png", "image/gif", "image/webp") else "image/jpeg"

    if HAS_ANTHROPIC_SDK and Anthropic is not None:
        try:
            client = Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1200,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": safe_mime,
                                    "data": image_base64,
                                },
                            },
                            {"type": "text", "text": prompt_text},
                        ],
                    }
                ],
            )
            parts = [
                getattr(blk, "text", "")
                for blk in (getattr(msg, "content", None) or [])
                if getattr(blk, "type", "") == "text"
            ]
            out = "\n".join(parts).strip()
            if out:
                return out
        except Exception:
            pass

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1200,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": safe_mime,
                            "data": image_base64,
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }
        ],
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            blocks = data.get("content") or []
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            return "\n".join(texts).strip() or None
    except Exception:
        return None


def _call_opensource_ai_backend(user_prompt: str, system_text: str = "") -> Optional[str]:
    """Automatic built-in AI API gateway requiring zero manual user API key setup."""
    openrouter_key = _get_setting("OPENROUTER_API_KEY")
    if openrouter_key:
        for model_id in (
            "anthropic/claude-sonnet-4",
            "deepseek/deepseek-chat-v3-0324:free",
            "qwen/qwen-2.5-72b-instruct:free",
        ):
            or_payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 2400,
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
                with urllib.request.urlopen(or_req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                    if content and len(content) >= 60:
                        return content
            except Exception:
                continue

    # Zero-config built-in OpenAI-compatible AI endpoint (works out-of-the-box)
    zero_cfg_payload = {
        "model": "openai",
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
    }
    zero_cfg_req = urllib.request.Request(
        "https://text.pollinations.ai/openai",
        data=json.dumps(zero_cfg_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DNDFinance/5.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(zero_cfg_req, timeout=11) as resp:
            raw_body = resp.read().decode("utf-8", errors="ignore").strip()
            if raw_body.startswith("{"):
                data = json.loads(raw_body)
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if content and len(content) >= 80 and "<html" not in content.lower():
                    return content
            elif len(raw_body) >= 80 and "<html" not in raw_body.lower():
                return raw_body
    except Exception:
        pass

    return None


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


class SmartParseTxPayload(BaseModel):
    text: str
    banks: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    lang: Optional[str] = "ID"


class QuantitativeIntelligencePayload(BaseModel):
    transactions: Optional[List[Dict[str, Any]]] = None
    wallets: Optional[Dict[str, float]] = None
    goals: Optional[List[Dict[str, Any]]] = None
    lang: Optional[str] = "ID"
    currency: Optional[str] = "IDR"


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


class ReportExportPayload(BaseModel):
    lang: Optional[str] = "ID"
    currency: Optional[str] = "IDR"
    transactions: Optional[List[Dict[str, Any]]] = None
    wallets: Optional[Dict[str, float]] = None
    goals: Optional[List[Dict[str, Any]]] = None


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
# REPORT FILE PARSER (EXCEL .XLSX/.XLS/.CSV & PDF .PDF)
# ==========================================
def _parse_money_value(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        if math.isnan(float(raw)):
            return 0.0
        return abs(float(raw))
    txt = str(raw).strip()
    if not txt:
        return 0.0
    txt = re.sub(r"[^\d,.-]", "", txt)
    if not txt or txt in ("-", ".", ","):
        return 0.0
    if "," in txt and "." in txt:
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", "")
    elif "," in txt:
        parts = txt.split(",")
        if len(parts[-1]) == 3:
            txt = txt.replace(",", "")
        else:
            txt = txt.replace(",", ".")
    elif "." in txt:
        parts = txt.split(".")
        if len(parts[-1]) == 3 and len(parts) >= 2:
            txt = txt.replace(".", "")
    try:
        return abs(float(txt))
    except Exception:
        return 0.0


def _infer_category_from_text(text: str) -> str:
    q = (text or "").lower()
    if any(k in q for k in ["gaji", "salary", "payroll", "bonus", "tunjangan", "honor", "freelance", "dividen"]):
        return "Salary"
    if any(k in q for k in ["makan", "food", "kopi", "coffee", "resto", "gofood", "grabfood", "warung", "cafe", "bakery", "nasi"]):
        return "Food"
    if any(k in q for k in ["grab", "gojek", "bensin", "pertamina", "shell", "parkir", "tol", "kereta", "mrt", "lrt", "ojek", "transport"]):
        return "Transport"
    if any(k in q for k in ["shopee", "tokopedia", "lazada", "tiktok", "blibli", "belanja", "shopping", "indomaret", "alfamart"]):
        return "Online Shopping"
    if any(k in q for k in ["listrik", "pln", "pdam", "air", "internet", "indihome", "wifi", "pulsa", "paket data", "telkomsel", "tagihan", "utilities"]):
        return "Utilities"
    if any(k in q for k in ["saham", "reksadana", "bibit", "bareksa", "emas", "gold", "invest", "deposito", "obligasi"]):
        return "Investment"
    if any(k in q for k in ["trading", "crypto", "binance", "indodax", "forex", "stock"]):
        return "Trading"
    return "Food"


def _normalize_date_str(raw_date: Any) -> str:
    if raw_date is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    s = str(raw_date).strip()
    if not s or s.lower() == "nan":
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
    ):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return s[:16]


def _promote_header_row_if_needed(df: Any) -> Any:
    header_keywords = {"date", "tanggal", "tgl", "amount", "nominal", "jumlah", "debit", "kredit", "credit", "type", "tipe", "jenis", "note", "catatan", "keterangan", "category", "kategori"}
    current_cols = {str(c).strip().lower() for c in df.columns}
    if current_cols & header_keywords:
        return df
    for r_idx in range(min(10, len(df))):
        row_vals = [str(v).strip().lower() for v in df.iloc[r_idx].tolist()]
        if set(row_vals) & header_keywords:
            new_df = df.iloc[r_idx + 1:].copy()
            new_df.columns = [str(v).strip() or f"col_{i}" for i, v in enumerate(df.iloc[r_idx].tolist())]
            return new_df.reset_index(drop=True)
    return df


def _parse_excel_or_csv_bytes(file_bytes: bytes, filename: str, default_wallet: str = "Bank BCA") -> List[Dict[str, Any]]:
    import pandas as pd

    lower_name = filename.lower()
    if lower_name.endswith(".csv"):
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), sep=None, engine="python")
        except Exception:
            df = pd.read_csv(io.BytesIO(file_bytes))
    else:
        xls = pd.ExcelFile(io.BytesIO(file_bytes))
        target_sheet = xls.sheet_names[0]
        for s in xls.sheet_names:
            if any(k in s.lower() for k in ["transaksi", "transaction", "ledger", "mutasi", "data", "laporan"]):
                target_sheet = s
                break
        df = pd.read_excel(xls, sheet_name=target_sheet)

    if df is None or df.empty:
        return []

    df = _promote_header_row_if_needed(df)
    df.columns = [str(c).strip().lower() for c in df.columns]
    col_date = next((c for c in df.columns if any(k in c for k in ["date", "tanggal", "tgl", "waktu"])), None)
    col_amount = next((c for c in df.columns if any(k in c for k in ["amount", "nominal", "jumlah", "nilai", "total"])), None)
    col_credit = next((c for c in df.columns if any(k in c for k in ["kredit", "credit", "masuk", "income", "pemasukan"])), None)
    col_debit = next((c for c in df.columns if any(k in c for k in ["debit", "keluar", "expense", "pengeluaran"])), None)
    col_type = next((c for c in df.columns if any(k in c for k in ["type", "tipe", "jenis", "arus", "db/cr", "mutasi"])), None)
    col_cat = next((c for c in df.columns if any(k in c for k in ["category", "kategori", "pos"])), None)
    col_acc = next((c for c in df.columns if any(k in c for k in ["account", "dompet", "rekening", "bank", "wallet", "akun"])), None)
    col_to_acc = next((c for c in df.columns if any(k in c for k in ["to_account", "to account", "tujuan", "ke_dompet"])), None)
    col_note = next((c for c in df.columns if any(k in c for k in ["note", "catatan", "keterangan", "deskripsi", "uraian", "merchant"])), None)

    extracted: List[Dict[str, Any]] = []
    now_ts = int(time.time() * 1000)

    for idx, row in df.iterrows():
        tx_type = "expense"
        amount = 0.0

        if col_credit and col_debit:
            cr_val = _parse_money_value(row.get(col_credit))
            db_val = _parse_money_value(row.get(col_debit))
            if cr_val > 0:
                tx_type = "income"
                amount = cr_val
            elif db_val > 0:
                tx_type = "expense"
                amount = db_val
        elif col_amount:
            raw_amt = row.get(col_amount)
            amount = _parse_money_value(raw_amt)
            if isinstance(raw_amt, (int, float)) and not math.isnan(float(raw_amt)) and float(raw_amt) < 0:
                tx_type = "expense"

        if amount <= 0:
            continue

        note_val = str(row.get(col_note) or "").strip() if col_note else ""
        if note_val.lower() == "nan":
            note_val = ""

        if col_type:
            t_raw = str(row.get(col_type) or "").strip().lower()
            if any(k in t_raw for k in ["income", "masuk", "pemasukan", "cr", "kredit", "credit"]):
                tx_type = "income"
            elif any(k in t_raw for k in ["transfer", "pindah"]):
                tx_type = "transfer"
            elif any(k in t_raw for k in ["expense", "keluar", "pengeluaran", "db", "debit"]):
                tx_type = "expense"
        elif not (col_credit and col_debit):
            if any(k in note_val.lower() for k in ["gaji", "salary", "masuk", "bonus", "terima"]):
                tx_type = "income"

        cat_val = str(row.get(col_cat) or "").strip() if col_cat else ""
        if not cat_val or cat_val.lower() == "nan":
            cat_val = "Transfer" if tx_type == "transfer" else ("Salary" if tx_type == "income" else _infer_category_from_text(note_val))

        acc_val = str(row.get(col_acc) or "").strip() if col_acc else ""
        if not acc_val or acc_val.lower() == "nan":
            acc_val = default_wallet

        to_acc_val = str(row.get(col_to_acc) or "").strip() if col_to_acc else ""
        if to_acc_val.lower() == "nan":
            to_acc_val = ""

        date_val = _normalize_date_str(row.get(col_date) if col_date else None)

        extracted.append({
            "id": now_ts + idx,
            "amount": round(amount, 2),
            "type": tx_type,
            "category": cat_val,
            "account": acc_val,
            "toAccount": to_acc_val,
            "adminFee": 0,
            "note": note_val or f"Import ({cat_val})",
            "currency": "IDR",
            "date": date_val,
        })

    return extracted


def _parse_pdf_bytes(file_bytes: bytes, default_wallet: str = "Bank BCA") -> List[Dict[str, Any]]:
    full_lines: List[str] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        for page in reader.pages:
            txt = page.extract_text() or ""
            for line in txt.splitlines():
                clean = line.strip()
                if clean:
                    full_lines.append(clean)
    except Exception:
        raw_ascii = file_bytes.decode("latin-1", errors="ignore")
        for m in re.findall(r"\(([^()]{3,160})\)\s*Tj", raw_ascii):
            clean = m.replace("\\(", "(").replace("\\)", ")").strip()
            if clean:
                full_lines.append(clean)

    extracted: List[Dict[str, Any]] = []
    now_ts = int(time.time() * 1000)

    dnd_pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)\s*\|\s*(income|expense|transfer|withdrawal)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([\d.,]+)(?:\s*\|\s*(.*))?$",
        re.IGNORECASE,
    )
    general_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{2,4})\s+(.+?)\s+(?:Rp\.?\s*)?([+-]?\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d{4,})\s*(CR|DB|KREDIT|DEBIT)?$",
        re.IGNORECASE,
    )

    for idx, line in enumerate(full_lines):
        m_dnd = dnd_pattern.match(line)
        if m_dnd:
            dt_str, t_type, cat, acc, amt_str, note = m_dnd.groups()
            amt = _parse_money_value(amt_str)
            if amt > 0:
                extracted.append({
                    "id": now_ts + idx,
                    "amount": round(amt, 2),
                    "type": t_type.lower().strip(),
                    "category": cat.strip(),
                    "account": acc.strip() or default_wallet,
                    "toAccount": "",
                    "adminFee": 0,
                    "note": (note or "").strip() or f"PDF Import ({cat.strip()})",
                    "currency": "IDR",
                    "date": _normalize_date_str(dt_str),
                })
            continue

        m_gen = general_pattern.search(line)
        if m_gen:
            dt_str, desc, amt_str, flag = m_gen.groups()
            amt = _parse_money_value(amt_str)
            if amt <= 0:
                continue
            desc_clean = re.sub(r"\s+", " ", desc).strip()
            flag_up = (flag or "").upper()
            is_income = (
                flag_up in ("CR", "KREDIT")
                or amt_str.startswith("+")
                or any(k in desc_clean.lower() for k in ["gaji", "salary", "kredit", "transfer masuk", "top up", "setor", "income", "pemasukan"])
            )
            tx_type = "income" if is_income else "expense"
            cat = "Salary" if is_income else _infer_category_from_text(desc_clean)
            extracted.append({
                "id": now_ts + idx,
                "amount": round(amt, 2),
                "type": tx_type,
                "category": cat,
                "account": default_wallet,
                "toAccount": "",
                "adminFee": 0,
                "note": desc_clean[:120],
                "currency": "IDR",
                "date": _normalize_date_str(dt_str),
            })

    if not extracted and full_lines:
        sample_text = "\n".join(full_lines[:90])
        ai_prompt = (
            "Extract all financial transactions from this PDF report text. "
            "Return ONLY a valid JSON array of objects with keys: "
            '[{"date": "YYYY-MM-DD HH:MM", "type": "income" or "expense" or "transfer", '
            '"category": string, "account": string, "amount": number, "note": string}].\n\n'
            f"PDF TEXT:\n{sample_text}"
        )
        raw_ai = _call_official_anthropic(
            ai_prompt,
            system_text="You are a financial ledger parser. Return ONLY a valid JSON array, no markdown.",
            max_tokens=2000,
            temperature=0.1,
        )
        if not raw_ai:
            raw_ai = _call_opensource_ai_backend(
                ai_prompt,
                system_text="Return ONLY a valid JSON array, no markdown.",
            )
        if raw_ai:
            try:
                clean_json = re.sub(r"^```(?:json)?|```$", "", raw_ai.strip(), flags=re.MULTILINE).strip()
                arr = json.loads(clean_json)
                if isinstance(arr, list):
                    for idx, item in enumerate(arr):
                        if not isinstance(item, dict):
                            continue
                        amt = _parse_money_value(item.get("amount"))
                        if amt <= 0:
                            continue
                        t_raw = str(item.get("type") or "expense").lower().strip()
                        tx_type = t_raw if t_raw in ("income", "expense", "transfer", "withdrawal") else "expense"
                        cat = str(item.get("category") or "").strip() or (
                            "Salary" if tx_type == "income" else _infer_category_from_text(str(item.get("note") or ""))
                        )
                        acc = str(item.get("account") or "").strip() or default_wallet
                        extracted.append({
                            "id": now_ts + idx,
                            "amount": round(amt, 2),
                            "type": tx_type,
                            "category": cat,
                            "account": acc,
                            "toAccount": "",
                            "adminFee": 0,
                            "note": str(item.get("note") or f"PDF Import ({cat})")[:120],
                            "currency": "IDR",
                            "date": _normalize_date_str(item.get("date")),
                        })
            except Exception:
                pass

    return extracted


# ==========================================
# QUANTITATIVE INTELLIGENCE & ANOMALY ENGINE
# ==========================================
def _compute_quantitative_intelligence(
    tx_rows: List[Dict[str, Any]],
    wallets: Dict[str, float],
    goals: List[Dict[str, Any]],
    lang: str = "ID",
) -> Dict[str, Any]:
    is_id = (lang or "ID").upper() == "ID"
    metrics = _compute_metrics_with_cpp(tx_rows)
    inc = float(metrics["total_income"])
    exp = float(metrics["total_expense"]) + float(metrics["total_withdrawal"])
    bal = float(metrics["net_balance"])

    unique_days = set()
    expense_amounts: List[float] = []
    expense_txs: List[Dict[str, Any]] = []
    cat_totals: Dict[str, float] = {}

    for t in tx_rows:
        d_str = str(t.get("date") or "")[:10]
        if d_str:
            unique_days.add(d_str)
        t_type = str(t.get("type") or "").lower()
        if t_type in ("expense", "withdrawal"):
            amt = float(t.get("amount") or 0.0)
            if amt > 0:
                expense_amounts.append(amt)
                expense_txs.append(t)
                cat = str(t.get("category") or "Other")
                cat_totals[cat] = cat_totals.get(cat, 0.0) + amt

    active_days = max(1, len(unique_days))
    daily_burn = round(exp / active_days, 2) if exp > 0 else 0.0
    monthly_burn_est = round(daily_burn * 30, 2) if daily_burn > 0 else 0.0
    runway_days = int(round(bal / daily_burn)) if (bal > 0 and daily_burn > 0) else (999 if bal > 0 else 0)

    savings_ratio = ((inc - exp) / inc) if inc > 0 else (1.0 if bal > 0 else 0.0)
    savings_pts = max(0.0, min(45.0, savings_ratio * 150.0))
    runway_pts = max(0.0, min(35.0, (min(runway_days, 180) / 180.0) * 35.0))
    positive_wallets = sum(1 for v in wallets.values() if float(v or 0) >= 0)
    total_wallets = max(1, len(wallets))
    wallet_pts = (positive_wallets / total_wallets) * 20.0
    composite_score = int(round(max(0, min(100, savings_pts + runway_pts + wallet_pts))))
    if not tx_rows and bal == 0:
        composite_score = 50

    if composite_score >= 75:
        grade = "Prima / Optimal" if is_id else "Prime / Optimal"
    elif composite_score >= 50:
        grade = "Stabil / Bertumbuh" if is_id else "Stable / Growing"
    elif composite_score >= 30:
        grade = "Waspada Arus Kas" if is_id else "Cashflow Caution"
    else:
        grade = "Perlu Restrukturisasi" if is_id else "Needs Restructuring"

    anomalies: List[Dict[str, Any]] = []
    if len(expense_amounts) >= 3:
        mean_exp = sum(expense_amounts) / len(expense_amounts)
        variance = sum((x - mean_exp) ** 2 for x in expense_amounts) / len(expense_amounts)
        std_dev = math.sqrt(variance)
        if std_dev > 0:
            for t in expense_txs:
                amt = float(t.get("amount") or 0.0)
                z_score = (amt - mean_exp) / std_dev
                if z_score >= 1.65 and amt > mean_exp * 1.6:
                    anomalies.append({
                        "note": str(t.get("note") or t.get("category") or "Expense"),
                        "category": str(t.get("category") or "Other"),
                        "amount": round(amt, 2),
                        "date": str(t.get("date") or ""),
                        "z_score": round(z_score, 2),
                        "multiplier": round(amt / max(1.0, mean_exp), 1),
                    })
    anomalies.sort(key=lambda x: x["amount"], reverse=True)

    monthly_net = inc - exp
    optimized_monthly_save = max(monthly_net, inc * 0.20) if inc > 0 else max(0.0, monthly_net)
    forecast = {
        "current_pace_6m": round(bal + (monthly_net * 6), 2),
        "current_pace_12m": round(bal + (monthly_net * 12), 2),
        "smart_20pct_6m": round(bal + (optimized_monthly_save * 6), 2),
        "smart_20pct_12m": round(bal + (optimized_monthly_save * 12), 2),
    }

    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
    top_category = sorted_cats[0][0] if sorted_cats else "-"
    top_category_amount = round(sorted_cats[0][1], 2) if sorted_cats else 0.0

    return {
        "health_score": composite_score,
        "health_grade": grade,
        "savings_rate_pct": round(savings_ratio * 100, 1) if inc > 0 else 0.0,
        "daily_burn_rate": daily_burn,
        "monthly_burn_est": monthly_burn_est,
        "runway_days": runway_days,
        "top_category": top_category,
        "top_category_amount": top_category_amount,
        "anomalies": anomalies[:3],
        "forecast": forecast,
        "total_goals_saved": round(sum(float(g.get("current") or 0) for g in goals), 2),
    }


def _extract_mentioned_salary(prompt_text: str) -> Optional[float]:
    q = (prompt_text or "").lower()
    m_juta = re.search(r"(?:gaji|penghasilan|pendapatan|income|salary|modal|uang|budget)[^\d]{0,18}(\d+(?:[.,]\d+)?)\s*(?:jt|juta|million|m\b)", q)
    if m_juta:
        val = float(m_juta.group(1).replace(",", ".")) * 1_000_000
        if val > 0:
            return val
    m_ribu = re.search(r"(?:gaji|penghasilan|pendapatan|income|salary|modal|uang|budget)[^\d]{0,18}(\d+(?:[.,]\d+)?)\s*(?:rb|ribu|k\b)", q)
    if m_ribu:
        val = float(m_ribu.group(1).replace(",", ".")) * 1_000
        if val > 0:
            return val
    m_num = re.search(r"(?:gaji|penghasilan|pendapatan|income|salary|modal|uang|budget)[^\d]{0,18}(?:rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+|\d{5,})", q)
    if m_num:
        digits = re.sub(r"[^\d]", "", m_num.group(1))
        if digits:
            val = float(digits)
            if val > 0:
                return val
    return None


def _build_expert_financial_advice(req: AIAnalysisRequest, metrics: Dict[str, Any]) -> str:
    is_id = (req.lang or "ID").upper() == "ID"
    curr_code = (req.currency or "IDR").upper()
    prefix = "Rp " if curr_code == "IDR" else f"{curr_code} "
    fmt = lambda v: f"{prefix}{float(v or 0):,.0f}"

    tx_rows = req.transactions or []
    wallets = req.wallets or {}
    goals = req.goals or []
    rules = req.automation_rules or []
    raw_prompt = (req.prompt or "").strip()
    q = raw_prompt.lower()

    intel = _compute_quantitative_intelligence(tx_rows, wallets, goals, req.lang or "ID")
    inc = float(metrics.get("total_income") or 0.0)
    exp = float(metrics.get("total_expense") or 0.0) + float(metrics.get("total_withdrawal") or 0.0)
    bal = float(metrics.get("net_balance") or 0.0)
    savings_rate = intel["savings_rate_pct"]

    mentioned_salary = _extract_mentioned_salary(raw_prompt)
    reference_income = mentioned_salary if (mentioned_salary and mentioned_salary > 0) else inc

    cat_totals: Dict[str, float] = {}
    for t in tx_rows:
        if str(t.get("type") or "").lower() in ("expense", "withdrawal"):
            amt = float(t.get("amount") or 0.0)
            cat = str(t.get("category") or "Other")
            cat_totals[cat] = cat_totals.get(cat, 0.0) + amt

    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
    top_cat_name, top_cat_val = sorted_cats[0] if sorted_cats else ("-", 0.0)

    neg_wallets = sorted([(k, float(v)) for k, v in wallets.items() if float(v or 0) < 0], key=lambda x: x[1])
    pos_wallets = sorted([(k, float(v)) for k, v in wallets.items() if float(v or 0) > 0], key=lambda x: x[1], reverse=True)

    alloc_needs = round(reference_income * 0.50)
    alloc_savings = round(reference_income * 0.20)
    alloc_wants = round(reference_income * 0.20)
    alloc_invest = round(reference_income * 0.10)
    weekly_allowance = round(alloc_wants / 4)
    ideal_emergency_3m = max(round(exp * 3), round(reference_income * 3 * 0.7))
    ideal_emergency_6m = ideal_emergency_3m * 2

    is_invest_q = any(k in q for k in ["invest", "saham", "emas", "reksa", "reksadana", "crypto", "deposito", "obligasi", "sbn", "dividen"])
    is_debt_q = any(k in q for k in ["utang", "hutang", "cicilan", "kpr", "kredit", "pinjol", "paylater", "bunga", "lunas"])
    is_business_q = any(k in q for k in ["bisnis", "usaha", "jualan", "modal", "side hustle", "penghasilan tambahan", "freelance", "omzet"])
    is_tax_q = any(k in q for k in ["pajak", "pph", "ppn", "npwp", "spt", "tax"])
    is_emergency_q = any(k in q for k in ["dana darurat", "emergency", "tabungan", "nikah", "rumah", "liburan", "beli"])
    is_autopilot_q = any(k in q for k in ["autopilot", "aturan", "rule", "otomatis"])
    is_forecast_q = any(k in q for k in ["prediksi", "forecast", "proyeksi", "target", "goal", "masa depan"])
    is_budget_q = any(k in q for k in ["anggaran", "budget", "hemat", "boros", "kurangi", "efisiensi"])

    if is_id:
        sections: List[str] = []

        if is_invest_q:
            inv_monthly = max(alloc_invest, round(max(0.0, inc - exp) * 0.4))
            sections.append("\n".join([
                "### Jawaban & Strategi Investasi Berdasarkan Profil Keuangan Anda",
                f"- **Kapasitas Investasi Bulanan Anda:** Dengan pemasukan **{fmt(reference_income)}** dan pengeluaran **{fmt(exp)}**, alokasi investasi ideal Anda adalah **{fmt(inv_monthly)}/bulan** setelah dana darurat minimal **{fmt(ideal_emergency_3m)}** mulai terbentuk.",
                "1. **Reksa Dana Pasar Uang / Deposito Digital (40%):** Likuid, stabil, cocok sebagai penyangga jangka pendek (< 1 tahun).",
                "2. **Emas / Logam Mulia & SBN Ritel (35%):** Melindungi nilai kekayaan dari inflasi jangka menengah (1–3 tahun).",
                "3. **Reksa Dana Indeks / Saham Blue-Chip (25%):** Pertumbuhan jangka panjang (> 3–5 tahun) dengan pembelian rutin setiap tanggal gajian.",
            ]))
        elif is_debt_q:
            max_installment = round(reference_income * 0.30)
            sections.append("\n".join([
                "### Strategi Pelunasan Cicilan & Manajemen Utang Sehat",
                f"- **Batas Aman Cicilan Bulanan:** Berdasarkan penghasilan **{fmt(reference_income)}**, total cicilan maksimal **30% atau {fmt(max_installment)}/bulan**.",
                "1. **Metode Avalanche (Prioritas Bunga Tertinggi):** Lunasi lebih dulu utang dengan bunga terbesar (paylater/kartu kredit), bayar minimum untuk lainnya.",
                "2. **Metode Snowball (Prioritas Saldo Terkecil):** Tutup utang berpokok paling kecil terlebih dahulu untuk mempercepat arus kas bebas.",
                "3. **Bekukan Utang Konsumtif Baru:** Gunakan saldo tunai/e-wallet yang sudah dijatah mingguan.",
            ]))
        elif is_business_q:
            seed_cap = max(500000, round(max(bal, alloc_invest * 3)))
            sections.append("\n".join([
                "### Panduan Modal Usaha & Penghasilan Tambahan",
                f"- **Kesiapan Modal Aman:** Dari saldo bersih **{fmt(bal)}** dan penyisihan **{fmt(alloc_invest)}/bulan**, modal eksperimen aman Anda adalah **{fmt(seed_cap)}** tanpa mengganggu kebutuhan pokok (**{fmt(alloc_needs)}/bulan**).",
                "1. **Pisahkan Rekening Pribadi & Usaha:** Dedikasikan satu dompet (misal **Bank Jago** atau **SeaBank**) khusus kas usaha.",
                "2. **Model Low-Capex / Pre-Order:** Utamakan jasa keahlian atau reseller terkurasi agar perputaran kas kembali dalam 7–14 hari.",
            ]))
        elif is_tax_q:
            vat_est = float(metrics.get("vat_11_percent") or 0.0)
            sections.append("\n".join([
                "### Panduan Estimasi Pajak & Kepatuhan Finansial",
                f"- **Simulasi Pencadangan:** Dari total pemasukan tercatat **{fmt(inc)}**, cadangan kewajiban 11% setara **{fmt(vat_est)}**.",
                "1. **Karyawan Tetap (PPh 21):** Pastikan pemasukan yang dicatat adalah gaji bersih (*take-home pay*) sesuai bukti potong 1721-A1.",
                "2. **Freelancer / UMKM:** Sisihkan **5%–10%** setiap pembayaran masuk ke dompet terpisah untuk pelaporan SPT Tahunan.",
            ]))
        elif is_emergency_q:
            sections.append("\n".join([
                "### Peta Jalan Dana Darurat & Target Finansial",
                f"- **Target Dana Darurat Ideal:** Tahap aman 3 bulan sebesar **{fmt(ideal_emergency_3m)}**, dan tahap penuh 6 bulan sebesar **{fmt(ideal_emergency_6m)}**.",
                f"- **Estimasi Waktu Tercapai:** Dengan menabung **{fmt(alloc_savings)}/bulan**, tahap pertama tercapai dalam **~{max(1, round(ideal_emergency_3m / max(1, alloc_savings), 1))} bulan**.",
            ]))
        elif not any([is_autopilot_q, is_forecast_q, is_budget_q]) and not any(k in q for k in ["gaji", "kelola", "mengatur", "atur", "analisis", "audit", "keuangan", "uang"]):
            sections.append("\n".join([
                f"### Jawaban Asisten Cerdas untuk: \"{raw_prompt}\"",
                f"- **Konteks Finansial Anda:** Skor kesehatan keuangan Anda saat ini **{intel['health_score']}/100 ({intel['health_grade']})**, dengan saldo bersih **{fmt(bal)}**, pemasukan **{fmt(inc)}**, dan pengeluaran **{fmt(exp)}**.",
                f"- **Panduan Keputusan:** Pastikan setiap komitmen baru tetap berada di bawah kapasitas belanja mingguan Anda (**{fmt(weekly_allowance)}/minggu**) serta selaras dengan target tabungan jangka menengah.",
            ]))

        diag_lines = [
            f"### 1. Diagnosis Kuantitatif & Skor Kesehatan ({intel['health_score']}/100 — {intel['health_grade']})",
            f"- **Arus Kas & Rasio Simpanan:** Pemasukan **{fmt(inc)}** vs pengeluaran **{fmt(exp)}**, saldo bersih **{fmt(bal)}** (rasio simpanan **{savings_rate}%**, target sehat **≥ 20%**).",
            f"- **Burn Rate & Ketahanan Kas:** Pengeluaran harian rata-rata **{fmt(intel['daily_burn_rate'])}/hari** dengan estimasi ketahanan dana (**runway**) **{intel['runway_days']} hari**.",
        ]
        if sorted_cats:
            top3_txt = ", ".join(f"**{c}** ({fmt(v)} / {round((v / exp) * 100, 1)}%)" for c, v in sorted_cats[:3])
            diag_lines.append(f"- **Pos Pengeluaran Terbesar:** {top3_txt}.")
        if intel["anomalies"]:
            anom_txt = ", ".join(f"**{a['note']}** ({fmt(a['amount'])}, {a['multiplier']}x rata-rata)" for a in intel["anomalies"])
            diag_lines.append(f"- **Deteksi Lonjakan Tidak Biasa:** {anom_txt}.")
        sections.append("\n".join(diag_lines))

        if reference_income > 0:
            basis_label = (
                f"nominal **{fmt(reference_income)}** yang Anda sebutkan"
                if mentioned_salary
                else f"pemasukan tercatat Anda (**{fmt(reference_income)}**)"
            )
            sections.append("\n".join([
                "### 2. Pembagian Pos Gaji Ideal (Formula 50 / 20 / 20 / 10)",
                f"Berdasarkan {basis_label}:",
                f"1. **50% Kebutuhan Pokok & Tagihan — {fmt(alloc_needs)}/bulan**",
                f"2. **20% Tabungan & Dana Darurat (Bayar Diri Sendiri di Awal) — {fmt(alloc_savings)}/bulan**",
                f"3. **20% Gaya Hidup & Kebutuhan Fleksibel — {fmt(alloc_wants)}/bulan (~{fmt(weekly_allowance)}/minggu)**",
                f"4. **10% Investasi & Aset Produktif — {fmt(alloc_invest)}/bulan**",
            ]))

        wallet_lines = ["### 3. Penataan Multi-Dompet & Proyeksi Kekayaan"]
        if neg_wallets:
            neg_list = ", ".join(f"**{k}** ({fmt(v)})" for k, v in neg_wallets)
            wallet_lines.append(f"- **Perbaikan Dompet Bersaldo Minus:** Perbarui saldo awal atau lakukan transfer penyeimbang pada {neg_list}.")
        if pos_wallets:
            pos_list = ", ".join(f"**{k}** ({fmt(v)})" for k, v in pos_wallets[:4])
            wallet_lines.append(f"- **Dompet Aktif:** {pos_list}.")
        wallet_lines.append(
            f"- **Proyeksi 6 & 12 Bulan:** Dengan pola saat ini saldo diproyeksikan ke **{fmt(intel['forecast']['current_pace_6m'])}** (6 bln); dengan disiplin 20% tabungan mencapai **{fmt(intel['forecast']['smart_20pct_6m'])}** (6 bln) & **{fmt(intel['forecast']['smart_20pct_12m'])}** (12 bln)."
        )
        sections.append("\n".join(wallet_lines))

        rec_save_pct = 15 if savings_rate < 10 else 20
        rec_cap = max(250000, round((top_cat_val * 0.85) / 50000) * 50000) if top_cat_val > 0 else 1000000
        active_rules_cnt = sum(1 for r in rules if r.get("enabled"))
        sections.append("\n".join([
            "### 4. Langkah Aksi & Otomatisasi Autopilot",
            f"1. **Aturan Autopilot ({active_rules_cnt} aktif):** Aktifkan penyisihan otomatis **{rec_save_pct}%** setiap pemasukan ke **Dana Darurat**.",
            f"2. **Batas Anggaran {top_cat_name}:** Pasang batas maksimal **{fmt(rec_cap)}/bulan** pada tab **Target**.",
            f"3. **Disiplin Belanja Mingguan:** Batasi transaksi fleksibel maksimal **{fmt(weekly_allowance)}/minggu** agar rasio simpanan terjaga di atas **20%**.",
        ]))

        return "\n\n".join(sections)

    return "\n\n".join([
        "\n".join([
            f"### 1. Quantitative Diagnosis ({intel['health_score']}/100 — {intel['health_grade']})",
            f"- **Cashflow & Savings Rate:** Income **{fmt(inc)}** vs Outflow **{fmt(exp)}**, net balance **{fmt(bal)}** (**{savings_rate}%** savings rate).",
            f"- **Burn Rate & Runway:** Daily burn **{fmt(intel['daily_burn_rate'])}/day** with **{intel['runway_days']} days** cash runway.",
            f"- **Top Expense Category:** **{top_cat_name}** ({fmt(top_cat_val)}).",
        ]),
        "\n".join([
            "### 2. Recommended Monthly Allocation (50 / 20 / 20 / 10)",
            f"1. **50% Essential Needs ({fmt(alloc_needs)}/mo)**",
            f"2. **20% Pay-Yourself-First Savings ({fmt(alloc_savings)}/mo)** toward **{fmt(ideal_emergency_3m)}** emergency fund.",
            f"3. **20% Flexible Wants ({fmt(alloc_wants)}/mo or ~{fmt(weekly_allowance)}/wk)**",
            f"4. **10% Investments ({fmt(alloc_invest)}/mo)**",
        ]),
        "\n".join([
            "### 3. 6-Month & 12-Month Wealth Forecast",
            f"- **Current Pace (6m / 12m):** **{fmt(intel['forecast']['current_pace_6m'])}** / **{fmt(intel['forecast']['current_pace_12m'])}**",
            f"- **20% Smart Saver Pace (6m / 12m):** **{fmt(intel['forecast']['smart_20pct_6m'])}** / **{fmt(intel['forecast']['smart_20pct_12m'])}**",
        ]),
    ])


# ==========================================
# HEALTH & APP METADATA ENDPOINTS
# ==========================================
@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "cpp_engine": _load_cpp_engine() is not None,
        "anthropic_sdk_installed": HAS_ANTHROPIC_SDK,
        "ai_ready": True,
    }


@app.get("/api/app/info")
async def get_app_info():
    return {
        "platform": "DND Finance",
        "developer": DEVELOPER_NAME,
        "instagram_handle": INSTAGRAM_HANDLE,
        "instagram_url": INSTAGRAM_URL,
        "google_client_id": _get_setting("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID),
        "ai_ready": True,
    }


# ==========================================
# QUANTITATIVE INTELLIGENCE & SMART TX PARSER
# ==========================================
@app.post("/api/analytics/intelligence")
async def get_quantitative_intelligence(payload: QuantitativeIntelligencePayload):
    txs = payload.transactions or []
    wallets = payload.wallets or {}
    goals = payload.goals or []
    data = _compute_quantitative_intelligence(txs, wallets, goals, payload.lang or "ID")
    return {"status": "ok", **data}


@app.post("/api/ai/parse-tx")
async def smart_parse_transaction(payload: SmartParseTxPayload):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text required")

    banks = payload.banks or ["Bank Mandiri", "Bank BCA", "Bank BNI", "Bank Jago", "SeaBank", "Gopay", "Cash"]
    categories = payload.categories or ["Food", "Transport", "Trading", "Online Shopping", "Investment", "Salary", "Utilities"]

    ai_prompt = (
        f"Parse this natural language financial entry into JSON.\n"
        f"Available Wallets: {json.dumps(banks)}\n"
        f"Available Categories: {json.dumps(categories)}\n"
        f"Input Text: \"{text}\"\n"
        'Return ONLY valid JSON: {"type": "income" or "expense" or "transfer", "amount": number, '
        '"category": string, "account": string, "to_account": string, "note": string}.'
    )
    raw = _call_official_anthropic(
        ai_prompt,
        system_text="You are a financial transaction parser. Return ONLY raw valid JSON without markdown.",
        max_tokens=400,
        temperature=0.0,
    )
    if raw:
        try:
            clean = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(clean)
            if float(parsed.get("amount") or 0) > 0:
                return {
                    "status": "ok",
                    "source": "DND Smart AI",
                    "type": parsed.get("type") if parsed.get("type") in ("income", "expense", "transfer") else "expense",
                    "amount": round(float(parsed.get("amount")), 2),
                    "category": parsed.get("category") if parsed.get("category") in categories else _infer_category_from_text(text),
                    "account": parsed.get("account") if parsed.get("account") in banks else banks[0],
                    "to_account": parsed.get("to_account") or (banks[1] if len(banks) > 1 else banks[0]),
                    "note": str(parsed.get("note") or text)[:100],
                }
        except Exception:
            pass

    q = text.lower()
    amount = 0.0
    m_jt = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:jt|juta|million|m\b)", q)
    m_rb = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:rb|ribu|k\b)", q)
    m_num = re.search(r"(?:rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+|\d{4,})", q)
    if m_jt:
        amount = float(m_jt.group(1).replace(",", ".")) * 1_000_000
    elif m_rb:
        amount = float(m_rb.group(1).replace(",", ".")) * 1_000
    elif m_num:
        amount = _parse_money_value(m_num.group(1))

    tx_type = "expense"
    if any(k in q for k in ["transfer", "kirim ke", "pindah ke", "tf ke"]):
        tx_type = "transfer"
    elif any(k in q for k in ["gaji", "terima", "dapat", "masuk", "bonus", "salary", "income", "jual"]):
        tx_type = "income"

    matched_bank = banks[0]
    for b in banks:
        b_low = b.lower()
        short_b = b_low.replace("bank ", "").strip()
        if b_low in q or (short_b and short_b in q):
            matched_bank = b
            break

    cat = "Transfer" if tx_type == "transfer" else ("Salary" if tx_type == "income" else _infer_category_from_text(text))
    if cat not in categories and categories:
        cat = categories[0]

    clean_note = re.sub(r"(?:rp\.?\s*)?\d+(?:[.,]\d+)*\s*(?:jt|juta|rb|ribu|k|million)?", "", text, flags=re.IGNORECASE).strip(" -–,.")
    return {
        "status": "ok",
        "source": "DND NLP Engine",
        "type": tx_type,
        "amount": round(amount, 2),
        "category": cat,
        "account": matched_bank,
        "to_account": banks[1] if len(banks) > 1 else banks[0],
        "note": (clean_note or text)[:100],
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
# SPLIT BILL & RECEIPT VISION OCR ENDPOINTS
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
    raw = _call_official_anthropic_vision(
        payload.image_base64,
        payload.mime_type or "image/jpeg",
        prompt,
    )
    if raw:
        try:
            clean_json = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(clean_json)
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
# REPORT IMPORT ENDPOINT (EXCEL / CSV / PDF)
# ==========================================
@app.post("/api/import/report")
async def import_financial_report(request: Request, file: UploadFile = File(...)):
    filename = (file.filename or "report.xlsx").strip()
    lower = filename.lower()
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    default_wallet = (request.headers.get("X-Default-Wallet") or "Bank BCA").strip() or "Bank BCA"
    user_email = (request.headers.get("X-User-Email") or "guest").strip().lower()

    try:
        if lower.endswith((".xlsx", ".xls", ".csv")):
            parsed_txs = _parse_excel_or_csv_bytes(raw_bytes, filename, default_wallet=default_wallet)
        elif lower.endswith(".pdf"):
            parsed_txs = _parse_pdf_bytes(raw_bytes, default_wallet=default_wallet)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Use .xlsx, .xls, .csv, or .pdf")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to parse report: {str(exc)[:120]}")

    if parsed_txs:
        conn = get_db()
        for t in parsed_txs:
            conn.execute(
                """
                INSERT INTO transactions (user_email, amount, type, category, account, to_account, admin_fee, note, currency, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_email,
                    t["amount"],
                    t["type"],
                    t["category"],
                    t["account"],
                    t.get("toAccount") or "",
                    t.get("adminFee") or 0.0,
                    t.get("note") or "",
                    t.get("currency") or "IDR",
                    t["date"],
                ),
            )
        conn.commit()
        conn.close()

    return {
        "status": "ok",
        "filename": filename,
        "imported_count": len(parsed_txs),
        "transactions": parsed_txs,
    }


# ==========================================
# BUILT-IN SMART AI FINANCIAL ANALYST
# ==========================================
@app.post("/api/ai/analyze")
async def analyze_financial_data(req: AIAnalysisRequest):
    tx_rows = req.transactions if req.transactions is not None else []
    wallets = req.wallets or {}
    goals = req.goals or []
    metrics = _compute_metrics_with_cpp(tx_rows)
    intel = _compute_quantitative_intelligence(tx_rows, wallets, goals, req.lang or "ID")
    is_id = (req.lang or "ID").upper() == "ID"

    wallet_lines = ", ".join(f"{k}: {v:,.0f}" for k, v in wallets.items()) or "-"
    goal_lines = ", ".join(
        f"{g.get('name')} ({float(g.get('current') or 0):,.0f}/{float(g.get('target') or 0):,.0f})"
        for g in goals
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
        for t in tx_rows[:35]
    ) or ("Tidak ada transaksi" if is_id else "No transactions")

    history_lines = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('text', '')}"
        for m in (req.chat_history or [])[-6:]
    )

    full_dossier = (
        f"Mata Uang: {req.currency or 'IDR'}\n"
        f"Skor Kesehatan Finansial: {intel['health_score']}/100 ({intel['health_grade']})\n"
        f"Ringkasan Kuantitatif: Pemasukan={metrics['total_income']:,.0f}, "
        f"Pengeluaran={metrics['total_expense'] + metrics['total_withdrawal']:,.0f}, "
        f"Saldo Bersih={metrics['net_balance']:,.0f}, Rasio Simpanan={intel['savings_rate_pct']}%, "
        f"Daily Burn Rate={intel['daily_burn_rate']:,.0f}/hari, Runway={intel['runway_days']} hari\n"
        f"Proyeksi 6 Bulan (Pola Sekarang vs 20% Saver): {intel['forecast']['current_pace_6m']:,.0f} vs {intel['forecast']['smart_20pct_6m']:,.0f}\n"
        f"Saldo Semua Dompet/Rekening: {wallet_lines}\n"
        f"Target Tabungan: {goal_lines}\n"
        f"Anggaran Kategori: {budget_lines}\n"
        f"Aturan Autopilot: {rule_lines}\n"
        f"Konteks Tambahan:\n{req.financial_context or ''}\n"
        f"35 Transaksi Terakhir:\n{recent_tx_lines}"
    )
    if history_lines:
        full_dossier += f"\n\nRiwayat Percakapan Sebelumnya:\n{history_lines}"

    system_instruction = (
        "Anda adalah Penasihat Kekayaan & Perencana Keuangan Cerdas (CFP) di DND Finance. "
        "Anda mampu menjawab SEMUA jenis pertanyaan pengguna secara mendalam, komprehensif, dan akurat—baik tentang "
        "pengelolaan keuangan pribadi, pembagian gaji, investasi (emas, reksa dana, saham, obligasi), strategi melunasi utang/cicilan, "
        "modal bisnis/side hustle, pajak, maupun pertanyaan umum di luar rekomendasi standar. "
        "Selalu jawab inti pertanyaan pengguna secara langsung terlebih dahulu, lalu selaraskan dengan data keuangan aktual pengguna "
        "(pemasukan, pengeluaran, skor kesehatan finansial, burn rate, runway, saldo tiap dompet, target tabungan, anggaran, dan Autopilot). "
        "Gunakan struktur markdown yang rapi (### Judul Bagian, poin-poin dengan angka nyata). "
        f"Jawab sepenuhnya dalam {'Bahasa Indonesia yang tajam, profesional, hangat, dan solutif' if is_id else 'sharp, professional, and actionable English'}."
    )
    combined_prompt = f"Pertanyaan Pengguna: {req.prompt}\n\n=== DATA KEUANGAN LENGKAP PENGGUNA ===\n{full_dossier}"

    ai_text = _call_official_anthropic(
        combined_prompt,
        system_text=system_instruction,
        chat_history=req.chat_history,
    )
    source_used = "DND Smart AI"

    if not ai_text:
        ai_text = _call_opensource_ai_backend(combined_prompt, system_text=system_instruction)
        source_used = "DND Smart AI"

    if not ai_text:
        ai_text = _build_expert_financial_advice(req, metrics)
        source_used = "DND Financial Intelligence"

    ai_text = ai_text.replace("(python_fallback)", "").replace("python_fallback", "").replace("(cpp17_native)", "").strip()
    return {"status": "success", "source": source_used, "analysis": ai_text}


# ==========================================
# EXCEL & PDF EXPORT ENDPOINTS (DIRECT DATA & DB)
# ==========================================
def _generate_excel_bytes(rows: List[Dict[str, Any]], wallets: Dict[str, float], goals: List[Dict[str, Any]], lang: str, currency: str) -> io.BytesIO:
    import pandas as pd

    metrics = _compute_metrics_with_cpp(rows)
    intel = _compute_quantitative_intelligence(rows, wallets, goals, lang)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        wb = writer.book
        hdr_fmt = wb.add_format({"bold": True, "bg_color": "#2563EB", "font_color": "#FFFFFF", "border": 1})
        num_fmt = wb.add_format({"num_format": "#,##0", "border": 1})
        cell_fmt = wb.add_format({"border": 1})

        summary_data = [
            {"Metric": "Platform", "Value": "DND Finance"},
            {"Metric": "Generated At", "Value": datetime.now().strftime("%Y-%m-%d %H:%M")},
            {"Metric": "Currency", "Value": currency},
            {"Metric": "Total Income", "Value": metrics["total_income"]},
            {"Metric": "Total Expense", "Value": metrics["total_expense"] + metrics["total_withdrawal"]},
            {"Metric": "Net Balance", "Value": metrics["net_balance"]},
            {"Metric": "Financial Health Score", "Value": f"{intel['health_score']}/100 ({intel['health_grade']})"},
            {"Metric": "Daily Burn Rate", "Value": intel["daily_burn_rate"]},
            {"Metric": "Cash Runway (Days)", "Value": intel["runway_days"]},
        ]
        for w_name, w_bal in (wallets or {}).items():
            summary_data.append({"Metric": f"Wallet: {w_name}", "Value": float(w_bal or 0)})
        for g in (goals or []):
            summary_data.append({
                "Metric": f"Goal: {g.get('name', 'Target')}",
                "Value": f"{float(g.get('current') or 0):,.0f} / {float(g.get('target') or 0):,.0f}",
            })

        df_sum = pd.DataFrame(summary_data)
        df_sum.to_excel(writer, index=False, sheet_name="Summary")
        ws_sum = writer.sheets["Summary"]
        ws_sum.set_column(0, 0, 28)
        ws_sum.set_column(1, 1, 26)
        for col_idx, col_name in enumerate(df_sum.columns):
            ws_sum.write(0, col_idx, col_name, hdr_fmt)

        clean_rows = []
        for r in rows:
            clean_rows.append({
                "Date": r.get("date", ""),
                "Type": r.get("type", ""),
                "Category": r.get("category", ""),
                "Account": r.get("account", ""),
                "To Account": r.get("toAccount") or r.get("to_account") or "",
                "Amount": float(r.get("amount") or 0),
                "Admin Fee": float(r.get("adminFee") or r.get("admin_fee") or 0),
                "Note": r.get("note", ""),
            })
        df_tx = pd.DataFrame(clean_rows) if clean_rows else pd.DataFrame(
            columns=["Date", "Type", "Category", "Account", "To Account", "Amount", "Admin Fee", "Note"]
        )
        df_tx.to_excel(writer, index=False, sheet_name="Transactions")
        ws_tx = writer.sheets["Transactions"]
        widths = [18, 12, 18, 18, 18, 16, 12, 34]
        for i, w in enumerate(widths):
            ws_tx.set_column(i, i, w)
        for col_idx, col_name in enumerate(df_tx.columns):
            ws_tx.write(0, col_idx, col_name, hdr_fmt)
        for row_idx, row_vals in enumerate(df_tx.values, start=1):
            for col_idx, val in enumerate(row_vals):
                if col_idx in (5, 6):
                    ws_tx.write_number(row_idx, col_idx, float(val or 0), num_fmt)
                else:
                    ws_tx.write(row_idx, col_idx, str(val or ""), cell_fmt)

    output.seek(0)
    return output


def _generate_pdf_bytes(rows: List[Dict[str, Any]], wallets: Dict[str, float], lang: str, currency: str) -> io.BytesIO:
    metrics = _compute_metrics_with_cpp(rows)
    intel = _compute_quantitative_intelligence(rows, wallets, [], lang)
    lines = [
        "DND Finance - Personal Wealth & Cashflow Report",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Currency: {currency}",
        "=" * 74,
        f"Total Income   : {currency} {metrics['total_income']:,.0f}",
        f"Total Expense  : {currency} {metrics['total_expense'] + metrics['total_withdrawal']:,.0f}",
        f"Net Balance    : {currency} {metrics['net_balance']:,.0f} | Health Score: {intel['health_score']}/100",
        "-" * 74,
    ]
    if wallets:
        w_str = " | ".join(f"{k}: {float(v or 0):,.0f}" for k, v in wallets.items())
        lines.append(f"Wallets: {w_str[:70]}")
        lines.append("-" * 74)

    lines.append("DATE             | TYPE     | CATEGORY        | WALLET       | AMOUNT")
    lines.append("-" * 74)
    for r in rows[:45]:
        dt = str(r.get("date") or "")[:16].ljust(16)
        tp = str(r.get("type") or "")[:8].ljust(8)
        ct = str(r.get("category") or "")[:15].ljust(15)
        ac = str(r.get("account") or "")[:12].ljust(12)
        am = f"{float(r.get('amount') or 0):,.0f}"
        lines.append(f"{dt} | {tp} | {ct} | {ac} | {am}")
        note = str(r.get("note") or "").strip()
        if note:
            lines.append(f"   Note: {note[:66]}")

    safe_lines = [ln.encode("latin-1", errors="replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    stream_cmds = ["BT", "/F1 9 Tf", "36 755 Td"]
    for idx, ln in enumerate(safe_lines):
        if idx == 0:
            stream_cmds.append(f"({ln}) Tj")
        else:
            stream_cmds.append(f"0 -12 Td ({ln}) Tj")
    stream_cmds.append("ET")
    stream_bytes = "\n".join(stream_cmds).encode("latin-1")

    obj1 = b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
    obj2 = b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
    obj3 = b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
    obj4 = f"4 0 obj<< /Length {len(stream_bytes)} >>stream\n".encode("ascii") + stream_bytes + b"\nendstream\nendobj\n"
    obj5 = b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>endobj\n"

    header = b"%PDF-1.4\n"
    offsets = [0]
    body = bytearray()
    for obj in (obj1, obj2, obj3, obj4, obj5):
        offsets.append(len(header) + len(body))
        body.extend(obj)

    xref_offset = len(header) + len(body)
    xref = [f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii")]
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n".encode("ascii"))
    trailer = f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")

    pdf_full = header + bytes(body) + b"".join(xref) + trailer
    return io.BytesIO(pdf_full)


@app.post("/api/export/excel")
async def export_excel_post(payload: ReportExportPayload):
    rows = payload.transactions or []
    wallets = payload.wallets or {}
    goals = payload.goals or []
    buf = _generate_excel_bytes(rows, wallets, goals, payload.lang or "ID", payload.currency or "IDR")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=DND_Financial_Report_{payload.lang or 'ID'}.xlsx"},
    )


@app.get("/api/export/excel")
async def export_excel_get(lang: str = "ID", email: str = ""):
    clean_email = (email or "").strip().lower()
    conn = get_db()
    rows = []
    wallets = {}
    goals = []
    if clean_email:
        ws_row = conn.execute("SELECT state_json FROM workspace_states WHERE email = ?", (clean_email,)).fetchone()
        if ws_row and ws_row["state_json"]:
            st = json.loads(ws_row["state_json"])
            rows = st.get("transactions") or []
            goals = st.get("goals") or []
    if not rows:
        db_rows = conn.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
        rows = [dict(r) for r in db_rows]
    conn.close()
    buf = _generate_excel_bytes(rows, wallets, goals, lang, "IDR")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=DND_Financial_Report_{lang}.xlsx"},
    )


@app.post("/api/export/pdf")
async def export_pdf_post(payload: ReportExportPayload):
    rows = payload.transactions or []
    wallets = payload.wallets or {}
    buf = _generate_pdf_bytes(rows, wallets, payload.lang or "ID", payload.currency or "IDR")
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=DND_Financial_Report_{payload.lang or 'ID'}.pdf"},
    )


@app.get("/api/export/pdf")
async def export_pdf_get(lang: str = "ID", email: str = ""):
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
    buf = _generate_pdf_bytes(rows, {}, lang, "IDR")
    return StreamingResponse(
        buf,
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
