"""
skill_hyperliquid.py — JARVIS Skill : Trading Auto sur Hyperliquid
==================================================================
Trading automatique sur Hyperliquid DEX avec analyse Claude AI.

Modes :
  paper — Simulation complète (défaut, aucun ordre réel)
  live  — Ordres réels sur Hyperliquid

Flux automatique :
  1. Polling toutes les X minutes sur les symboles configurés
  2. Collecte données Binance (prix, funding, OI, CVD, RSI, MACD, Ichimoku)
  3. Analyse Claude → signal BUY/SELL/NEUTRE + niveaux
  4. Si signal >= seuil confiance → ordre market + TP/SL automatiques
  5. Notification Telegram à chaque action

Commandes Telegram :
  /hlstart [BTC,ETH] [4h] — Démarrer le scanner auto
  /hlstop                 — Arrêter le scanner
  /hlstatus               — État + positions + PnL
  /hlbalance              — Balance USDC sur Hyperliquid
  /hlpositions            — Positions ouvertes détaillées
  /hlclose <SYMBOL>       — Fermer une position manuellement
  /hltrades               — Historique des trades
  /hlconfig               — Voir/modifier la config
  /hlwallet               — Infos wallet + créer un nouveau

Variables .env :
  HL_PRIVATE_KEY=0x...       — Clé privée EVM (NE JAMAIS PARTAGER)
  HL_TRADE_MODE=paper        — paper | live
  HL_SYMBOLS=BTC,ETH         — Symboles à scanner
  HL_TIMEFRAME=4h            — Timeframe d'analyse
  HL_POLL_INTERVAL=240       — Secondes entre deux scans (défaut 4min)
  HL_LEVERAGE=5              — Levier (défaut 5x)
  HL_SIZE_USD=50             — Taille de position en USDC (défaut $50)
  HL_SIZE_USD_HIGH=100       — Taille pour signal haute confiance
  HL_MIN_CONFIDENCE=2        — Confiance min pour trader (1=faible 2=modéré 3=élevé)
  HL_TP1_PCT=2.0             — TP1 en % (défaut +2%)
  HL_TP2_PCT=4.0             — TP2 en % (défaut +4%)
  HL_SL_PCT=1.5              — SL en % (défaut -1.5%)
  HL_TP1_CLOSE_PCT=40        — % position fermée au TP1
  HL_TP2_CLOSE_PCT=40        — % position fermée au TP2
  HL_MAX_POSITIONS=3         — Positions simultanées max
  HL_MAX_DAILY_LOSS=150      — Perte journalière max en USDC avant pause
  ANTHROPIC_API_KEY=...      — Clé Claude (obligatoire)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.Hyperliquid")

# ── Endpoints ────────────────────────────────────────────────────
HL_API          = "https://api.hyperliquid.xyz"
HL_INFO         = HL_API + "/info"
HL_EXCHANGE     = HL_API + "/exchange"
BINANCE_SPOT    = "https://api.binance.com/api/v3"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1"
ANTHROPIC_API   = "https://api.anthropic.com/v1/messages"


# ══════════════════════════════════════════════════════════════════
#   CALCULS TECHNIQUES (pure Python, sans pandas)
# ══════════════════════════════════════════════════════════════════

def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def _rma(values: list, period: int) -> list:
    if len(values) < period:
        return []
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return result

def calc_rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    ag = _rma(gains, period)
    al = _rma(losses, period)
    if not ag or al[-1] == 0:
        return 100.0
    return round(100 - 100 / (1 + ag[-1] / al[-1]), 2)

def calc_macd(closes: list, fast=12, slow=26, signal=9) -> dict:
    if len(closes) < slow + signal:
        return {}
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    offset = len(ef) - len(es)
    macd_line = [ef[i + offset] - es[i] for i in range(len(es))]
    sig_line  = _ema(macd_line, signal)
    if not sig_line:
        return {}
    off2 = len(macd_line) - len(sig_line)
    hist = [macd_line[i + off2] - sig_line[i] for i in range(len(sig_line))]
    return {
        "macd":       round(macd_line[-1], 4),
        "signal":     round(sig_line[-1], 4),
        "histogram":  round(hist[-1], 4),
        "bullish":    hist[-1] > 0,
        "cross_up":   hist[-1] > 0 and hist[-2] <= 0 if len(hist) >= 2 else False,
        "cross_down": hist[-1] < 0 and hist[-2] >= 0 if len(hist) >= 2 else False,
    }

def calc_ichimoku(highs: list, lows: list, closes: list) -> dict:
    def mid(h, l, p, i):
        if i < p - 1: return None
        return (max(h[i-p+1:i+1]) + min(l[i-p+1:i+1])) / 2
    n = len(closes)
    if n < 52:
        return {}
    i = n - 1
    tenkan = mid(highs, lows, 9, i)
    kijun  = mid(highs, lows, 26, i)
    sa = ((tenkan or 0) + (kijun or 0)) / 2
    sb = mid(highs, lows, 52, i)
    price = closes[-1]
    if sa and sb:
        ct, cb = max(sa, sb), min(sa, sb)
        cloud_pos = "AU-DESSUS" if price > ct else ("EN-DESSOUS" if price < cb else "DANS LE CLOUD")
    else:
        ct = cb = cloud_pos = None
    return {
        "tenkan":   round(tenkan, 2) if tenkan else None,
        "kijun":    round(kijun, 2)  if kijun  else None,
        "senkou_a": round(sa, 2),
        "senkou_b": round(sb, 2)     if sb     else None,
        "cloud_top": round(ct, 2)    if ct     else None,
        "cloud_bot": round(cb, 2)    if cb     else None,
        "cloud_pos": cloud_pos,
        "tk_signal": "bullish" if tenkan and kijun and tenkan > kijun else
                     "bearish" if tenkan and kijun and tenkan < kijun else "neutre",
    }

def calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """
    Calcule l'ATR (Average True Range) sur les N dernières bougies.
    ATR = RMA(True Range) ou RMA est un Wilder Moving Average.
    Retourne l'ATR en valeur absolue (prix).
    """
    if len(closes) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],                    # high - low
            abs(highs[i] - closes[i - 1]),          # |high - prev_close|
            abs(lows[i]  - closes[i - 1]),          # |low  - prev_close|
        )
        true_ranges.append(tr)
    # Wilder MA (RMA)
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 2)


def calc_atr_pct(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """ATR en pourcentage du prix actuel."""
    atr = calc_atr(highs, lows, closes, period)
    price = closes[-1] if closes else 1
    return round(atr / price * 100, 4) if price else 0.0


# Multiplicateurs ATR par timeframe
# SL = sl_mult * ATR | TP1 = tp1_mult * ATR | TP2 = tp2_mult * ATR
TF_ATR_MULTIPLIERS = {
    "1m":  {"sl": 1.5, "tp1": 2.0, "tp2": 3.0},
    "3m":  {"sl": 1.5, "tp1": 2.0, "tp2": 3.5},
    "5m":  {"sl": 1.5, "tp1": 2.0, "tp2": 3.5},
    "15m": {"sl": 1.5, "tp1": 2.0, "tp2": 3.5},
    "30m": {"sl": 1.5, "tp1": 2.0, "tp2": 3.5},
    "1h":  {"sl": 1.5, "tp1": 2.5, "tp2": 4.0},
    "2h":  {"sl": 1.5, "tp1": 2.5, "tp2": 4.0},
    "4h":  {"sl": 1.5, "tp1": 2.5, "tp2": 4.5},
    "6h":  {"sl": 1.5, "tp1": 2.5, "tp2": 5.0},
    "8h":  {"sl": 1.5, "tp1": 2.5, "tp2": 5.0},
    "12h": {"sl": 2.0, "tp1": 3.0, "tp2": 5.0},
    "1d":  {"sl": 2.0, "tp1": 3.0, "tp2": 6.0},
    "3d":  {"sl": 2.0, "tp1": 3.0, "tp2": 6.0},
    "1w":  {"sl": 2.0, "tp1": 3.0, "tp2": 6.0},
}


def calc_atr_levels(highs: list, lows: list, closes: list,
                    entry: float, side: str, tf: str,
                    atr_period: int = 14) -> dict:
    """
    Calcule les niveaux SL/TP basés sur l'ATR du timeframe.
    Retourne {sl, tp1, tp2, atr, atr_pct}.
    """
    atr  = calc_atr(highs, lows, closes, atr_period)
    mults = TF_ATR_MULTIPLIERS.get(tf, {"sl": 1.5, "tp1": 2.5, "tp2": 4.0})
    mult = 1 if side == "long" else -1

    # Fallback sur % fixes si ATR indisponible
    if atr == 0:
        price = entry or closes[-1]
        return {
            "sl":      entry * (1 - mult * 1.5 / 100),
            "tp1":     entry * (1 + mult * 2.0 / 100),
            "tp2":     entry * (1 + mult * 3.5 / 100),
            "atr":     0,
            "atr_pct": 0,
        }

    sl_dist  = atr * mults["sl"]
    tp1_dist = atr * mults["tp1"]
    tp2_dist = atr * mults["tp2"]

    return {
        "sl":      round(entry - mult * sl_dist,  2),
        "tp1":     round(entry + mult * tp1_dist, 2),
        "tp2":     round(entry + mult * tp2_dist, 2),
        "atr":     atr,
        "atr_pct": round(atr / entry * 100, 3) if entry else 0,
    }


def calc_cvd(klines: list) -> dict:
    if not klines:
        return {}
    cvd = 0.0
    for k in klines:
        tv  = float(k.get("volume", 0))
        bv  = float(k.get("taker_buy_base_vol", 0))
        cvd += bv - (tv - bv)
    last = klines[-1]
    tv_l = float(last.get("volume", 0))
    bv_l = float(last.get("taker_buy_base_vol", 0))
    delta_l = bv_l - (tv_l - bv_l)
    return {
        "cvd":        round(cvd, 2),
        "bias":       "BUY" if cvd > 0 else "SELL",
        "last_delta": round(delta_l, 2),
        "last_candle": "BUY" if delta_l > 0 else "SELL",
    }


# ══════════════════════════════════════════════════════════════════
#   SKILL
# ══════════════════════════════════════════════════════════════════

class HyperliquidSkill(BaseSkill):

    SKILL_NAME    = "hyperliquid"
    SKILL_DESC    = "Trading automatique sur Hyperliquid DEX avec analyse Claude AI"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR  = "JARVIS"

    SKILL_COMMANDS = {
        "hlstart":     "Démarrer le scanner auto (`/hlstart [BTC,ETH] [4h]`)",
        "hlstop":      "Arrêter le scanner",
        "hlstatus":    "État + positions + PnL",
        "hlbalance":   "Balance USDC sur Hyperliquid",
        "hlpositions": "Positions ouvertes détaillées",
        "hlclose":     "Fermer une position (`/hlclose BTC`)",
        "hltrades":    "Historique des trades",
        "hlconfig":    "Voir/modifier la config",
        "hlwallet":    "Infos wallet + créer un nouveau",
        "hldebug":     "Debug réponse brute API Hyperliquid",
        "hllearn":     "Bilan apprentissage : patterns gagnants/perdants",
        "hlmemory":    "Voir la mémoire des trades (N derniers)",

    }

    VALID_TF = {
        "1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w"
    }
    TF_ALIASES = {"24h":"1d","day":"1d","daily":"1d","week":"1w","weekly":"1w"}

    def __init__(self, settings=None):
        super().__init__(settings)

        # Wallet
        self.private_key    = os.getenv("HL_PRIVATE_KEY", "")
        self._wallet_address = ""  # dérivé à l'init

        # Config trading
        self.trade_mode     = os.getenv("HL_TRADE_MODE", "paper")
        self.symbols        = [s.strip().upper() for s in
                               os.getenv("HL_SYMBOLS", "BTC,ETH").split(",")]
        self.timeframe      = os.getenv("HL_TIMEFRAME", "4h")
        self.poll_interval  = int(os.getenv("HL_POLL_INTERVAL", "240"))
        self.leverage       = int(os.getenv("HL_LEVERAGE", "5"))
        self.size_usd       = float(os.getenv("HL_SIZE_USD", "50"))
        self.size_usd_high  = float(os.getenv("HL_SIZE_USD_HIGH", "100"))
        self.min_confidence = int(os.getenv("HL_MIN_CONFIDENCE", "2"))
        self.tp1_pct        = float(os.getenv("HL_TP1_PCT", "2.0"))
        self.tp2_pct        = float(os.getenv("HL_TP2_PCT", "4.0"))
        self.sl_pct         = float(os.getenv("HL_SL_PCT", "1.5"))
        self.tp1_close_pct  = float(os.getenv("HL_TP1_CLOSE_PCT", "40")) / 100
        self.tp2_close_pct  = float(os.getenv("HL_TP2_CLOSE_PCT", "40")) / 100
        self.max_positions  = int(os.getenv("HL_MAX_POSITIONS", "3"))
        self.max_daily_loss = float(os.getenv("HL_MAX_DAILY_LOSS", "150"))

        # Clé Anthropic
        self.anthropic_key  = os.getenv("ANTHROPIC_API_KEY", "")

        # État runtime
        self._running        = False
        self._scanner_task   = None
        self._monitor_task   = None  # suivi positions paper (toutes les 15s)
        self._positions: dict = {}       # symbol → position
        self._trades: list    = []       # historique
        self._daily_pnl      = 0.0
        self._daily_pnl_date = ""
        self._total_pnl      = 0.0
        self._trade_lock     = asyncio.Lock()
        self._cache: dict    = {}
        # Mémoire des trades : snapshots complets pour l'apprentissage
        self._trade_memory: list = []  # max 100 trades avec contexte complet
        self._notify_uid     = 0
        self._context        = None
        self._send_callback  = None  # injecté par bot.py

    def set_send_callback(self, callback):
        """Injecté par bot.py pour envoyer des messages Telegram proactifs."""
        self._send_callback = callback

    async def setup(self) -> bool:
        if self.private_key:
            self._wallet_address = self._derive_address(self.private_key)
            if self._wallet_address:
                logger.info("HL wallet: %s", self._wallet_address[:10] + "...")
        if not self.anthropic_key:
            logger.warning("HyperliquidSkill: ANTHROPIC_API_KEY manquant")
        self._ready = True
        return True

    async def teardown(self):
        self._running = False
        if self._scanner_task:
            self._scanner_task.cancel()
        if self._monitor_task:
            self._monitor_task.cancel()
        self._ready = False

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        self._context = context
        if not self._notify_uid:
            self._notify_uid = context.user_id
        cmd = command.lower().strip()
        routes = {
            "hlstart":     self._cmd_start,
            "hlstop":      self._cmd_stop,
            "hlstatus":    self._cmd_status,
            "hlbalance":   self._cmd_balance,
            "hlpositions": self._cmd_positions,
            "hlclose":     self._cmd_close,
            "hltrades":    self._cmd_trades,
            "hlconfig":    self._cmd_config,
            "hlwallet":    self._cmd_wallet,
            "hldebug":     self._cmd_debug,
            "hllearn":     self._cmd_learn,
            "hlmemory":    self._cmd_memory,

        }
        handler = routes.get(cmd)
        if handler:
            try:
                return await handler(args.strip(), context)
            except Exception as e:
                logger.error("HL erreur %s: %s", cmd, e, exc_info=True)
                return "❌ Erreur : %s" % str(e)[:120]
        return "❓ Commande inconnue : `/%s`" % command

    # ══════════════════════════════════════════════════════════════
    #   COMMANDES TELEGRAM
    # ══════════════════════════════════════════════════════════════

    async def _cmd_start(self, args: str, ctx: SkillContext) -> str:
        if self._running:
            return "⚠️ Scanner déjà actif. `/hlstop` pour arrêter."
        if not self.anthropic_key:
            return "❌ `ANTHROPIC_API_KEY` manquant dans `.env`"

        # Parser les args : /hlstart BTC,ETH 4h
        parts = args.split() if args else []
        for p in parts:
            p_lower = p.lower()
            resolved = self.TF_ALIASES.get(p_lower, p_lower)
            if resolved in self.VALID_TF:
                self.timeframe = resolved
            elif "," in p or p.upper().replace(",", "").isalpha():
                self.symbols = [s.strip().upper() for s in p.split(",")]

        mode_icon = "📄" if self.trade_mode == "paper" else "💸"
        self._running = True
        self._notify_uid = ctx.user_id
        self._scanner_task = asyncio.create_task(self._scanner_loop(ctx))
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        return (
            "🚀 **Scanner Hyperliquid démarré**\n"
            "━━━━━━━━━━━━━━━\n"
            "%s Mode    : **%s**\n"
            "📊 Symboles: %s\n"
            "⏱ TF      : %s\n"
            "🔁 Polling : toutes les %ds\n"
            "📐 Levier  : %dx\n"
            "💰 Taille  : $%.0f (high: $%.0f)\n"
            "⭐ Confiance min : %d/3\n\n"
            "%s"
        ) % (
            mode_icon, self.trade_mode.upper(),
            ", ".join(self.symbols),
            self.timeframe,
            self.poll_interval,
            self.leverage,
            self.size_usd, self.size_usd_high,
            self.min_confidence,
            "_Premier scan dans quelques secondes..._" if self.trade_mode == "live"
            else "⚠️ Mode PAPER — aucun ordre réel n'est placé.",
        )

    async def _cmd_stop(self, args: str, ctx: SkillContext) -> str:
        if not self._running:
            return "⚠️ Scanner déjà arrêté."
        self._running = False
        if self._scanner_task:
            self._scanner_task.cancel()
            self._scanner_task = None
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        return "⏹ **Scanner arrêté.**\nUtilise `/hlstatus` pour voir les positions ouvertes."

    async def _cmd_status(self, args: str, ctx: SkillContext) -> str:
        mode_icon = "📄" if self.trade_mode == "paper" else "💸"
        state_icon = "🟢" if self._running else "🔴"
        lines = [
            "%s **Hyperliquid** — %s %s\n━━━━━━━━━━━━━━━" % (
                mode_icon, state_icon, "ACTIF" if self._running else "ARRÊTÉ")
        ]
        lines.append("📊 Symboles : %s | TF : %s" % (", ".join(self.symbols), self.timeframe))

        # PnL journalier
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today
        pnl_icon = "🟢" if self._daily_pnl >= 0 else "🔴"
        lines.append("%s PnL aujourd'hui : **$%.2f** | Total : **$%.2f**" % (
            pnl_icon, self._daily_pnl, self._total_pnl))

        # Positions
        if self._positions:
            lines.append("\n📋 **Positions ouvertes (%d)** :" % len(self._positions))
            for sym, pos in self._positions.items():
                current_price = await self._fetch_price_simple(sym + "USDT")
                if current_price and pos.get("entry_price"):
                    pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                    pct *= (1 if pos["side"] == "long" else -1)
                    pct_lev = pct * self.leverage
                    p_icon = "🟢" if pct_lev >= 0 else "🔴"
                    pnl_usd = pos["size_usd"] * pct_lev / 100
                    lines.append(
                        "%s **%s** %s @ $%s → $%s (%s%+.1f%% / %+.2f$)" % (
                            p_icon, sym,
                            "LONG 📈" if pos["side"] == "long" else "SHORT 📉",
                            self._fmt_price(pos["entry_price"]),
                            self._fmt_price(current_price),
                            "🔥" if abs(pct_lev) > 5 else "",
                            pct_lev, pnl_usd,
                        )
                    )
                    lines.append(
                        "   SL: $%s | TP1: $%s | TP2: $%s" % (
                            self._fmt_price(pos.get("sl")),
                            self._fmt_price(pos.get("tp1")),
                            self._fmt_price(pos.get("tp2")),
                        )
                    )
        else:
            lines.append("\n_Aucune position ouverte._")

        return "\n".join(lines)

    async def _cmd_balance(self, args: str, ctx: SkillContext) -> str:
        if self.trade_mode == "paper":
            return (
                "📄 **Mode PAPER** — pas de balance réelle\n"
                "Configure `HL_TRADE_MODE=live` et `HL_PRIVATE_KEY` pour le live."
            )
        if not self._wallet_address:
            return "❌ Wallet non configuré. `/hlwallet` pour créer un wallet."

        balance = await self._fetch_balance()
        if balance is None:
            return (
                "❌ Impossible de récupérer la balance\n"
                "Vérifie que `HL_PRIVATE_KEY` est bien configuré et que "
                "l'adresse `%s` a bien des fonds sur Hyperliquid Perps." % self._wallet_address
            )

        lines = [
            "💳 **Balance Hyperliquid**\n━━━━━━━━━━━━━━━",
            "📬 `%s`" % self._wallet_address,
        ]

        if balance == 0.0:
            lines.append("⚠️ **0.00 USDC** — compte vide ou adresse incorrecte")
            lines.append(
                "Vérifie sur : https://app.hyperliquid.xyz/portfolio/%s" % self._wallet_address
            )
        else:
            lines.append("💰 **%.2f USDC**" % balance)
            engaged = sum(p.get("size_usd", 0) for p in self._positions.values())
            if engaged:
                lines.append("📊 Engagé : $%.2f | Libre : $%.2f" % (engaged, balance - engaged))
            lines.append("\n🔗 https://app.hyperliquid.xyz/portfolio/%s" % self._wallet_address)

        return "\n".join(lines)

    async def _cmd_positions(self, args: str, ctx: SkillContext) -> str:
        if not self._positions:
            return "📋 Aucune position ouverte."
        lines = ["📋 **Positions ouvertes**\n━━━━━━━━━━━━━━━"]
        for sym, pos in self._positions.items():
            opened = datetime.fromtimestamp(
                pos.get("opened_at", time.time()), tz=timezone.utc
            ).strftime("%d/%m %H:%M")
            lines.append(
                "**%s** %s\n"
                "  Entrée : $%s | Taille : $%.0f | Levier : %dx\n"
                "  SL : $%s | TP1 : $%s | TP2 : $%s\n"
                "  Ouvert le %s | Score : %d/3 | TF : %s" % (
                    sym,
                    "LONG 📈" if pos["side"] == "long" else "SHORT 📉",
                    self._fmt_price(pos.get("entry_price")),
                    pos.get("size_usd", 0),
                    self.leverage,
                    self._fmt_price(pos.get("sl")),
                    self._fmt_price(pos.get("tp1")),
                    self._fmt_price(pos.get("tp2")),
                    opened,
                    pos.get("confidence", 0),
                    pos.get("tf", self.timeframe),
                )
            )
        return "\n".join(lines)

    async def _cmd_close(self, args: str, ctx: SkillContext) -> str:
        symbol = args.strip().upper().replace("USDT", "")
        if not symbol:
            return "Usage : `/hlclose BTC`"
        if symbol not in self._positions:
            return "❌ Aucune position ouverte sur **%s**" % symbol
        pos = self._positions[symbol]
        price = await self._fetch_price_simple(symbol + "USDT") or pos["entry_price"]
        await self._close_position(symbol, pos, price, reason="Manuel")
        return "✅ Position **%s** fermée manuellement @ $%s" % (symbol, self._fmt_price(price))

    async def _cmd_trades(self, args: str, ctx: SkillContext) -> str:
        if not self._trades:
            return "📜 Aucun trade enregistré."
        n = min(int(args) if args.isdigit() else 10, len(self._trades))
        lines = ["📜 **%d derniers trades**\n━━━━━━━━━━━━━━━" % n]
        for t in self._trades[-n:][::-1]:
            pnl = t.get("pnl", 0)
            icon = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                "%s **%s** %s | Entrée: $%s → Sortie: $%s\n"
                "   PnL: **%+.2f$** (%+.1f%%) | %s → %s | %s" % (
                    icon,
                    t.get("symbol", "?"),
                    t.get("side", "?").upper(),
                    self._fmt_price(t.get("entry_price")),
                    self._fmt_price(t.get("exit_price")),
                    pnl,
                    t.get("pnl_pct", 0),
                    t.get("opened_str", "?"),
                    t.get("closed_str", "?"),
                    t.get("reason", "?"),
                )
            )
        lines.append("\n💰 PnL total : **$%.2f**" % self._total_pnl)
        return "\n".join(lines)

    async def _cmd_config(self, args: str, ctx: SkillContext) -> str:
        if not args:
            mode_icon = "📄" if self.trade_mode == "paper" else "💸"
            return (
                "⚙️ **Config Hyperliquid**\n━━━━━━━━━━━━━━━\n"
                "%s `trade_mode`     = %s\n"
                "📊 `symbols`       = %s\n"
                "⏱ `timeframe`     = %s\n"
                "🔁 `poll_interval` = %ds\n"
                "📐 `leverage`      = %dx\n"
                "💰 `size_usd`      = $%.0f\n"
                "💰 `size_usd_high` = $%.0f\n"
                "⭐ `min_confidence`= %d/3\n"
                "🎯 `tp1_pct`       = +%.1f%%\n"
                "🎯 `tp2_pct`       = +%.1f%%\n"
                "🛡 `sl_pct`        = -%.1f%%\n"
                "📊 `max_positions` = %d\n"
                "🚨 `max_daily_loss`= $%.0f\n\n"
                "Modifier : `/hlconfig <clé> <valeur>`\n"
                "Ex: `/hlconfig leverage 10`"
            ) % (
                mode_icon, self.trade_mode,
                ", ".join(self.symbols),
                self.timeframe, self.poll_interval,
                self.leverage,
                self.size_usd, self.size_usd_high,
                self.min_confidence,
                self.tp1_pct, self.tp2_pct, self.sl_pct,
                self.max_positions, self.max_daily_loss,
            )

        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            return "Usage : `/hlconfig <clé> <valeur>`"
        key, val = parts

        config_map = {
            "trade_mode":     ("trade_mode",     str),
            "timeframe":      ("timeframe",       str),
            "poll_interval":  ("poll_interval",   int),
            "leverage":       ("leverage",        int),
            "size_usd":       ("size_usd",        float),
            "size_usd_high":  ("size_usd_high",   float),
            "min_confidence": ("min_confidence",  int),
            "tp1_pct":        ("tp1_pct",         float),
            "tp2_pct":        ("tp2_pct",         float),
            "sl_pct":         ("sl_pct",          float),
            "max_positions":  ("max_positions",   int),
            "max_daily_loss": ("max_daily_loss",  float),
        }

        if key == "symbols":
            self.symbols = [s.strip().upper() for s in val.split(",")]
            return "✅ `symbols` → `%s`" % ", ".join(self.symbols)

        if key == "trade_mode":
            if val not in ("paper", "live"):
                return "❌ Valeurs valides : `paper` | `live`"
            if val == "live" and not self.private_key:
                return (
                    "❌ Impossible de passer en mode live sans wallet.\n"
                    "Configure `HL_PRIVATE_KEY` dans `.env` ou utilise `/hlwallet create`"
                )
            self.trade_mode = val
            icon = "📄" if val == "paper" else "💸"
            return "%s Mode → **%s**" % (icon, val.upper())

        if key not in config_map:
            return "❌ Clé inconnue : `%s`" % key

        attr, cast = config_map[key]
        try:
            setattr(self, attr, cast(val))
            return "✅ `%s` → `%s`" % (key, val)
        except ValueError:
            return "❌ Valeur invalide : `%s`" % val

    async def _cmd_debug(self, args: str, ctx: SkillContext) -> str:
        """Affiche adresse derivee + reponse brute API (texte brut, pas de Markdown)."""
        # Retourner du texte brut pour eviter les erreurs de parsing Markdown
        # Les adresses 0x et le JSON ont des caracteres qui cassent le Markdown v1
        lines = ["=== Debug Hyperliquid ==="]

        if self.private_key:
            derived = self._derive_address(self.private_key)
            lines.append("Cle privee : %d chars" % len(self.private_key))
            lines.append("Adresse derivee : " + (derived or "ERREUR derivation"))
            lines.append("Adresse config  : " + self._wallet_address)
            if derived and derived.lower() != self._wallet_address.lower():
                lines.append(">>> MISMATCH - la cle ne correspond pas au wallet !")
                lines.append("    Exporte la cle privee de " + self._wallet_address + " depuis Rabby")
            elif derived:
                lines.append(">>> OK - cle et adresse correspondent")
        else:
            lines.append("Aucune cle privee (HL_PRIVATE_KEY manquant dans .env)")

        raw = await self._fetch_balance_debug()
        lines.append("")
        lines.append("=== API Response ===")
        lines.append(raw[:800])
        # Remplacer _ par - pour eviter le Markdown v1 (italique)
        result = "\n".join(lines)
        result = result.replace("_", "-")
        return result

    async def _cmd_wallet(self, args: str, ctx: SkillContext) -> str:
        sub = args.strip().lower()

        if sub == "create":
            return await self._create_wallet()

        if not self._wallet_address:
            return (
                "❌ Aucun wallet configuré.\n\n"
                "**Option 1** — Créer un nouveau wallet depuis JARVIS :\n"
                "`/hlwallet create`\n\n"
                "**Option 2** — Utiliser un wallet existant :\n"
                "Ajoute dans `.env` :\n"
                "`HL_PRIVATE_KEY=0xTaCléPrivée`\n\n"
                "⚠️ La clé privée donne accès total aux fonds — ne la partage jamais."
            )

        lines = [
            "💳 **Wallet Hyperliquid**\n━━━━━━━━━━━━━━━",
            "📬 Adresse : `%s`" % self._wallet_address,
            "🔗 https://app.hyperliquid.xyz/portfolio/%s" % self._wallet_address,
            "\n⚠️ Clé privée configurée — garde-la secrète.",
            "\n**Commandes :**",
            "`/hlwallet create` — Créer un nouveau wallet (génère une nouvelle clé)",
        ]
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #   SCANNER AUTOMATIQUE
    # ══════════════════════════════════════════════════════════════

    async def _scanner_loop(self, ctx: SkillContext):
        """Boucle principale du scanner — analyse chaque symbole toutes les N secondes."""
        logger.info("HL scanner démarré — symboles: %s | TF: %s", self.symbols, self.timeframe)
        await self._notify("🔄 **Scanner démarré** — première analyse dans 5s...")
        await asyncio.sleep(5)

        while self._running:
            scan_start = time.time()
            try:
                for symbol in self.symbols:
                    if not self._running:
                        break
                    try:
                        await self._analyze_and_trade(symbol, ctx)
                    except Exception as e:
                        logger.error("Scanner erreur %s: %s", symbol, e)


            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Scanner loop erreur: %s", e)

            # Attendre jusqu'au prochain cycle
            elapsed = time.time() - scan_start
            wait = max(10, self.poll_interval - elapsed)
            logger.debug("HL scanner: prochain cycle dans %.0fs", wait)
            await asyncio.sleep(wait)

        logger.info("HL scanner arrêté")

    async def _analyze_and_trade(self, symbol: str, ctx: SkillContext):
        """Analyse un symbole et place un ordre si le signal est suffisant."""
        # Ne pas appeler Claude si une position est déjà ouverte sur ce symbole
        # — inutile et coûteux. Le monitor gère le suivi jusqu'à la clôture.
        if symbol in self._positions:
            logger.debug("Skipping analyse %s — position déjà ouverte", symbol)
            return

        binance_symbol = symbol + "USDT"

        # Collecter les données
        data = await self._collect_market_data(binance_symbol, self.timeframe)
        if not data:
            logger.warning("Données indisponibles pour %s", symbol)
            return

        # Analyser avec Claude
        signal = await self._get_claude_signal(symbol, self.timeframe, data)
        if not signal:
            return

        direction    = signal.get("direction")   # "long" | "short" | "neutral"
        confidence   = signal.get("confidence", 0)  # 1 | 2 | 3
        entry_price  = signal.get("entry_price", data["price"])
        sl_price     = signal.get("sl_price")
        tp1_price    = signal.get("tp1_price")
        tp2_price    = signal.get("tp2_price")
        reason       = signal.get("reason", "")

        logger.info("Signal %s: %s confiance=%d prix=%s",
                    symbol, direction, confidence, entry_price)

        # Ignorer si confiance insuffisante
        if direction not in ("long", "short") or confidence < self.min_confidence:
            logger.debug("Signal ignoré: direction=%s confiance=%d < min=%d",
                         direction, confidence, self.min_confidence)
            return

        # Vérifications avant d'ouvrir
        async with self._trade_lock:
            if symbol in self._positions:
                logger.debug("Déjà en position sur %s", symbol)
                return

            if len(self._positions) >= self.max_positions:
                logger.warning("Max positions atteint (%d)", self.max_positions)
                return

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._daily_pnl_date != today:
                self._daily_pnl = 0.0
                self._daily_pnl_date = today
            if self._daily_pnl <= -self.max_daily_loss:
                await self._notify(
                    "🚨 **Perte journalière max atteinte** ($%.2f)\n"
                    "Scanner en pause jusqu'à demain." % self._daily_pnl
                )
                self._running = False
                return

            # Taille de position
            size = self.size_usd_high if confidence == 3 else self.size_usd

            # Calculer TP/SL depuis l'ATR du timeframe si Claude n'en fournit pas
            # ATR adapte automatiquement les zones à la volatilité du TF choisi
            mult = 1 if direction == "long" else -1
            if not sl_price or not tp1_price or not tp2_price:
                klines = data.get("klines", [])
                if klines:
                    highs_k  = [k["high"]  for k in klines]
                    lows_k   = [k["low"]   for k in klines]
                    closes_k = [k["close"] for k in klines]
                    atr_lvls = calc_atr_levels(
                        highs_k, lows_k, closes_k,
                        entry=float(entry_price),
                        side=direction,
                        tf=self.timeframe,
                    )
                    logger.info("ATR levels %s %s: SL=%s TP1=%s TP2=%s (ATR=%.2f %.3f%%)",
                        symbol, self.timeframe,
                        atr_lvls["sl"], atr_lvls["tp1"], atr_lvls["tp2"],
                        atr_lvls["atr"], atr_lvls["atr_pct"])
                    if not sl_price:
                        sl_price  = atr_lvls["sl"]
                    if not tp1_price:
                        tp1_price = atr_lvls["tp1"]
                    if not tp2_price:
                        tp2_price = atr_lvls["tp2"]
                else:
                    # Fallback % fixes si pas de klines
                    if not sl_price:
                        sl_price  = entry_price * (1 - mult * self.sl_pct / 100)
                    if not tp1_price:
                        tp1_price = entry_price * (1 + mult * self.tp1_pct / 100)
                    if not tp2_price:
                        tp2_price = entry_price * (1 + mult * self.tp2_pct / 100)

            # Placer l'ordre
            success = await self._open_position(
                symbol, direction, size, entry_price, sl_price, tp1_price, tp2_price, confidence
            )

            if success:
                # Attacher le snapshot marché pour l'apprentissage
                if symbol in self._positions:
                    self._positions[symbol]["entry_snapshot"] = entry_snapshot
                mode_tag = "📄 PAPER" if self.trade_mode == "paper" else "💸 LIVE"
                # Calcul R/R
                sl_dist  = abs(entry_price - sl_price)  if sl_price  else 0
                tp1_dist = abs(tp1_price   - entry_price) if tp1_price else 0
                tp2_dist = abs(tp2_price   - entry_price) if tp2_price else 0
                rr1 = round(tp1_dist / sl_dist, 1) if sl_dist else 0
                rr2 = round(tp2_dist / sl_dist, 1) if sl_dist else 0
                await self._notify(
                    "%s **Nouveau trade**\n"
                    "━━━━━━━━━━━━━━━\n"
                    "📊 **%s** %s\n"
                    "💰 Entrée : $%s | $%.0f (x%d)\n"
                    "🛡 SL  : $%s  (-%s%%)\n"
                    "🎯 TP1 : $%s  (+%s%%) — R/R %.1f\n"
                    "🎯 TP2 : $%s  (+%s%%) — R/R %.1f\n"
                    "⭐ Confiance : %s\n"
                    "%s" % (
                        mode_tag,
                        symbol,
                        "LONG 📈" if direction == "long" else "SHORT 📉",
                        self._fmt_price(entry_price), size, self.leverage,
                        self._fmt_price(sl_price),
                        self._fmt_pct(entry_price, sl_price),
                        self._fmt_price(tp1_price),
                        self._fmt_pct(entry_price, tp1_price), rr1,
                        self._fmt_price(tp2_price),
                        self._fmt_pct(entry_price, tp2_price), rr2,
                        "⭐" * confidence,
                        ("_" + reason + "_") if reason else "",
                    )
                )

    async def _monitor_loop(self):
        """
        Tâche dédiée au suivi des positions — tourne toutes les 15s.

        Paper : simulation locale (prix Binance/HL, TP/SL calculés)
        Live  : poll Hyperliquid pour détecter les ordres exécutés
                et synchroniser l'état des positions
        """
        logger.info("HL monitor démarré (mode=%s)", self.trade_mode)
        while self._running:
            try:
                if self._positions:
                    if self.trade_mode == "paper":
                        await self._check_positions()
                    else:
                        await self._sync_live_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor loop: %s", e)
            await asyncio.sleep(15)
        logger.info("HL monitor arrêté")

    async def _sync_live_positions(self):
        """
        Mode LIVE : synchronise les positions JARVIS avec Hyperliquid.

        Stratégie :
        1. Récupérer les positions ouvertes réelles sur HL
        2. Pour chaque position suivie par JARVIS :
           - Si elle n'existe plus sur HL → elle a été fermée (TP ou SL touché)
           - Récupérer le PnL réel depuis l'historique des trades HL
           - Notifier + mettre à jour les stats
        3. Détecter les fermetures partielles (TP1 touché)
        """
        try:
            addr_lower = self._wallet_address.lower()
            async with aiohttp.ClientSession() as session:

                # ── Positions ouvertes sur HL ─────────────────────
                async with session.post(
                    HL_INFO,
                    json={"type": "clearinghouseState", "user": addr_lower},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    hl_state = await r.json()

                hl_positions = {}
                for ap in hl_state.get("assetPositions", []):
                    pos = ap.get("position", {})
                    coin = pos.get("coin", "")
                    szi  = float(pos.get("szi", 0))
                    if szi != 0:
                        hl_positions[coin] = {
                            "szi":        szi,
                            "entry_px":   float(pos.get("entryPx", 0) or 0),
                            "unrealized": float(pos.get("unrealizedPnl", 0) or 0),
                            "side":       "long" if szi > 0 else "short",
                        }

                # ── Trades récents sur HL (historique) ────────────
                async with session.post(
                    HL_INFO,
                    json={"type": "userFills", "user": addr_lower},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    fills_data = await r.json()

                # Index des fills récents par coin (les 50 plus récents)
                recent_fills = {}
                for fill in (fills_data or [])[:50]:
                    coin = fill.get("coin", "")
                    if coin not in recent_fills:
                        recent_fills[coin] = []
                    recent_fills[coin].append(fill)

            # ── Comparer avec les positions JARVIS ─────────────
            for symbol in list(self._positions.keys()):
                pos = self._positions.get(symbol)
                if not pos:
                    continue

                hl_pos = hl_positions.get(symbol)

                if hl_pos is None:
                    # Position fermée sur HL (SL ou TP2 touché)
                    await self._handle_live_close(symbol, pos, recent_fills, reason_default="SL/TP")

                elif (not pos.get("tp1_hit") and
                      pos.get("hl_szi") and
                      # Attendre au moins 60s avant de surveiller le TP1
                      # (évite faux positifs juste après l'ouverture)
                      time.time() - pos.get("opened_at", time.time()) > 60 and
                      abs(hl_pos["szi"]) < abs(pos["hl_szi"]) * (1 - self.tp1_close_pct + 0.05)):
                    # Taille réduite ≈ tp1_close_pct → TP1 partiel touché
                    await self._handle_live_tp1(symbol, pos, hl_pos, recent_fills)

                else:
                    # Position toujours ouverte — mettre à jour le PnL non réalisé
                    pos["unrealized_pnl"] = hl_pos["unrealized"]
                    pos["hl_szi"]         = hl_pos["szi"]

        except Exception as e:
            logger.error("_sync_live_positions: %s", e)

    async def _handle_live_close(self, symbol: str, pos: dict,
                                  recent_fills: dict, reason_default: str):
        """Traite une fermeture complète détectée sur Hyperliquid."""
        entry   = pos["entry_price"]
        side    = pos["side"]
        size    = pos["size_usd"]
        mult    = 1 if side == "long" else -1

        # Chercher le prix de sortie dans les fills récents
        exit_price = entry
        reason     = reason_default
        pnl_real   = None

        for fill in recent_fills.get(symbol, []):
            fill_side = fill.get("side", "")  # "A" = sell, "B" = buy
            fill_px   = float(fill.get("px", 0))
            fill_time = fill.get("time", 0)
            # Fill de clôture = sens inverse de la position
            is_close_fill = (side == "long" and fill_side == "A") or                             (side == "short" and fill_side == "B")
            if is_close_fill and fill_time > pos.get("opened_at", 0) * 1000:
                exit_price = fill_px
                # Détecter SL vs TP
                if side == "long":
                    reason = "TP2" if fill_px >= pos.get("tp2", 0) * 0.995 else                              "TP1" if fill_px >= pos.get("tp1", 0) * 0.995 else "Stop Loss"
                else:
                    reason = "TP2" if fill_px <= pos.get("tp2", 0) * 1.005 else                              "TP1" if fill_px <= pos.get("tp1", 0) * 1.005 else "Stop Loss"
                # PnL réel depuis le fill
                pnl_real = float(fill.get("closedPnl", 0))
                break

        # Calculer le PnL si non fourni par les fills
        if pnl_real is None:
            pct      = (exit_price - entry) / entry * 100 * mult
            pnl_real = size * pct * self.leverage / 100

        await self._close_position_live(symbol, pos, exit_price, pnl_real, reason)

    async def _handle_live_tp1(self, symbol: str, pos: dict,
                                hl_pos: dict, recent_fills: dict):
        """
        Traite un TP1 partiel détecté sur Hyperliquid.
        1. Met à jour la mémoire interne
        2. Annule l'ancien ordre SL sur HL
        3. Crée un nouveau SL au breakeven sur HL
        """
        pos["tp1_hit"] = True
        entry      = pos["entry_price"]
        close_frac = self.tp1_close_pct
        close_size = pos["size_usd"] * close_frac
        side       = pos["side"]
        is_buy     = side == "long"

        # Chercher le prix réel du TP1 dans les fills
        tp1_price = pos.get("tp1", entry)
        pnl_real  = 0.0
        for fill in recent_fills.get(symbol, []):
            fill_time = fill.get("time", 0)
            if fill_time > pos.get("opened_at", 0) * 1000:
                tp1_price = float(fill.get("px", tp1_price))
                pnl_real  = float(fill.get("closedPnl", 0))
                break

        self._daily_pnl += pnl_real
        self._total_pnl += pnl_real
        pos["size_usd"] *= (1 - close_frac)
        pos["sl"]        = entry  # breakeven en mémoire
        pos["hl_szi"]    = hl_pos["szi"]

        self._trades.append({
            "symbol":      symbol, "side": side,
            "entry_price": entry,  "exit_price": tp1_price,
            "size_usd":    close_size, "pnl": round(pnl_real, 2),
            "pnl_pct":     round(pnl_real / close_size * 100, 2) if close_size else 0,
            "reason":      "TP1 partiel (live)",
            "opened_str":  datetime.fromtimestamp(
                pos.get("opened_at", time.time()), tz=timezone.utc
            ).strftime("%d/%m %H:%M"),
            "closed_str": datetime.now(timezone.utc).strftime("%d/%m %H:%M"),
        })

        # ── Mettre à jour le SL sur Hyperliquid ──────────────────
        sl_updated = await self._update_sl_on_hl(symbol, entry, is_buy, hl_pos["szi"])

        msg = ("TP1 touche (LIVE) -- %s\n"
               "Prix : $%s | PnL : +$%.2f\n"
               "Ferme %.0f%% | SL deplace au breakeven : $%s\n"
               "Reste vers TP2 : $%s\n"
               "%s") % (
            symbol, self._fmt_price(tp1_price), pnl_real,
            close_frac * 100, self._fmt_price(entry),
            self._fmt_price(pos.get("tp2")),
            "SL mis a jour sur Hyperliquid" if sl_updated else
            "Attention : SL non mis a jour sur HL (verifier manuellement)",
        )
        await self._notify(msg)

    async def _cancel_existing_orders(self, symbol: str, exchange,
                                       loop, sl_only: bool = False):
        """
        Annule les ordres ouverts reduce-only sur ce symbole (SL et/ou TP).
        sl_only=True → annule uniquement les ordres trigger (SL).
        sl_only=False → annule tous les ordres reduce-only (SL + TP).
        Évite les doublons à l'ouverture ou au déplacement du SL.
        """
        try:
            addr_lower = self._wallet_address.lower()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_INFO,
                    json={"type": "openOrders", "user": addr_lower},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    open_orders = await r.json() or []

            orders_for_symbol = [o for o in open_orders if o.get("coin") == symbol]
            logger.info("openOrders %s: %s", symbol,
                        [{"oid": o.get("oid"), "type": o.get("orderType")}
                         for o in orders_for_symbol])

            for o in orders_for_symbol:
                order_type = o.get("orderType", "").lower()
                is_trigger = (
                    "stop" in order_type or
                    "trigger" in order_type or
                    o.get("isTrigger") is True or
                    o.get("triggerCondition") is not None
                )
                is_reduce  = o.get("reduceOnly", False)

                should_cancel = (
                    is_trigger if sl_only
                    else (is_trigger or is_reduce)
                )
                if should_cancel:
                    oid = o.get("oid")
                    if oid:
                        r = await loop.run_in_executor(
                            None,
                            lambda oid=oid: exchange.cancel(symbol, oid)
                        )
                        logger.info("Ordre annule %s oid=%s: %s", symbol, oid, r)
        except Exception as e:
            logger.error("_cancel_existing_orders %s: %s", symbol, e)

    async def _update_sl_on_hl(self, symbol: str, new_sl: float,
                                 is_buy: bool, current_sz: float) -> bool:
        """
        Annule tous les ordres SL ouverts sur ce symbole et place un nouveau SL.
        Retourne True si succès.
        """
        if not self.private_key:
            return False
        try:
            import eth_account as _eth
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants

            key      = self.private_key if self.private_key.startswith("0x") else "0x" + self.private_key
            wallet   = _eth.Account.from_key(key)
            exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=wallet.address)
            info     = Info(constants.MAINNET_API_URL, skip_ws=True)
            loop     = asyncio.get_event_loop()

            # 1. Récupérer les ordres ouverts
            addr = self._wallet_address.lower()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_INFO,
                    json={"type": "openOrders", "user": addr},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    open_orders = await r.json()

            # 2. Annuler tous les ordres SL existants via helper partagé
            await self._cancel_existing_orders(symbol, exchange, loop, sl_only=True)

            # 3. Placer le nouveau SL au breakeven
            meta   = await self._fetch_hl_meta(symbol)
            sz_dec = meta.get("sz_decimals", 3) if meta else 3
            px_dec = meta.get("px_decimals", 2) if meta else 2

            def rp(price):
                import math
                p = float(price)
                if p == 0: return 0.0
                mag = math.floor(math.log10(abs(p)))
                sig_dec = max(0, 4 - mag)
                decimals = max(0, min(sig_dec, px_dec))
                return round(p, decimals)

            sl_px   = rp(new_sl)
            sl_qty  = round(abs(float(current_sz)), sz_dec)
            close_buy = not is_buy

            def place_new_sl(ex=exchange, sym=symbol, cb=close_buy,
                             q=float(sl_qty), sl=float(sl_px)):
                return ex.order(sym, cb, q, sl,
                    {"trigger": {"isMarket": True, "tpsl": "sl",
                                 "triggerPx": sl}},
                    reduce_only=True)

            r = await loop.run_in_executor(None, place_new_sl)
            ok = r.get("status") == "ok" if isinstance(r, dict) else False
            logger.info("Nouveau SL %s @ %s: %s (ok=%s)", symbol, sl_px, r, ok)
            return ok

        except ImportError:
            logger.error("SDK manquant pour _update_sl_on_hl")
            return False
        except Exception as e:
            logger.error("_update_sl_on_hl %s: %s", symbol, e)
            return False

    async def _close_position_live(self, symbol: str, pos: dict,
                                    exit_price: float, pnl: float, reason: str):
        """Ferme une position live avec le PnL réel d'Hyperliquid."""
        self._daily_pnl += pnl
        self._total_pnl += pnl

        trade_record = {
            "symbol":      symbol,
            "side":        pos["side"],
            "entry_price": pos["entry_price"],
            "exit_price":  exit_price,
            "size_usd":    pos["size_usd"],
            "pnl":         round(pnl, 2),
            "pnl_pct":     round(pnl / pos["size_usd"] * 100, 2) if pos["size_usd"] else 0,
            "reason":      reason + " (live)",
            "opened_str":  datetime.fromtimestamp(
                pos.get("opened_at", time.time()), tz=timezone.utc
            ).strftime("%d/%m %H:%M"),
            "closed_str":  datetime.now(timezone.utc).strftime("%d/%m %H:%M"),
        }
        self._trades.append(trade_record)
        if len(self._trades) > 200:
            self._trades = self._trades[-200:]
        # Enregistrer dans la mémoire d'apprentissage (même que paper mode)
        self._record_trade_memory(trade_record, pos)

        del self._positions[symbol]

        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        msg_close = ("%s Position fermee LIVE -- %s (%s)\n"
                     "Entree: $%s -> Sortie: $%s\n"
                     "PnL reel : %+.2f USDC") % (
            pnl_icon, symbol, reason,
            self._fmt_price(pos["entry_price"]),
            self._fmt_price(exit_price),
            pnl,
        )
        await self._notify(msg_close)

    async def _check_positions(self):
        """Vérifie les TP/SL des positions paper toutes les 15s."""
        for symbol in list(self._positions.keys()):
            pos = self._positions.get(symbol)
            if not pos:
                continue
            price = await self._fetch_price_simple(symbol + "USDT")
            if not price:
                continue
            side  = pos["side"]
            entry = pos["entry_price"]
            mult  = 1 if side == "long" else -1

            # ── SL touché ───────────────────────────────────────
            sl_hit = (side == "long"  and price <= pos["sl"]) or                      (side == "short" and price >= pos["sl"])
            if sl_hit:
                await self._close_position(symbol, pos, pos["sl"], reason="Stop Loss")
                continue

            # ── TP1 touché → fermeture partielle ────────────────
            if not pos.get("tp1_hit"):
                tp1_hit = (side == "long"  and price >= pos["tp1"]) or                           (side == "short" and price <= pos["tp1"])
                if tp1_hit:
                    pos["tp1_hit"] = True
                    close_frac = self.tp1_close_pct
                    close_size = pos["size_usd"] * close_frac
                    pct_gain   = abs(pos["tp1"] - entry) / entry * 100
                    partial_pnl = close_size * pct_gain * self.leverage / 100
                    # Comptabiliser le PnL partiel
                    self._daily_pnl  += partial_pnl
                    self._total_pnl  += partial_pnl
                    pos["size_usd"]  *= (1 - close_frac)
                    # Déplacer le SL au breakeven après TP1
                    pos["sl"] = entry
                    self._trades.append({
                        "symbol":      symbol,
                        "side":        side,
                        "entry_price": entry,
                        "exit_price":  pos["tp1"],
                        "size_usd":    close_size,
                        "pnl":         round(partial_pnl, 2),
                        "pnl_pct":     round(pct_gain * self.leverage, 2),
                        "reason":      "TP1 partiel",
                        "opened_str":  datetime.fromtimestamp(
                            pos.get("opened_at", time.time()), tz=timezone.utc
                        ).strftime("%d/%m %H:%M"),
                        "closed_str":  datetime.now(timezone.utc).strftime("%d/%m %H:%M"),
                    })
                    msg_tp1 = ("TP1 touche -- %s\n"
                        "Prix : $%s\n"
                        "Ferme %.0f%% | PnL partiel : +$%.2f\n"
                        "SL au breakeven : $%s\n"
                        "Reste %.0f%% vers TP2 : $%s") % (
                        symbol, self._fmt_price(price),
                        close_frac * 100, partial_pnl,
                        self._fmt_price(entry),
                        (1 - close_frac) * 100, self._fmt_price(pos["tp2"]),
                    )
                    await self._notify(msg_tp1)

            # ── TP2 touché → fermeture totale ───────────────────
            if symbol in self._positions:  # peut avoir été fermé au SL ci-dessus
                pos = self._positions[symbol]
                tp2_hit = (side == "long"  and price >= pos["tp2"]) or                           (side == "short" and price <= pos["tp2"])
                if tp2_hit:
                    await self._close_position(symbol, pos, pos["tp2"], reason="TP2")

    async def _open_position(self, symbol, side, size_usd, entry_price,
                             sl, tp1, tp2, confidence) -> bool:
        """Ouvre une position — paper ou live."""
        if self.trade_mode == "paper":
            self._positions[symbol] = {
                "side":        side,
                "entry_price": entry_price,
                "size_usd":    size_usd,
                "sl":          sl,
                "tp1":         tp1,
                "tp2":         tp2,
                "tp1_hit":     False,
                "confidence":  confidence,
                "opened_at":   time.time(),
                "tf":          self.timeframe,
            }
            logger.info("PAPER: ouvert %s %s @ %s size=$%.0f", symbol, side, entry_price, size_usd)
            return True

        # Mode LIVE — ordre sur Hyperliquid
        return await self._place_hl_order(symbol, side, size_usd, entry_price, sl, tp1, tp2)

    async def _close_position(self, symbol, pos, exit_price, reason=""):
        """Ferme une position et calcule le PnL."""
        entry  = pos["entry_price"]
        side   = pos["side"]
        size   = pos["size_usd"]
        mult   = 1 if side == "long" else -1
        pct    = (exit_price - entry) / entry * 100 * mult
        pnl    = size * pct * self.leverage / 100

        self._daily_pnl += pnl
        self._total_pnl += pnl

        trade_record = {
            "symbol":      symbol,
            "side":        side,
            "entry_price": entry,
            "exit_price":  exit_price,
            "size_usd":    size,
            "pnl":         round(pnl, 2),
            "pnl_pct":     round(pct * self.leverage, 2),
            "reason":      reason,
            "opened_str":  datetime.fromtimestamp(
                pos.get("opened_at", time.time()), tz=timezone.utc
            ).strftime("%d/%m %H:%M"),
            "closed_str":  datetime.now(timezone.utc).strftime("%d/%m %H:%M"),
        }
        self._trades.append(trade_record)
        if len(self._trades) > 200:
            self._trades = self._trades[-200:]
        # Enregistrer dans la mémoire d'apprentissage avec le snapshot complet
        self._record_trade_memory(trade_record, pos)

        del self._positions[symbol]

        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        await self._notify(
            "%s **Position fermée** — %s (%s)\n"
            "Entrée: $%s → Sortie: $%s\n"
            "PnL : **%+.2f$** (%+.1f%%)" % (
                pnl_icon, symbol, reason,
                self._fmt_price(entry), self._fmt_price(exit_price),
                pnl, pct * self.leverage,
            )
        )

    # ══════════════════════════════════════════════════════════════
    #   HYPERLIQUID API
    # ══════════════════════════════════════════════════════════════

    async def _place_hl_order(self, symbol, side, size_usd, entry_price,
                              sl, tp1, tp2) -> bool:
        """
        Place un ordre market + TP/SL atomiques sur Hyperliquid.

        Stratégie en 2 étapes :
        1. market_open() → exécution immédiate, récupère le prix réel
        2. bulk_orders(grouping="normalTpsl") → SL + TP1 + TP2 en une seule
           requête atomique (tous créés ou aucun)

        Source SDK : examples/basic_tpsl.py
        """
        if not self.private_key:
            logger.error("LIVE ORDER: HL_PRIVATE_KEY non configure")
            return False
        try:
            import eth_account as _eth
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants

            key      = self.private_key if self.private_key.startswith("0x") else "0x" + self.private_key
            wallet   = _eth.Account.from_key(key)
            exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=wallet.address)

            price  = float(entry_price or await self._fetch_price_simple(symbol + "USDT") or 1)
            is_buy = (side == "long")

            # Taille en tokens
            meta   = await self._fetch_hl_meta(symbol)
            sz_dec = meta.get("sz_decimals", 3) if meta else 3
            qty    = round(size_usd * self.leverage / price, sz_dec)

            loop = asyncio.get_event_loop()

            # ── Étape 1 : ordre market ────────────────────────────
            result = await loop.run_in_executor(
                None,
                lambda: exchange.market_open(symbol, is_buy, qty, slippage=0.03)
            )
            logger.info("HL market_open: %s", result)

            if result.get("status") != "ok":
                logger.error("HL order failed: %s", result)
                return False

            # Prix d'exécution réel
            filled     = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
            exec_price = float(filled.get("filled", {}).get("avgPx") or price)

            # ── Étape 2 : TP/SL atomiques via bulk_orders ─────────
            mult      = 1 if is_buy else -1
            # px_decimals depuis l'API (maxDecimals du symbol)
            # Défaut 2 si absent = permet ETH/SOL sans perdre de précision
            px_dec = meta.get("px_decimals", 2) if meta else 2

            def rp(price):
                """
                Arrondi au format prix Hyperliquid :
                - max 5 chiffres significatifs
                - max px_decimals décimales
                Source: https://docs.chainstack.com/docs/hyperliquid-order-precision
                """
                p = float(price)
                if p == 0:
                    return 0.0
                # 5 chiffres significatifs max
                import math
                mag = math.floor(math.log10(abs(p)))
                sig_dec = max(0, 4 - mag)  # 5 sig figs → 4 - magnitude
                # Respecter aussi px_decimals
                decimals = min(sig_dec, px_dec) if px_dec >= 0 else sig_dec
                return round(p, max(0, decimals))

            # Niveaux ATR-based sur le prix d'execution reel
            side_str    = "long" if is_buy else "short"
            klines_live = await self._fetch_klines(symbol + "USDT", self.timeframe, 50)
            if klines_live:
                _h = [k["high"]  for k in klines_live]
                _l = [k["low"]   for k in klines_live]
                _c = [k["close"] for k in klines_live]
                atr_lvl  = calc_atr_levels(_h, _l, _c, exec_price, side_str, self.timeframe)
                real_sl  = rp(atr_lvl["sl"])
                real_tp1 = rp(atr_lvl["tp1"])
                real_tp2 = rp(atr_lvl["tp2"])
                logger.info("ATR %s %s: SL=%s TP1=%s TP2=%s (ATR=%.2f %.3f%%)",
                    symbol, self.timeframe, real_sl, real_tp1, real_tp2,
                    atr_lvl["atr"], atr_lvl["atr_pct"])
            else:
                # Fallback % fixes
                real_sl  = rp(exec_price * (1 - mult * self.sl_pct  / 100))
                real_tp1 = rp(exec_price * (1 + mult * self.tp1_pct / 100))
                real_tp2 = rp(exec_price * (1 + mult * self.tp2_pct / 100))
            tp1_qty   = round(qty * self.tp1_close_pct, sz_dec)
            tp2_qty   = round(qty * self.tp2_close_pct, sz_dec)
            sl_qty    = round(qty, sz_dec)
            close_buy = not is_buy

            # Annuler tout ordre SL/TP existant sur ce symbole avant d'en créer
            # (évite les doublons si le scanner tourne pendant une position)
            await self._cancel_existing_orders(symbol, exchange, loop)

            # SL = trigger stop-market | TP = limit GTC
            def send_sl(ex=exchange, sym=symbol, cb=close_buy,
                        q=float(sl_qty), sl=float(real_sl)):
                return ex.order(sym, cb, q, sl,
                    {"trigger": {"isMarket": True, "tpsl": "sl",
                                 "triggerPx": sl}},
                    reduce_only=True)

            def send_tp1(ex=exchange, sym=symbol, cb=close_buy,
                         q=float(tp1_qty), px=float(real_tp1)):
                return ex.order(sym, cb, q, px,
                    {"limit": {"tif": "Gtc"}},
                    reduce_only=True)

            def send_tp2(ex=exchange, sym=symbol, cb=close_buy,
                         q=float(tp2_qty), px=float(real_tp2)):
                return ex.order(sym, cb, q, px,
                    {"limit": {"tif": "Gtc"}},
                    reduce_only=True)

            r_sl  = await loop.run_in_executor(None, send_sl)
            r_tp1 = await loop.run_in_executor(None, send_tp1)
            r_tp2 = await loop.run_in_executor(None, send_tp2)
            logger.info("HL SL  result: %s", r_sl)
            logger.info("HL TP1 result: %s", r_tp1)
            logger.info("HL TP2 result: %s", r_tp2)

            sl_ok  = r_sl.get("status") == "ok"  if isinstance(r_sl,  dict) else False
            tp1_ok = r_tp1.get("status") == "ok" if isinstance(r_tp1, dict) else False
            tp2_ok = r_tp2.get("status") == "ok" if isinstance(r_tp2, dict) else False
            if not sl_ok:
                logger.warning("SL order echoue: %s", r_sl)
            if not tp1_ok:
                logger.warning("TP1 order echoue: %s", r_tp1)
            if not tp2_ok:
                logger.warning("TP2 order echoue: %s", r_tp2)

            # Enregistrer la position
            self._positions[symbol] = {
                "side":        side,
                "entry_price": exec_price,
                "size_usd":    size_usd,
                "sl":          real_sl,
                "tp1":         real_tp1,
                "tp2":         real_tp2,
                "tp1_hit":     False,
                "confidence":  0,
                "opened_at":   time.time(),
                "tf":          self.timeframe,
                "hl_szi":      qty if side == "long" else -qty,  # taille initiale pour suivi live
            }
            logger.info("LIVE: %s %s @ %.2f | SL:%.2f TP1:%.2f TP2:%.2f",
                        symbol, side, exec_price, real_sl, real_tp1, real_tp2)
            return True

        except ImportError:
            logger.error("SDK manquant: pip install hyperliquid-python-sdk")
            return False
        except Exception as e:
            logger.error("_place_hl_order %s: %s", symbol, e, exc_info=True)
            return False

    async def _fetch_hl_meta(self, symbol: str) -> Optional[dict]:
        """Récupère l'index et les décimales d'un asset sur Hyperliquid."""
        cache_key = "hl_meta_%s" % symbol
        cached = self._get_cache(cache_key, ttl=3600)
        if cached:
            return cached
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_INFO,
                    json={"type": "meta"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    data = await r.json()
                    universes = data.get("universe", [])
                    for i, asset in enumerate(universes):
                        if asset.get("name") == symbol:
                            result = {
                                "asset_idx":    i,
                                "sz_decimals":  asset.get("szDecimals", 3),
                                "px_decimals":  asset.get("maxDecimals", 0),
                                "name":         symbol,
                            }
                            self._set_cache(cache_key, result, ttl=3600)
                            return result
            return None
        except Exception as e:
            logger.error("fetch_hl_meta %s: %s", symbol, e)
            return None

    async def _fetch_balance(self) -> Optional[float]:
        """
        Recupere la balance USDC sur Hyperliquid perps.
        Essaie lowercase ET checksum car l'API est stricte sur la casse.
        """
        if not self._wallet_address:
            return None

        # Hyperliquid accepte uniquement lowercase
        addr_lower = self._wallet_address.lower()

        try:
            async with aiohttp.ClientSession() as session:

                # ── Compte unifié : chercher le solde USDC dans spotClearinghouseState ──
                # Depuis la migration vers le compte unifié Hyperliquid, le cash USDC
                # est dans les balances spot (coin="USDC"), pas dans marginSummary.
                async with session.post(
                    HL_INFO,
                    json={"type": "spotClearinghouseState", "user": addr_lower},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    spot_data = await r.json()
                    logger.debug("HL spot raw: %s", str(spot_data)[:400])

                    if isinstance(spot_data, dict):
                        for balance in spot_data.get("balances", []):
                            if balance.get("coin") in ("USDC", "USD", "USDC.e"):
                                total = float(balance.get("total", 0))
                                if total > 0:
                                    return total

                # ── Fallback : clearinghouseState (compte perps classique) ──
                async with session.post(
                    HL_INFO,
                    json={"type": "clearinghouseState", "user": addr_lower},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data = await r.json()
                    logger.debug("HL perps raw: %s", str(data)[:400])

                    if isinstance(data, dict):
                        w = data.get("withdrawable")
                        if w is not None and float(w) > 0:
                            return float(w)
                        ms = data.get("marginSummary", {})
                        av = ms.get("accountValue")
                        if av is not None and float(av) > 0:
                            return float(av)

                    return 0.0

        except Exception as e:
            logger.error("fetch_balance: %s", e)
            return None

    async def _fetch_balance_debug(self) -> str:
        """Retourne les réponses brutes des deux endpoints pour debug."""
        if not self._wallet_address:
            return "Pas d'adresse wallet configurée"
        addr_lower = self._wallet_address.lower()
        results = []
        try:
            async with aiohttp.ClientSession() as session:
                for ep_type in ("clearinghouseState", "spotClearinghouseState"):
                    async with session.post(
                        HL_INFO,
                        json={"type": ep_type, "user": addr_lower},
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        data = await r.json()
                        results.append("[%s]\n%s" % (ep_type, json.dumps(data, indent=2)[:400]))
            return "\n\n".join(results)
        except Exception as e:
            return "Erreur: %s" % e



    # ══════════════════════════════════════════════════════════════
    #   COLLECTE DONNÉES MARCHÉ (Binance)
    # ══════════════════════════════════════════════════════════════

    async def _collect_market_data(self, symbol: str, tf: str) -> Optional[dict]:
        """Collecte toutes les données de marché en parallèle."""
        try:
            klines, price, funding, oi = await asyncio.gather(
                self._fetch_klines(symbol, tf, 200),
                self._fetch_price_data(symbol),
                self._fetch_funding(symbol),
                self._fetch_oi(symbol),
                return_exceptions=True,
            )
            if isinstance(klines, Exception) or not klines:
                return None

            closes = [k["close"] for k in klines]
            highs  = [k["high"]  for k in klines]
            lows   = [k["low"]   for k in klines]

            atr_data = calc_atr_levels(
                highs, lows, closes,
                entry=closes[-1], side="long",  # côté calculé à l'entrée réelle
                tf=tf, atr_period=14
            )
            return {
                "price":    price.get("price", 0) if isinstance(price, dict) else 0,
                "change":   price.get("change_pct", 0) if isinstance(price, dict) else 0,
                "high_24h": price.get("high_24h", 0) if isinstance(price, dict) else 0,
                "low_24h":  price.get("low_24h", 0) if isinstance(price, dict) else 0,
                "high_20":  max(highs[-20:]) if len(highs) >= 20 else max(highs),
                "low_20":   min(lows[-20:]) if len(lows) >= 20 else min(lows),
                "funding":  funding if isinstance(funding, dict) else {},
                "oi":       oi if isinstance(oi, dict) else {},
                "rsi":      calc_rsi(closes),
                "macd":     calc_macd(closes),
                "ichimoku": calc_ichimoku(highs, lows, closes),
                "cvd":      calc_cvd(klines),
                "volume":   float(klines[-1]["volume"]) if klines else 0,
                "atr":      atr_data["atr"],
                "atr_pct":  atr_data["atr_pct"],
                "klines":   klines,  # conservé pour recalcul ATR à l'entrée
            }
        except Exception as e:
            logger.error("_collect_market_data %s: %s", symbol, e)
            return None

    async def _fetch_klines(self, symbol: str, tf: str, limit: int = 200) -> list:
        cache_key = "klines_%s_%s" % (symbol, tf)
        cached = self._get_cache(cache_key, ttl=30)
        if cached:
            return cached
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "%s/klines" % BINANCE_SPOT,
                    params={"symbol": symbol, "interval": tf, "limit": limit},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        return []
                    raw = await r.json()
                    klines = [{
                        "open":  float(row[1]), "high": float(row[2]),
                        "low":   float(row[3]), "close": float(row[4]),
                        "volume": float(row[5]),
                        "taker_buy_base_vol": float(row[9]),
                    } for row in raw]
                    self._set_cache(cache_key, klines, ttl=30)
                    return klines
        except Exception as e:
            logger.debug("fetch_klines %s: %s", symbol, e)
            return []

    async def _fetch_price_data(self, symbol: str) -> dict:
        cache_key = "price_%s" % symbol
        cached = self._get_cache(cache_key, ttl=10)
        if cached:
            return cached
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "%s/ticker/24hr" % BINANCE_SPOT,
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    d = await r.json()
                    result = {
                        "price":      float(d["lastPrice"]),
                        "change_pct": float(d["priceChangePercent"]),
                        "high_24h":   float(d["highPrice"]),
                        "low_24h":    float(d["lowPrice"]),
                    }
                    self._set_cache(cache_key, result, ttl=10)
                    return result
        except Exception as e:
            logger.debug("fetch_price %s: %s", symbol, e)
            return {}

    async def _fetch_price_simple(self, symbol: str) -> Optional[float]:
        """Retourne le prix depuis Hyperliquid (perps) avec fallback Binance."""
        # Priorité : prix Hyperliquid pour cohérence avec les positions perps
        hl_price = await self._fetch_hl_price(symbol.replace("USDT", ""))
        if hl_price:
            return hl_price
        # Fallback Binance
        d = await self._fetch_price_data(symbol)
        return d.get("price") if d else None

    async def _fetch_hl_price(self, symbol: str) -> Optional[float]:
        """Récupère le prix mark depuis l'API Hyperliquid (plus précis pour les perps)."""
        cache_key = "hl_price_%s" % symbol
        cached = self._get_cache(cache_key, ttl=5)
        if cached:
            return cached
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_INFO,
                    json={"type": "allMids"},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    # allMids retourne {symbol: "price_str", ...}
                    price_str = data.get(symbol)
                    if price_str:
                        price = float(price_str)
                        self._set_cache(cache_key, price, ttl=5)
                        return price
                    return None
        except Exception as e:
            logger.debug("_fetch_hl_price %s: %s", symbol, e)
            return None

    async def _fetch_funding(self, symbol: str) -> dict:
        cache_key = "funding_%s" % symbol
        cached = self._get_cache(cache_key, ttl=60)
        if cached:
            return cached
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "%s/premiumIndex" % BINANCE_FUTURES,
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        return {}
                    d = await r.json()
                    result = {"rate": float(d.get("lastFundingRate", 0))}
                    self._set_cache(cache_key, result, ttl=60)
                    return result
        except Exception:
            return {}

    async def _fetch_oi(self, symbol: str) -> dict:
        cache_key = "oi_%s" % symbol
        cached = self._get_cache(cache_key, ttl=30)
        if cached:
            return cached
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "%s/openInterest" % BINANCE_FUTURES,
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        return {}
                    d = await r.json()
                    price = await self._fetch_price_simple(symbol)
                    oi = float(d.get("openInterest", 0))
                    result = {"oi_usd": oi * (price or 1)}
                    self._set_cache(cache_key, result, ttl=30)
                    return result
        except Exception:
            return {}

    # ══════════════════════════════════════════════════════════════
    #   ANALYSE CLAUDE
    # ══════════════════════════════════════════════════════════════

    async def _get_claude_signal(self, symbol: str, tf: str, data: dict) -> Optional[dict]:
        """
        Envoie les données à Claude et récupère un signal structuré JSON.
        Retourne : {direction, confidence, entry_price, sl_price, tp1_price, tp2_price}
        """
        if not self.anthropic_key:
            return None

        price = data["price"]
        rsi   = data.get("rsi")
        macd  = data.get("macd", {})
        ichi  = data.get("ichimoku", {})
        cvd   = data.get("cvd", {})
        fr    = data.get("funding", {}).get("rate", 0) * 100
        oi    = data.get("oi", {}).get("oi_usd", 0)

        # Contexte mémoire des trades précédents
        memory_ctx = self._build_memory_context(symbol, tf, data)

        prompt = (
            "Tu es un trader algorithmique expert. Analyse ce marche et retourne UNIQUEMENT un JSON.\n\n"
            "## Donnees -- " + symbol + " " + tf + "\n"
            "- Prix : $" + self._fmt_price(price) + "\n"
            "- Variation 24h : " + str(round(data.get("change", 0), 2)) + "%\n"
            "- High/Low 20 bougies : $" + self._fmt_price(data.get("high_20")) + " / $" + self._fmt_price(data.get("low_20")) + "\n"
            "- RSI(14) : " + (str(round(rsi, 1)) if rsi else "N/A") + "\n"
            "- MACD : " + ("haussier cross_up" if macd.get("cross_up") else
                           "baissier cross_down" if macd.get("cross_down") else
                           "haussier" if macd.get("bullish") else "baissier") + "\n"
            "- Ichimoku cloud : " + (ichi.get("cloud_pos", "N/A") if ichi else "N/A") + "\n"
            "- TK signal : " + (ichi.get("tk_signal", "N/A") if ichi else "N/A") + "\n"
            "- CVD biais : " + (cvd.get("bias", "N/A") if cvd else "N/A") + "\n"
            "- Funding Rate : " + ("%.4f%%" % fr) + "\n"
            "- Open Interest : $" + self._fmt_large(oi) + "\n\n"
            + memory_ctx + "\n"
            "## Regles STRICTES\n"
            "- direction : 'long' OU 'short' UNIQUEMENT -- jamais neutral\n"
            "- Tu DOIS trancher même si les signaux sont mixtes\n"
            "- Si mixte → prends le biais dominant, baisse la confidence\n"
            "- confidence : 1 (signaux mixtes) | 2 (biais clair) | 3 (forte confluence)\n"
            "- entry_price : prix d'entrée optimal (support/résistance le plus proche)\n"
            "- sl_price : dernier support/résistance technique significatif\n"
            "- tp1_price / tp2_price : prochains niveaux techniques importants\n\n"
            "## Format de réponse (JSON STRICT, rien d'autre)\n"
            "{\n"
            '  "direction": "long|short",\n'
            '  "confidence": 1|2|3,\n'
            '  "entry_price": float,\n'
            '  "sl_price": float,\n'
            '  "tp1_price": float,\n'
            '  "tp2_price": float,\n'
            '  "reason": "2-3 mots résumant le signal"\n'
            "}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    ANTHROPIC_API,
                    headers={
                        "x-api-key":         self.anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type":      "application/json",
                    },
                    json={
                        "model":      "claude-haiku-4-5-20251001",  # Haiku pour vitesse + coût
                        "max_tokens": 300,
                        "messages":   [{"role": "user", "content": prompt}],
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status != 200:
                        return None
                    resp = await r.json()
                    text = resp.get("content", [{}])[0].get("text", "")
                    return self._parse_json(text)
        except Exception as e:
            logger.error("_get_claude_signal: %s", e)
            return None


    # ══════════════════════════════════════════════════════════════
    #   MÉMOIRE & APPRENTISSAGE
    # ══════════════════════════════════════════════════════════════

    def _build_entry_snapshot(self, symbol: str, tf: str,
                               data: dict, signal: dict) -> dict:
        """Snapshot complet du contexte marché au moment de l'entrée."""
        rsi  = data.get("rsi")
        macd = data.get("macd", {})
        ichi = data.get("ichimoku", {})
        cvd  = data.get("cvd", {})
        fr   = data.get("funding", {}).get("rate", 0) * 100
        oi   = data.get("oi", {}).get("oi_usd", 0)
        return {
            "symbol":     symbol,
            "tf":         tf,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "price":      data.get("price", 0),
            "change_24h": data.get("change", 0),
            "rsi":        round(rsi, 1) if rsi else None,
            "macd_bias":  ("cross_up" if macd.get("cross_up") else
                           "cross_down" if macd.get("cross_down") else
                           "bullish" if macd.get("bullish") else "bearish") if macd else None,
            "macd_hist":  macd.get("histogram"),
            "cloud_pos":  ichi.get("cloud_pos") if ichi else None,
            "tk_signal":  ichi.get("tk_signal") if ichi else None,
            "cvd_bias":   cvd.get("bias") if cvd else None,
            "cvd_last":   cvd.get("last_candle") if cvd else None,
            "funding":    round(fr, 4),
            "oi_usd":     oi,
            "direction":  signal.get("direction"),
            "confidence": signal.get("confidence", 0),
            "reason":     signal.get("reason", ""),
        }

    def _record_trade_memory(self, trade: dict, pos: dict):
        """Enregistre un trade terminé dans la mémoire d'apprentissage."""
        snapshot = pos.get("entry_snapshot", {})
        if not snapshot:
            return
        memory_entry = {
            **snapshot,
            "result":     trade.get("reason", "?"),  # Stop Loss / TP1 / TP2
            "pnl":        trade.get("pnl", 0),
            "pnl_pct":    trade.get("pnl_pct", 0),
            "win":        trade.get("pnl", 0) > 0,
            "exit_price": trade.get("exit_price"),
            "closed_str": trade.get("closed_str"),
        }
        self._trade_memory.append(memory_entry)
        if len(self._trade_memory) > 100:
            self._trade_memory = self._trade_memory[-100:]
        logger.info("Trade memorise: %s %s %s PnL=%.2f",
                    snapshot.get("symbol"), snapshot.get("direction"),
                    memory_entry["result"], memory_entry["pnl"])

    def _build_memory_context(self, symbol: str, tf: str,
                               current_data: dict) -> str:
        """
        Construit le contexte d'apprentissage à injecter dans le prompt Claude.
        Retourne une chaîne vide si pas assez d'historique.

        Stratégie :
        - 5 trades récents sur le même symbole/TF (contexte immédiat)
        - Bilan global sur les 20 derniers trades (patterns)
        """
        if not self._trade_memory:
            return ""

        # ── Trades similaires (même symbole + TF) ─────────────
        similar = [t for t in self._trade_memory
                   if t.get("symbol") == symbol and t.get("tf") == tf][-5:]

        # ── Stats globales sur les 20 derniers ─────────────────
        recent = self._trade_memory[-20:]
        wins   = [t for t in recent if t.get("win")]
        losses = [t for t in recent if not t.get("win")]
        wr     = len(wins) / len(recent) * 100 if recent else 0
        avg_win  = sum(t["pnl"] for t in wins)  / len(wins)  if wins  else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

        # Patterns dominants chez les perdants
        loss_reasons = {}
        for t in losses:
            r = t.get("rsi")
            m = t.get("macd_bias")
            c = t.get("cloud_pos", "")
            key = "RSI>70" if r and r > 70 else "RSI<30" if r and r < 30 else ""
            if m in ("cross_down", "bearish") and t.get("direction") == "long":
                key += " MACD_bearish_long"
            if "EN-DESSOUS" in (c or "") and t.get("direction") == "long":
                key += " sous_cloud_long"
            if key:
                loss_reasons[key] = loss_reasons.get(key, 0) + 1

        top_mistakes = sorted(loss_reasons.items(), key=lambda x: x[1], reverse=True)[:3]

        lines = ["\n## Memoire des trades precedents\n"]

        # Bilan global
        lines.append("### Bilan global (%d derniers trades)" % len(recent))
        lines.append("- Win rate : %.0f%% (%d/%d)" % (wr, len(wins), len(recent)))
        lines.append("- Gain moyen : +$%.2f | Perte moyenne : -$%.2f" % (avg_win, abs(avg_loss)))
        if top_mistakes:
            lines.append("- Patterns d'echec recurrents : " +
                         ", ".join("%s (%dx)" % (k, v) for k, v in top_mistakes))

        # Trades similaires récents
        if similar:
            lines.append("\n### %d trades recents sur %s %s" % (len(similar), symbol, tf))
            for t in similar[::-1]:
                icon  = "V" if t.get("win") else "X"
                lines.append(
                    "[%s] %s @ $%s -> $%s | %s | RSI:%.0f MACD:%s Cloud:%s | PnL:%+.1f$" % (
                        icon,
                        t.get("direction", "?").upper(),
                        self._fmt_price(t.get("price")),
                        self._fmt_price(t.get("exit_price")),
                        t.get("result", "?"),
                        t.get("rsi") or 0,
                        t.get("macd_bias", "?"),
                        (t.get("cloud_pos") or "?")[:12],
                        t.get("pnl", 0),
                    )
                )
            lines.append("Raisons des signaux : " +
                         " | ".join(t.get("reason", "?") for t in similar[-3:]))

        lines.append(
            "\n### Instruction\n"
            "Utilise cet historique pour affiner ton analyse. "
            "Si des patterns similaires ont echoue recemment, "
            "sois plus conservateur sur la confidence. "
            "Si le win rate est < 40%%, etre tres selectif (confidence >= 3 seulement)."
        )

        return "\n".join(lines)

    async def _cmd_learn(self, args: str, ctx: SkillContext) -> str:
        """Bilan apprentissage complet via Claude."""
        if not self._trade_memory:
            return "Aucun trade en memoire. Lance /hlstart et laisse tourner quelques trades."

        if not self.anthropic_key:
            return self._local_learn_report()

        # Construire un rapport detaille pour Claude
        trades_str = json.dumps(self._trade_memory[-50:], indent=2)
        prompt = (
            "Tu es un analyste trading expert. Voici l'historique des %d derniers trades "
            "de ce bot algorithmique sur Hyperliquid.\n\n"
            "Historique JSON :\n%s\n\n"
            "Analyse et retourne un rapport CONCIS en francais couvrant :\n"
            "1. Win rate global et par direction (long/short)\n"
            "2. Indicateurs les plus predictifs (RSI, MACD, Ichimoku, CVD, Funding)\n"
            "3. Patterns d'echec recurrents (quand le bot perd)\n"
            "4. Patterns de succes (quand le bot gagne)\n"
            "5. Recommandations concretes pour ameliorer les parametres\n"
            "   (seuils RSI, conditions MACD, niveaux de confidence a eviter)\n\n"
            "Sois direct et actionnable. Max 400 mots."
        ) % (len(self._trade_memory), trades_str[:3000])

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    ANTHROPIC_API,
                    headers={
                        "x-api-key":         self.anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type":      "application/json",
                    },
                    json={
                        "model":      "claude-haiku-4-5-20251001",
                        "max_tokens": 600,
                        "messages":   [{"role": "user", "content": prompt}],
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status != 200:
                        return self._local_learn_report()
                    resp = await r.json()
                    analysis = resp.get("content", [{}])[0].get("text", "")
                    header = "Bilan apprentissage (%d trades)\n" % len(self._trade_memory)
                    return header + analysis
        except Exception as e:
            logger.error("_cmd_learn: %s", e)
            return self._local_learn_report()

    def _local_learn_report(self) -> str:
        """Rapport local sans Claude (fallback)."""
        m = self._trade_memory
        if not m:
            return "Aucune memoire disponible."
        wins   = [t for t in m if t.get("win")]
        losses = [t for t in m if not t.get("win")]
        wr     = len(wins) / len(m) * 100

        lines = ["Bilan apprentissage (%d trades)\n" % len(m)]
        lines.append("Win rate : %.0f%% (%d gagnants / %d perdants)" % (wr, len(wins), len(losses)))

        if wins:
            avg_w = sum(t["pnl"] for t in wins) / len(wins)
            lines.append("Gain moyen : +$%.2f" % avg_w)
        if losses:
            avg_l = sum(t["pnl"] for t in losses) / len(losses)
            lines.append("Perte moyenne : -$%.2f" % abs(avg_l))

        # Analyse par direction
        for d in ("long", "short"):
            dt = [t for t in m if t.get("direction") == d]
            if dt:
                dw = [t for t in dt if t.get("win")]
                lines.append("%s : %d trades | %.0f%% win" % (
                    d.upper(), len(dt), len(dw)/len(dt)*100))

        # Resultats par indicateurs
        for bucket, label in [
            (lambda t: t.get("rsi", 50) > 65, "RSI > 65"),
            (lambda t: t.get("rsi", 50) < 35, "RSI < 35"),
            (lambda t: t.get("macd_bias") in ("cross_up", "bullish"), "MACD haussier"),
            (lambda t: "AU-DESSUS" in (t.get("cloud_pos") or ""), "Au-dessus du cloud"),
        ]:
            bt = [t for t in m if bucket(t)]
            if len(bt) >= 2:
                bw = [t for t in bt if t.get("win")]
                lines.append("%s : %d trades | %.0f%% win" % (
                    label, len(bt), len(bw)/len(bt)*100))

        lines.append("\nUtilise /hllearn pour une analyse Claude detaillee.")
        return "\n".join(lines)

    async def _cmd_memory(self, args: str, ctx: SkillContext) -> str:
        """Affiche les N derniers trades en memoire."""
        if not self._trade_memory:
            return "Memoire vide - aucun trade complete."
        n = min(int(args) if args.isdigit() else 10, len(self._trade_memory))
        lines = ["Memoire des %d derniers trades\n" % n]
        for t in self._trade_memory[-n:][::-1]:
            icon = "V" if t.get("win") else "X"
            lines.append(
                "[%s] %s %s %s | RSI:%.0f MACD:%s | %s | PnL:%+.2f$" % (
                    icon,
                    t.get("symbol", "?"),
                    t.get("direction", "?").upper(),
                    t.get("result", "?"),
                    t.get("rsi") or 0,
                    t.get("macd_bias", "?"),
                    t.get("reason", ""),
                    t.get("pnl", 0),
                )
            )
        wins = sum(1 for t in self._trade_memory[-n:] if t.get("win"))
        lines.append("\nWin rate : %.0f%%" % (wins / n * 100))
        return "\n".join(lines)

    def _parse_json(self, text: str) -> Optional[dict]:
        """Parse JSON robuste — résistant aux textes parasites."""
        if not text:
            return None
        clean = text.strip()
        if "```" in clean:
            import re
            clean = re.sub(r"```(?:json)?", "", clean).replace("```", "").strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        try:
            result = json.loads(clean[start:end])
            if "direction" not in result:
                return None
            return result
        except json.JSONDecodeError:
            return None

    # ══════════════════════════════════════════════════════════════
    #   WALLET
    # ══════════════════════════════════════════════════════════════

    def _derive_address(self, private_key: str) -> str:
        """Dérive l'adresse Ethereum depuis la clé privée."""
        if not private_key:
            return ""
        try:
            from eth_account import Account
            # S'assurer que la clé a le préfixe 0x
            key = private_key if private_key.startswith("0x") else "0x" + private_key
            acct = Account.from_key(key)
            return acct.address
        except ImportError:
            logger.error("eth_account non installe — pip install eth-account")
            return ""
        except ValueError as e:
            logger.error("_derive_address: cle privee invalide — %s", e)
            return ""
        except Exception as e:
            logger.error("_derive_address: %s", e)
            return ""

    async def _create_wallet(self) -> str:
        """Génère un nouveau wallet EVM et retourne les infos (sans stocker la clé)."""
        try:
            from eth_account import Account
            Account.enable_unaudited_hdwallet_features()
            acct, mnemonic = Account.create_with_mnemonic()
            return (
                "🔑 **Nouveau wallet créé**\n"
                "━━━━━━━━━━━━━━━\n"
                "📬 Adresse : `%s`\n\n"
                "🔐 **Clé privée :**\n`%s`\n\n"
                "📝 **Phrase mnémonique :**\n`%s`\n\n"
                "━━━━━━━━━━━━━━━\n"
                "⚠️ **SAUVEGARDE IMMÉDIATE OBLIGATOIRE**\n"
                "• Copie la clé privée dans un endroit sûr\n"
                "• Ne la partage JAMAIS\n"
                "• Ce message sera inaccessible après fermeture\n\n"
                "**Pour l'utiliser dans JARVIS :**\n"
                "`/pumpconfig env HL_PRIVATE_KEY %s`\n\n"
                "**Déposer des fonds :**\n"
                "🔗 https://app.hyperliquid.xyz/portfolio/%s"
            ) % (acct.address, acct.key.hex(), mnemonic, acct.key.hex(), acct.address)
        except ImportError:
            return (
                "❌ `eth_account` non installé\n"
                "```\npip install eth-account --break-system-packages\n```"
            )
        except Exception as e:
            return "❌ Erreur génération wallet : %s" % e

    # ══════════════════════════════════════════════════════════════
    #   HELPERS
    # ══════════════════════════════════════════════════════════════

    def _fmt_price(self, price) -> str:
        if price is None: return "N/A"
        p = float(price)
        if p >= 1000:  return "{:,.0f}".format(p)
        if p >= 1:     return "{:.4f}".format(p)
        return "{:.6f}".format(p)

    def _fmt_large(self, value) -> str:
        if value is None: return "N/A"
        v = abs(float(value))
        s = "-" if float(value) < 0 else ""
        if v >= 1e9:  return "%s%.2fB" % (s, v / 1e9)
        if v >= 1e6:  return "%s%.2fM" % (s, v / 1e6)
        if v >= 1e3:  return "%s%.1fK" % (s, v / 1e3)
        return "%s%.2f" % (s, v)

    def _fmt_pct(self, entry, target) -> str:
        if not entry or not target: return "?"
        return "%.2f" % abs((target - entry) / entry * 100)

    def _get_cache(self, key: str, ttl: int = 30) -> Optional[object]:
        if key not in self._cache: return None
        ts, data = self._cache[key]
        if time.time() - ts > ttl:
            del self._cache[key]
            return None
        return data

    def _set_cache(self, key: str, data, ttl: int = 30) -> None:
        self._cache[key] = (time.time(), data)

    async def _notify(self, text: str) -> None:
        """Envoie une notification proactive via Telegram (même pattern que skill_pumpfun)."""
        if not self._send_callback:
            return
        uid = self._notify_uid or (self._context.user_id if self._context else 0)
        if uid:
            try:
                await self._send_callback(uid, text)
            except Exception as e:
                logger.error("_notify: %s", e)
        else:
            logger.warning("_notify: aucun user_id, message perdu: %s", text[:50])
