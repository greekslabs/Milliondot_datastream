from django.core.management.base import BaseCommand, CommandError
from django.core.cache import cache

from kiteconnect import KiteTicker, KiteConnect

import json
import os
import sys
import time
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pyotp
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from api.v1.market_data.redis_client import queue_redis


# =========================================================
# PROJECT / ENV
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)


# =========================================================
# HELPERS
# =========================================================

def normalize_token(token):
    if token is None:
        return None

    token = str(token).strip()

    if not token:
        return None

    try:
        return str(int(float(token)))
    except (ValueError, TypeError):
        return token


def extract_request_token(url):
    if not url:
        return None

    try:
        return parse_qs(urlparse(url).query).get("request_token", [None])[0]
    except Exception:
        return None


def find_linux_chrome():
    paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]

    for path in paths:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(f"Google Chrome not found. Checked: {', '.join(paths)}")


def find_linux_chromedriver():
    paths = [
        "/usr/local/bin/chromedriver",
        "/usr/bin/chromedriver",
    ]

    for path in paths:
        if os.path.isfile(path):
            return path

    return None


# =========================================================
# CHROME DRIVER
# =========================================================

def create_chrome_driver():
    options = webdriver.ChromeOptions()

    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if sys.platform.startswith("linux"):
        chrome_path = find_linux_chrome()
        chromedriver_path = find_linux_chromedriver()

        print(f"🌐 Chrome: {chrome_path}")

        options.binary_location = chrome_path
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        if chromedriver_path:
            print(f"🚗 ChromeDriver: {chromedriver_path}")
            service = Service(executable_path=chromedriver_path)
            return webdriver.Chrome(service=service, options=options)

        print("ℹ️ Using Selenium Manager")
        return webdriver.Chrome(options=options)

    return webdriver.Chrome(options=options)


# =========================================================
# LOGIN DEBUG
# =========================================================

def print_visible_inputs(driver):
    try:
        inputs = driver.find_elements(By.TAG_NAME, "input")
        print(f"🔎 Total inputs found: {len(inputs)}")

        for index, element in enumerate(inputs):
            try:
                if not element.is_displayed():
                    continue

                print(
                    f"Input #{index}"
                    f" | id={element.get_attribute('id')}"
                    f" | name={element.get_attribute('name')}"
                    f" | type={element.get_attribute('type')}"
                    f" | label={element.get_attribute('label')}"
                    f" | placeholder={element.get_attribute('placeholder')}"
                    f" | autocomplete={element.get_attribute('autocomplete')}"
                    f" | aria-label={element.get_attribute('aria-label')}"
                )
            except Exception:
                continue

    except Exception as exc:
        print(f"⚠️ Could not inspect inputs: {exc}")


# =========================================================
# FIND TOTP
# =========================================================

