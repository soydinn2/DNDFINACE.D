# DND Finance — Paket Lengkap Aplikasi Web & DND AI AGENT

Seluruh kode sumber dan konfigurasi **DND Finance** (`dnd-finance-3590e`) telah disusun lengkap dalam satu paket folder utama berikut:

```text
dnd-finance/
├── index.html               # Antarmuka Web Utama (UI bersih, DND AI AGENT, Login Google Browser, Scanner Struk & Split Bill)
├── main.py                  # Server Backend FastAPI (Google OAuth2 Verify, Sync Firebase RTDB, EA Agent, Jembatan C++17)
├── quant_ai_engine.cpp      # Mesin Kuantitatif & Rekonsiliasi Matematika Struk berkecepatan tinggi (C++17)
├── database.rules.json      # Aturan keamanan Firebase Realtime Database untuk proyek dnd-finance-3590e
├── .env                     # Konfigurasi rahasia Firebase Realtime Database & Google OAuth2 Client ID
├── requirements.txt         # Daftar pustaka Python (FastAPI, Uvicorn, HTTPX, Firebase Admin, Pillow, Pytesseract)
├── run.sh                   # Skrip compile & jalankan otomatis di Linux / macOS / Termux
└── run.bat                  # Skrip compile & jalankan otomatis di Windows
```

---

## Cara Menjalankan Aplikasi

1. **Publish Security Rules di Firebase Console (`dnd-finance-3590e`)**
   - Buka **Firebase Console -> Build -> Realtime Database -> tab `Rules`**.
   - Tempelkan seluruh isi file **`database.rules.json`**, lalu klik **`Publish`**.

2. **Simpan Semua File dalam Satu Folder (`dnd-finance/`)**
   - Pastikan ke-8 file (`index.html`, `main.py`, `quant_ai_engine.cpp`, `database.rules.json`, `.env`, `requirements.txt`, `run.sh`, `run.bat`) berada sejajar di dalam satu folder yang sama.

3. **Jalankan Server**
   - **Linux / macOS / Termux:** jalankan `bash run.sh`
   - **Windows:** klik ganda `run.bat` atau jalankan `run.bat` di Command Prompt.

4. **Akses & Verifikasi**
   - Cek status koneksi database Firebase di **`http://localhost:8000/api/firebase/status`** (memastikan `"connected": true`).
   - Buka aplikasi utama di **`http://localhost:8000`**, tekan **Masuk Google** langsung di browser, dan mulai gunakan **DND Finance** beserta **DND AI AGENT**.