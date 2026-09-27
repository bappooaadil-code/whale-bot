import os
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CHAINS = {"solana", "ethereum", "base", "bsc"}
MIN_VOLUME_5M = 50_000
MIN_AVG_TRADE = 2_000
MIN_LIQUIDITY = 20_000


def send(text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)


def get_candidate_tokens():
    tokens = set()
    for url in ["https://api.dexscreener.com/token-boosts/latest/v1",
                "https://api.dexscreener.com/token-boosts/top/v1"]:
        try:
            for t in requests.get(url, timeout=15).json():
                if t.get("chainId") in CHAINS:
                    tokens.add(t["tokenAddress"])
        except Exception:
            pass
    return list(tokens)


def check_pair(p):
    vol5 = (p.get("volume") or {}).get("m5", 0) or 0
    tx = (p.get("txns") or {}).get("m5", {}) or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    trades = buys + sells
    if trades == 0 or vol5 < MIN_VOLUME_5M or liq < MIN_LIQUIDITY:
        return
    avg = vol5 / trades
    if avg < MIN_AVG_TRADE:
        return
    sym = p["baseToken"]["symbol"]
    side = "🟢 BUY pressure" if buys > sells * 1.5 else "🔴 SELL pressure" if sells > buys * 1.5 else "⚪ Mixed"
    send(f"🐋 <b>{sym}</b> on {p['chainId']}\n"
         f"5-min volume: ${vol5:,.0f} ({trades} trades)\n"
         f"Avg trade: ${avg:,.0f}\n"
         f"{side} — {buys} buys / {sells} sells\n"
         f"Price 5m: {(p.get('priceChange') or {}).get('m5', 0)}%  |  Liquidity: ${liq:,.0f}\n"
         f"{p.get('url', '')}")


if __name__ == "__main__":
    tokens = get_candidate_tokens()
    for i in range(0, len(tokens), 30):
        batch = ",".join(tokens[i:i + 30])
        try:
            pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{batch}",
                                 timeout=15).json().get("pairs") or []
        except Exception:
            continue
        for p in pairs:
            check_pair(p)
