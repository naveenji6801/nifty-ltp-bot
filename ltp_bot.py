import os
import time
import json
import requests
from datetime import datetime
from sahiapi import SahiAPI

KEY_ID = os.environ.get("SAHI_API_KEY", "").strip()
SECRET = os.environ.get("SAHI_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

LOT_SIZE = 65  # Nifty 1 Lot = 65
SL_POINTS = 15
TARGET_POINTS = 30
STATE_FILE = "live_dashboard_data.json"

portfolio = {
    "capital": 100000,
    "active_trade": None,
    "closed_trades": [],
    "total_pnl": 0.0,
    "market_state": {}
}

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print("Telegram Error:", e)

def get_nifty_instrument_id(api):
    """Sahi SDK ke saare possible segments/symbols mein NIFTY Index ID dhundhega"""
    segments = [
        getattr(SahiAPI, attr) for attr in dir(SahiAPI) if attr.startswith("EXCHANGE_SEGMENT")
    ]
    if not segments:
        segments = ["NSE_INDEX", "NSE", "NFO", "NSE_FO"]

    for sym in ["NIFTY", "NIFTY 50", "Nifty 50", "NIFTY50"]:
        for seg in segments:
            try:
                res = api.get_instruments_by_symbol(sym, seg)
                if res and isinstance(res, list) and len(res) > 0:
                    print(f"Found NIFTY ID: {res[0]} in segment {seg}")
                    return res[0]["id"]
            except Exception:
                continue

    # Agar symbol lookup na chale toh scrip master se direct NIFTY search karein
    if hasattr(api, "search_instruments"):
        res = api.search_instruments("NIFTY")
        for item in res:
            if item.get("symbol") in ["NIFTY", "NIFTY 50", "Nifty 50"]:
                return item["id"]

    # Standard NSE Nifty 50 token fallback (26000)
    return 26000

def parse_sahi_option_chain(chain):
    spot_price = round(chain["last_price"] / 100, 2)
    strikes_data = []

    for row in chain.get("strikes", []):
        strike = round(row["strike"] / 100, 2)
        if abs(strike - spot_price) > 600:
            continue

        ce = row.get("ce") or {}
        pe = row.get("pe") or {}

        strikes_data.append({
            "strike": strike,
            "ce_ltp": round((ce.get("last_price") or 0) / 100, 2),
            "ce_oi": ce.get("open_interest") or ce.get("oi") or 0,
            "pe_ltp": round((pe.get("last_price") or 0) / 100, 2),
            "pe_oi": pe.get("open_interest") or pe.get("oi") or 0
        })

    return spot_price, strikes_data

def analyze_ltp_rules(spot_price, option_chain):
    ce_list = sorted(option_chain, key=lambda x: x['ce_oi'], reverse=True)
    pe_list = sorted(option_chain, key=lambda x: x['pe_oi'], reverse=True)

    res_1, res_2 = ce_list[0], ce_list[1]
    sup_1, sup_2 = pe_list[0], pe_list[1]

    ce_pct = round((res_2['ce_oi'] / res_1['ce_oi']) * 100, 1) if res_1['ce_oi'] else 0
    pe_pct = round((sup_2['pe_oi'] / sup_1['pe_oi']) * 100, 1) if sup_1['pe_oi'] else 0

    res_status = "Strong"
    if ce_pct >= 75:
        res_status = "WTT" if res_2['strike'] > res_1['strike'] else "WTB"

    sup_status = "Strong"
    if pe_pct >= 75:
        sup_status = "WTT" if sup_2['strike'] > sup_1['strike'] else "WTB"

    eos = round(sup_1['strike'] - sup_1['pe_ltp'], 2)
    eor = round(res_1['strike'] + res_1['ce_ltp'], 2)

    return {
        "spot": spot_price,
        "support_strike": sup_1['strike'],
        "support_status": f"{sup_status} ({pe_pct}%)",
        "resistance_strike": res_1['strike'],
        "resistance_status": f"{res_status} ({ce_pct}%)",
        "eos": eos,
        "eor": eor,
        "sup_raw": sup_status,
        "res_raw": res_status,
        "atm_ce_ltp": sup_1['ce_ltp'],
        "atm_pe_ltp": res_1['pe_ltp']
    }

def evaluate_trades(state):
    spot = state['spot']
    trade = portfolio['active_trade']

    if trade:
        curr_ltp = state['atm_ce_ltp'] if trade['type'] == "CE" else state['atm_pe_ltp']
        pnl = round((curr_ltp - trade['entry']) * LOT_SIZE, 2)
        trade['current_ltp'] = curr_ltp
        trade['pnl'] = pnl

        if curr_ltp <= trade['sl'] or curr_ltp >= trade['target']:
            reason = "TARGET HIT ✅" if curr_ltp >= trade['target'] else "SL HIT ❌"
            trade['exit_time'] = datetime.now().strftime("%H:%M:%S")
            trade['exit_price'] = curr_ltp
            trade['status'] = reason
            portfolio['total_pnl'] = round(portfolio['total_pnl'] + pnl, 2)
            portfolio['closed_trades'].insert(0, trade)
            portfolio['active_trade'] = None

            send_telegram(
                f"<b>{reason} (Paper Trade)</b>\n"
                f"Strike: {trade['strike']} {trade['type']}\n"
                f"Exit: ₹{curr_ltp} | P&L: ₹{pnl}\n"
                f"Total Day P&L: ₹{portfolio['total_pnl']}"
            )
        return

    if abs(spot - state['eos']) <= 10 and state['sup_raw'] in ["Strong", "WTT"] and state['res_raw'] != "WTB":
        entry_price = state['atm_ce_ltp']
        portfolio['active_trade'] = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "strike": state['support_strike'],
            "type": "CE",
            "qty": LOT_SIZE,
            "entry": entry_price,
            "current_ltp": entry_price,
            "sl": round(entry_price - SL_POINTS, 2),
            "target": round(entry_price + TARGET_POINTS, 2),
            "pnl": 0.0,
            "reason": f"EOS Reversal ({state['eos']}) | Sup: {state['support_status']}"
        }
        send_telegram(
            f"🟢 <b>BUY CALL (Paper Trade - 65 Qty)</b>\n"
            f"Strike: {state['support_strike']} CE @ ₹{entry_price}\n"
            f"SL: ₹{entry_price - SL_POINTS} | Target: ₹{entry_price + TARGET_POINTS}\n"
            f"Logic: {portfolio['active_trade']['reason']}"
        )

    elif abs(spot - state['eor']) <= 10 and state['res_raw'] in ["Strong", "WTB"] and state['sup_raw'] != "WTT":
        entry_price = state['atm_pe_ltp']
        portfolio['active_trade'] = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "strike": state['resistance_strike'],
            "type": "PE",
            "qty": LOT_SIZE,
            "entry": entry_price,
            "current_ltp": entry_price,
            "sl": round(entry_price - SL_POINTS, 2),
            "target": round(entry_price + TARGET_POINTS, 2),
            "pnl": 0.0,
            "reason": f"EOR Reversal ({state['eor']}) | Res: {state['resistance_status']}"
        }
        send_telegram(
            f"🔴 <b>BUY PUT (Paper Trade - 65 Qty)</b>\n"
            f"Strike: {state['resistance_strike']} PE @ ₹{entry_price}\n"
            f"SL: ₹{entry_price - SL_POINTS} | Target: ₹{entry_price + TARGET_POINTS}\n"
            f"Logic: {portfolio['active_trade']['reason']}"
        )

