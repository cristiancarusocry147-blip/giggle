import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, jsonify

# ---------------- CONFIG ----------------
CONFIG_FILE = "config.json"
POLL_INTERVAL = 3
DATA = {}
HISTORY = {}  # nuovo: memorizza storico spread
TASKS = {}
LOCK = asyncio.Lock()

# ---------------- LOGGING ----------------
os.makedirs("logs", exist_ok=True)
log_filename = datetime.now().strftime("logs/%Y-%m-%d.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_filename, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)

# ---------------- LOAD CONFIG ----------------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        example = {
            "TELEGRAM_TOKEN": "INSERISCI_IL_TUO_TOKEN",
            "CHAT_ID": "INSERISCI_IL_TUO_CHAT_ID",
            "SPREAD_THRESHOLD": 1.0,
            "PAIRS": ["GIGGLE/USDT"],
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(example, f, indent=4)
        print("Creato config.json — inserisci i tuoi valori e riavvia.")
        sys.exit(1)
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

CONFIG = load_config()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", CONFIG.get("TELEGRAM_TOKEN"))
CHAT_ID = os.getenv("CHAT_ID", CONFIG.get("CHAT_ID"))
SPREAD_THRESHOLD = float(CONFIG.get("SPREAD_THRESHOLD", 1.0))
PAIRS = CONFIG.get("PAIRS", ["GIGGLE/USDT"])

# ---------------- TELEGRAM ----------------
async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data=payload)
    except Exception as e:
        logging.error(f"Errore Telegram: {e}")

# ---------------- FETCH PRICES ----------------
async def fetch_mexc_price(symbol):
    try:
        mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        ticker = await mexc.fetch_ticker(symbol)
        await mexc.close()
        return ticker.get("last")
    except Exception as e:
        logging.warning(f"Errore MEXC {symbol}: {e}")
        return None

async def fetch_quanto_price(symbol):
    try:
        base = symbol.split("/")[0]
        url = f"https://api.quanto.trade/v3/depth?marketCode={base}-USD-SWAP-LIN&level=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get("success") and "data" in data:
                    bids = data["data"].get("bids", [])
                    asks = data["data"].get("asks", [])
                    if bids and asks:
                        bid = float(bids[0][0])
                        ask = float(asks[0][0])
                        return (bid + ask) / 2
    except Exception as e:
        logging.error(f"Errore Quanto.Trade {symbol}: {e}")
    return None

# ---------------- MONITOR ----------------
async def monitor_pair(symbol):
    last_spread = 0.0
    DATA[symbol] = {"mexc": 0, "quanto": 0, "spread": 0}
    HISTORY[symbol] = []

    while True:
        try:
            mexc_price = await fetch_mexc_price(symbol)
            quanto_price = await fetch_quanto_price(symbol)

            if mexc_price and quanto_price:
                spread = (quanto_price - mexc_price) / mexc_price * 100
                DATA[symbol] = {"mexc": mexc_price, "quanto": quanto_price, "spread": spread}

                # aggiorna storico
                HISTORY[symbol].append({"time": datetime.now().strftime("%H:%M:%S"), "spread": spread})
                if len(HISTORY[symbol]) > 50:
                    HISTORY[symbol].pop(0)

                if abs(spread) >= SPREAD_THRESHOLD and abs(spread) > abs(last_spread):
                    direction = "🟢" if spread > 0 else "🔴"
                    msg = (
                        f"{direction} {symbol} Arbitrage Alert\n"
                        f"Spread: {spread:.2f}%\n"
                        f"MEXC: {mexc_price:.5f}\n"
                        f"Quanto: {quanto_price:.5f}\n"
                        f"Trade: {'Compra su MEXC / Vendi su Quanto' if spread>0 else 'Vendi su MEXC / Compra su Quanto'}"
                    )
                    asyncio.create_task(send_telegram_message(msg))

                last_spread = spread
        except Exception as e:
            logging.error(f"Errore loop {symbol}: {e}\n{traceback.format_exc()}")

        await asyncio.sleep(POLL_INTERVAL)

