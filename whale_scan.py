"""
Whale Watcher v2 - runs on GitHub Actions every ~5 minutes.
1. Meme coins: whale-like trading on DexScreener (free, no key)
2. Wider market: big 1-hour moves in the top 100 coins + new trending coins (CoinGecko, free key)
3. Whale wallets: trades by Solana wallets you choose (Helius, free key)
Telegram commands: /add WALLET name, /remove WALLET-or-name, /list, /help
"""

import html
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HELIUS_KEY = os.environ.get("HELIUS_API_KEY", "").strip()
CG_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

# ---------- Settings you can tune ----------
CHAINS = {"solana", "ethereum", "base", "bsc"}
MIN_VOLUME_5M = 50_000        # meme coins: USD traded in last 5 min
MIN_AVG_TRADE = 2_000         # meme coins: average trade size (whale proxy)
MIN_LIQUIDITY = 20_000        # meme coins: ignore tiny pools
DEX_COOLDOWN = 3600           # don't repeat the same meme coin within 1 hour

BIG_MOVE_1H = 5.0             # top-100 coins: alert on a move of 5%+ in 1 hour
CG_COOLDOWN = 3 * 3600        # don't repeat the same big coin within 3 hours
CG_EVERY = 15 * 60            # check CoinGecko every 15 min (keeps within free limit)

WALLET_EVERY = 10 * 60        # check whale wallets every 10 min
MIN_WHALE_SOL = 10            # only report wallet trades of 10+ SOL...
MIN_WHALE_USD = 2_000         # ...or $2,000+ in USDC/USDT
HELIUS_MONTHLY_BUDGET = 900_000  # stop before the free 1M monthly credits run out
# --------------------------------------------

STATE_FILE = "state.json"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
NOW = int(time.time())


# ---------- helpers ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    defaults = {"tg_offset": 0, "wallets": {}, "dex_alerted": {}, "cg_alerted": {},
                "trending": [], "last_cg": 0, "last_wallets": 0,
                "helius_month": "", "helius_credits": 0, "helius_warned": False}
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def esc(s):
    return html.escape(str(s or ""))


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}"


def fmt_price(p):
    p = float(p or 0)
    return f"{p:,.2f}" if p >= 1 else f"{p:.10f}".rstrip("0")


def tg(method, **params):
    try:
        return requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                             json=params, timeout=20).json()
    except Exception as e:
        print("Telegram error:", e)
        return {}


def send(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML",
       disable_web_page_preview=True)


# ---------- Telegram commands ----------
HELP = ("🐋 <b>Whale Watcher</b>\n"
        "/add WALLET name – watch a Solana whale wallet\n"
        "/remove WALLET-or-name – stop watching\n"
        "/list – show watched wallets\n"
        "Replies can take up to 5–10 minutes.")


def handle_commands(state):
    r = tg("getUpdates", offset=state["tg_offset"] + 1, timeout=0)
    wallets = state["wallets"]
    for u in r.get("result", []):
        state["tg_offset"] = u["update_id"]
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        parts = (msg.get("text") or "").split()
        if not parts:
            continue
        cmd = parts[0].lower().split("@")[0]
        if cmd == "/add":
            if len(parts) < 2 or not BASE58.match(parts[1]):
                send("To add a wallet, send:\n/add WALLET_ADDRESS name")
                continue
            addr = parts[1]
            name = " ".join(parts[2:]) or short(addr)
            wallets[addr] = {"name": name, "last_sig": None}
            note = "" if HELIUS_KEY else "\n⚠️ Helius key not added yet, so tracking hasn't started."
            send(f"✅ Watching <b>{esc(name)}</b> ({short(addr)}). "
                 f"Alerts start from its next big trade.{note}")
        elif cmd == "/remove":
            target = " ".join(parts[1:]).lower()
            match = [a for a, w in wallets.items()
                     if a.lower() == target or w["name"].lower() == target]
            for a in match:
                wallets.pop(a)
            send("🗑 Removed." if match else "Couldn't find that wallet. Send /list to check.")
        elif cmd == "/list":
            if wallets:
                lines = [f"• <b>{esc(w['name'])}</b> – <code>{a}</code>" for a, w in wallets.items()]
                send("Watching:\n" + "\n".join(lines))
            else:
                send("No wallets yet. Send /add WALLET_ADDRESS name")
        elif cmd in ("/start", "/help"):
            send(HELP)


