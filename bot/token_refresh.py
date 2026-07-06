# ============================================================
#  bot/token_refresh.py  —  Verify Dhan token is valid
# ============================================================
"""
Checks if today’s token in config/.env is working.
Run at 8:55 AM before starting the bot.

    python -m bot.token_refresh

If it shows OK  -> proceed to health_check and live_bot
If it shows FAIL -> get fresh token from Dhan app and repaste

Fix log:
  2026-07-06: Balance showed 'Rs.check app' because Dhan v2
              fundlimit wraps the value inside a nested dict or
              list and uses the misspelling 'availabelBalance'.
              Now tries every known nesting pattern and prints
              the raw keys if none match, so balance is always
              visible.
"""

import os, sys, logging, requests

from dotenv import load_dotenv, dotenv_values

log = logging.getLogger("token_refresh")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

ENV_PATH = os.path.join("config", ".env")


def read_credentials():
    """Read directly from file — bypasses any dotenv caching."""
    if not os.path.exists(ENV_PATH):
        return None, None
    values    = dotenv_values(ENV_PATH)
    token     = values.get("DHAN_ACCESS_TOKEN", "").strip()
    client_id = values.get("DHAN_CLIENT_ID", "").strip()
    return token, client_id


def _extract_balance(data: dict) -> str:
    """
    Dhan v2 /fundlimit response structure variations:

      Variant A (most common):
        { "status": "success",
          "data": { "availabelBalance": 12345.0, ... } }

      Variant B (older SDK):
        { "availabelBalance": 12345.0, ... }

      Variant C (list-wrapped):
        { "data": [ { "availabelBalance": 12345.0 } ] }

    Tries all three patterns; falls back to printing every key
    so the actual balance is never hidden behind 'check app'.
    """
    # Both spellings Dhan has used historically
    BALANCE_KEYS = (
        "availabelBalance",   # Dhan's consistent misspelling
        "availableBalance",   # correct spelling (used in some SDK versions)
        "net",
        "sodLimit",
    )

    inner = data

    # Unwrap 'data' if present
    if "data" in data:
        inner = data["data"]
        # If data is a non-empty list, take the first element
        if isinstance(inner, list) and inner:
            inner = inner[0]

    if isinstance(inner, dict):
        for key in BALANCE_KEYS:
            val = inner.get(key)
            if val is not None:
                return str(val)
        # None of the known keys matched — show all keys so it’s easy to debug
        pairs = ", ".join(f"{k}={v}" for k, v in inner.items())
        return f"(raw) {pairs}"

    # Fallback: show the whole payload
    return f"(raw) {data}"


def verify_token(token: str, client_id: str) -> tuple:
    """
    Verify token against Dhan v2 fundlimit endpoint.
    Returns (is_valid: bool, message: str)
    """
    if not token or len(token) < 20:
        return False, "Token is empty or too short in config/.env"

    if token == "paste_todays_token_here":
        return False, "Token not updated — still has placeholder text"

    headers = {
        "access-token": token,
        "client-id":    client_id,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(
            "https://api.dhan.co/v2/fundlimit",
            headers=headers,
            timeout=10,
        )

        if resp.status_code == 200:
            try:
                data      = resp.json()
                available = _extract_balance(data)
                return True, f"Token valid — available balance: Rs.{available}"
            except Exception:
                return True, "Token valid (connected to Dhan)"

        elif resp.status_code == 401:
            return False, "Token expired or invalid — generate a new one from Dhan app"

        elif resp.status_code == 429:
            return True, "Token likely valid — rate limited (429), try again in 2 seconds"

        else:
            return False, f"Dhan returned HTTP {resp.status_code} — check token"

    except requests.exceptions.ConnectionError:
        return False, "No internet connection"
    except requests.exceptions.Timeout:
        return False, "Dhan API timeout — try again"
    except Exception as e:
        return False, f"Error: {e}"


def run_token_refresh():
    try:
        from bot.telegram_alert import _send
        telegram_available = True
    except Exception:
        telegram_available = False

    log.info("Reading token from %s ...", ENV_PATH)
    token, client_id = read_credentials()

    if not token:
        msg = "config/.env not found or DHAN_ACCESS_TOKEN missing"
        log.error(msg)
        if telegram_available:
            _send(f"FAIL - {msg}")
        return False

    log.info("Verifying token against Dhan v2 API...")
    is_valid, message = verify_token(token, client_id)

    if is_valid:
        log.info("OK - %s", message)
        if telegram_available:
            _send(f"Token OK - {message}")
        return True
    else:
        log.error("FAIL - %s", message)
        if telegram_available:
            _send(
                f"TOKEN FAIL\n"
                f"{message}\n\n"
                f"Fix: Dhan app -> Profile -> Data APIs -> Copy token\n"
                f"Paste in config/.env on DHAN_ACCESS_TOKEN line"
            )
        return False


if __name__ == "__main__":
    ok = run_token_refresh()
    sys.exit(0 if ok else 1)