def save_and_push_dashboard(state):
    portfolio['market_state'] = state
    portfolio['last_updated'] = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    with open(STATE_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)
    os.system('git config user.name "LTP-Bot" && git config user.email "bot@users.noreply.github.com"')
    os.system(f'git add {STATE_FILE} && git commit -m "Update live data" && git push')

if __name__ == "__main__":
    api = SahiAPI.from_credentials(key_id=KEY_ID, secret=SECRET)
    nifty_id = get_nifty_instrument_id(api)
    send_telegram(f"🚀 <b>Nifty LTP Bot Connected!</b> (Nifty ID: {nifty_id})")

    while True:
        now = datetime.now().strftime("%H:%M")
        if now >= "15:25":
            send_telegram(f"🔔 <b>Market Closed!</b> Final Day P&L: ₹{portfolio['total_pnl']}")
            break
        try:
            chain = api.get_option_chain(nifty_id)
            spot_price, strikes = parse_sahi_option_chain(chain)
            state = analyze_ltp_rules(spot_price, strikes)
            evaluate_trades(state)
            save_and_push_dashboard(state)
            print("Updated:", state["spot"], "EOS:", state["eos"], "EOR:", state["eor"])
        except Exception as e:
            print("Loop Error:", e)
        time.sleep(20)