# ---------- 1. Meme coins (DexScreener) ----------
def scan_dexscreener(state):
    alerted = {k: t for k, t in state["dex_alerted"].items() if NOW - t < DEX_COOLDOWN}
    state["dex_alerted"] = alerted
    tokens = set()
    for url in ["https://api.dexscreener.com/token-boosts/latest/v1",
                "https://api.dexscreener.com/token-boosts/top/v1"]:
        try:
            for t in requests.get(url, timeout=15).json():
                if t.get("chainId") in CHAINS:
                    tokens.add(t["tokenAddress"])
        except Exception:
            pass
    tokens = list(tokens)
    for i in range(0, len(tokens), 30):
        try:
            pairs = requests.get("https://api.dexscreener.com/latest/dex/tokens/" +
                                 ",".join(tokens[i:i + 30]), timeout=15).json().get("pairs") or []
        except Exception:
            continue
        for p in pairs:
            vol5 = (p.get("volume") or {}).get("m5", 0) or 0
            tx = (p.get("txns") or {}).get("m5", {}) or {}
            buys, sells = tx.get("buys", 0), tx.get("sells", 0)
            liq = (p.get("liquidity") or {}).get("usd", 0) or 0
            trades = buys + sells
            if trades == 0 or vol5 < MIN_VOLUME_5M or liq < MIN_LIQUIDITY:
                continue
            avg = vol5 / trades
            key = p.get("pairAddress")
            if avg < MIN_AVG_TRADE or key in alerted:
                continue
            alerted[key] = NOW
            side = ("🟢 BUY pressure" if buys > sells * 1.5 else
                    "🔴 SELL pressure" if sells > buys * 1.5 else "⚪ Mixed")
            send(f"🐋 <b>{esc(p['baseToken']['symbol'])}</b> on {p['chainId']}\n"
                 f"5-min volume: ${vol5:,.0f} ({trades} trades)\n"
                 f"Avg trade: ${avg:,.0f}\n"
                 f"{side} — {buys} buys / {sells} sells\n"
                 f"Price 5m: {(p.get('priceChange') or {}).get('m5', 0)}%  |  "
                 f"Liquidity: ${liq:,.0f}\n{p.get('url', '')}")


