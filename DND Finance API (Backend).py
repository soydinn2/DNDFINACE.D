from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from urllib.parse import urlencode
import pandas as pd
import sqlite3
import os
import re
import json
import base64
import urllib.request
import time

# Inisialisasi Aplikasi FastAPI
app = FastAPI(title="DND Finance Real API", description="Production-ready Full-Stack Backend with Direct Google OAuth 2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "dnd_finance.db"
DEFAULT_BANKS = ["Bank BCA", "Bank Mandiri", "Bank BNI", "Bank Jago", "Corporate Bank", "Gopay", "Cash"]

def init_db():
    """Inisialisasi & migrasi skema Database SQLite nyata"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS transactions
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         amount REAL, type TEXT, category TEXT, account TEXT,
         to_account TEXT, admin_fee REAL, note TEXT, currency TEXT, date TEXT)
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS custom_banks
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS users
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         google_sub TEXT UNIQUE, email TEXT, name TEXT, picture TEXT, last_login TEXT)
    ''')

    c.execute("PRAGMA table_info(transactions)")
    existing_cols = {row[1] for row in c.fetchall()}
    if "to_account" not in existing_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN to_account TEXT DEFAULT ''")
    if "admin_fee" not in existing_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN admin_fee REAL DEFAULT 0.0")
    if "note" not in existing_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN note TEXT DEFAULT ''")

    c.execute("SELECT COUNT(*) FROM custom_banks")
    if c.fetchone()[0] == 0:
        for bank in DEFAULT_BANKS:
            c.execute("INSERT OR IGNORE INTO custom_banks (name) VALUES (?)", (bank,))

    conn.commit()
    conn.close()

init_db()

class BankAccount(BaseModel):
    name: str

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

class AIAnalysisRequest(BaseModel):
    prompt: str
    financial_context: Optional[str] = ""
    lang: Optional[str] = "ID"
    mode: Optional[str] = "personal"

class ReceiptScanRequest(BaseModel):
    image_base64: str
    mime_type: Optional[str] = "image/jpeg"
    mode: Optional[str] = "personal"

class GoogleAuthSession(BaseModel):
    access_token: Optional[str] = ""
    sub: Optional[str] = ""
    email: Optional[str] = ""
    name: Optional[str] = ""
    picture: Optional[str] = ""

def _upsert_google_user(sub: str, email: str, name: str, picture: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        '''
        INSERT INTO users (google_sub, email, name, picture, last_login)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(google_sub) DO UPDATE SET
            email = excluded.email,
            name = excluded.name,
            picture = excluded.picture,
            last_login = excluded.last_login
        ''',
        (sub or email, email, name, picture, now_str),
    )
    conn.commit()
    conn.close()

def _call_gemini_backend(parts: list, system_text: str = "", json_mode: bool = False) -> Optional[str]:
    """Helper Python untuk memanggil Gemini API langsung dari server jika GEMINI_API_KEY tersedia"""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={api_key}"
    payload = {"contents": [{"role": "user", "parts": parts}]}
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
    except Exception:
        return None

@app.get("/")
async def serve_frontend():
    """Menyajikan antarmuka DND Finance langsung jika dibuka melalui port Python"""
    if os.path.exists("index.html"):
        return FileResponse("index.html", media_type="text/html")
    return {"status": "ok", "message": "DND Finance Full-Stack API Running"}

@app.get("/api/health")
async def health_check():
    """Endpoint pengecekan koneksi backend"""
    return {"status": "ok", "service": "DND Finance API"}

@app.get("/api/auth/config")
async def get_google_oauth_config():
    """Mengembalikan Google OAuth 2.0 Client ID langsung dari environment server"""
    return {"google_client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip()}

@app.get("/api/auth/google/login")
async def google_oauth_redirect(request: Request):
    """Redirect langsung ke Google OAuth 2.0 Authorization Server"""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
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
    """Menukar authorization code dari Google menjadi access_token dan profil pengguna"""
    if not code:
        return RedirectResponse(url="/?google_auth=cancelled")
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
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
        name = g_info.get("name", "DND Executive")
        picture = g_info.get("picture", "")
        _upsert_google_user(sub, email, name, picture)

        qs = urlencode({
            "google_auth": "success",
            "sub": sub,
            "email": email,
            "name": name,
            "picture": picture,
            "access_token": access_token,
        })
        return RedirectResponse(url=f"/?{qs}")
    except Exception:
        return RedirectResponse(url="/?google_auth=error")

@app.post("/api/auth/google")
async def verify_google_auth(session: GoogleAuthSession):
    """Verifikasi token OAuth2 Google langsung melalui Google UserInfo API dan simpan sesi pengguna"""
    sub = session.sub or ""
    email = session.email or ""
    name = session.name or "DND User"
    picture = session.picture or ""

    if session.access_token:
        try:
            req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {session.access_token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                g_info = json.loads(resp.read().decode("utf-8"))
                sub = g_info.get("sub", sub)
                email = g_info.get("email", email)
                name = g_info.get("name", name)
                picture = g_info.get("picture", picture)
        except Exception:
            pass

    if not email and not sub:
        return {"status": "error", "message": "Data akun Google tidak valid"}

    _upsert_google_user(sub or email, email, name, picture)
    return {
        "status": "authenticated",
        "user": {"sub": sub or email, "email": email, "name": name, "picture": picture},
    }

@app.get("/api/banks")
async def get_banks():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name FROM custom_banks ORDER BY id ASC")
    banks = [row[0] for row in c.fetchall()]
    conn.close()
    return {"banks": banks or DEFAULT_BANKS}

@app.post("/api/banks")
async def add_bank(bank: BankAccount):
    name = bank.name.strip()
    if not name:
        return {"status": "error", "message": "Nama bank tidak boleh kosong"}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO custom_banks (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    return {"status": "success", "name": name}

@app.delete("/api/banks/{bank_name}")
async def delete_bank(bank_name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM custom_banks WHERE name = ?", (bank_name,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "name": bank_name}

@app.get("/api/transactions")
async def get_transactions():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC")
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"transactions": rows}

@app.post("/api/transactions")
async def create_transaction(tx: Transaction):
    tx_date = tx.date or datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''
        INSERT INTO transactions (amount, type, category, account, to_account, admin_fee, note, currency, date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (tx.amount, tx.type, tx.category, tx.account, tx.to_account or "", tx.admin_fee or 0.0, tx.note or "", tx.currency or "IDR", tx_date),
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return {"status": "success", "id": new_id}

@app.delete("/api/transactions/{tx_id}")
async def delete_transaction(tx_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": tx_id}

@app.post("/api/ai/analyze")
async def ai_analyze_finances(req: AIAnalysisRequest):
    """Endpoint Full-Stack Python untuk DND AI Financial Analyst"""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM transactions", conn)
    conn.close()

    server_summary = ""
    if not df.empty:
        inc = float(df[df["type"] == "income"]["amount"].sum())
        exp = float(df[df["type"].isin(["expense", "withdrawal"])]["amount"].sum())
        bal = inc - exp
        top_cats = df[df["type"] == "expense"].groupby("category")["amount"].sum().sort_values(ascending=False).head(3).to_dict()
        server_summary = f"\n[RINGKASAN DATABASE PYTHON]: Saldo={bal:,.0f}, Pemasukan={inc:,.0f}, Pengeluaran={exp:,.0f}, Top Pengeluaran={top_cats}"

    sys_prompt = (
        "Anda adalah DND AI Financial Analyst profesional. Berikan analisis keuangan yang ringkas, tajam, terstruktur dalam poin-poin jelas."
        if req.lang == "ID"
        else "You are DND AI Financial Analyst. Provide a concise, structured executive financial analysis."
    )
    combined_prompt = f"{req.financial_context}{server_summary}\n\nPERMINTAAN:\n{req.prompt}"
    gemini_reply = _call_gemini_backend([{"text": combined_prompt}], system_text=sys_prompt)

    if gemini_reply:
        return {"status": "success", "source": "gemini_backend", "analysis": gemini_reply}

    if df.empty:
        fallback_text = (
            "### Analisis Server DND Finance\n"
            "- Belum terdapat data transaksi di database server.\n"
            "- Silakan catat pemasukan (Salary) dan pengeluaran pertama Anda agar AI dapat menghitung rasio tabungan dan distribusi kas secara otomatis."
        )
    else:
        inc = float(df[df["type"] == "income"]["amount"].sum())
        exp = float(df[df["type"].isin(["expense", "withdrawal"])]["amount"].sum())
        bal = inc - exp
        ratio = round(((inc - exp) / inc) * 100, 1) if inc > 0 else 0.0
        cat_series = df[df["type"] == "expense"].groupby("category")["amount"].sum().sort_values(ascending=False)
        top_cat_name = cat_series.index[0] if len(cat_series) > 0 else "-"
        top_cat_val = float(cat_series.iloc[0]) if len(cat_series) > 0 else 0.0
        fallback_text = (
            f"### Audit Finansial Server (Python Engine)\n"
            f"- **Total Saldo Bersih:** Rp {bal:,.0f} (Rasio Tabungan: **{ratio}%**)\n"
            f"- **Total Pemasukan vs Pengeluaran:** Rp {inc:,.0f} vs Rp {exp:,.0f}\n"
            f"- **Pos Pengeluaran Terbesar:** {top_cat_name} (Rp {top_cat_val:,.0f})\n"
            f"### Rekomendasi Tindakan\n"
            f"- Jaga rasio tabungan di atas **20%** dan pisahkan saldo operasional ke rekening berbeda melalui fitur **Transfer**."
        )
    return {"status": "success", "source": "python_engine", "analysis": fallback_text}

@app.post("/api/scan/receipt")
async def scan_receipt_endpoint(req: ReceiptScanRequest):
    """Endpoint Full-Stack Python untuk ekstraksi kamera/struk belanja (Vision & OCR)"""
    prompt_text = (
        'Ekstrak total pembayaran akhir, kategori (pilih satu: Food, Transport, Utilities, Shopping, Health & Lifestyle, Office Rent, SaaS/Software), '
        'dan nama toko/keterangan singkat dari gambar struk ini dalam JSON: {"amount": 0, "category": "Food", "note": "..."}'
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
                return {
                    "status": "success",
                    "amount": float(parsed["amount"]),
                    "category": parsed.get("category", "Food"),
                    "note": parsed.get("note", "Hasil scan struk"),
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
            return {
                "status": "success",
                "amount": max(valid_nums),
                "category": "Food",
                "note": "Hasil OCR Struk (Python)",
            }
    except Exception:
        pass

    return {"status": "fallback", "amount": 0}

@app.get("/api/export/excel")
async def export_excel_report(background_tasks: BackgroundTasks):
    """Export Excel Power BI Style (Overview Analysis + Transactions)"""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM transactions", conn)
    conn.close()

    if df.empty:
        df = pd.DataFrame([{
            "id": 1,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "category": "Salary",
            "type": "income",
            "account": "Bank BCA",
            "to_account": "",
            "note": "Initial Entry",
            "amount": 0.0
        }])

    export_path = f"DND_Financial_Report_{int(time.time())}.xlsx"
    writer = pd.ExcelWriter(export_path, engine="xlsxwriter")
    workbook = writer.book

    ws_overview = workbook.add_worksheet("Overview Analysis")
    ws_tx = workbook.add_worksheet("Transactions")

    hdr_fmt = workbook.add_format({"bold": True, "bg_color": "#1D4ED8", "font_color": "#FFFFFF", "border": 1})
    tbl_hdr = workbook.add_format({"bold": True, "bg_color": "#E2E8F0", "font_color": "#0F172A", "border": 1})
    cell_fmt = workbook.add_format({"border": 1})
    curr_fmt = workbook.add_format({"num_format": '"Rp "#,##0', "border": 1})
    kpi_lbl = workbook.add_format({"bold": True, "font_color": "#475569", "bg_color": "#F8FAFC", "border": 1})
    kpi_val = workbook.add_format({"bold": True, "font_size": 14, "num_format": '"Rp "#,##0', "bg_color": "#FFFFFF", "border": 1})

    headers = ["ID", "Date", "Category", "Type", "Account / Route", "Note", "Amount", "Fees (1.5%)", "Tax (11%)"]
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

    ws_overview.merge_range("D5:F5", "Total Amount", kpi_lbl)
    ws_overview.merge_range("D6:F6", f"=SUM(Transactions!G2:G{last_row})", kpi_val)

    ws_overview.merge_range("H5:J5", "Total Transactions", kpi_lbl)
    ws_overview.merge_range("H6:J6", f"=COUNTA(Transactions!A2:A{last_row})", kpi_val)

    ws_overview.merge_range("L5:N5", "Avg Transaction Value", kpi_lbl)
    ws_overview.merge_range("L6:N6", f"=IFERROR(AVERAGE(Transactions!G2:G{last_row}), 0)", kpi_val)

    summary = df.groupby("category")["amount"].sum().reset_index()
    ws_overview.write("K22", "Transaction Type", tbl_hdr)
    ws_overview.write("L22", "Amount", tbl_hdr)
    ws_overview.write("M22", "Fees (1.5%)", tbl_hdr)
    ws_overview.write("N22", "Tax (11%)", tbl_hdr)

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
    bar_chart.add_series({
        "name": "Total Amount by Category",
        "categories": ["Overview Analysis", 23, 10, end_mat - 1, 10],
        "values": ["Overview Analysis", 23, 11, end_mat - 1, 11],
        "fill": {"color": "#1D4ED8"}
    })
    bar_chart.set_title({"name": "Total Amount by Category"})
    bar_chart.set_legend({"none": True})
    ws_overview.insert_chart("D22", bar_chart, {"x_scale": 1.15, "y_scale": 1.05})

    donut_chart = workbook.add_chart({"type": "doughnut"})
    donut_chart.add_series({
        "name": "Transaction Breakdown",
        "categories": ["Overview Analysis", 23, 10, end_mat - 1, 10],
        "values": ["Overview Analysis", 23, 11, end_mat - 1, 11],
    })
    donut_chart.set_title({"name": "Total Amount by Transaction Status"})
    ws_overview.insert_chart("K9", donut_chart, {"x_scale": 0.95, "y_scale": 0.9})

    writer.close()

    def remove_file(path: str):
        time.sleep(2)
        if os.path.exists(path):
            os.remove(path)

    background_tasks.add_task(remove_file, export_path)
    return FileResponse(export_path, filename=export_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)