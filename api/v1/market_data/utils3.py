import requests
import pandas as pd
import threading
import json
from datetime import datetime

import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from django.core.cache import cache
from decouple import config


# ================== ANGEL CREDENTIALS ==================

ANGEL_USER_ID = config("ANGEL_USER_ID")
ANGEL_MPIN = config("ANGEL_MPIN")
ANGEL_TOTP_KEY = config("ANGEL_TOTP_KEY")
ANGEL_API_KEY = config("ANGEL_API_KEY")



# ================== GLOBAL STATE ==================
stock_template = {}
ordered_tokens = []


# ================== LOAD NSE EQ SYMBOLS ==================
def load_symbols3():
    """
    Fetch NSE EQ symbols and prepare token map
    """
    global stock_template, ordered_tokens

    stock_template = {}
    ordered_tokens = []

    SCRAP_URL = (
        "https://margincalculator.angelbroking.com/"
        "OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    response = requests.get(SCRAP_URL, timeout=10)
    response.raise_for_status()

    df = pd.DataFrame(response.json())

    df = df[
        (df["exch_seg"] == "NSE") &
        (df["symbol"].str.endswith("-EQ"))
    ][["symbol", "token"]].sort_values("symbol")

    # limit universe (same as your original logic)
    df = df.iloc[2000:]


    for _, r in df.iterrows():
        token = str(r["token"])
        ordered_tokens.append(token)

        stock_template[token] = {
            "symbol": r["symbol"],
            "token": token,
            "ltp": None,
            "prev": None,
            "time": None,
        }

    print("Loaded stocks:", len(ordered_tokens))
    return stock_template, ordered_tokens


# ================== ANGEL WEBSOCKET PRODUCER ==================
def start_angel_ws3():
    """
    Starts Angel One WebSocket and pushes LTPs into Redis
    """

    master = SmartConnect(api_key=ANGEL_API_KEY)
    otp = pyotp.TOTP(ANGEL_TOTP_KEY).now()

    session = master.generateSession(
        ANGEL_USER_ID,
        ANGEL_MPIN,
        otp
    )

    feed_token = session["data"]["feedToken"]

    sws = SmartWebSocketV2(
        feed_token,
        ANGEL_API_KEY,
        ANGEL_USER_ID,
        feed_token
    )

    def on_open(wsapp):
        print("Angel WS connected. Subscribing...")
        sws.subscribe(
            "nse",
            2,
            [{"exchangeType": 1, "tokens": ordered_tokens}]
        )

    def on_data(wsapp, msg):
        token = str(msg.get("token"))
        if token not in stock_template:
            return

        ltp = float(msg["last_traded_price"]) / 100
        closed_price = float(msg.get("closed_price", 0)) / 100
        row = stock_template[token]

        row["prev"] = row["ltp"]
        row["ltp"] = ltp
        row["closed_price"] = closed_price
        row["time"] = datetime.now().strftime("%H:%M:%S")

        payload = {
            "symbol": row["symbol"],
            "token": token,
            "ltp": ltp,
            "prev": row["prev"],
            "closed_price": closed_price,
            "time": row["time"],
        }

        # ✅ YOUR PREFERRED REDIS KEY
        cache_key = f"stock:{token}:data"

        # store latest LTP (short TTL is ideal for ticks)
        cache.set(cache_key, json.dumps(payload),timeout=None)

        # maintain active token set
        cache.client.get_client().sadd("active_tokens", token)

    def on_error(wsapp, error):
        print("Angel WS error:", error)

    def on_close(wsapp):
        print("Angel WS closed")

    sws.on_open = on_open
    sws.on_data = on_data
    sws.on_error = on_error
    sws.on_close = on_close

    threading.Thread(target=sws.connect, daemon=True).start()


# ================== PUBLIC ENTRY POINT ==================
def start_service3():
    """
    Call this from a management command or service runner
    """
    load_symbols3()
    start_angel_ws3()