# ---------- 2. Wider market (CoinGecko) ----------
def cg_get(path, **params):
    r = requests.get("https://api.coingecko.com/api/v3" + path, params=params,
                     headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
    r.raise_for_status()
    return r.json()


def scan_coingecko(state):
    if not CG_KEY or NOW - state["last_cg"] < CG_EVERY - 60:
        return
    state["last_cg"] = NOW
    alerted = {k: t for k, t in state["cg_alerted"].items() if NOW - t < CG_COOLDOWN}
    state["cg_alerted"] = alerted

    try:
        coins = cg_get("/coins/markets", vs_currency="usd", order="market_cap_desc",
                       per_page=100, page=1, price_change_percentage="1h,24h")
    except Exception as e:
        print("CoinGecko markets error:", e)
        coins = []
    for c in coins:
        ch = c.get("price_change_percentage_1h_in_currency")
        if ch is None or abs(ch) < BIG_MOVE_1H or c["id"] in alerted:
            continue
        alerted[c["id"]] = NOW
        d24 = c.get("price_change_percentage_24h_in_currency") or 0
        send(f"{'🚀' if ch > 0 else '📉'} <b>{esc(c['name'])}</b> "
             f"({esc(c['symbol']).upper()}) {ch:+.1f}% in 1 hour\n"
             f"Price: ${fmt_price(c.get('current_price'))}\n"
             f"24h: {d24:+.1f}%  |  Rank #{c.get('market_cap_rank')}\n"
             f"https://www.coingecko.com/en/coins/{c['id']}")

    try:
        items = [x["item"] for x in cg_get("/search/trending").get("coins", [])]
    except Exception as e:
        print("CoinGecko trending error:", e)
        return
    old = set(state["trending"])
    new = [i for i in items if i["id"] not in old]
    if old and new:
        lines = [f"• <b>{esc(i['name'])}</b> ({esc(i['symbol']).upper()})"
                 f"{' – rank #' + str(i['market_cap_rank']) if i.get('market_cap_rank') else ''}"
                 for i in new]
        send("🔥 <b>New on CoinGecko trending</b>\n" + "\n".join(lines))
    if items:
        state["trending"] = [i["id"] for i in items]


# ---------- 3. Whale wallets (Helius) ----------
def use_credits(state, n):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state["helius_month"] != month:
        state.update(helius_month=month, helius_credits=0, helius_warned=False)
    state["helius_credits"] += n


def helius_rpc(method, params):
    try:
        r = requests.post(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
                          json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          timeout=20).json()
        if "error" in r:
            print("Helius RPC error:", r["error"])
        return r.get("result")
    except Exception as e:
        print("Helius RPC failed:", e)
        return None


def helius_parse(sigs):
    for host in ("https://api-mainnet.helius-rpc.com", "https://api.helius.xyz"):
        try:
            r = requests.post(f"{host}/v0/transactions/?api-key={HELIUS_KEY}",
                              json={"transactions": sigs}, timeout=30)
            if r.ok:
                return r.json()
            print("Helius parse error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Helius parse failed:", e)
    return []


def describe_trade(addr, tx):
    n_in = n_out = w_in = w_out = usd = 0.0
    got, sent = [], []
    for n in tx.get("nativeTransfers") or []:
        amt = abs(n.get("amount") or 0) / 1e9
        if n.get("fromUserAccount") == addr:
            n_out += amt
        if n.get("toUserAccount") == addr:
            n_in += amt
    for t in tx.get("tokenTransfers") or []:
        to_me, from_me = t.get("toUserAccount") == addr, t.get("fromUserAccount") == addr
        if not (to_me or from_me):
            continue
        mint, amt = t.get("mint"), float(t.get("tokenAmount") or 0)
        if mint == WSOL:
            w_in += amt if to_me else 0
            w_out += amt if from_me else 0
        elif mint in STABLES:
            usd += amt
        else:
            (got if to_me else sent).append(mint)
    sol = max(n_in, n_out, w_in, w_out)
    return sol, usd, got, sent


def scan_wallets(state):
    if not HELIUS_KEY or not state["wallets"] or NOW - state["last_wallets"] < WALLET_EVERY - 60:
        return
    use_credits(state, 0)
    if state["helius_credits"] >= HELIUS_MONTHLY_BUDGET:
        if not state["helius_warned"]:
            send("⚠️ This month's free Helius credits are nearly used up. "
                 "Wallet tracking is paused until next month; other alerts continue.")
            state["helius_warned"] = True
        return
    state["last_wallets"] = NOW
    for addr, w in state["wallets"].items():
        first_time = not w.get("last_sig")
        opts = {"limit": 1} if first_time else {"limit": 20, "until": w["last_sig"]}
        res = helius_rpc("getSignaturesForAddress", [addr, opts])
        use_credits(state, 10)
        if not res:
            continue
        w["last_sig"] = res[0]["signature"]
        sigs = [s["signature"] for s in res if not s.get("err")]
        if first_time or not sigs:
            continue
        txs = helius_parse(sigs)
        use_credits(state, 100)
        for tx in reversed(txs):
            sol, usd, got, sent = describe_trade(addr, tx)
            if sol < MIN_WHALE_SOL and usd < MIN_WHALE_USD:
                continue
            action = ("🟢 BOUGHT" if got and not sent else
                      "🔴 SOLD" if sent and not got else "🔄 Moved funds")
            token = (got or sent or [None])[0]
            size = f"~{sol:,.1f} SOL" if sol >= usd / 150 else f"~${usd:,.0f}"
            desc = (tx.get("description") or tx.get("type") or "")[:300]
            chart = f"\nChart: https://dexscreener.com/solana/{token}" if token else ""
            send(f"🐋 <b>{esc(w['name'])}</b> {action}\n{esc(desc)}\n"
                 f"Size: {size}{chart}\n"
                 f"Tx: https://solscan.io/tx/{tx.get('signature')}")


if __name__ == "__main__":
    state = load_state()
    for step in (handle_commands, scan_dexscreener, scan_coingecko, scan_wallets):
        try:
            step(state)
        except Exception as e:
            print(step.__name__, "failed:", e)
    save_state(state)
