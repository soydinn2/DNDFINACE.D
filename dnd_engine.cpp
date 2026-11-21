#include <cmath>
#include <algorithm>

#ifdef _WIN32
#define DND_EXPORT extern "C" __declspec(dllexport)
#else
#define DND_EXPORT extern "C" __attribute__((visibility("default")))
#endif

struct CppMetricsStruct {
    double total_income;
    double total_expense;
    double total_withdrawal;
    double net_balance;
    int health_score;
    double vat_11_percent;
    double fee_1_5_percent;
};

struct CppAutomationStruct {
    int triggered;
    double allocated_amount;
};

DND_EXPORT CppMetricsStruct dnd_compute_ledger_metrics(const double* amounts, const int* types, int count) {
    CppMetricsStruct res{0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0};
    if (!amounts || !types || count <= 0) {
        return res;
    }

    for (int i = 0; i < count; ++i) {
        double amt = amounts[i] > 0.0 ? amounts[i] : 0.0;
        int t = types[i];
        if (t == 1) {
            res.total_income += amt;
        } else if (t == 2) {
            res.total_expense += amt;
        } else if (t == 3) {
            res.total_withdrawal += amt;
        }
    }

    double total_outflow = res.total_expense + res.total_withdrawal;
    res.net_balance = res.total_income - total_outflow;

    if (res.total_income > 0.0) {
        double ratio = ((res.total_income - total_outflow) / res.total_income) * 100.0;
        int score = static_cast<int>(std::round(ratio));
        res.health_score = std::max(0, std::min(100, score));
    } else {
        res.health_score = 0;
    }

    res.vat_11_percent = res.total_income * 0.11;
    res.fee_1_5_percent = (res.total_income + total_outflow) * 0.015;
    return res;
}

DND_EXPORT void dnd_compute_split_bill(double subtotal, double tax_percent, int people, double* grand_total_out, double* per_person_out) {
    double safe_sub = subtotal > 0.0 ? subtotal : 0.0;
    double safe_tax = tax_percent > 0.0 ? tax_percent : 0.0;
    int safe_people = people > 0 ? people : 1;

    double grand = safe_sub + (safe_sub * (safe_tax / 100.0));
    if (grand_total_out) {
        *grand_total_out = grand;
    }
    if (per_person_out) {
        *per_person_out = grand / static_cast<double>(safe_people);
    }
}

DND_EXPORT CppAutomationStruct dnd_evaluate_automation_rule(
    int rule_code,
    int tx_type_code,
    double tx_amount,
    double min_amount,
    double percent,
    double threshold_amount,
    double fixed_amount
) {
    CppAutomationStruct out{0, 0.0};
    if (tx_amount <= 0.0) {
        return out;
    }

    if (rule_code == 1 && tx_type_code == 1 && tx_amount >= min_amount) {
        out.triggered = 1;
        out.allocated_amount = std::round(tx_amount * (percent / 100.0));
    } else if (rule_code == 2 && tx_type_code == 1 && tx_amount >= min_amount) {
        out.triggered = 1;
        out.allocated_amount = std::round(fixed_amount);
    } else if (rule_code == 3 && tx_type_code == 2 && tx_amount >= threshold_amount) {
        out.triggered = 1;
        out.allocated_amount = tx_amount;
    } else if (rule_code == 4 && tx_type_code == 2 && tx_amount >= min_amount) {
        out.triggered = 1;
        out.allocated_amount = std::round(tx_amount * (percent / 100.0));
    }
    return out;
}
