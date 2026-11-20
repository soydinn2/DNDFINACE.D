#include <cmath>
#include <algorithm>

extern "C" {

/**
 * Struktur hasil metrik buku besar yang dikirim ke Python via ctypes
 */
struct DndLedgerMetrics {
    double total_income;
    double total_expense;
    double total_withdrawal;
    double net_balance;
    int health_score;
    double vat_11_percent;
    double fee_1_5_percent;
};

/**
 * Struktur hasil evaluasi aturan otomasi keuangan
 */
struct DndAutomationResult {
    int triggered;
    double allocated_amount;
};

/**
 * Menghitung ringkasan arus kas, saldo bersih, rasio kesehatan finansial,
 * estimasi PPN 11%, dan biaya transaksi 1.5% secara deterministik dalam C++17.
 *
 * Kode tipe transaksi:
 * 1 = income, 2 = expense, 3 = withdrawal, 4 = transfer
 */
DndLedgerMetrics dnd_compute_ledger_metrics(const double* amounts, const int* types, int count) {
    DndLedgerMetrics out{0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0};
    if (!amounts || !types || count <= 0) {
        return out;
    }

    double gross_volume = 0.0;
    for (int i = 0; i < count; ++i) {
        const double amt = amounts[i] > 0.0 ? amounts[i] : 0.0;
        const int tx_type = types[i];

        if (tx_type == 1) {
            out.total_income += amt;
            gross_volume += amt;
        } else if (tx_type == 2) {
            out.total_expense += amt;
            gross_volume += amt;
        } else if (tx_type == 3) {
            out.total_withdrawal += amt;
            gross_volume += amt;
        }
    }

    const double total_outflow = out.total_expense + out.total_withdrawal;
    out.net_balance = out.total_income - total_outflow;

    if (out.total_income > 0.0) {
        const double raw_ratio = ((out.total_income - total_outflow) / out.total_income) * 100.0;
        const int rounded = static_cast<int>(std::lround(raw_ratio));
        out.health_score = std::max(0, std::min(100, rounded));
    } else {
        out.health_score = 0;
    }

    out.vat_11_percent = out.total_income * 0.11;
    out.fee_1_5_percent = gross_volume * 0.015;
    return out;
}

/**
 * Kalkulasi pembagian tagihan patungan (Split Bill) presisi tinggi
 */
void dnd_compute_split_bill(double subtotal, double tax_percent, int people, double* out_grand_total, double* out_per_person) {
    const double clean_sub = subtotal > 0.0 ? subtotal : 0.0;
    const double clean_tax = tax_percent > 0.0 ? tax_percent : 0.0;
    const int clean_people = people >= 1 ? people : 1;

    const double grand = clean_sub + (clean_sub * (clean_tax / 100.0));
    const double per_head = grand / static_cast<double>(clean_people);

    if (out_grand_total) *out_grand_total = grand;
    if (out_per_person) *out_per_person = per_head;
}

/**
 * Evaluasi aturan otomasi transaksi:
 * rule_type: 1 = auto_save_goal, 2 = round_up_save, 3 = auto_transfer_wallet, 4 = spend_alert
 * tx_type:   1 = income, 2 = expense, 3 = withdrawal, 4 = transfer
 */
DndAutomationResult dnd_evaluate_automation_rule(
    int rule_type,
    int tx_type,
    double tx_amount,
    double min_amount,
    double percent,
    double threshold_amount,
    double round_unit
) {
    DndAutomationResult res{0, 0.0};
    if (tx_amount <= 0.0) return res;

    if ((rule_type == 1 || rule_type == 3) && tx_type == 1 && tx_amount >= min_amount && percent > 0.0) {
        res.triggered = 1;
        res.allocated_amount = std::round(tx_amount * (percent / 100.0));
    } else if (rule_type == 2 && tx_type == 2 && round_unit > 0.0) {
        const double rem = std::fmod(tx_amount, round_unit);
        if (rem > 0.0) {
            res.triggered = 1;
            res.allocated_amount = std::round(round_unit - rem);
        }
    } else if (rule_type == 4 && tx_type == 2 && tx_amount >= threshold_amount) {
        res.triggered = 1;
        res.allocated_amount = tx_amount;
    }

    return res;
}

/**
 * Estimasi jumlah bulan yang dibutuhkan untuk mencapai target tabungan
 */
int dnd_forecast_savings_months(double current_amount, double target_amount, double monthly_net_savings) {
    const double remaining = target_amount - current_amount;
    if (remaining <= 0.0) return 0;
    if (monthly_net_savings <= 0.0) return -1;
    return static_cast<int>(std::ceil(remaining / monthly_net_savings));
}

} // extern "C"
