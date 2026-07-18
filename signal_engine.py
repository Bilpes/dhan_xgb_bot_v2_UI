# ============================================================
# signal_engine.py — dhan_xgb_bot_v2
# v4.4 PATCH 2026-07-18:
#   GAP-2/3: get_signal() now accepts tp_mult parameter.
#     - tp_mult is computed by bot._get_tp_mult() before calling get_signal.
#     - 1.8x on NEUTRAL/WEAK/SIDEWAYS days (choppy market — hit TP sooner)
#     - 2.5x on BULL + above resistance days (ride the trend)
#     - SL/TP in result dict computed with this tp_mult.
#     - tp_mult stored in result dict so trade_manager can use same value.
#
# v4.3 PATCH 2026-07-18 (retained):
#   FIX-1b: REMOVED daily_target gate (double-gate bug fixed)
#   FIX-7b: Lunch hour gate added
#
# v4.0 Changes 2026-07-17 (retained):
#   SELL (SHORT) signal, per-stock sideways skip, opening momentum bonus
# ============================================================

import pickle, logging, os, json, hashlib
import numpy as np
import pandas as pd
from datetime import datetime

from features import build_features, FEATURE_COLS
import config as cfg

log = logging.getLogger("signal_engine")

# ── Lazy SymbolPenalty ────────────────────────────────────────────────
_PENALTY = None

def _get_penalty():
    global _PENALTY
    if _PENALTY is None:
        try:
            from bot.symbol_penalty import SymbolPenalty
            _PENALTY = SymbolPenalty()
        except Exception as e:
            log.debug("[SymbolPenalty] Not available: %s", e)
    return _PENALTY


# ── Redis helpers ─────────────────────────────────────────────────────
def _get_redis():
    try:
        import redis as _redis
        pool = _redis.ConnectionPool(
            host=cfg.REDIS_HOST, port=cfg.REDIS_PORT, db=cfg.REDIS_DB,
            password=cfg.REDIS_PASSWORD, max_connections=cfg.REDIS_MAX_CONNECTIONS,
            socket_timeout=cfg.REDIS_SOCKET_TIMEOUT,
            retry_on_timeout=cfg.REDIS_RETRY_ON_TIMEOUT,
            decode_responses=True,
        )
        r = _redis.Redis(connection_pool=pool)
        r.ping()
        return r
    except Exception:
        return None

_R = None

def _r():
    global _R
    if not cfg.REDIS_ENABLED:
        return None
    if _R is None:
        _R = _get_redis()
        if _R is None:
            log.warning("[Redis] Unavailable — running without cache")
    return _R

def _safe_get(key):
    try:
        r = _r()
        return r.get(key) if r else None
    except Exception:
        return None

def _safe_set(key, value, ex):
    try:
        r = _r()
        if r:
            r.setex(key, ex, value)
    except Exception:
        pass

def _safe_exists(key):
    try:
        r = _r()
        return bool(r.exists(key)) if r else False
    except Exception:
        return False

def _safe_setex_nx(key, ex, value):
    try:
        r = _r()
        if r:
            return r.set(key, value, ex=ex, nx=True)
    except Exception:
        pass
    return None


