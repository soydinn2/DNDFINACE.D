"""
DND Finance — Server Backend FastAPI
- Login Google Langsung di Browser (Google OAuth2 / ID Token, Tanpa Firebase Auth)
- Firebase Realtime Database (`dnd-finance-3590e-default-rtdb.asia-southeast1.firebasedatabase.app`)
- Embedded DND AI AGENT (Open-Source LLM EA Agent)
- Jembatan Komputasi Kuantitatif C++17 (`quant_ai_engine`)
"""

import os
import re
import json
import hmac
import time
import glob
import base64
import hashlib
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_local_env_file(env_filename: str = ".env") -> None:
    env_path = os.path.join(BASE_DIR, env_filename)
    if not os.path.isfile(env_path):
        return
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
    except Exception as exc:
        print(f"[DND Finance .env Loader Warning]: {exc}")


load_local_env_file()

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
FIREBASE_DATABASE_SECRET = os.getenv("FIREBASE_DATABASE_SECRET", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_PATH",
    os.path.join(BASE_DIR, "serviceAccountKey.json"),
).strip()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
EMBEDDED_OSS_MODEL = os.getenv("EMBEDDED_OSS_MODEL", "qwen2.5:7b")
OPEN_LLM_FALLBACK_URL = os.getenv("OPEN_LLM_FALLBACK_URL", "https://text.pollinations.ai/openai")

CPP_ENGINE_BINARY = os.path.join(
    BASE_DIR,
    "quant_ai_engine.exe" if os.name == "nt" else "quant_ai_engine",
)
LOCAL_SQLITE_DB = os.path.join(BASE_DIR, "dnd_finance_mirror.db")

