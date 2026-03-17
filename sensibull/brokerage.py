"""
Brokerage Calculator for Options Trades

Calculates all charges for a single options order (entry or exit):
  - Brokerage:            Flat ₹20 per executed order
  - STT:                  0.1% of premium on SELL side only
  - Transaction Charges:  0.03503% (NSE) or 0.0325% (BSE) on premium
  - SEBI Charges:         ₹10 per crore = 0.000001% of premium
  - Stamp Charges:        0.003% of premium on BUY side only
  - GST:                  18% on (brokerage + SEBI charges + transaction charges)

Usage:
    from brokerage import calculate_option_brokerage, get_exchange_for_underlying

    brokerage = calculate_option_brokerage(
        premium=85.0,
        lot_size=75,        # total quantity (lots * lot_size)
        transaction_type="SELL",
        exchange="NSE"
    )
"""

from typing import Dict


# ---------------------------------------------------------------------------
# Configuration — mirrors the JavaScript BROKERAGE_CONFIG exactly
# ---------------------------------------------------------------------------

BROKERAGE_CONFIG: Dict = {
    # Flat brokerage per executed order (entry OR exit)
    "OPTIONS_BROKERAGE": 20.0,

    # STT (Securities Transaction Tax)
    "STT": {
        "SELL": 0.001,         # 0.1% on premium for option sells
        "BUY_EXERCISED": 0.00125,  # 0.125% of intrinsic value for exercised options (not used here)
        "EQUITY": 0.001,
    },

    # Transaction charges on premium
    "TRANSACTION_CHARGES": {
        "NSE": 0.0003503,      # 0.03503% for NSE options
        "BSE": 0.000325,       # 0.0325% for BSE options
        "EQUITY_NSE": 0.0000297,
    },

    # GST on (brokerage + SEBI charges + transaction charges)
    "GST_RATE": 0.18,

    # SEBI charges: ₹10 per crore = 0.000001 of turnover
    "SEBI_CHARGES": 0.000001,

    # Stamp charges on BUY side only
    "STAMP_CHARGES": {
        "OPTIONS": 0.00003,    # 0.003% on premium for options buy
        "EQUITY": 0.00015,
    },
}


# ---------------------------------------------------------------------------
# Exchange detection helper
# ---------------------------------------------------------------------------

def get_exchange_for_underlying(underlying: str) -> str:
    """
    Return the transaction-charge exchange key for a given underlying.

    SENSEX options trade on BSE (BFO), everything else (NIFTY, BANKNIFTY,
    FINNIFTY, MIDCPNIFTY, etc.) trades on NSE (NFO).

    Args:
        underlying: Underlying symbol string, e.g. "NIFTY", "SENSEX".

    Returns:
        "BSE" for SENSEX, "NSE" for all others.
    """
    if underlying and underlying.upper() in ("SENSEX", "BANKEX"):
        return "BSE"
    return "NSE"


# ---------------------------------------------------------------------------
# Core brokerage calculator
# ---------------------------------------------------------------------------

def calculate_option_brokerage(
    premium: float,
    lot_size: int,
    transaction_type: str,
    exchange: str = "NSE",
) -> float:
    """
    Calculate total brokerage charges for a single options order.

    This mirrors the JavaScript ``calculateOptionBrokerage`` function exactly.
    Both entry and exit orders are assumed to be executed (filled), so the
    flat brokerage of ₹20 is always included.

    Args:
        premium:          Premium per share (LTP at the time of the order).
        lot_size:         Total number of shares (lots × lot_size).
        transaction_type: "BUY" or "SELL".
        exchange:         "NSE" (default) or "BSE".

    Returns:
        Total brokerage charge (₹) rounded to 2 decimal places.
    """
    return calculate_option_brokerage_detailed(
        premium=premium,
        lot_size=lot_size,
        transaction_type=transaction_type,
        exchange=exchange,
    )["total"]


def calculate_option_brokerage_detailed(
    premium: float,
    lot_size: int,
    transaction_type: str,
    exchange: str = "NSE",
) -> Dict[str, float]:
    """
    Same as ``calculate_option_brokerage`` but returns a breakdown dict.

    Useful for logging and debugging.

    Returns:
        Dict with keys: brokerage, stt, transaction_charges, sebi_charges,
        stamp_charges, gst, total.
    """
    cfg = BROKERAGE_CONFIG
    transaction_type = transaction_type.upper()
    exchange_key = exchange.upper() if exchange.upper() in ("NSE", "BSE") else "NSE"

    total_premium = premium * lot_size

    brokerage = cfg["OPTIONS_BROKERAGE"]
    transaction_charges = total_premium * cfg["TRANSACTION_CHARGES"][exchange_key]
    sebi_charges = total_premium * cfg["SEBI_CHARGES"]

    stt = 0.0
    if transaction_type == "SELL":
        stt = total_premium * cfg["STT"]["SELL"]

    stamp_charges = 0.0
    if transaction_type == "BUY":
        stamp_charges = total_premium * cfg["STAMP_CHARGES"]["OPTIONS"]

    gst_base = brokerage + sebi_charges + transaction_charges
    gst = gst_base * cfg["GST_RATE"]

    total = brokerage + stt + transaction_charges + sebi_charges + stamp_charges + gst

    return {
        "brokerage": round(brokerage, 4),
        "stt": round(stt, 4),
        "transaction_charges": round(transaction_charges, 4),
        "sebi_charges": round(sebi_charges, 4),
        "stamp_charges": round(stamp_charges, 4),
        "gst": round(gst, 4),
        "total": round(total, 2),
    }
