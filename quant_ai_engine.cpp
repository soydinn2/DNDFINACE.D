/**
 * DND Finance — Mesin Kuantitatif C++17, Rekonsiliasi Matematika Struk & Inti DND AI AGENT
 *
 * Kompilasi:
 *   g++ -O3 -std=c++17 quant_ai_engine.cpp -o quant_ai_engine
 */

#include <algorithm>
#include <cmath>
#include <iostream>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace dnd_finance_quant {

struct Transaction {
    std::string id;
    std::string type;
    std::string category;
    std::string account;
    std::string date;
    double amount = 0.0;
};

struct ReceiptItem {
    std::string name;
    int qty = 1;
    double unit_price = 0.0;
    double line_total = 0.0;
};

struct ReceiptMathResult {
    double subtotal = 0.0;
    double discount = 0.0;
    double net_base = 0.0;
    double tax_percent = 0.0;
    double tax_amount = 0.0;
    double grand_total = 0.0;
    int people_count = 1;
    double per_person = 0.0;
};

struct QuantitativeReport {
    int health_score = 50;
    std::string health_grade = "Stabil / Bertumbuh";
    int savings_rate_pct = 0;
    double total_income = 0.0;
    double total_expense = 0.0;
    double net_balance = 0.0;
    double daily_burn_rate = 0.0;
    double monthly_burn_est = 0.0;
    int runway_days = 999;
    double forecast_6m_smart = 0.0;
    double forecast_12m_smart = 0.0;
};

ReceiptMathResult reconcile_receipt_math(
    const std::vector<ReceiptItem>& items,
    double fallback_subtotal,
    double discount,
    double tax_percent,
    int people_count
) {
    ReceiptMathResult res;
    double items_sum = 0.0;
    for (const auto& it : items) {
        int q = std::max(1, it.qty);
        double line = (it.line_total > 0.0) ? it.line_total : (q * std::max(0.0, it.unit_price));
        items_sum += line;
    }

    res.subtotal = (items_sum > 0.0) ? items_sum : std::max(0.0, fallback_subtotal);
    res.discount = std::max(0.0, discount);
    res.net_base = std::max(0.0, res.subtotal - res.discount);
    res.tax_percent = std::max(0.0, tax_percent);
    res.tax_amount = std::round(res.net_base * (res.tax_percent / 100.0) * 100.0) / 100.0;
    res.grand_total = std::round((res.net_base + res.tax_amount) * 100.0) / 100.0;
    res.people_count = std::max(1, people_count);
    res.per_person = std::round((res.grand_total / static_cast<double>(res.people_count)) * 100.0) / 100.0;
    return res;
}

QuantitativeReport analyze_ledger(
    const std::vector<Transaction>& txs,
    int goals_count,
    int active_rules_count
) {
    QuantitativeReport rep;
    std::set<std::string> expense_days;

    for (const auto& tx : txs) {
        if (tx.type == "income") {
            rep.total_income += std::max(0.0, tx.amount);
        } else if (tx.type == "expense" || tx.type == "withdrawal") {
            rep.total_expense += std::max(0.0, tx.amount);
            if (tx.date.size() >= 10) {
                expense_days.insert(tx.date.substr(0, 10));
            }
        }
    }

    rep.net_balance = rep.total_income - rep.total_expense;
    int active_days = std::max(1, static_cast<int>(expense_days.size()));
    rep.daily_burn_rate = rep.total_expense / static_cast<double>(active_days);
    rep.monthly_burn_est = rep.daily_burn_rate * 30.0;

    if (rep.total_income > 0.0) {
        double raw_rate = ((rep.total_income - rep.total_expense) / rep.total_income) * 100.0;
        rep.savings_rate_pct = std::max(-100, std::min(100, static_cast<int>(std::round(raw_rate))));
    } else {
        rep.savings_rate_pct = 0;
    }

    if (rep.daily_burn_rate > 0.0) {
        rep.runway_days = std::max(0, static_cast<int>(std::round(std::max(0.0, rep.net_balance) / rep.daily_burn_rate)));
    } else {
        rep.runway_days = 999;
    }

    int score = 50;
    if (rep.total_income > 0.0) {
        score += std::max(-25, std::min(30, static_cast<int>(std::round(rep.savings_rate_pct * 0.8))));
    }
    if (rep.net_balance > 0.0) score += 10;
    if (goals_count > 0) score += 5;
    if (active_rules_count > 0) score += 5;
    rep.health_score = std::max(5, std::min(100, score));

    if (rep.health_score >= 80) rep.health_grade = "Sangat Sehat (Prima)";
    else if (rep.health_score >= 60) rep.health_grade = "Sehat & Terkendali";
    else if (rep.health_score < 40) rep.health_grade = "Perlu Penghematan";
    else rep.health_grade = "Stabil / Bertumbuh";

    double monthly_smart_save = (rep.total_income > 0.0)
        ? (rep.total_income * 0.20)
        : std::max(0.0, rep.net_balance);

    rep.forecast_6m_smart = std::round(rep.net_balance + monthly_smart_save * 6.0);
    rep.forecast_12m_smart = std::round(rep.net_balance + monthly_smart_save * 12.0);
    return rep;
}