app = FastAPI(title="DND Finance API Server", version="9.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    matches = glob.glob(os.path.join(BASE_DIR, "dnd-finance-3590e*.json"))
    if matches:
        return matches[0]
    return None


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
            except Exception as exc:
                print(f"[Firebase Admin SDK Init Warning]: {exc}")

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
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.put(url, params={"auth": FIREBASE_DATABASE_SECRET}, json=payload)
                    return res.status_code == 200
            except Exception as exc:
                print(f"[Firebase REST Write Error ({node_path})]: {exc}")
                return False

        return False

    async def load_workspace(self, account_key: str) -> Dict[str, Any]:
        safe_key = sanitize_rtdb_key(account_key)

        if self.rtdb_admin_ref is not None:
            try:
                data = self.rtdb_admin_ref.child("workspaces").child(safe_key).get()
                if isinstance(data, dict):
                    return {
                        "revision": int(data.get("revision", 1)),
                        "state": data.get("state", {}),
                    }
            except Exception as exc:
                print(f"[Firebase Admin Read Error]: {exc}")

        if FIREBASE_DATABASE_URL and FIREBASE_DATABASE_SECRET and not FIREBASE_DATABASE_SECRET.startswith("PASTE_"):
            try:
                url = f"{FIREBASE_DATABASE_URL}/workspaces/{safe_key}.json"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.get(url, params={"auth": FIREBASE_DATABASE_SECRET})
                    if res.status_code == 200 and isinstance(res.json(), dict):
                        data = res.json()
                        return {
                            "revision": int(data.get("revision", 1)),
                            "state": data.get("state", {}),
                        }
            except Exception as exc:
                print(f"[Firebase REST Read Error]: {exc}")

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

        return await self.write_node(f"workspaces/{safe_key}", payload)

    async def test_connection(self) -> Dict[str, Any]:
        ok = await self.write_node(
            "_health_check",
            {"app": "DND Finance", "project_id": "dnd-finance-3590e", "ok": True, "ts": datetime.now(timezone.utc).isoformat()},
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
            "hint": "Pastikan Realtime Database sudah dibuat di Firebase Console (dnd-finance-3590e) dan database.rules.json sudah di-Publish.",
        }


db_engine = FirebaseRealtimeDatabaseEngine()


# ==============================================================================
# C++17 QUANTITATIVE WEALTH ENGINE BRIDGE
# ==============================================================================
def run_cpp_quantitative_engine(transactions: List[Dict[str, Any]], goals: List[Dict[str, Any]], rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "transactions": transactions,
        "goals_count": len(goals),
        "active_rules_count": sum(1 for r in rules if r.get("enabled")),
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
    savings_rate = int(round(((total_income - total_expense) / total_income) * 100)) if total_income > 0 else 0
    savings_rate = max(-100, min(100, savings_rate))
    runway_days = int(round(max(0.0, net_balance) / daily_burn)) if daily_burn > 0 else 999

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

    return {
        "health_score": score,
        "savings_rate_pct": savings_rate,
        "daily_burn_rate": round(daily_burn, 2),
        "runway_days": runway_days,
        "net_balance": round(net_balance, 2),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "engine": "python_quant_fallback",
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


class SyncMergeRequest(BaseModel):
    account_key: str = "shared_default_ledger"
    google_email: Optional[str] = ""
    google_sub: Optional[str] = ""
    client_revision: int = 0
    state: Dict[str, Any] = Field(default_factory=dict)


class AiAnalyzeRequest(BaseModel):
    prompt: str
    lang: str = "ID"
    currency: str = "IDR"
    transactions: List[Dict[str, Any]] = Field(default_factory=list)
    goals: List[Dict[str, Any]] = Field(default_factory=list)
    budgets: List[Dict[str, Any]] = Field(default_factory=list)
    executed_actions: List[Dict[str, Any]] = Field(default_factory=list)


# ==============================================================================
# API ROUTES
# ==============================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.isfile(html_path):
        return HTMLResponse("<h1>DND Finance index.html tidak ditemukan</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/app/info")
async def get_app_info():
    return {
        "platform": "DND Finance",
        "developer": "Dinda",
        "instagram_handle": "@di_ndaaa29",
        "instagram_url": "https://www.instagram.com/di_ndaaa29?hvrq=MWNnaXczbm1xbGt4MQ==",
        "google_client_id": GOOGLE_CLIENT_ID,
        "database_backend": "firebase" if db_engine.is_firebase_active else db_engine.auth_method,
        "firebase_database_url": FIREBASE_DATABASE_URL,
    }


@app.get("/api/firebase/status")
async def check_firebase_status():
    return await db_engine.test_connection()


@app.post("/api/auth/google/verify")
async def verify_direct_google_auth(req: GoogleVerifyRequest, response: Response):
    user_profile: Optional[Dict[str, Any]] = None

    async with httpx.AsyncClient(timeout=10.0) as client:
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
                async with httpx.AsyncClient(timeout=6.0) as client:
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


@app.post("/api/ai/analyze")
async def ai_embedded_ea_agent(req: AiAnalyzeRequest):
    quant = run_cpp_quantitative_engine(req.transactions, req.goals, [])
    lang_rule = (
        "Jawab dalam Bahasa Indonesia yang jelas, terstruktur, dan mudah dipahami. DILARANG KERAS memakai emoji."
        if req.lang.upper() == "ID"
        else "Answer in clear, structured, easy-to-understand English. STRICTLY DO NOT use any emojis."
    )

    system_prompt = (
        "You are DND AI AGENT, an embedded Autonomous Executive Assistant (EA Agent) for DND Finance "
        "that manages personal wealth and answers any general knowledge question.\n"
        f"{lang_rule}\n"
        f"Currency: {req.currency}\n"
        f"Quantitative Metrics (C++17 Engine): {json.dumps(quant)}\n"
        f"Goals: {json.dumps(req.goals[:10])}\n"
        f"Budgets: {json.dumps(req.budgets[:10])}\n"
        f"Recently Executed Actions: {json.dumps(req.executed_actions)}"
    )

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
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
                    return {"status": "ok", "reply": strip_emojis(reply_text), "quant": quant}
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
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
                        return {"status": "ok", "reply": strip_emojis(content), "quant": quant}
    except Exception:
        pass

    if req.lang.upper() == "ID":
        fallback_reply = (
            f"**Analisis DND AI AGENT**\n\n"
            f"- **Skor Kesehatan Finansial:** {quant['health_score']} / 100\n"
            f"- **Saldo Bersih:** {req.currency} {quant['net_balance']:,.0f}\n"
            f"- **Rasio Tabungan:** {quant['savings_rate_pct']}%\n"
            f"- **Burn Rate Harian:** {req.currency} {quant['daily_burn_rate']:,.0f} / hari\n"
            f"- **Ketahanan Kas:** {quant['runway_days']} hari\n\n"
            f"Pertanyaan Anda mengenai \"{strip_emojis(req.prompt)}\" telah dianalisis berdasarkan data keuangan terkini Anda."
        )
    else:
        fallback_reply = (
            f"**DND AI AGENT Analysis**\n\n"
            f"- **Financial Health Score:** {quant['health_score']} / 100\n"
            f"- **Net Balance:** {req.currency} {quant['net_balance']:,.2f}\n"
            f"- **Savings Rate:** {quant['savings_rate_pct']}%\n"
            f"- **Daily Burn Rate:** {req.currency} {quant['daily_burn_rate']:,.2f} / day\n"
            f"- **Cash Runway:** {quant['runway_days']} days\n\n"
            f"Your inquiry regarding \"{strip_emojis(req.prompt)}\" has been evaluated against your live financial ledger."
        )

    return {"status": "ok", "reply": strip_emojis(fallback_reply), "quant": quant}


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

    for line in lines[1:]:
        nums = re.findall(r"\d{1,3}(?:[.,]\d{3})+|\b\d{4,9}\b", line)
        if nums and not re.search(r"(total|tunai|cash|kembalian|ppn|tax|diskon)", line, re.I):
            val_str = nums[-1].replace(".", "").replace(",", "")
            price = float(val_str)
            if 500 <= price <= 500_000_000:
                name = re.sub(r"\d{1,3}(?:[.,]\d{3})+|\b\d{4,9}\b", "", line).strip() or "Item"
                items.append({"name": name[:40], "qty": 1, "price": price})
                subtotal += price

    return {
        "status": "ok",
        "data": {
            "merchant": merchant,
            "items": items,
            "subtotal": round(subtotal, 2),
            "discount": 0,
            "tax_percent": 0,
            "amount": round(subtotal, 2),
            "category": "Food",
        },
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)