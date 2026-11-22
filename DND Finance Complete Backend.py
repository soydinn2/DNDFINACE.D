from fastapi import FastAPI, BackgroundTasks, Request, Response, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
from urllib.parse import urlencode
import pandas as pd
import sqlite3
import os
import re
import json
import base64
import secrets
import urllib.request
import urllib.parse
import time
import ctypes
import subprocess

app = FastAPI(
    title="DND Finance Personal Ledger API",
    description="Full-Stack Public Multi-User Backend with C++17 Quantitative Engine, Google Identity Services (FedCM & OAuth + Cross-Device Drive AppData Refresh) Authentication, Receipt OCR/Vision Scan API, AI Financial Intelligence & PDF/Excel Export",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR) if os.path.basename(BASE_DIR) == "api" else BASE_DIR
IS_READONLY_FS = bool(os.getenv("VERCEL") or not os.access(PROJECT_ROOT, os.W_OK))
WORK_DIR = "/tmp" if IS_READONLY_FS else PROJECT_ROOT
DB_FILE = os.getenv("DND_DB_PATH", os.path.join(WORK_DIR, "dnd_finance.db"))

DEFAULT_GOOGLE_CLIENT_ID = "753367715469-972f7qjb3t97qab938sc5lo69mgp78h0.apps.googleusercontent.com"
DEFAULT_GOOGLE_CLIENT_SECRET = "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"
DEFAULT_BANKS = ["Bank Mandiri", "Bank BCA", "Bank BNI", "Bank Jago", "SeaBank", "Gopay", "Cash"]
DEFAULT_CATEGORIES = ["Food", "Transport", "Trading", "Online Shopping", "Investment", "Salary", "Utilities"]

MEMORY_SESSIONS: Dict[str, Dict[str, Any]] = {}
MEMORY_USERS_BY_EMAIL: Dict[str, Dict[str, Any]] = {}
MEMORY_WORKSPACES: Dict[str, Dict[str, Any]] = {}
MEMORY_REFRESH_TOKENS: Dict[str, str] = {}


