from django.core.management.base import BaseCommand
from django.core.cache import cache

from kiteconnect import KiteTicker, KiteConnect

import os
import sys
from dotenv import load_dotenv

# 🔐 SELENIUM
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import time
import pyotp, json
from urllib.parse import urlparse, parse_qs
import threading

from api.v1.market_data.redis_client import queue_redis


# ======================
# 🔐 ACCESS TOKEN GENERATOR
# ======================
def get_access_token(api_key):
    API_SECRET = os.getenv("KITE_API_SECRET")
    USER_ID = os.getenv("KITE_USER_ID")
    PASSWORD = os.getenv("KITE_PASSWORD")
    TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    # ======================
    # 🔥 CHROME OPTIONS
    # ======================
    options = webdriver.ChromeOptions()

    # ======================
    # 🖥️ OS-SPECIFIC CONFIG
    # ======================

    if sys.platform.startswith("linux"):
        # Linux / AWS
        options.binary_location = "/usr/bin/chromium-browser"

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver_service = Service("/usr/bin/chromedriver")

    elif sys.platform.startswith("win"):
        # Windows
        # Selenium Manager automatically finds ChromeDriver
        driver_service = None

    else:
        # macOS / other
        driver_service = None

    # ======================
    # 🔥 COMMON CHROME OPTIONS
    # ======================

    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # 🔥 Anti detection
    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )

    options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation"]
    )

    options.add_experimental_option(
        "useAutomationExtension",
        False
    )

    # ======================
    # 🔥 CREATE DRIVER
    # ======================

    if driver_service:
        driver = webdriver.Chrome(
            service=driver_service,
            options=options
        )
    else:
        driver = webdriver.Chrome(
            options=options
        )

    wait = WebDriverWait(driver, 30)

    try:
        driver.get(login_url)

        # ======================
        # 🔐 LOGIN
        # ======================
        userid = wait.until(
            EC.element_to_be_clickable(
                (By.ID, "userid")
            )
        )

        userid.click()
        userid.send_keys(USER_ID)

        password = wait.until(
            EC.element_to_be_clickable(
                (By.ID, "password")
            )
        )

        password.click()
        password.send_keys(PASSWORD)
        password.send_keys(Keys.RETURN)

        time.sleep(2)

        # ======================
        # 🔐 OTP
        # ======================
        otp = pyotp.TOTP(TOTP_SECRET).now()

        print("🔢 OTP:", otp)

        # 🔥 FIXED OTP SELECTOR
        otp_field = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//input")
            )
        )

        otp_field.send_keys(otp)

        # ======================
        # 🔥 WAIT FOR TOKEN
        # ======================
        wait.until(
            lambda d: "request_token" in d.current_url
        )

        current_url = driver.current_url

        parsed = urlparse(current_url)

        request_token = parse_qs(
            parsed.query
        ).get(
            "request_token",
            [None]
        )[0]

        if not request_token:
            raise Exception(
                "❌ request_token not found"
            )

    except Exception as e:
        driver.save_screenshot("error.png")

        print(
            "❌ Selenium Error:",
            e
        )

        raise e

    finally:
        driver.quit()

    # ======================
    # 🔥 GENERATE ACCESS TOKEN
    # ======================
    kite = KiteConnect(
        api_key=api_key
    )

    data = kite.generate_session(
        request_token,
        api_secret=API_SECRET
    )

    return data["access_token"]


