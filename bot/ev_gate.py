# ============================================================
# bot/ev_gate.py — Cost-aware Expected Value gate
#
# PROD-READY P1 (2026-07-10)
#
# Every trade candidate passes through compute_net_ev() before
# entry. EV is computed using after-charge reward and risk so
# the bot never enters a trade where charges consume the edge.
#
# Integration:
#   live_bot._scan_and_enter():
#     ev = compute_net_ev(prob_up, entry, sl, target, qty)
#     if not ev_passes(ev):
#         continue
# ============================================================

from __future__ import annotations
import logging

from bot.trade_policy import MIN_NET_EV_INR, REQUIRE_POSITIVE_NET_EV

log = logging.getLogger("ev_gate")


def compute_net_ev(
    prob_up:  float,
    entry:    float,
    sl:       float,
    target:   float,
    qty:      int,
    buy_charge_pct:  float = 0.0015,  # Dhan intraday approx: ETC + brokerage + GST
    sell_charge_pct: float = 0.0020,  # adds STT on sell side
) -> float:
    """
    Compute expected net P&L for a proposed trade.

    Formula:
        buy_cost   = entry * qty * buy_charge_pct
        sell_win   = target * qty * sell_charge_pct
        sell_loss  = sl     * qty * sell_charge_pct

        reward_net = (target - entry) * qty - buy_cost - sell_win
        risk_net   = (entry  - sl)    * qty + buy_cost + sell_loss

        EV = prob_up * reward_net - (1 - prob_up) * risk_net

    Returns EV in INR. Positive means edge after all charges.

    NOTE: These charge percentages are conservative estimates.
    Use brokerage.calculate_charges() for exact values when qty
    is known — this function is for fast pre-sizing filtering.
    """
    if qty <= 0 or entry <= 0 or sl >= entry or target <= entry:
        return -9999.0

    buy_cost  = entry  * qty * buy_charge_pct
    sell_win  = target * qty * sell_charge_pct
    sell_loss = sl     * qty * sell_charge_pct

    reward_net = (target - entry) * qty - buy_cost - sell_win
    risk_net   = (entry  - sl)    * qty + buy_cost + sell_loss

    ev = prob_up * reward_net - (1.0 - prob_up) * risk_net
    return round(ev, 2)


def ev_passes(ev: float, symbol: str = "") -> bool:
    """
    Returns True if the trade should be allowed to proceed.

    If REQUIRE_POSITIVE_NET_EV is False, always returns True
    (useful for backtesting to avoid gate interference).
    """
    if not REQUIRE_POSITIVE_NET_EV:
        return True
    passes = ev >= MIN_NET_EV_INR
    if not passes:
        log.info(
            "[EVGate] %s BLOCKED: net_ev=₹%.2f < threshold=₹%.2f",
            symbol, ev, MIN_NET_EV_INR,
        )
    return passes
