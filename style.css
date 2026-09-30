/* --- EXECUTIVE FINTECH DESIGN SYSTEM --- */
:root {
    /* Color Palette */
    --bg-body: #F4F7F9;
    --bg-surface: #FFFFFF;
    --bg-sidebar: #0B1120;
    
    --text-primary: #0F172A;
    --text-secondary: #64748B;
    --text-muted: #94A3B8;
    
    --border-light: #E2E8F0;
    
    /* Semantic Status Colors */
    --brand-blue: #2563EB;     /* Income / Revenue */
    --status-danger: #EF4444;  /* OPEX / Expense */
    --status-warning: #F59E0B; /* Withdrawal / Gold */
    --status-success: #10B981; /* Net Profit */
    
    /* Layout & Shadows */
    --radius-md: 8px;
    --radius-lg: 12px;
    --shadow-soft: 0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.03);
    --shadow-hover: 0 10px 15px -3px rgb(0 0 0 / 0.08), 0 4px 6px -4px rgb(0 0 0 / 0.05);
    
    --font-family: 'Inter', system-ui, sans-serif;
}

/* --- RESET & LOCK LAYOUT --- */
* { margin: 0; padding: 0; box-sizing: border-box; }
[x-cloak] { display: none !important; }

body {
    font-family: var(--font-family);
    background-color: var(--bg-body);
    color: var(--text-primary);
    overflow-x: hidden;
    -webkit-font-smoothing: antialiased;
}

.layout-wrapper { display: flex; min-height: 100vh; }

/* --- SIDEBAR --- */
.sidebar {
    width: 260px;
    background-color: var(--bg-sidebar);
    color: white;
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    position: sticky;
    top: 0;
    height: 100vh;
    z-index: 50;
}

.brand { padding: 32px 24px; display: flex; align-items: center; gap: 12px; }
.logo { font-size: 26px; font-weight: 800; letter-spacing: -1px; }
.badge {
    font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
    background: rgba(255,255,255,0.1); padding: 4px 8px;
    border-radius: 4px; color: #93C5FD;
}

.nav-menu { flex-grow: 1; padding: 0 16px; display: flex; flex-direction: column; gap: 6px; }
.nav-link {
    display: flex; align-items: center; gap: 12px; padding: 12px 16px;
    color: var(--text-muted); text-decoration: none; font-size: 14px;
    font-weight: 500; border-radius: var(--radius-md); transition: all 0.2s ease;
}
.nav-icon { width: 6px; height: 6px; border-radius: 50%; background-color: transparent; transition: background-color 0.2s; }
.nav-link:hover { color: white; background-color: rgba(255,255,255,0.05); transform: translateX(4px); }
.nav-link.active { color: white; background-color: var(--brand-blue); }
.nav-link.active .nav-icon { background-color: white; }

.btn-google-login {
    width: calc(100% - 32px); margin: 16px; display: flex; align-items: center; justify-content: center; gap: 10px;
    background: white; color: var(--text-primary); padding: 12px;
    border-radius: var(--radius-md); font-weight: 600; font-size: 13px;
    cursor: pointer; border: none; transition: transform 0.2s;
}
.btn-google-login:active { transform: scale(0.97); }