class SignalEngine:
    def __init__(self):
        self.model    = None
        self.scaler   = None
        self.features = FEATURE_COLS
        self._load_model()
        os.makedirs(os.path.dirname(cfg.SIGNAL_LOG_PATH), exist_ok=True)

    @staticmethod
    def _needs_header(path: str) -> bool:
        try:
            return not os.path.exists(path) or os.path.getsize(path) == 0
        except OSError:
            return True

    def _load_model(self):
        try:
            with open(cfg.MODEL_PATH,   "rb") as f: self.model    = pickle.load(f)
            with open(cfg.SCALER_PATH,  "rb") as f: self.scaler   = pickle.load(f)
            with open(cfg.FEATURE_PATH, "rb") as f: self.features = pickle.load(f)
            log.info("Model loaded")
        except FileNotFoundError:
            log.warning("No model found — run train.py first")

    def reload_model(self):
        self._load_model()

    # ──────────────────────────────────────────────────────────────────
    # MAIN SIGNAL: returns BUY, SELL, or HOLD
    # GAP-2/3: tp_mult parameter added — adaptive TP multiplier
    # ──────────────────────────────────────────────────────────────────
    def get_signal(self, symbol, df, current_positions,
                   nifty_regime="BULL", nifty_ret_5c=0.0,
                   daily_pnl=0.0, tp_mult: float = None):

        # Default tp_mult from config if not provided
        if tp_mult is None:
            tp_mult = getattr(cfg, "ATR_TP_MULT", 1.8)

        result = {"action": "HOLD", "prob": 0.0, "entry": 0.0,
                  "sl": 0.0, "target": 0.0, "reject_reason": None,
                  "atr": 0.0, "qty_factor": 1.0, "side": "LONG",
                  "tp_mult": tp_mult}   # pass through to trade_manager
        now = datetime.now().time()

        def reject(reason):
            result["reject_reason"] = reason
            self._log(symbol, result)
            return result

        # ── Time gate ──────────────────────────────────────────────────
        if now < cfg.NO_NEW_TRADE_BEFORE or now >= cfg.NO_NEW_TRADE_AFTER:
            return reject("OUTSIDE_HOURS")

        # ── Lunch hour gate (FIX-7b) ───────────────────────────────────
        if getattr(cfg, 'AVOID_LUNCH_HOURS', False):
            from datetime import time as _dtime
            try:
                lunch_start_h, lunch_start_m = map(int, cfg.LUNCH_START.split(':'))
                lunch_end_h,   lunch_end_m   = map(int, cfg.LUNCH_END.split(':'))
                lunch_start = _dtime(lunch_start_h, lunch_start_m)
                lunch_end   = _dtime(lunch_end_h,   lunch_end_m)
                if lunch_start <= now < lunch_end:
                    return reject("LUNCH_HOURS")
            except Exception:
                pass

        # ── Model gate ─────────────────────────────────────────────────
        if self.model is None:
            return reject("NO_MODEL")

        # ── Data quality ───────────────────────────────────────────────
        if len(df) < 50:
            return reject(f"INSUFFICIENT_DATA({len(df)})")
        if df["close"].iloc[-1] < cfg.MIN_STOCK_PRICE:
            return reject(f"PRICE_LOW({df['close'].iloc[-1]:.0f})")

        # ── Position limits ────────────────────────────────────────────
        if symbol in current_positions:
            return reject("ALREADY_OPEN")

        import watchlist as wl
        sector = wl.SECTOR_MAP.get(symbol, "OTHER")
        sector_count = sum(
            1 for s in current_positions
            if wl.SECTOR_MAP.get(s, "X") == sector
        )
        if sector_count >= cfg.MAX_PER_SECTOR:
            return reject(f"SECTOR_LIMIT({sector})")
        if len(current_positions) >= cfg.MAX_OPEN_POSITIONS:
            return reject("MAX_POSITIONS")

        # ── Cooldown / circuit breaker ─────────────────────────────────
        if _safe_exists(f"bot:cooldown:{symbol}"):
            return reject("COOLDOWN")
        if _safe_exists("bot:circuit_breaker"):
            return reject("CIRCUIT_BREAKER_OPEN")

        # ── Symbol penalty ─────────────────────────────────────────────
        penalty = _get_penalty()
        if penalty is not None and penalty.is_penalised(symbol):
            return reject("SYMBOL_PENALISED")

        # ── Duplicate guard ────────────────────────────────────────────
        dedup_key = f"bot:dedup:{symbol}:{datetime.now().strftime('%Y%m%d%H%M')}"
        if _safe_exists(dedup_key):
            return reject("DUPLICATE_SIGNAL")

        # ── Feature vector ─────────────────────────────────────────────
        feat_key  = f"bot:feat:{symbol}"
        feat_hash = hashlib.md5(str(df["close"].iloc[-1]).encode()).hexdigest()[:8]
        cached_feat_raw = _safe_get(feat_key + ":val")
        cached_feat_hsh = _safe_get(feat_key + ":hash")
        feat = None

        if cached_feat_raw and cached_feat_hsh == feat_hash:
            try:
                feat = pd.DataFrame([json.loads(cached_feat_raw)])
            except Exception:
                feat = None

        if feat is None:
            try:
                df2 = df.copy()
                df2["nifty_ret_5c"]      = nifty_ret_5c
                df2["nifty_above_ema20"] = 1 if nifty_regime in ("BULL", "NEUTRAL") else 0
                feat_full = build_features(df2)
                feat = feat_full.iloc[[-1]]
                row_dict = feat.iloc[0].to_dict()
                row_json = json.dumps({
                    k: (v if not (isinstance(v, float) and not np.isfinite(v)) else 0.0)
                    for k, v in row_dict.items()
                })
                _safe_set(feat_key + ":val",  row_json,  cfg.TTL_FEATURE)
                _safe_set(feat_key + ":hash", feat_hash, cfg.TTL_FEATURE)
            except Exception as e:
                return reject(f"FEAT_ERR({e})")

        # ── XGBoost prediction ─────────────────────────────────────────
        pred_key    = f"bot:pred:{symbol}:{feat_hash}"
        cached_prob = _safe_get(pred_key)
        if cached_prob is not None:
            prob_long = float(cached_prob)
        else:
            try:
                X = feat.iloc[0][self.features].values.reshape(1, -1)
                X = np.where(np.isfinite(X), X, 0.0)
                X_sc = self.scaler.transform(X)
                proba = self.model.predict_proba(X_sc)[0]
                prob_long = float(proba[1])
                _safe_set(pred_key, str(prob_long), cfg.TTL_PREDICTION)
            except Exception as e:
                return reject(f"PREDICT_ERR({e})")

        prob_short = 1.0 - prob_long
        result["prob"] = prob_long

        # ── Volume filter ──────────────────────────────────────────────
        vol_ratio = feat.iloc[0].get("vol_ratio", 1.0)
        if vol_ratio < cfg.MIN_VOLUME_RATIO:
            return reject(f"LOW_VOL({vol_ratio:.2f})")

        # ── ATR / price ────────────────────────────────────────────────
        price = df["close"].iloc[-1]
        atr_key    = f"bot:atr:{symbol}"
        cached_atr = _safe_get(atr_key)
        if cached_atr is not None:
            atr = float(cached_atr)
        else:
            atr = float(feat.iloc[0].get("atr14", price * 0.005))
            _safe_set(atr_key, str(atr), cfg.TTL_ATR)
        atr = max(atr, price * 0.005)

        # ── Per-stock sideways skip ────────────────────────────────────
        sideways_ratio = getattr(cfg, "SIDEWAYS_ATR_RATIO", 0.80)
        atr_expansion  = float(feat.iloc[0].get("atr_expansion", 1.0))
        if atr_expansion < sideways_ratio:
            return reject(f"STOCK_SIDEWAYS(atr_expansion={atr_expansion:.2f})")

        # ── Extension guard ────────────────────────────────────────────
        max_ext = getattr(cfg, "MAX_EXTENSION_PCT", 0.030)
        try:
            ema20   = df["close"].ewm(span=20, adjust=False).mean().iloc[-1]
            ext_pct = (price - ema20) / ema20 if ema20 > 0 else 0.0
        except Exception:
            ema20   = price
            ext_pct = 0.0

        # ── Opening momentum bonus ─────────────────────────────────────
        session_min = float(feat.iloc[0].get("session_min", 999))
        orb_break   = int(feat.iloc[0].get("orb_break", 0))
        is_opening  = session_min <= 15

        # ── Thresholds (adaptive per regime) ──────────────────────────
        buy_thr  = cfg.BUY_THRESHOLD_WEAK  if nifty_regime == "WEAK" else cfg.BUY_THRESHOLD_DEFAULT
        sell_thr = (getattr(cfg, "SELL_THRESHOLD_WEAK",    0.58)
                    if nifty_regime == "WEAK"
                    else getattr(cfg, "SELL_THRESHOLD_DEFAULT", 0.60))

        breakout_ok = int(feat.iloc[0].get("breakout_confirm", 1))
        above_vwap  = int(feat.iloc[0].get("above_vwap",  1))

        # ── BUY signal evaluation ──────────────────────────────────────
        action_buy  = False
        prob_buy    = prob_long

        if ext_pct <= max_ext:
            if cfg.REQUIRE_BREAKOUT_CONFIRMATION:
                if nifty_regime in ("SIDEWAYS", "WEAK", "NEUTRAL"):
                    if not breakout_ok:
                        prob_buy = 0.0
                else:
                    if breakout_ok:
                        prob_buy = min(prob_buy + 0.02, 0.99)

            vwap_penalty = getattr(cfg, "VWAP_SOFT_PENALTY", 0.03)
            if cfg.REQUIRE_VWAP_CONFIRM and not above_vwap:
                prob_buy = max(prob_buy - vwap_penalty, 0.0)

            if is_opening and orb_break:
                prob_buy = min(prob_buy + 0.02, 0.99)

            if prob_buy >= buy_thr:
                action_buy = True

        # ── SELL signal evaluation ─────────────────────────────────────
        action_sell = False
        prob_sell   = prob_short

        if cfg.ALLOW_SHORTS:
            ext_pct_down = (ema20 - price) / ema20 if ema20 > 0 else 0.0
            if ext_pct_down <= max_ext:
                if cfg.REQUIRE_BREAKOUT_CONFIRMATION:
                    if nifty_regime in ("BULL", "NEUTRAL"):
                        if not (above_vwap == 0):
                            prob_sell = max(prob_sell - 0.03, 0.0)

                if nifty_regime == "WEAK":
                    prob_sell = min(prob_sell + 0.02, 0.99)

                if is_opening and not orb_break:
                    prob_sell = min(prob_sell + 0.02, 0.99)

                if prob_sell >= sell_thr:
                    action_sell = True

        # ── Pick stronger signal ───────────────────────────────────────
        if action_buy and action_sell:
            if prob_buy >= prob_sell:
                action_sell = False
            else:
                action_buy = False

        if not action_buy and not action_sell:
            return reject(f"LOW_PROB(buy={prob_buy:.3f} sell={prob_sell:.3f})")

        # ── SL / TP calculation using adaptive tp_mult ─────────────────
        if action_buy:
            sl     = max(price - cfg.ATR_SL_MULT * atr, price * (1 - cfg.MAX_SL_PCT))
            sl     = min(sl, price * (1 - cfg.MIN_SL_PCT))
            target = price + tp_mult * atr         # GAP-2/3: adaptive TP
            rr     = ((target - price) / (price - sl)) if price > sl else 0
            side   = "LONG"
            prob   = prob_buy
        else:
            sl     = min(price + cfg.ATR_SL_MULT * atr, price * (1 + cfg.MAX_SL_PCT))
            sl     = max(sl, price * (1 + cfg.MIN_SL_PCT))
            target = price - tp_mult * atr         # GAP-2/3: adaptive TP
            rr     = ((price - target) / (sl - price)) if sl > price else 0
            side   = "SHORT"
            prob   = prob_sell

        if rr < cfg.MIN_RR_RATIO:
            return reject(f"LOW_RR({rr:.2f})")

        # ── qty_factor from symbol penalty ────────────────────────────
        qty_factor = 1.0
        if penalty is not None:
            qty_factor = penalty.penalty_factor(symbol) if hasattr(penalty, "penalty_factor") else 1.0
        result["qty_factor"] = round(qty_factor, 2)

        _safe_setex_nx(dedup_key, cfg.TTL_DEDUP_ORDER, "1")

        action = "BUY" if action_buy else "SELL"
        result.update({
            "action":        action,
            "side":          side,
            "entry":         round(price,  2),
            "sl":            round(sl,     2),
            "target":        round(target, 2),
            "atr":           round(atr,    2),
            "rr_ratio":      round(rr,     2),
            "prob":          round(prob,   4),
            "tp_mult":       round(tp_mult, 2),   # stored for trade_manager
            "reject_reason": None,
        })
        self._log(symbol, result)
        log.info(
            f"{action} {symbol} entry={price:.2f} sl={sl:.2f} tp={target:.2f} "
            f"prob={prob:.4f} rr={rr:.2f} side={side} tp_mult={tp_mult:.1f}x"
        )
        return result

    @staticmethod
    def set_cooldown(symbol: str, seconds: int = None):
        ttl = seconds or cfg.TTL_COOLDOWN
        _safe_setex_nx(f"bot:cooldown:{symbol}", ttl, "1")
        log.info(f"[Cooldown] {symbol} -> {ttl}s")

    @staticmethod
    def trip_circuit_breaker(seconds: int = None):
        ttl = seconds or cfg.TTL_CIRCUIT_BREAKER
        _safe_setex_nx("bot:circuit_breaker", ttl, "1")
        log.warning(f"[CircuitBreaker] Tripped for {ttl}s")

    @staticmethod
    def reset_circuit_breaker():
        try:
            r = _r()
            if r:
                r.delete("bot:circuit_breaker")
        except Exception:
            pass

    def _log(self, symbol, result):
        row = {
            "time":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":        symbol,
            "action":        result["action"],
            "side":          result.get("side", "LONG"),
            "prob":          round(result["prob"], 4),
            "entry":         result["entry"],
            "sl":            result["sl"],
            "target":        result["target"],
            "reject_reason": result.get("reject_reason", ""),
            "qty_factor":    result.get("qty_factor", 1.0),
            "tp_mult":       result.get("tp_mult", cfg.ATR_TP_MULT),
        }
        write_header = self._needs_header(cfg.SIGNAL_LOG_PATH)
        pd.DataFrame([row]).to_csv(
            cfg.SIGNAL_LOG_PATH, mode="a",
            header=write_header, index=False
        )


# ── Nifty regime ──────────────────────────────────────────────────────
def get_nifty_regime(nifty_df) -> tuple:
    cached = _safe_get("bot:nifty:regime")
    if cached:
        try:
            d = json.loads(cached)
            return d["regime"], float(d["ret5"])
        except Exception:
            pass

    if len(nifty_df) < 50:
        return "NEUTRAL", 0.0

    c   = nifty_df["close"]
    e20 = c.ewm(span=20).mean()
    e50 = c.ewm(span=50).mean()
    slp = (e20.iloc[-1] - e20.iloc[-5]) / e20.iloc[-5]
    ret5 = (c.iloc[-1] - c.iloc[-6]) / c.iloc[-6]

    if   c.iloc[-1] > e20.iloc[-1] > e50.iloc[-1] and slp > 0: regime = "BULL"
    elif c.iloc[-1] < e20.iloc[-1] < e50.iloc[-1] and slp < 0: regime = "WEAK"
    else:                                                        regime = "NEUTRAL"

    _safe_set(
        "bot:nifty:regime",
        json.dumps({"regime": regime, "ret5": ret5}),
        cfg.TTL_NIFTY_REGIME,
    )
    return regime, ret5
