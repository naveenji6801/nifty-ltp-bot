import os
import time
import json
import requests
from datetime import datetime

SAHI_API_KEY = os.environ.get("SAHI_API_KEY", "")
SAHI_ACCESS_TOKEN = os.environ.get("SAHI_ACCESS_TOKEN", "")
SAHI_BASE_URL = "https://api.sahi.com/v1" # Sahi API docs ke exact URL se replace karein

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LOT_SIZE = 65
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

def fetch_sahi_option_chain():
    headers = {
        "Authorization": f"Bearer {SAHI_ACCESS_TOKEN}",
        "x-api-key": SAHI_API_KEY
    }
    resp = requests.get(f"{SAHI_BASE_URL}/market/option-chain?symbol=NIFTY", headers=headers, timeout=10)
    return resp.json()

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
    send_telegram("🚀 <b>Nifty LTP Calculator Paper Bot Started on Cloud!</b>")
    while True:
        now = datetime.now().strftime("%H:%M")
        if now >= "15:25":
            send_telegram(f"🔔 <b>Market Closed!</b> Final Day P&L: ₹{portfolio['total_pnl']}")
            break
        try:
            raw_data = fetch_sahi_option_chain()
            state = analyze_ltp_rules(raw_data['spot_price'], raw_data['strikes'])
            evaluate_trades(state)
            save_and_push_dashboard(state)
        except Exception as e:
            print("Loop Error:", e)
        time.sleep(20)