/* --- WORKSPACE & TOPBAR --- */
.workspace { flex-grow: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar {
    height: 80px; background-color: var(--bg-surface);
    border-bottom: 1px solid var(--border-light); display: flex;
    align-items: center; justify-content: space-between;
    padding: 0 40px; position: sticky; top: 0; z-index: 10;
}
.page-title { font-size: 20px; font-weight: 600; }

.mode-toggle {
    display: flex; align-items: center; gap: 10px; padding: 8px 16px;
    border-radius: 30px; background-color: #F1F5F9; color: var(--text-secondary);
    font-weight: 500; font-size: 14px; border: 1px solid var(--border-light);
    cursor: pointer; transition: all 0.3s ease;
}
.mode-toggle .toggle-indicator { width: 10px; height: 10px; border-radius: 50%; background-color: var(--status-success); }
.mode-toggle.business { background-color: #EFF6FF; border-color: #BFDBFE; color: var(--brand-blue); }
.mode-toggle.business .toggle-indicator { background-color: var(--brand-blue); }

/* --- CARDS & FORMS --- */
.view-content { padding: 40px; }
.card {
    background-color: var(--bg-surface); border-radius: var(--radius-lg);
    border: 1px solid var(--border-light); box-shadow: var(--shadow-soft);
    transition: box-shadow 0.3s ease, transform 0.3s ease;
}
.card:hover { box-shadow: var(--shadow-hover); transform: translateY(-2px); }

.quick-add-card { margin-bottom: 24px; padding: 20px 24px; }
.entry-form { display: grid; grid-template-columns: 1fr 1fr 2fr 1.5fr auto; gap: 16px; align-items: center; }
.entry-form input, .entry-form select {
    padding: 12px 16px; border-radius: var(--radius-md); border: 1px solid var(--border-light);
    font-family: var(--font-family); font-size: 14px; background: #F8FAFC; outline: none;
    transition: border-color 0.2s;
}
.entry-form input:focus, .entry-form select:focus { border-color: var(--brand-blue); }
.btn-primary {
    background-color: var(--brand-blue); color: white; padding: 12px 24px;
    border-radius: var(--radius-md); border: none; font-weight: 600; cursor: pointer; transition: background 0.2s;
}
.btn-primary:hover { background-color: #1D4ED8; }

/* --- METRICS --- */
.metrics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; margin-bottom: 24px; }
.metric-card { padding: 24px; }
.metric-title { font-size: 13px; font-weight: 500; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.metric-value { font-size: 32px; font-weight: 700; letter-spacing: -1px; }

/* Semantic Text Colors */
.text-primary { color: var(--text-primary); }
.text-blue { color: var(--brand-blue); }
.text-red { color: var(--status-danger); }
.text-amber { color: var(--status-warning); } /* Withdrawal Color */

/* --- CHARTS --- */
.charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.chart-card { padding: 24px; }
.card-title { font-size: 16px; font-weight: 600; color: var(--text-primary); margin-bottom: 20px; }
.echart-container { width: 100%; height: 350px; }

/* --- TABLE & TAGS --- */
.records-card { padding: 24px; }
.data-table { width: 100%; border-collapse: collapse; }
.data-table th { text-align: left; padding: 16px; font-size: 12px; text-transform: uppercase; color: var(--text-secondary); border-bottom: 1px solid var(--border-light); }
.data-table td { padding: 16px; font-size: 14px; border-bottom: 1px solid var(--border-light); color: var(--text-primary); }
.table-row:hover { background-color: #F8FAFC; }

.text-right { text-align: right; }
.text-center { text-align: center; }
.font-medium { font-weight: 500; }
.font-semibold { font-weight: 600; }

/* Status Tags */
.tag { padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; letter-spacing: 0.3px; }
.tag-revenue { background: #DBEAFE; color: #1E40AF; }
.tag-expense { background: #FEE2E2; color: #991B1B; }
.tag-withdrawal { background: #FEF3C7; color: #B45309; border: 1px solid #FDE68A; } /* Gold Tag */

.btn-delete { background: transparent; color: var(--status-danger); border: 1px solid var(--status-danger); padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
.btn-delete:hover { background: var(--status-danger); color: white; }

/* --- MODAL PIN --- */
.modal-overlay { position: fixed; inset: 0; background: rgba(11, 17, 32, 0.6); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal-content { background: white; padding: 32px; border-radius: var(--radius-lg); width: 400px; box-shadow: 0 20px 25px -5px rgb(0 0 0 / 0.1); }
.modal-content h3 { margin-bottom: 8px; font-size: 18px; }
.modal-content p { color: var(--text-secondary); font-size: 14px; margin-bottom: 24px; }
.modal-content input { width: 100%; padding: 14px; border: 1px solid var(--border-light); border-radius: var(--radius-md); margin-bottom: 24px; font-size: 24px; text-align: center; letter-spacing: 4px; outline: none; }
.modal-content input:focus { border-color: var(--brand-blue); }
.modal-actions { display: flex; gap: 12px; justify-content: flex-end; }
.btn-secondary { background: white; border: 1px solid var(--border-light); padding: 10px 20px; border-radius: var(--radius-md); cursor: pointer; font-weight: 500; }
.error-text { color: var(--status-danger); margin-top: 16px; font-size: 13px; text-align: center; font-weight: 500; }