# ======================
# COMMAND
# ======================
class Command(BaseCommand):
    help = "Run Kite WebSocket"

    def handle(self, *args, **kwargs):
        load_dotenv()

        API_KEY = os.getenv("KITE_API_KEY")

        if not API_KEY:
            print("❌ Missing API_KEY")
            return

        print("🔐 Generating access token...")

        try:
            ACCESS_TOKEN = get_access_token(
                API_KEY
            )

            print("✅ Token generated")

        except Exception as e:
            print(
                "❌ Token generation failed:",
                e
            )

            return

        self.ws_connected = False
        self.subscribed_tokens = set()

        kite = KiteConnect(
            api_key=API_KEY
        )

        kite.set_access_token(
            ACCESS_TOKEN
        )

        print("📥 Loading instruments...")

        self.instruments = (
            kite.instruments("NFO")
            + kite.instruments("BFO")
        )

        print(
            "✅ Instruments loaded:",
            len(self.instruments)
        )

        self.token_map = {}
        self.reverse_token_map = {}

        for ins in self.instruments:
            inst = str(
                ins["instrument_token"]
            )

            exch = str(
                int(ins["exchange_token"])
            )

            self.token_map[exch] = inst
            self.reverse_token_map[inst] = exch

        print(
            "✅ Token map ready:",
            len(self.token_map)
        )

        self.kws = KiteTicker(
            API_KEY,
            ACCESS_TOKEN
        )

        self.kws.on_connect = self.on_connect
        self.kws.on_ticks = self.on_ticks
        self.kws.on_error = self.on_error
        self.kws.on_close = self.on_close

        print(
            "🚀 Starting Kite WebSocket..."
        )

        self.kws.connect()

    # ======================
    def on_connect(self, ws, response):
        print(
            "✅ Connected to Kite WebSocket"
        )

        self.ws_connected = True

        # 🔥 RESTORE TOKENS FROM token_usage:* AFTER RESTART
        print(
            "🔄 Restoring tokens from Redis token_usage..."
        )

        for key in queue_redis.scan_iter(
            "token_usage:*"
        ):
            key_str = (
                key.decode()
                if isinstance(key, bytes)
                else key
            )

            token = key_str.split(":")[-1]

            if queue_redis.scard(key) > 0:
                print(
                    f"🔁 Restoring subscription → {token}"
                )

                self.subscribe_by_token(token)

        print(
            "✅ Restore complete"
        )

        threading.Thread(
            target=self.listen_subscriptions,
            daemon=True
        ).start()

        threading.Thread(
            target=self.listen_unsubscribe,
            daemon=True
        ).start()

    # ======================
    def listen_subscriptions(self):
        print(
            "📡 Listening for subscriptions..."
        )

        while True:
            token = queue_redis.rpop(
                "kite_subscribe_queue"
            )

            if token:
                token = (
                    token.decode()
                    if isinstance(token, bytes)
                    else token
                )

                print(
                    f"📥 NEW SUB REQUEST → {token}"
                )

                self.subscribe_by_token(token)

            time.sleep(0.1)

    # ======================
    def listen_unsubscribe(self):
        print(
            "📡 Listening for unsubscriptions..."
        )

        while True:
            token = queue_redis.rpop(
                "kite_unsubscribe_queue"
            )

            if token:
                token = (
                    token.decode()
                    if isinstance(token, bytes)
                    else token
                )

                print(
                    f"📤 NEW UNSUB REQUEST → {token}"
                )

                key = f"token_usage:{token}"

                if queue_redis.scard(key) > 0:
                    print(
                        f"⏳ Still in use → skip: {token}"
                    )

                else:
                    print(
                        f"🧹 Proceed unsubscribe: {token}"
                    )

                    queue_redis.delete(key)

                    self.unsubscribe_by_token(
                        token
                    )

            time.sleep(0.1)

    # ======================
    def subscribe_by_token(
        self,
        exch_token
    ):
        if not self.ws_connected:
            print(
                f"⏳ WS not ready → retry: {exch_token}"
            )

            queue_redis.lpush(
                "kite_subscribe_queue",
                exch_token
            )

            return

        token = str(
            exch_token
        ).strip()

        instrument_token = (
            self.token_map.get(token)
            or self.token_map.get(
                str(int(token))
            )
        )

        if not instrument_token:
            print(
                f"❌ Token mapping not found: {exch_token}"
            )

            return

        if instrument_token in self.subscribed_tokens:
            print(
                f"⚠️ Already subscribed: {exch_token}"
            )

            return

        try:
            self.kws.subscribe(
                [int(instrument_token)]
            )

            self.kws.set_mode(
                self.kws.MODE_LTP,
                [int(instrument_token)]
            )

            self.subscribed_tokens.add(
                instrument_token
            )

            print(
                f"✅ SUBSCRIBED → Exch: {exch_token} | Inst: {instrument_token}"
            )

        except Exception as e:
            print(
                f"❌ Subscribe failed: {exch_token} → {e}"
            )

    # ======================
    def unsubscribe_by_token(
        self,
        exch_token
    ):
        if not self.ws_connected:
            print(
                f"⏳ WS not ready → retry unsubscribe: {exch_token}"
            )

            queue_redis.lpush(
                "kite_unsubscribe_queue",
                exch_token
            )

            return

        token = str(
            exch_token
        ).strip()

        instrument_token = (
            self.token_map.get(token)
            or self.token_map.get(
                str(int(token))
            )
        )

        if not instrument_token:
            print(
                f"❌ Token mapping not found unsubscribe: {exch_token}"
            )

            return

        try:
            self.kws.unsubscribe(
                [int(instrument_token)]
            )

            if instrument_token in self.subscribed_tokens:
                self.subscribed_tokens.remove(
                    instrument_token
                )

            queue_redis.srem(
                "active_tokens",
                exch_token
            )

            cache.delete(
                f"stock:{exch_token}:data"
            )

            print(
                f"❌ UNSUBSCRIBED → Exch: {exch_token} | Inst: {instrument_token}"
            )

        except Exception as e:
            print(
                f"❌ Unsubscribe failed: {exch_token} → {e}"
            )

    # ======================
    def on_ticks(
        self,
        ws,
        ticks
    ):
        redis_client = cache.client.get_client()

        for t in ticks:
            inst_token = str(
                t["instrument_token"]
            )

            ltp = t.get(
                "last_price"
            )

            exch_token = (
                self.reverse_token_map.get(
                    inst_token
                )
            )

            if ltp and exch_token:
                cache.set(
                    f"stock:{exch_token}:data",
                    json.dumps(
                        {
                            "token": exch_token,
                            "ltp": ltp
                        }
                    ),
                    timeout=None
                )

                redis_client.sadd(
                    "active_tokens",
                    exch_token
                )

    # ======================
    def on_error(
        self,
        ws,
        code,
        reason
    ):
        print(
            "❌ Error:",
            reason
        )

    # ======================
    def on_close(
        self,
        ws,
        code,
        reason
    ):
        print(
            "🔌 Connection closed:",
            reason
        )

        self.ws_connected = False