std::vector<Transaction> parse_transactions_from_json(const std::string& raw_json) {
    std::vector<Transaction> txs;
    size_t pos = 0;
    while ((pos = raw_json.find("\"type\"", pos)) != std::string::npos) {
        Transaction tx;
        size_t colon = raw_json.find(':', pos);
        size_t q1 = raw_json.find('"', colon + 1);
        size_t q2 = (q1 != std::string::npos) ? raw_json.find('"', q1 + 1) : std::string::npos;
        if (q1 != std::string::npos && q2 != std::string::npos) {
            tx.type = raw_json.substr(q1 + 1, q2 - q1 - 1);
        }

        size_t amt_pos = raw_json.find("\"amount\"", pos);
        size_t obj_end = raw_json.find('}', pos);
        if (amt_pos != std::string::npos && (obj_end == std::string::npos || amt_pos < obj_end)) {
            size_t a_colon = raw_json.find(':', amt_pos);
            if (a_colon != std::string::npos) {
                std::string num_buf;
                for (size_t i = a_colon + 1; i < raw_json.size(); ++i) {
                    char c = raw_json[i];
                    if ((c >= '0' && c <= '9') || c == '.' || c == '-') {
                        num_buf.push_back(c);
                    } else if (!num_buf.empty()) {
                        break;
                    }
                }
                if (!num_buf.empty()) {
                    try {
                        tx.amount = std::stod(num_buf);
                    } catch (...) {
                        tx.amount = 0.0;
                    }
                }
            }
        }

        size_t date_pos = raw_json.find("\"date\"", pos);
        if (date_pos != std::string::npos && (obj_end == std::string::npos || date_pos < obj_end)) {
            size_t d_colon = raw_json.find(':', date_pos);
            size_t dq1 = raw_json.find('"', d_colon + 1);
            size_t dq2 = (dq1 != std::string::npos) ? raw_json.find('"', dq1 + 1) : std::string::npos;
            if (dq1 != std::string::npos && dq2 != std::string::npos) {
                tx.date = raw_json.substr(dq1 + 1, dq2 - dq1 - 1);
            }
        }

        if (!tx.type.empty()) {
            txs.push_back(tx);
        }
        pos = (obj_end != std::string::npos) ? obj_end + 1 : pos + 6;
    }
    return txs;
}

}  // namespace dnd_finance_quant

int main(int argc, char* argv[]) {
    std::ostringstream ss;
    ss << std::cin.rdbuf();
    std::string input = ss.str();

    std::string mode = (argc > 1) ? argv[1] : "--analyze";
    if (mode == "--analyze") {
        auto txs = dnd_finance_quant::parse_transactions_from_json(input);
        auto rep = dnd_finance_quant::analyze_ledger(txs, 1, 1);

        std::cout << "{"
                  << "\"engine\":\"cpp17_dnd_finance_core\","
                  << "\"health_score\":" << rep.health_score << ","
                  << "\"health_grade\":\"" << rep.health_grade << "\","
                  << "\"savings_rate_pct\":" << rep.savings_rate_pct << ","
                  << "\"total_income\":" << rep.total_income << ","
                  << "\"total_expense\":" << rep.total_expense << ","
                  << "\"net_balance\":" << rep.net_balance << ","
                  << "\"daily_burn_rate\":" << rep.daily_burn_rate << ","
                  << "\"runway_days\":" << rep.runway_days << ","
                  << "\"forecast_6m_smart\":" << rep.forecast_6m_smart << ","
                  << "\"forecast_12m_smart\":" << rep.forecast_12m_smart
                  << "}\n";
    }
    return 0;
}