# ---------------- FLASK DASHBOARD ----------------
app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>💹 Arbitrage Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial; background: #111; color: #ddd; text-align: center; }
        table { margin: 20px auto; border-collapse: collapse; width: 80%; }
        th, td { border: 1px solid #333; padding: 8px; }
        th { background: #222; }
        .green { color: #00ff7f; }
        .red { color: #ff5050; }
        form { margin: 20px; }
        canvas { max-width: 600px; margin: 20px auto; display: block; }
    </style>
</head>
<body>
<h1>💹 MEXC ↔ Quanto.Trade Arbitrage</h1>
<table>
<tr><th>Coppia</th><th>MEXC</th><th>Quanto</th><th>Spread (%)</th><th>Grafico</th><th>Rimuovi</th></tr>
{% for pair, d in data.items() %}
<tr>
<td>{{ pair }}</td>
<td>{{ "%.5f"|format(d.mexc) }}</td>
<td>{{ "%.5f"|format(d.quanto) }}</td>
<td class="{{ 'green' if d.spread>0 else 'red' }}">{{ "%.2f"|format(d.spread) }}</td>
<td><canvas id="chart_{{ loop.index }}"></canvas></td>
<td><a href="/remove?pair={{pair}}">❌</a></td>
</tr>
{% endfor %}
</table>

<form action="/add" method="post">
<input name="pair" placeholder="es. BTC/USDT" required>
<button type="submit">➕ Aggiungi Coppia</button>
</form>

<script>
const pairs = {{ pairs|tojson }};
function updateCharts() {
  fetch("/data").then(r => r.json()).then(res => {
    pairs.forEach((p, i) => {
      const ctx = document.getElementById("chart_" + (i+1));
      if (!ctx) return;
      const d = res.history[p] || [];
      const labels = d.map(e => e.time);
      const data = d.map(e => e.spread);
      if (!ctx.chart) {
        ctx.chart = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{ label: p + " Spread %", data, borderColor: "#00ff7f", tension: 0.3 }] },
          options: { scales: { x: { ticks: { color: "#aaa" } }, y: { ticks: { color: "#aaa" } } } }
        });
      } else {
        ctx.chart.data.labels = labels;
        ctx.chart.data.datasets[0].data = data;
        ctx.chart.update();
      }
    });
  });
}
setInterval(updateCharts, 4000);
updateCharts();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML, data=DATA, pairs=PAIRS)

@app.route("/data")
def data():
    return jsonify({"data": DATA, "history": HISTORY})

@app.route("/add", methods=["POST"])
def add_pair():
    pair = request.form.get("pair").upper().strip()
    if pair and pair not in PAIRS:
        PAIRS.append(pair)
        CONFIG["PAIRS"] = PAIRS
        save_config(CONFIG)
        asyncio.run_coroutine_threadsafe(start_pair(pair), asyncio.get_event_loop())
    return redirect(url_for("index"))

@app.route("/remove")
def remove_pair():
    pair = request.args.get("pair")
    if pair in PAIRS:
        PAIRS.remove(pair)
        CONFIG["PAIRS"] = PAIRS
        save_config(CONFIG)
        if pair in TASKS:
            TASKS[pair].cancel()
            TASKS.pop(pair, None)
            DATA.pop(pair, None)
            HISTORY.pop(pair, None)
    return redirect(url_for("index"))

# ---------------- CONTROLLO ----------------
async def start_pair(pair):
    if pair not in TASKS:
        TASKS[pair] = asyncio.create_task(monitor_pair(pair))
        await asyncio.sleep(0.1)

async def main():
    await send_telegram_message("🤖 Dashboard con grafici avviata!")
    for p in PAIRS:
        await start_pair(p)
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False), daemon=True).start()

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