def find_totp_field(driver):
    selectors = [
        (By.CSS_SELECTOR, 'input[label="External TOTP"]'),
        (By.CSS_SELECTOR, 'input[aria-label="External TOTP"]'),
        (By.CSS_SELECTOR, 'input[autocomplete="one-time-code"]'),
        (By.CSS_SELECTOR, 'input[name="totp"]'),
        (By.CSS_SELECTOR, 'input[id="totp"]'),
        (By.CSS_SELECTOR, 'input[name="otp"]'),
        (
            By.XPATH,
            "//*[contains(normalize-space(.), 'External TOTP')]/following::input[1]"
        ),
    ]

    for by, selector in selectors:
        try:
            for element in driver.find_elements(by, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue

    try:
        candidates = []

        for element in driver.find_elements(By.TAG_NAME, "input"):
            try:
                if not element.is_displayed() or not element.is_enabled():
                    continue

                input_type = (element.get_attribute("type") or "text").lower()

                if input_type == "password":
                    continue

                candidates.append(element)

            except Exception:
                continue

        if candidates:
            return candidates[-1]

    except Exception:
        pass

    return False


def find_continue_button(driver):
    selectors = [
        (By.XPATH, "//button[normalize-space()='Continue']"),
        (By.XPATH, "//button[contains(normalize-space(.), 'Continue')]"),
        (By.CSS_SELECTOR, 'button[type="submit"]'),
    ]

    for by, selector in selectors:
        try:
            for element in driver.find_elements(by, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue

    return False


def wait_for_request_token(driver, timeout):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: extract_request_token(d.current_url) is not None
        )

        return extract_request_token(driver.current_url)

    except TimeoutException:
        return None


# =========================================================
# ACCESS TOKEN
# =========================================================

def get_access_token(api_key):
    api_secret = os.getenv("KITE_API_SECRET")
    user_id = os.getenv("KITE_USER_ID")
    password = os.getenv("KITE_PASSWORD")
    totp_secret = os.getenv("KITE_TOTP_SECRET")

    missing = []

    if not api_key:
        missing.append("KITE_API_KEY")
    if not api_secret:
        missing.append("KITE_API_SECRET")
    if not user_id:
        missing.append("KITE_USER_ID")
    if not password:
        missing.append("KITE_PASSWORD")
    if not totp_secret:
        missing.append("KITE_TOTP_SECRET")

    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    totp_secret = totp_secret.strip().replace(" ", "").upper()

    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    driver = None
    request_token = None

    try:
        print("🌐 Starting Chrome...")

        driver = create_chrome_driver()
        wait = WebDriverWait(driver, 30)

        print("🌐 Opening Kite login...")
        driver.get(login_url)

        print(f"🔗 Login URL: {driver.current_url}")

        # USER ID
        userid_field = wait.until(EC.element_to_be_clickable((By.ID, "userid")))
        userid_field.clear()
        userid_field.send_keys(user_id)

        print("✅ User ID entered")

        # PASSWORD
        password_field = wait.until(EC.element_to_be_clickable((By.ID, "password")))
        password_field.clear()
        password_field.send_keys(password)
        password_field.send_keys(Keys.RETURN)

        print("✅ Password submitted")

        # TOTP
        print("⏳ Waiting for External TOTP...")

        otp_field = WebDriverWait(driver, 30).until(find_totp_field)

        print("✅ External TOTP field found")

        otp = pyotp.TOTP(totp_secret).now()

        print("🔐 TOTP generated")

        try:
            otp_field.click()
        except Exception:
            pass

        try:
            otp_field.clear()
        except Exception:
            pass

        otp_field.send_keys(str(otp))

        print("✅ TOTP entered")

        # Zerodha may auto-submit
        request_token = extract_request_token(driver.current_url)

        if request_token:
            print("✅ TOTP auto-submitted")
        else:
            print("⏳ Waiting for authentication redirect...")
            request_token = wait_for_request_token(driver, 7)

        # Continue button fallback
        if not request_token:
            print("ℹ️ Auto-submit not detected; checking Continue button...")

            try:
                continue_button = WebDriverWait(driver, 5).until(find_continue_button)
            except TimeoutException:
                continue_button = None

            request_token = extract_request_token(driver.current_url)

            if not request_token and continue_button:
                try:
                    continue_button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", continue_button)

                print("✅ Continue clicked")

        if not request_token:
            print("⏳ Waiting for request token...")
            request_token = wait_for_request_token(driver, 30)

        current_url = driver.current_url

        if not request_token:
            raise RuntimeError(
                f"Kite login did not return request_token. Current URL: {current_url}"
            )

        print(f"🔗 Redirect URL: {current_url}")
        print("✅ Request token received")

    except Exception as exc:
        print("")
        print("❌ SELENIUM LOGIN ERROR")
        print(f"❌ Type: {type(exc).__name__}")
        print(f"❌ Error: {repr(exc)}")

        if driver:
            try:
                print(f"🔗 Current URL: {driver.current_url}")
            except Exception:
                pass

            print_visible_inputs(driver)

            try:
                driver.save_screenshot("/tmp/kite_login_error.png")
                print("📸 Screenshot saved: /tmp/kite_login_error.png")
            except Exception:
                pass

        raise RuntimeError(
            f"Selenium login failed: {type(exc).__name__}: {repr(exc)}"
        ) from exc

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    # REQUEST TOKEN → ACCESS TOKEN
    print("🔐 Generating Kite access token...")

    kite = KiteConnect(api_key=api_key)

    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        raise RuntimeError(f"Kite generate_session failed: {exc}") from exc

    access_token = session_data.get("access_token")

    if not access_token:
        raise RuntimeError("Kite access_token not returned")

    print("✅ Kite access token generated")

    return access_token


# =========================================================
# MANAGEMENT COMMAND
# =========================================================

class Command(BaseCommand):
    help = "Run Kite WebSocket"

    def handle(self, *args, **kwargs):
        load_dotenv(ENV_FILE, override=False)

        api_key = os.getenv("KITE_API_KEY")

        if not api_key:
            raise CommandError("KITE_API_KEY missing from .env")

        self.ws_connected = False
        self.subscribed_tokens = set()
        self.listener_threads_started = False
        self.subscription_lock = threading.Lock()

        self.kws = None
        self.token_map = {}
        self.reverse_token_map = {}

        # LOGIN
        print("")
        print("🔐 Generating access token...")

        try:
            access_token = get_access_token(api_key)
        except Exception as exc:
            raise CommandError(f"Token generation failed: {exc}") from exc

        print("✅ Access token generated")

        # REST CLIENT
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # NFO
        print("")
        print("📥 Loading NFO instruments...")

        try:
            nfo_instruments = kite.instruments("NFO")
            print(f"✅ NFO loaded: {len(nfo_instruments)}")
        except Exception as exc:
            raise CommandError(f"Unable to load NFO instruments: {exc}") from exc

        # BFO
        print("📥 Loading BFO instruments...")

        try:
            bfo_instruments = kite.instruments("BFO")
            print(f"✅ BFO loaded: {len(bfo_instruments)}")
        except Exception as exc:
            print(f"⚠️ BFO loading failed: {exc}")
            bfo_instruments = []

        self.instruments = nfo_instruments + bfo_instruments

        print(f"✅ Total instruments loaded: {len(self.instruments)}")

        # TOKEN MAP
        print("🔨 Building token map...")

        for instrument in self.instruments:
            instrument_token = instrument.get("instrument_token")
            exchange_token = normalize_token(instrument.get("exchange_token"))

            if instrument_token is None or not exchange_token:
                continue

            instrument_token = str(instrument_token)

            self.token_map[exchange_token] = instrument_token
            self.reverse_token_map[instrument_token] = exchange_token

        print(f"✅ Token map ready: {len(self.token_map)}")

        # WEBSOCKET
        self.kws = KiteTicker(api_key, access_token)

        self.kws.on_connect = self.on_connect
        self.kws.on_ticks = self.on_ticks
        self.kws.on_error = self.on_error
        self.kws.on_close = self.on_close

        print("")
        print("🚀 Starting Kite WebSocket...")

        try:
            self.kws.connect()
        except KeyboardInterrupt:
            print("🛑 Kite WebSocket stopped")
        except Exception as exc:
            raise CommandError(f"Kite WebSocket failed: {exc}") from exc


    # =====================================================
    # CONNECT
    # =====================================================

    def on_connect(self, ws, response):
        print("")
        print("✅ Connected to Kite WebSocket")

        self.ws_connected = True

        with self.subscription_lock:
            self.subscribed_tokens.clear()

        print("🔄 Restoring Redis subscriptions...")

        restored = 0

        try:
            for key in queue_redis.scan_iter("token_usage:*"):
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                token = normalize_token(key_str.split(":")[-1])

                if not token:
                    continue

                try:
                    usage_count = queue_redis.scard(key)
                except Exception as exc:
                    print(f"⚠️ Redis usage check failed for {token}: {exc}")
                    continue

                if usage_count <= 0:
                    continue

                print(f"🔁 Restoring → {token}")

                if self.subscribe_by_token(token):
                    restored += 1

        except Exception as exc:
            print(f"❌ Subscription restore error: {exc}")

        print(f"✅ Restore complete: {restored}")

        if not self.listener_threads_started:
            self.listener_threads_started = True

            threading.Thread(
                target=self.listen_subscriptions,
                name="kite-subscribe-listener",
                daemon=True
            ).start()

            threading.Thread(
                target=self.listen_unsubscribe,
                name="kite-unsubscribe-listener",
                daemon=True
            ).start()

            print("✅ Redis listener threads started")


    # =====================================================
    # SUBSCRIBE QUEUE - DB 9
    # =====================================================

    def listen_subscriptions(self):
        print("📡 Listening for subscriptions...")

        while True:
            try:
                token = queue_redis.rpop("kite_subscribe_queue")

                if not token:
                    time.sleep(0.1)
                    continue

                token = token.decode() if isinstance(token, bytes) else str(token)
                token = normalize_token(token)

                if not token:
                    continue

                print(f"📥 NEW SUB REQUEST → {token}")

                if not self.ws_connected:
                    queue_redis.rpush("kite_subscribe_queue", token)
                    time.sleep(1)
                    continue

                self.subscribe_by_token(token)

            except Exception as exc:
                print(f"❌ Subscribe queue error: {exc}")
                time.sleep(1)


    # =====================================================
    # UNSUBSCRIBE QUEUE - DB 9
    # =====================================================

    def listen_unsubscribe(self):
        print("📡 Listening for unsubscriptions...")

        while True:
            try:
                token = queue_redis.rpop("kite_unsubscribe_queue")

                if not token:
                    time.sleep(0.1)
                    continue

                token = token.decode() if isinstance(token, bytes) else str(token)
                token = normalize_token(token)

                if not token:
                    continue

                print(f"📤 NEW UNSUB REQUEST → {token}")

                usage_key = f"token_usage:{token}"
                usage_count = queue_redis.scard(usage_key)

                if usage_count > 0:
                    print(f"⏳ Still in use ({usage_count}) → skip unsubscribe: {token}")
                    continue

                if not self.ws_connected:
                    queue_redis.rpush("kite_unsubscribe_queue", token)
                    time.sleep(1)
                    continue

                queue_redis.delete(usage_key)
                self.unsubscribe_by_token(token)

            except Exception as exc:
                print(f"❌ Unsubscribe queue error: {exc}")
                time.sleep(1)


    # =====================================================
    # SUBSCRIBE
    # =====================================================

    def subscribe_by_token(self, exch_token):
        token = normalize_token(exch_token)

        if not token:
            return False

        if not self.ws_connected:
            print(f"⏳ WebSocket not ready → subscribe deferred: {token}")
            return False

        instrument_token = self.token_map.get(token)

        if not instrument_token:
            print(f"❌ Token mapping not found: {token}")
            return False

        with self.subscription_lock:
            if instrument_token in self.subscribed_tokens:
                print(f"⚠️ Already subscribed: {token}")
                return True

            try:
                instrument_token_int = int(instrument_token)

                self.kws.subscribe([instrument_token_int])
                self.kws.set_mode(
		    self.kws.MODE_LTP,
		    [instrument_token_int],
		)

                self.subscribed_tokens.add(instrument_token)

                print(
                    f"✅ SUBSCRIBED → Exch: {token} | Inst: {instrument_token}"
                )

                return True

            except Exception as exc:
                print(f"❌ Subscribe failed {token}: {exc}")
                return False


    # =====================================================
    # UNSUBSCRIBE
    # =====================================================

    def unsubscribe_by_token(self, exch_token):
        token = normalize_token(exch_token)

        if not token:
            return False

        if not self.ws_connected:
            print(f"⏳ WebSocket not ready → unsubscribe deferred: {token}")
            return False

        instrument_token = self.token_map.get(token)

        if not instrument_token:
            print(f"❌ Token mapping not found for unsubscribe: {token}")
            return False

        with self.subscription_lock:
            try:
                self.kws.unsubscribe([int(instrument_token)])
                self.subscribed_tokens.discard(instrument_token)

                # OLD WORKING METHOD:
                # active_tokens is in Django cache Redis DB 2
                redis_client = cache.client.get_client()

                redis_client.srem("active_tokens", token)
                cache.delete(f"stock:{token}:data")

                print(
                    f"✅ UNSUBSCRIBED → Exch: {token} | Inst: {instrument_token}"
                )

                return True

            except Exception as exc:
                print(f"❌ Unsubscribe failed {token}: {exc}")
                return False


    # =====================================================
    # TICKS
    # =====================================================

    def on_ticks(self, ws, ticks):
        # DB 2
        redis_client = cache.client.get_client()

        for tick in ticks:
            try:
                instrument_token = tick.get("instrument_token")
                ltp = tick.get("last_price")

                if instrument_token is None or ltp is None:
                    continue

                instrument_token = str(instrument_token)
                exchange_token = self.reverse_token_map.get(instrument_token)

                if not exchange_token:
                    print(f"⚠️ Reverse mapping missing → {instrument_token}")
                    continue

                data = {
                    "token": exchange_token,
                    "ltp": ltp
                }

                cache_key = f"stock:{exchange_token}:data"

                # LTP → DB 2
                cache.set(cache_key, json.dumps(data), timeout=None)

                # ACTIVE TOKENS → SAME DB 2
                redis_client.sadd("active_tokens", exchange_token)

                print(
                    f"💾 REDIS SAVED → {cache_key} = {cache.get(cache_key)}"
                )

            except Exception as exc:
                print(f"❌ Tick processing error: {exc}")


    # =====================================================
    # ERROR
    # =====================================================

    def on_error(self, ws, code, reason):
        print(f"❌ Kite WebSocket Error | Code: {code} | Reason: {reason}")


    # =====================================================
    # CLOSE
    # =====================================================

    def on_close(self, ws, code, reason):
        print(f"🔌 Kite WebSocket closed | Code: {code} | Reason: {reason}")

        self.ws_connected = False

        with self.subscription_lock:
            self.subscribed_tokens.clear()
