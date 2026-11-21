#include <cmath>
#include <algorithm>

#ifdef _WIN32
#define DND_EXPORT extern "C" __declspec(dllexport)
#else
#define DND_EXPORT extern "C" __attribute__((visibility("default")))
#endif

struct DndMetricsResult {
    double total_income;
    double total_expense;
    double total_withdrawal;
    double net_balance;
    int health_score;
    double vat_11_percent;
    double fee_1_5_percent;
};

struct DndAutomationResult {
    int triggered;
    double allocated_amount;
};

DND_EXPORT DndMetricsResult dnd_compute_ledger_metrics(
    const double* amounts,
    const int* types,
    int count
) {
    DndMetricsResult res{0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0};
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

    double outflow = res.total_expense + res.total_withdrawal;
    res.net_balance = res.total_income - outflow;

    if (res.total_income > 0.0) {
        double ratio = ((res.total_income - outflow) / res.total_income) * 100.0;
        int rounded = static_cast<int>(std::llround(ratio));
        res.health_score = std::max(0, std::min(100, rounded));
    } else {
        res.health_score = 0;
    }

    res.vat_11_percent = res.total_income * 0.11;
    res.fee_1_5_percent = (res.total_income + outflow) * 0.015;
    return res;
}

DND_EXPORT void dnd_compute_split_bill(
    double subtotal,
    double tax_percent,
    int people,
    double* grand_total_out,
    double* per_person_out
) {
    double clean_sub = subtotal > 0.0 ? subtotal : 0.0;
    double clean_tax = tax_percent > 0.0 ? tax_percent : 0.0;
    int clean_people = people > 0 ? people : 1;

    double grand = clean_sub + (clean_sub * (clean_tax / 100.0));
    double per_person = grand / static_cast<double>(clean_people);

    if (grand_total_out) *grand_total_out = grand;
    if (per_person_out) *per_person_out = per_person;
}

DND_EXPORT DndAutomationResult dnd_evaluate_automation_rule(
    int rule_type,
    int tx_type,
    double tx_amount,
    double min_amount,
    double percent,
    double threshold_amount,
    double fixed_amount
) {
    DndAutomationResult out{0, 0.0};
    if (tx_amount <= 0.0) return out;

    // 1 = auto_save_goal / auto_transfer_wallet on income
    if (rule_type == 1 && tx_type == 1 && tx_amount >= min_amount) {
        out.triggered = 1;
        out.allocated_amount = std::round(tx_amount * (percent / 100.0));
    }
    // 2 = fixed_save_goal on income
    else if (rule_type == 2 && tx_type == 1 && tx_amount >= min_amount && fixed_amount > 0.0) {
        out.triggered = 1;
        out.allocated_amount = std::round(fixed_amount);
    }
    // 3 = spend_alert on expense
    else if (rule_type == 3 && tx_type == 2 && tx_amount >= threshold_amount) {
        out.triggered = 1;
        out.allocated_amount = tx_amount;
    }
    // 4 = expense_save_goal / expense_transfer_wallet on expense
    else if (rule_type == 4 && tx_type == 2 && tx_amount >= min_amount) {
        out.triggered = 1;
        out.allocated_amount = std::round(tx_amount * (percent / 100.0));
    }
    return out;
}