def _kv_request(command_parts: List[str]) -> Any:
    """Mendukung persistensi Upstash / Vercel KV REST API secara otomatis jika tersedia di environment"""
    kv_url = (os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL") or "").strip().rstrip("/")
    kv_token = (os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if not kv_url or not kv_token:
        return None
    try:
        req = urllib.request.Request(
            kv_url,
            data=json.dumps(command_parts).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {kv_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            return parsed.get("result")
    except Exception:
        return None


def _save_user_refresh_token(email: str, refresh_token: str) -> None:
    clean_email = (email or "").strip().lower()
    rt = (refresh_token or "").strip()
    if not clean_email or not rt:
        return
    MEMORY_REFRESH_TOKENS[clean_email] = rt
    _kv_request(["SET", f"dnd_rtoken:{clean_email}", rt])
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET refresh_token = ? WHERE LOWER(email) = ?", (rt, clean_email))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_user_refresh_token(email: str) -> str:
    clean_email = (email or "").strip().lower()
    if not clean_email:
        return ""
    if clean_email in MEMORY_REFRESH_TOKENS and MEMORY_REFRESH_TOKENS[clean_email]:
        return MEMORY_REFRESH_TOKENS[clean_email]
    kv_rt = _kv_request(["GET", f"dnd_rtoken:{clean_email}"])
    if kv_rt:
        MEMORY_REFRESH_TOKENS[clean_email] = str(kv_rt)
        return str(kv_rt)
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("SELECT refresh_token FROM users WHERE LOWER(email) = ?", (clean_email,))
        row = c.fetchone()
        conn.close()
        if row and row["refresh_token"]:
            MEMORY_REFRESH_TOKENS[clean_email] = str(row["refresh_token"])
            return str(row["refresh_token"])
    except Exception:
        pass
    return ""


def _exchange_refresh_token_for_access_token(refresh_token: str) -> Dict[str, Any]:
    rtoken = (refresh_token or "").strip()
    if not rtoken:
        return {}
    client_id = _get_setting("GOOGLE_CLIENT_ID")
    client_secret = _get_setting("GOOGLE_CLIENT_SECRET")
    body_data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": rtoken,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))
            return {
                "access_token": tokens.get("access_token", ""),
                "expires_in": tokens.get("expires_in", 3500),
            }
    except Exception:
        return {}


def _merge_workspace_dicts(existing: Optional[Dict[str, Any]], incoming: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Menggabungkan state keuangan dari berbagai perangkat (Handphone, Laptop, iPad, Mac) termasuk penghapusan"""
    base = dict(existing or {})
    inc = dict(incoming or {})
    if not base:
        return inc
    if not inc:
        return base

    del_tx_ids = list({str(x) for x in list(base.get("deletedTxIds") or []) + list(inc.get("deletedTxIds") or [])})
    del_rule_ids = list({str(x) for x in list(base.get("deletedRuleIds") or []) + list(inc.get("deletedRuleIds") or [])})
    del_goal_ids = list({str(x) for x in list(base.get("deletedGoalIds") or []) + list(inc.get("deletedGoalIds") or [])})
    del_budget_ids = list({str(x) for x in list(base.get("deletedBudgetIds") or []) + list(inc.get("deletedBudgetIds") or [])})
    del_banks = list({str(x) for x in list(base.get("deletedBanks") or []) + list(inc.get("deletedBanks") or [])})
    del_categories = list({str(x) for x in list(base.get("deletedCategories") or []) + list(inc.get("deletedCategories") or [])})

    del_tx_set = set(del_tx_ids)
    del_rule_set = set(del_rule_ids)
    del_goal_set = set(del_goal_ids)
    del_budget_set = set(del_budget_ids)
    del_bank_set = set(del_banks)
    del_cat_set = set(del_categories)

    # 1. Union banks (respecting deletions)
    banks: List[str] = []
    for b in list(base.get("banks") or []) + list(inc.get("banks") or []):
        clean = str(b or "").strip()
        if clean and clean not in del_bank_set and clean not in banks:
            banks.append(clean)

    # 2. Union categories (respecting deletions)
    categories: List[str] = []
    for c in list(base.get("categories") or []) + list(inc.get("categories") or []):
        clean = str(c or "").strip()
        if clean and clean not in del_cat_set and clean not in categories:
            categories.append(clean)

    # 3. Merge transactions by id and exact signature (excluding tombstones)
    merged_tx: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_sigs = set()
    for tx in list(inc.get("transactions") or []) + list(base.get("transactions") or []):
        if not isinstance(tx, dict):
            continue
        amt = float(tx.get("amount") or 0)
        if amt <= 0:
            continue
        tx_id = str(tx.get("id") or "")
        if tx_id and tx_id in del_tx_set:
            continue
        sig = f"{tx.get('date')}|{tx.get('type')}|{amt}|{tx.get('account')}|{tx.get('category')}|{tx.get('note') or ''}"
        if (tx_id and tx_id in seen_ids) or sig in seen_sigs:
            continue
        if tx_id:
            seen_ids.add(tx_id)
        seen_sigs.add(sig)
        merged_tx.append(tx)

    merged_tx.sort(key=lambda x: (str(x.get("date") or ""), int(x.get("id") or 0) if str(x.get("id") or "").isdigit() else 0), reverse=True)

    # 4. Merge goals
    goals_map: Dict[str, Dict[str, Any]] = {}
    for g in list(base.get("goals") or []) + list(inc.get("goals") or []):
        if not isinstance(g, dict) or not g.get("name"):
            continue
        if str(g.get("id") or "") in del_goal_set:
            continue
        g_key = str(g.get("name")).strip().lower()
        if g_key not in goals_map:
            goals_map[g_key] = dict(g)
        else:
            goals_map[g_key]["current"] = max(float(goals_map[g_key].get("current") or 0), float(g.get("current") or 0))
            goals_map[g_key]["target"] = max(float(goals_map[g_key].get("target") or 0), float(g.get("target") or 0))

    # 5. Merge budgets (latest updatedAt wins)
    budgets_map: Dict[str, Dict[str, Any]] = {}
    for b in list(base.get("budgets") or []) + list(inc.get("budgets") or []):
        if not isinstance(b, dict) or not b.get("category"):
            continue
        if str(b.get("id") or "") in del_budget_set:
            continue
        cat_key = str(b["category"])
        if cat_key not in budgets_map or int(b.get("updatedAt") or 0) >= int(budgets_map[cat_key].get("updatedAt") or 0):
            budgets_map[cat_key] = dict(b)

    # 6. Merge automation rules (latest updatedAt wins)
    rules_map: Dict[str, Dict[str, Any]] = {}
    for r in list(base.get("automationRules") or []) + list(inc.get("automationRules") or []):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        r_id = str(r["id"])
        if r_id in del_rule_set:
            continue
        if r_id not in rules_map or int(r.get("updatedAt") or 0) >= int(rules_map[r_id].get("updatedAt") or 0):
            rules_map[r_id] = dict(r)

    return {
        "banks": banks or DEFAULT_BANKS,
        "categories": categories or DEFAULT_CATEGORIES,
        "transactions": merged_tx,
        "goals": list(goals_map.values()),
        "budgets": list(budgets_map.values()),
        "automationRules": list(rules_map.values()),
        "deletedTxIds": del_tx_ids[-300:],
        "deletedRuleIds": del_rule_ids[-150:],
        "deletedGoalIds": del_goal_ids[-150:],
        "deletedBudgetIds": del_budget_ids[-150:],
        "deletedBanks": del_banks[-100:],
        "deletedCategories": del_categories[-100:],
        "nickname": inc.get("nickname") or base.get("nickname") or "",
        "currency": inc.get("currency") or base.get("currency") or "IDR",
        "lang": inc.get("lang") or base.get("lang") or "ID",
        "updatedAt": int(time.time() * 1000),
    }


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
    cpp_src = os.path.join(PROJECT_ROOT, "dnd_engine.cpp")
    lib_filename = "dnd_engine.dll" if os.name == "nt" else "dnd_engine.so"
    lib_path = os.path.join(WORK_DIR, lib_filename)
    precompiled_path = os.path.join(PROJECT_ROOT, lib_filename)

    target_lib = precompiled_path if os.path.exists(precompiled_path) else lib_path

    if os.path.exists(cpp_src) and not os.path.exists(precompiled_path) and not IS_READONLY_FS:
        need_build = not os.path.exists(lib_path) or (
            os.path.getmtime(cpp_src) > os.path.getmtime(lib_path)
        )
        if need_build:
            try:
                subprocess.run(
                    ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", cpp_src, "-o", lib_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                target_lib = lib_path
            except Exception:
                pass

    if os.path.exists(target_lib):
        try:
            lib = ctypes.CDLL(target_lib)
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


def _get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute(
            '''
            CREATE TABLE IF NOT EXISTS app_settings
            (key TEXT PRIMARY KEY,
             value TEXT)
            '''
        )
        c.execute(
            '''
            CREATE TABLE IF NOT EXISTS transactions
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             user_email TEXT DEFAULT '',
             amount REAL, type TEXT, category TEXT, account TEXT, to_account TEXT DEFAULT '',
             admin_fee REAL DEFAULT 0, note TEXT, currency TEXT DEFAULT 'IDR', date TEXT)
            '''
        )
        c.execute(
            '''
            CREATE TABLE IF NOT EXISTS users
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             google_sub TEXT UNIQUE, email TEXT, name TEXT, nickname TEXT DEFAULT '',
             picture TEXT, last_login TEXT, session_token TEXT DEFAULT '', refresh_token TEXT DEFAULT '')
            '''
        )
        c.execute("PRAGMA table_info(users)")
        user_cols = {row[1] for row in c.fetchall()}
        if "refresh_token" not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN refresh_token TEXT DEFAULT ''")

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

        c.execute("DELETE FROM custom_banks WHERE name = 'Corporate Bank'")
        for bank_name in DEFAULT_BANKS:
            c.execute("INSERT OR IGNORE INTO custom_banks (name) VALUES (?)", (bank_name,))

        c.execute("DELETE FROM custom_categories WHERE LOWER(name) IN ('otomotif', 'automotive')")
        for cat_name in DEFAULT_CATEGORIES:
            c.execute("INSERT OR IGNORE INTO custom_categories (name) VALUES (?)", (cat_name,))

        conn.commit()
        conn.close()
    except Exception:
        pass


init_db()


def _get_setting(key: str, default: str = "") -> str:
    env_val = os.getenv(key, "").strip()
    if env_val:
        return env_val
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        if row and row["value"]:
            return row["value"].strip()
    except Exception:
        pass
    if key == "GOOGLE_CLIENT_ID":
        return DEFAULT_GOOGLE_CLIENT_ID
    if key == "GOOGLE_CLIENT_SECRET":
        return DEFAULT_GOOGLE_CLIENT_SECRET
    return default.strip()


def _get_public_base_url(request: Request) -> str:
    """Memastikan URL publik selalu memakai HTTPS di belakang proxy Vercel"""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded_host:
        proto = forwarded_proto or "https"
        return f"{proto}://{forwarded_host}".rstrip("/")
    base = str(request.base_url).rstrip("/")
    if "vercel.app" in base and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


def _get_authenticated_email(request: Request) -> str:
    """Mengidentifikasi email pengguna terautentikasi dari cookie sesi, bearer token, atau header"""
    session_token = request.cookies.get("dnd_google_session", "").strip()
    if not session_token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            session_token = auth_header[7:].strip()
    if session_token:
        if session_token in MEMORY_SESSIONS and MEMORY_SESSIONS[session_token].get("email"):
            return str(MEMORY_SESSIONS[session_token]["email"]).strip().lower()
        try:
            conn = _get_db_connection()
            c = conn.cursor()
            c.execute("SELECT email FROM users WHERE session_token = ?", (session_token,))
            row = c.fetchone()
            conn.close()
            if row and row["email"]:
                return (row["email"] or "").strip().lower()
        except Exception:
            pass
    header_email = request.headers.get("X-User-Email", "").strip().lower()
    return header_email


def _upsert_google_user(
    sub: str,
    email: str,
    name: str,
    picture: str,
    nickname: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> tuple[str, str]:
    """Menyimpan atau memperbarui profil akun Google terautentikasi dan mengembalikan (nickname, session_token)"""
    clean_email = (email or "").strip().lower()
    session_token = secrets.token_urlsafe(32)
    final_nickname = nickname.strip() if nickname is not None else ""
    existing_rt = _load_user_refresh_token(clean_email)
    final_rt = (refresh_token or "").strip() or existing_rt

    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("SELECT nickname, refresh_token FROM users WHERE google_sub = ? OR LOWER(email) = ?", (sub, clean_email))
        existing = c.fetchone()
        if nickname is None and existing and existing["nickname"]:
            final_nickname = existing["nickname"]
        if not final_rt and existing and existing["refresh_token"]:
            final_rt = existing["refresh_token"]
        c.execute(
            '''
            INSERT INTO users (google_sub, email, name, nickname, picture, last_login, session_token, refresh_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(google_sub) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                nickname = excluded.nickname,
                picture = excluded.picture,
                last_login = excluded.last_login,
                session_token = excluded.session_token,
                refresh_token = CASE WHEN excluded.refresh_token <> '' THEN excluded.refresh_token ELSE users.refresh_token END
            ''',
            (sub, clean_email, name, final_nickname, picture, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_token, final_rt),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    prev_mem = MEMORY_USERS_BY_EMAIL.get(clean_email) or {}
    if nickname is None and prev_mem.get("nickname"):
        final_nickname = prev_mem["nickname"]

    if final_rt:
        _save_user_refresh_token(clean_email, final_rt)

    user_record = {
        "sub": sub or clean_email,
        "email": clean_email,
        "name": name or clean_email.split("@")[0],
        "nickname": final_nickname,
        "picture": picture or "",
        "session_token": session_token,
    }
    MEMORY_SESSIONS[session_token] = dict(user_record)
    MEMORY_USERS_BY_EMAIL[clean_email] = dict(user_record)
    return final_nickname, session_token


def _decode_jwt_payload_fallback(id_token: str) -> Dict[str, Any]:
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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


def _extract_receipt_from_bytes(img_bytes: bytes, mime_type: str = "image/jpeg") -> Dict[str, Any]:
    b64_str = base64.b64encode(img_bytes).decode("utf-8")
    prompt_text = (
        'Ekstrak subtotal sebelum pajak, persentase pajak/layanan (jika ada, misal 10 atau 11, jika tidak ada 0), '
        'total pembayaran akhir, kategori (pilih satu: Food, Transport, Utilities, Online Shopping, Health & Lifestyle, Shopping), '
        'nama toko/merchant singkat, dan daftar item dari gambar struk ini dalam JSON: '
        '{"subtotal": 0, "tax_percent": 0, "amount": 0, "category": "Food", "note": "...", "items": [{"name": "...", "price": 0}]}'
    )
    gemini_json = _call_gemini_backend(
        [
            {"text": prompt_text},
            {"inlineData": {"mimeType": mime_type or "image/jpeg", "data": b64_str}},
        ],
        json_mode=True,
    )
    if gemini_json:
        try:
            parsed = json.loads(gemini_json)
            total_amt = float(parsed.get("amount", 0))
            sub_amt = float(parsed.get("subtotal", 0))
            tax_pct = float(parsed.get("tax_percent", 0))
            if total_amt > 0 or sub_amt > 0:
                cat = parsed.get("category", "Food")
                if str(cat).lower() in ("otomotif", "automotive"):
                    cat = "Transport"
                return {
                    "status": "success",
                    "engine": "gemini_vision",
                    "subtotal": sub_amt if sub_amt > 0 else total_amt,
                    "tax_percent": tax_pct if sub_amt > 0 else 0.0,
                    "amount": total_amt if total_amt > 0 else sub_amt,
                    "category": cat,
                    "note": parsed.get("note", "Pindai Struk"),
                    "items": parsed.get("items") or [],
                }
        except Exception:
            pass

    return {"status": "fallback", "subtotal": 0, "tax_percent": 0, "amount": 0, "items": []}


def _generate_pure_python_pdf(title: str, subtitle_lines: List[str], table_headers: List[str], table_rows: List[List[str]], output_path: str) -> None:
    def _pdf_escape(s: str) -> str:
        clean = str(s or "").encode("latin-1", "replace").decode("latin-1")
        return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    lines_per_page = 42
    all_lines: List[str] = [title, "=" * 78] + subtitle_lines + ["", "-" * 78]
    header_line = " | ".join(h[:18].ljust(18) for h in table_headers)
    all_lines.append(header_line)
    all_lines.append("-" * 78)
    for row in table_rows:
        all_lines.append(" | ".join(str(cell)[:18].ljust(18) for cell in row))

    pages = [all_lines[i : i + lines_per_page] for i in range(0, max(1, len(all_lines)), lines_per_page)]
    objects: List[bytes] = []

    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    page_kids = []
    next_obj_id = 4

    page_objs: List[bytes] = []
    for p_idx, page_lines in enumerate(pages):
        page_id = next_obj_id
        content_id = next_obj_id + 1
        next_obj_id += 2
        page_kids.append(f"{page_id} 0 R")

        stream_cmds = ["BT", "/F1 9 Tf", "40 790 Td", "14 TL"]
        for l_idx, ln in enumerate(page_lines):
            if l_idx == 0 and p_idx == 0:
                stream_cmds.append("/F1 13 Tf")
                stream_cmds.append(f"({_pdf_escape(ln)}) Tj")
                stream_cmds.append("/F1 9 Tf")
            else:
                stream_cmds.append("T*")
                stream_cmds.append(f"({_pdf_escape(ln)}) Tj")
        stream_cmds.append("ET")
        stream_bytes = "\n".join(stream_cmds).encode("latin-1")

        page_objs.append(
            f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >> endobj\n".encode("latin-1")
        )
        page_objs.append(
            f"{content_id} 0 obj << /Length {len(stream_bytes)} >> stream\n".encode("latin-1")
            + stream_bytes
            + b"\nendstream endobj\n"
        )

    kids_str = " ".join(page_kids)
    objects.append(f"2 0 obj << /Type /Pages /Kids [{kids_str}] /Count {len(pages)} >> endobj\n".encode("latin-1"))
    objects.append(b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Courier >> endobj\n")
    objects.extend(page_objs)

    pdf_buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf_buf))
        pdf_buf.extend(obj)

    xref_offset = len(pdf_buf)
    pdf_buf.extend(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    pdf_buf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf_buf.extend(f"{off:010d} 00000 n \n".encode("latin-1"))

    pdf_buf.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("latin-1")
    )
    with open(output_path, "wb") as f:
        f.write(pdf_buf)


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


class GoogleTokenVerifyPayload(BaseModel):
    access_token: Optional[str] = ""
    id_token: Optional[str] = ""


class GoogleRefreshTokenPayload(BaseModel):
    refresh_token: Optional[str] = ""
    email: Optional[str] = ""


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


class SplitBillPayload(BaseModel):
    subtotal: float
    tax_percent: Optional[float] = 0.0
    people: int = 1


@app.get("/")
async def serve_root():
    for candidate in [
        os.path.join(PROJECT_ROOT, "index.html"),
        os.path.join(BASE_DIR, "index.html"),
        "index.html",
    ]:
        if os.path.exists(candidate):
            return FileResponse(candidate, media_type="text/html")
    return {"status": "ok", "message": "DND Finance Personal Ledger API Running"}


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "DND Finance Personal Ledger API",
        "cpp_engine_active": CPP_ENGINE is not None,
    }


@app.post("/api/engine/split-bill")
async def compute_split_bill_api(payload: SplitBillPayload):
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


# ==========================================
# GOOGLE IDENTITY & CROSS-DEVICE CLOUD SYNC
# ==========================================

@app.get("/api/auth/google/config")
async def get_google_oauth_config(request: Request):
    client_id = _get_setting("GOOGLE_CLIENT_ID")
    client_secret = _get_setting("GOOGLE_CLIENT_SECRET")
    base_url = _get_public_base_url(request)
    return {
        "client_id": client_id,
        "has_client_secret": bool(client_secret),
        "redirect_uri": f"{base_url}/api/auth/google/callback",
    }


@app.get("/api/auth/google/login")
async def google_oauth_login_redirect(request: Request):
    client_id = _get_setting("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=400, detail="GOOGLE_CLIENT_ID belum dikonfigurasi")
    base_url = _get_public_base_url(request)
    redirect_uri = f"{base_url}/api/auth/google/callback"
    state = secrets.token_urlsafe(24)
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile https://www.googleapis.com/auth/drive.appdata",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent select_account",
            "state": state,
        }
    )
    return RedirectResponse(url=f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/api/auth/google/callback")
async def google_oauth_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(url="/?google_auth_error=access_denied")

    client_id = _get_setting("GOOGLE_CLIENT_ID")
    client_secret = _get_setting("GOOGLE_CLIENT_SECRET")
    base_url = _get_public_base_url(request)
    redirect_uri = f"{base_url}/api/auth/google/callback"

    token_data = urllib.parse.urlencode(
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
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))

        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        userinfo_req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(userinfo_req, timeout=15) as uresp:
            info = json.loads(uresp.read().decode("utf-8"))

        sub = info.get("sub") or info.get("email", "")
        email = (info.get("email") or "").strip().lower()
        name = info.get("name") or email.split("@")[0]
        picture = info.get("picture") or ""
        _, session_token = _upsert_google_user(sub, email, name, picture, refresh_token=refresh_token)
        stored_rt = refresh_token or _load_user_refresh_token(email)

        query_dict = {
            "google_auth_success": "1",
            "google_session": session_token,
            "google_drive_token": access_token,
            "google_email": email,
            "google_name": name,
            "google_picture": picture,
            "google_sub": sub,
        }
        if stored_rt:
            query_dict["google_refresh_token"] = stored_rt

        redirect_query = urllib.parse.urlencode(query_dict)
        redirect_resp = RedirectResponse(url=f"/?{redirect_query}")
        redirect_resp.set_cookie(
            key="dnd_google_session",
            value=session_token,
            path="/",
            httponly=True,
            secure=base_url.startswith("https://"),
            samesite="lax",
            max_age=2592000,
        )
        return redirect_resp
    except Exception as exc:
        return RedirectResponse(url=f"/?google_auth_error={urllib.parse.quote(str(exc))}")


@app.post("/api/auth/google/refresh-token")
async def refresh_google_drive_access_token(payload: GoogleRefreshTokenPayload):
    """Menukar refresh_token menjadi access_token baru agar sinkronisasi HP, Laptop, iPad & Mac tetap berjalan otomatis"""
    rtoken = (payload.refresh_token or "").strip()
    if not rtoken and payload.email:
        rtoken = _load_user_refresh_token(payload.email)
    if not rtoken:
        raise HTTPException(status_code=400, detail="refresh_token tidak tersedia")

    refreshed = _exchange_refresh_token_for_access_token(rtoken)
    if not refreshed.get("access_token"):
        raise HTTPException(status_code=401, detail="Gagal memperbarui token Google Drive")

    return {
        "access_token": refreshed["access_token"],
        "expires_in": refreshed.get("expires_in", 3500),
        "refresh_token": rtoken,
    }


@app.post("/api/auth/google/verify-token")
async def verify_google_browser_token(payload: GoogleTokenVerifyPayload, request: Request, response: Response):
    info: Dict[str, Any] = {}
    token_id = (payload.id_token or "").strip()
    token_access = (payload.access_token or "").strip()

    try:
        if token_id:
            url = f"https://oauth2.googleapis.com/tokeninfo?id_token={urllib.parse.quote(token_id)}"
            try:
                with urllib.request.urlopen(url, timeout=12) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
            except Exception:
                info = _decode_jwt_payload_fallback(token_id)
        elif token_access:
            req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {token_access}"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                info = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token Google tidak valid: {exc}")

    email = (info.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Gagal mendapatkan email dari token Google")

    configured_cid = _get_setting("GOOGLE_CLIENT_ID")
    aud = info.get("aud", "")
    if configured_cid and aud and aud != configured_cid:
        raise HTTPException(status_code=401, detail="Audience token Google tidak cocok")

    sub = info.get("sub") or email
    name = info.get("name") or email.split("@")[0]
    picture = info.get("picture") or ""
    saved_nickname, session_token = _upsert_google_user(sub, email, name, picture)
    base_url = _get_public_base_url(request)

    stored_rt = _load_user_refresh_token(email)
    drive_access_token = ""
    if stored_rt:
        refreshed = _exchange_refresh_token_for_access_token(stored_rt)
        drive_access_token = refreshed.get("access_token", "")

    response.set_cookie(
        key="dnd_google_session",
        value=session_token,
        path="/",
        httponly=True,
        secure=base_url.startswith("https://"),
        samesite="lax",
        max_age=2592000,
    )
    return {
        "status": "authenticated",
        "session_token": session_token,
        "drive_access_token": drive_access_token,
        "refresh_token": stored_rt,
        "user": {
            "sub": sub,
            "email": email,
            "name": name,
            "nickname": saved_nickname,
            "picture": picture,
        },
    }


@app.post("/api/auth/google/nickname")
async def update_google_nickname(payload: NicknameUpdatePayload):
    clean_email = payload.email.strip().lower()
    if not clean_email:
        raise HTTPException(status_code=400, detail="Email required")
    clean_nick = payload.nickname.strip()
    if clean_email in MEMORY_USERS_BY_EMAIL:
        MEMORY_USERS_BY_EMAIL[clean_email]["nickname"] = clean_nick
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET nickname = ? WHERE LOWER(email) = ?", (clean_nick, clean_email))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"status": "updated", "email": clean_email, "nickname": clean_nick}


@app.get("/api/auth/google/me")
async def get_current_google_user(request: Request, session_token: Optional[str] = None, email: Optional[str] = None):
    token = (session_token or request.cookies.get("dnd_google_session", "")).strip()
    if not token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    row = None
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        if token:
            c.execute("SELECT google_sub, email, name, nickname, picture, session_token, refresh_token FROM users WHERE session_token = ?", (token,))
            row = c.fetchone()
        if not row and email:
            clean_email = email.strip().lower()
            c.execute("SELECT google_sub, email, name, nickname, picture, session_token, refresh_token FROM users WHERE LOWER(email) = ?", (clean_email,))
            row = c.fetchone()
        conn.close()
    except Exception:
        row = None

    user_dict: Optional[Dict[str, Any]] = None
    resolved_token = token

    if row and row["email"]:
        resolved_token = row["session_token"] or token
        if row["refresh_token"]:
            _save_user_refresh_token(row["email"], row["refresh_token"])
        user_dict = {
            "sub": row["google_sub"] or row["email"],
            "email": row["email"],
            "name": row["name"] or row["email"].split("@")[0],
            "nickname": row["nickname"] or "",
            "picture": row["picture"] or "",
        }
    elif token and token in MEMORY_SESSIONS:
        mem = MEMORY_SESSIONS[token]
        user_dict = {
            "sub": mem.get("sub") or mem.get("email"),
            "email": mem.get("email"),
            "name": mem.get("name"),
            "nickname": mem.get("nickname") or "",
            "picture": mem.get("picture") or "",
        }
    elif email:
        clean_email = email.strip().lower()
        if clean_email in MEMORY_USERS_BY_EMAIL:
            mem = MEMORY_USERS_BY_EMAIL[clean_email]
            resolved_token = mem.get("session_token") or token
            user_dict = {
                "sub": mem.get("sub") or mem.get("email"),
                "email": mem.get("email"),
                "name": mem.get("name"),
                "nickname": mem.get("nickname") or "",
                "picture": mem.get("picture") or "",
            }

    if user_dict and user_dict.get("email"):
        stored_rt = _load_user_refresh_token(user_dict["email"])
        drive_access_token = ""
        if stored_rt:
            refreshed = _exchange_refresh_token_for_access_token(stored_rt)
            drive_access_token = refreshed.get("access_token", "")
        return {
            "authenticated": True,
            "session_token": resolved_token,
            "drive_access_token": drive_access_token,
            "refresh_token": stored_rt,
            "user": user_dict,
        }

    return {"authenticated": False, "user": None}


@app.post("/api/auth/google/logout")
async def logout_google_session(request: Request, response: Response):
    session_token = request.cookies.get("dnd_google_session", "").strip()
    if session_token:
        MEMORY_SESSIONS.pop(session_token, None)
        try:
            conn = _get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE users SET session_token = '' WHERE session_token = ?", (session_token,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    response.delete_cookie("dnd_google_session", path="/")
    return {"status": "logged_out"}


def _load_saved_workspace(clean_email: str) -> Optional[Dict[str, Any]]:
    if not clean_email:
        return None
    kv_raw = _kv_request(["GET", f"dnd_ws:{clean_email}"])
    if kv_raw:
        try:
            parsed = json.loads(kv_raw)
            if isinstance(parsed, dict):
                MEMORY_WORKSPACES[clean_email] = parsed
                return parsed
        except Exception:
            pass
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute("SELECT state_json FROM user_workspace_state WHERE email = ?", (clean_email,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                MEMORY_WORKSPACES[clean_email] = parsed
                return parsed
    except Exception:
        pass
    return MEMORY_WORKSPACES.get(clean_email)


@app.post("/api/sync/push")
async def push_workspace_state(payload: WorkspaceSyncPayload):
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = _load_saved_workspace(email)
    merged = _merge_workspace_dicts(existing, payload.state)
    MEMORY_WORKSPACES[email] = merged
    serialized = json.dumps(merged)

    _kv_request(["SET", f"dnd_ws:{email}", serialized])
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO user_workspace_state (email, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            ''',
            (email, serialized, now_str),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"status": "synced", "email": email, "updated_at": now_str, "merged_state": merged}


@app.get("/api/sync/pull")
async def pull_workspace_state(email: str):
    clean_email = email.strip().lower()
    saved = _load_saved_workspace(clean_email)
    if saved:
        return {"status": "ok", "state": saved}
    return {"status": "empty", "state": None}


@app.post("/api/transactions")
async def create_transaction(tx: Transaction, request: Request):
    if tx.amount <= 0:
        raise HTTPException(status_code=400, detail="Nominal transaksi harus lebih dari 0")
    email = _get_authenticated_email(request)
    tx_date = tx.date or datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        conn = _get_db_connection()
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO transactions (user_email, amount, type, category, account, to_account, admin_fee, note, currency, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                email,
                tx.amount,
                tx.type,
                tx.category,
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


@app.post("/api/ai/analyze")
async def analyze_financial_data(req: AIAnalysisRequest):
    tx_rows = req.transactions if req.transactions is not None else []
    metrics = _compute_metrics_with_cpp(tx_rows)
    inc = metrics["total_income"]
    exp = metrics["total_expense"] + metrics["total_withdrawal"]
    bal = metrics["net_balance"]
    ratio = metrics["health_score"]

    ai_text = _call_gemini_backend(
        [{"text": f"Pertanyaan/Instruksi: {req.prompt}\n\nData Keuangan:\n{req.financial_context or ''}"}],
        system_text=(
            "Anda adalah analis keuangan pribadi profesional DND Finance. Jawab secara ringkas, terstruktur, "
            "berbasis data angka aktual, dan gunakan bahasa yang sama dengan pengguna."
        ),
    )
    if ai_text:
        return {"status": "success", "source": "gemini_api", "analysis": ai_text}

    curr_prefix = "Rp " if (req.currency or "IDR") == "IDR" else f"{req.currency} "
    if req.lang == "ID":
        text = (
            f"### Ringkasan Kondisi Keuangan\n"
            f"- **Saldo Bersih:** {curr_prefix}{bal:,.0f} (Rasio Sehat: **{ratio}%**)\n"
            f"- **Pemasukan:** {curr_prefix}{inc:,.0f} | **Pengeluaran:** {curr_prefix}{exp:,.0f}\n"
            f"- **Saran Utama:** Catat transaksi secara rutin dan aktifkan aturan **Autopilot** untuk menjaga target tabungan."
        )
    else:
        text = (
            f"### Financial Summary\n"
            f"- **Net Balance:** {curr_prefix}{bal:,.0f} (Cash Health: **{ratio}%**)\n"
            f"- **Income:** {curr_prefix}{inc:,.0f} | **Expense:** {curr_prefix}{exp:,.0f}\n"
            f"- **Key Recommendation:** Track transactions consistently and enable **Autopilot** rules to hit savings goals."
        )
    return {"status": "success", "source": metrics["engine"], "analysis": text}


@app.post("/api/scan/receipt")
async def scan_receipt_endpoint(req: ReceiptScanRequest):
    try:
        img_bytes = base64.b64decode(req.image_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Format base64 gambar tidak valid")
    return _extract_receipt_from_bytes(img_bytes, req.mime_type or "image/jpeg")


@app.post("/api/scan/upload")
async def scan_receipt_upload_file(file: UploadFile = File(...)):
    img_bytes = await file.read()
    if not img_bytes:
        raise HTTPException(status_code=400, detail="File gambar kosong")
    return _extract_receipt_from_bytes(img_bytes, file.content_type or "image/jpeg")


def _load_report_dataframe_and_wallets(email: str, is_id: bool):
    clean_email = email.strip().lower()
    df = pd.DataFrame()
    wallet_balances = {b: 0.0 for b in DEFAULT_BANKS}

    saved_ws = _load_saved_workspace(clean_email) if clean_email else None
    if saved_ws:
        if isinstance(saved_ws.get("banks"), list) and saved_ws["banks"]:
            wallet_balances = {b: 0.0 for b in saved_ws["banks"]}
        if isinstance(saved_ws.get("transactions"), list) and len(saved_ws["transactions"]) > 0:
            df = pd.DataFrame(saved_ws["transactions"])
            if "toAccount" in df.columns and "to_account" not in df.columns:
                df["to_account"] = df["toAccount"]
            if "adminFee" in df.columns and "admin_fee" not in df.columns:
                df["admin_fee"] = df["adminFee"]

    if df.empty:
        try:
            conn = _get_db_connection()
            df = pd.read_sql_query("SELECT * FROM transactions WHERE user_email = ? ORDER BY date DESC, id DESC", conn, params=(clean_email,))
            conn.close()
        except Exception:
            pass

    if not df.empty:
        first_wallet = next(iter(wallet_balances.keys()), "Cash")
        for _, r in df.iterrows():
            amt = float(r.get("amount") or 0.0)
            fee = float(r.get("admin_fee") or 0.0)
            src = r.get("account") or first_wallet
            wallet_balances.setdefault(src, 0.0)
            if r.get("type") == "income":
                wallet_balances[src] += amt
            elif r.get("type") in ("expense", "withdrawal"):
                wallet_balances[src] -= amt
            elif r.get("type") == "transfer":
                dst = r.get("to_account") or ""
                wallet_balances[src] -= (amt + fee)
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
    return df, wallet_balances


@app.get("/api/export/excel")
async def export_excel_report(background_tasks: BackgroundTasks, lang: str = "ID", email: str = ""):
    is_id = lang.upper() == "ID"
    df, wallet_balances = _load_report_dataframe_and_wallets(email, is_id)
    filename = f"DND_Financial_Report_{lang.upper()}_{int(time.time())}.xlsx"
    export_path = os.path.join(WORK_DIR, filename)

    writer = pd.ExcelWriter(export_path, engine="xlsxwriter")
    workbook = writer.book
    ws_overview = workbook.add_worksheet("Ringkasan" if is_id else "Overview")
    ws_wallets = workbook.add_worksheet("Wallet" if is_id else "Wallets")
    ws_tx = workbook.add_worksheet("Transaksi" if is_id else "Transactions")

    hdr_fmt = workbook.add_format({"bold": True, "bg_color": "#1D4ED8", "font_color": "#FFFFFF", "border": 1})
    cell_fmt = workbook.add_format({"border": 1})
    curr_fmt = workbook.add_format({"num_format": '"Rp "#,##0', "border": 1})

    w_headers = ["Wallet / Bank", "Saldo"] if is_id else ["Wallet / Bank", "Balance"]
    for col_idx, text in enumerate(w_headers):
        ws_wallets.write(0, col_idx, text, hdr_fmt)
        ws_wallets.set_column(col_idx, col_idx, 24)
    for idx, (w_name, w_bal) in enumerate(wallet_balances.items()):
        ws_wallets.write(idx + 1, 0, w_name, cell_fmt)
        ws_wallets.write_number(idx + 1, 1, float(w_bal), curr_fmt)

    tx_headers = ["ID", "Tanggal", "Kategori", "Tipe", "Wallet", "Catatan", "Nominal"] if is_id else ["ID", "Date", "Category", "Type", "Wallet", "Note", "Amount"]
    for col_idx, text in enumerate(tx_headers):
        ws_tx.write(0, col_idx, text, hdr_fmt)
        ws_tx.set_column(col_idx, col_idx, 18)

    for idx, row in df.iterrows():
        r = idx + 1
        ws_tx.write(r, 0, row.get("id", r), cell_fmt)
        ws_tx.write(r, 1, str(row.get("date", "")), cell_fmt)
        ws_tx.write(r, 2, str(row.get("category", "")), cell_fmt)
        ws_tx.write(r, 3, str(row.get("type", "")), cell_fmt)
        ws_tx.write(r, 4, str(row.get("account", "")), cell_fmt)
        ws_tx.write(r, 5, str(row.get("note") or "-"), cell_fmt)
        ws_tx.write_number(r, 6, float(row.get("amount") or 0.0), curr_fmt)

    metrics = _compute_metrics_with_cpp(df.to_dict(orient="records"))
    ws_overview.write(0, 0, "Metrik" if is_id else "Metric", hdr_fmt)
    ws_overview.write(0, 1, "Nilai" if is_id else "Value", hdr_fmt)
    summary_items = [
        ("Saldo Bersih" if is_id else "Net Balance", metrics["net_balance"]),
        ("Total Pemasukan" if is_id else "Total Income", metrics["total_income"]),
        ("Total Pengeluaran" if is_id else "Total Outflow", metrics["total_expense"] + metrics["total_withdrawal"]),
    ]
    for idx, (k, v) in enumerate(summary_items):
        ws_overview.write(idx + 1, 0, k, cell_fmt)
        ws_overview.write_number(idx + 1, 1, float(v), curr_fmt)

    writer.close()

    def remove_file(path: str):
        time.sleep(2)
        if os.path.exists(path):
            os.remove(path)

    background_tasks.add_task(remove_file, export_path)
    return FileResponse(
        export_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/export/pdf")
async def export_pdf_report(background_tasks: BackgroundTasks, lang: str = "ID", email: str = ""):
    is_id = lang.upper() == "ID"
    df, wallet_balances = _load_report_dataframe_and_wallets(email, is_id)
    tx_records = df.to_dict(orient="records")
    metrics = _compute_metrics_with_cpp(tx_records)

    filename = f"DND_Financial_Report_{lang.upper()}_{int(time.time())}.pdf"
    export_path = os.path.join(WORK_DIR, filename)
    title = "LAPORAN KEUANGAN DND FINANCE" if is_id else "DND FINANCE FINANCIAL LEDGER REPORT"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    subtitle_lines = [
        f"Tanggal Cetak     : {generated_at}",
        f"Total Saldo Bersih: Rp {metrics['net_balance']:,.0f}",
        f"Total Pemasukan   : Rp {metrics['total_income']:,.0f}",
        f"Total Pengeluaran : Rp {(metrics['total_expense'] + metrics['total_withdrawal']):,.0f}",
        "Saldo Wallet      : " + ", ".join(f"{k}: Rp {v:,.0f}" for k, v in wallet_balances.items()),
    ]
    headers = ["Tanggal", "Kategori", "Tipe", "Nominal"] if is_id else ["Date", "Category", "Type", "Amount"]
    rows = [
        [
            str(r.get("date") or ""),
            str(r.get("category") or ""),
            str(r.get("type") or ""),
            f"Rp {float(r.get('amount') or 0):,.0f}",
        ]
        for r in tx_records
    ]
    _generate_pure_python_pdf(title, subtitle_lines, headers, rows, export_path)

    def remove_file(path: str):
        time.sleep(2)
        if os.path.exists(path):
            os.remove(path)

    background_tasks.add_task(remove_file, export_path)
    return FileResponse(export_path, filename=filename, media_type="application/pdf")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)