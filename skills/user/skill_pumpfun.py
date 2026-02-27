"""
skill_pumpfun.py — JARVIS Skill : Scanner Memecoins Pump.fun (Solana)
======================================================================
Scan en temps réel les tokens en phase Pump.fun avant migration,
applique des filtres stricts (style Kabuki), puis score chaque candidat
via Claude API (0-100) avant d'envoyer une alerte Telegram.

Commandes Telegram :
  /pumpstart         — Démarrer le scanner
  /pumpstop          — Arrêter le scanner
  /pumpstatus        — État + dernières alertes + positions ouvertes
  /pumpfilters       — Voir/modifier les filtres actifs
  /pumplog           — 20 dernières alertes scorées
  /pumpblacklist     — Lister les tokens blacklistés
  /pumptest <addr>   — Analyser un token manuellement
  /pumpmode fast|normal|deep — Changer la vitesse de scan
  /pumptrademode paper|live|off — Activer/désactiver le trading auto
  /pumptrades        — Positions ouvertes + P&L paper/live
  /pumpclose <addr>  — Fermer manuellement une position
  /pumppaperreset    — Remettre à zéro le paper trading
  /pumpconfig        — Voir/modifier la config trading

Variables d'environnement (.env) :
  PUMP_SCAN_INTERVAL=120      — Secondes entre scans (défaut 120)
  PUMP_MC_MIN=10000           — Market cap minimum ($)
  PUMP_MC_MAX=20000           — Market cap maximum ($)
  PUMP_VOLUME_MIN=25000       — Volume minimum ($)
  PUMP_HOLDERS_MIN=70         — Holders minimum
  PUMP_SNIPERS_MAX=10         — Snipers maximum
  PUMP_DEV_HOLD_MAX=15        — Dev holdings max (%)
  PUMP_TOP10_MAX=20           — Top 10 holders max (%)
  PUMP_AGE_MAX_HOURS=2        — Âge max du token (heures)
  PUMP_SCORE_ALERT=70         — Score minimum pour alerte
  PUMP_SCORE_HIGH=85          — Score pour alerte high conviction
  PUMP_MAX_ALERTS_PER_HOUR=10 — Anti-spam
  PUMP_RUGCHECK_ENABLED=true  — Vérifier RugCheck.xyz
  ANTHROPIC_API_KEY=...       — Clé API Anthropic
  # Trading automatique
  PUMP_TRADE_MODE=paper       — paper | live | off (défaut: paper)
  PUMP_BUY_SCORE_MIN=70       — Score min pour buy auto (défaut 70)
  PUMP_BUY_SCORE_HIGH=85      — Score high conviction (mise x2)
  PUMP_BUY_AMOUNT=10          — Mise de base en USDC (défaut $10)
  PUMP_BUY_AMOUNT_HIGH=20     — Mise high conviction en USDC (défaut $20)
  PUMP_TP1=2.0                — Take profit 1 (x2, vendre 40%)
  PUMP_TP2=3.0                — Take profit 2 (x3, vendre 35%)
  PUMP_TP3=5.0                — Take profit 3 (x5, vendre 25%)
  PUMP_SL=0.50                — Stop loss (50% de perte = -0.50)
  PUMP_MAX_OPEN_TRADES=5      — Positions simultanées max
  PUMP_MAX_DAILY_LOSS=50      — Perte journalière max avant pause ($)
  PUMP_SOLANA_RPC=https://... — RPC Solana pour trades live
  PUMP_WALLET_KEY=...         — Clé privée wallet (NE PAS PARTAGER)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from skills.base import BaseSkill, SkillContext
try:
    import websockets
except ImportError:
    websockets = None  # pip install websockets

load_dotenv()
logger = logging.getLogger("skill_pumpfun")

# ── APIs ─────────────────────────────────────────────────────────
PUMPPORTAL_WS     = "wss://pumpportal.fun/api/data"           # WebSocket officiel temps réel
PUMPPORTAL_TRADE  = "https://pumpportal.fun/api/trade-local"
PUMPFUN_API       = "https://frontend-api-v3.pump.fun"        # enrichissement par adresse
MORALIS_SOL_GW    = "https://solana-gateway.moralis.io"        # volume + pairs fiables
DEXSCREENER_V1    = "https://api.dexscreener.com/token-pairs/v1/solana"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/tokens"
RUGCHECK_API      = "https://api.rugcheck.xyz/v1"
ANTHROPIC_API     = "https://api.anthropic.com/v1/messages"
BIRDEYE_API       = "https://public-api.birdeye.so"
COINGECKO_API     = "https://api.coingecko.com/api/v3"

# ── Constantes ───────────────────────────────────────────────────
SOLANA_CHAIN      = "solana"
PUMP_FUN_PROGRAM  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


# ══════════════════════════════════════════════════════════════════
#   SKILL CLASS
# ══════════════════════════════════════════════════════════════════

class PumpFunSkill(BaseSkill):
    """
    Scanner de memecoins Pump.fun avec scoring Claude.
    Intègre dans l'architecture JARVIS (handle / SkillContext).
    """

    SKILL_NAME    = "pumpfun"
    SKILL_DESC    = "Scanner memecoins Pump.fun avec scoring Claude et trading auto"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR  = "JARVIS"

    SKILL_COMMANDS = {
        "pumpstart":      "Démarrer le scanner Pump.fun",
        "pumpstop":       "Arrêter le scanner",
        "pumpstatus":     "État + positions ouvertes + P&L",
        "pumpfilters":    "Voir/modifier les filtres",
        "pumplog":        "20 dernières alertes scorées",
        "pumpblacklist":  "Gérer la blacklist tokens",
        "pumptest":       "Analyser un token manuellement (`/pumptest <addr>`)",
        "pumpmode":       "Vitesse de scan (`/pumpmode fast|normal|deep`)",
        "pumptrademode":  "Mode trading (`/pumptrademode paper|live|off`)",
        "pumptrades":     "Positions ouvertes + P&L paper/live",
        "pumpclose":      "Fermer une position (`/pumpclose <addr>`)",
        "pumppaperreset": "Remettre à zéro le paper trading",
        "pumpconfig":     "Voir/modifier la config trading",
        "pumpscanlog":    "Log détaillé des scans (`/pumpscanlog [N]`)",
    }

    # ── Init ────────────────────────────────────────────────────

    def __init__(self, settings=None):
        # Filtres configurables via .env
        self.scan_interval    = int(os.getenv("PUMP_SCAN_INTERVAL", "120"))
        self.mc_min           = float(os.getenv("PUMP_MC_MIN",        "10000"))
        self.mc_max           = float(os.getenv("PUMP_MC_MAX",        "20000"))
        self.volume_min       = float(os.getenv("PUMP_VOLUME_MIN",    "25000"))
        self.holders_min      = int(os.getenv("PUMP_HOLDERS_MIN",     "70"))
        self.snipers_max      = int(os.getenv("PUMP_SNIPERS_MAX",     "10"))
        self.dev_hold_max     = float(os.getenv("PUMP_DEV_HOLD_MAX",  "15"))
        self.top10_max        = float(os.getenv("PUMP_TOP10_MAX",     "20"))
        self.age_max_hours    = float(os.getenv("PUMP_AGE_MAX_HOURS", "2"))
        self.score_alert      = int(os.getenv("PUMP_SCORE_ALERT",     "70"))
        self.score_high       = int(os.getenv("PUMP_SCORE_HIGH",      "85"))
        self.notify_mc_zone   = os.getenv("PUMP_NOTIFY_MC_ZONE", "true").lower() == "true"
        self.max_alerts_hour  = int(os.getenv("PUMP_MAX_ALERTS_PER_HOUR", "10"))
        self.rugcheck_enabled = os.getenv("PUMP_RUGCHECK_ENABLED", "true").lower() == "true"

        # Clés API
        self.anthropic_key    = os.getenv("ANTHROPIC_API_KEY", "")
        self.moralis_key      = os.getenv("MORALIS_API_KEY", "")
        self.solana_rpc       = os.getenv("PUMP_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
        self.wallet_key       = os.getenv("PUMP_WALLET_KEY", "")  # NE JAMAIS LOGGER

        # ── Config trading auto ──────────────────────────────
        self.trade_mode       = os.getenv("PUMP_TRADE_MODE", "paper")  # paper|live|off
        self.buy_score_min    = int(os.getenv("PUMP_BUY_SCORE_MIN",    "70"))
        self.buy_score_high   = int(os.getenv("PUMP_BUY_SCORE_HIGH",   "85"))
        self.buy_amount       = float(os.getenv("PUMP_BUY_AMOUNT",     "10"))
        self.buy_amount_high  = float(os.getenv("PUMP_BUY_AMOUNT_HIGH","20"))
        self.tp1              = float(os.getenv("PUMP_TP1", "2.0"))   # x2 → vendre 40%
        self.tp2              = float(os.getenv("PUMP_TP2", "3.0"))   # x3 → vendre 35%
        self.tp3              = float(os.getenv("PUMP_TP3", "5.0"))   # x5 → vendre 25%
        self.sl               = float(os.getenv("PUMP_SL",  "0.50"))  # -50%
        self.max_open_trades  = int(os.getenv("PUMP_MAX_OPEN_TRADES", "5"))
        self.max_daily_loss   = float(os.getenv("PUMP_MAX_DAILY_LOSS","50"))

        # État interne
        self._running         = False
        self._loop_task       = None
        self._context         = None
        self._send_callback   = None
        self._notify_user_id: int = 0   # user_id stocké au /pumpstart pour alertes proactives

        # Données
        self._alerts_log: list   = []       # Toutes les alertes envoyées
        self._scan_log: list     = []       # Log détaillé de chaque scan
        self._seen_tokens: set   = set()    # Tokens déjà vus (déduplication)
        self._blacklist: set     = set()    # Tokens manuellement blacklistés
        self._scan_count: int    = 0
        self._alerts_this_hour: list = []   # Timestamps des alertes (anti-spam)

        # ── Positions de trading ─────────────────────────────
        # Structure : addr → {symbol, entry_price, amount_usd, shares,
        #                     opened_at, tp1_hit, tp2_hit, paper,
        #                     score, conviction, sl_price}
        self._positions: dict    = {}
        self._closed_trades: list = []      # Historique des trades clôturés
        self._daily_pnl: float   = 0.0     # P&L du jour (reset à minuit)
        self._daily_pnl_date: str = ""     # Date du dernier reset
        self._total_pnl: float   = 0.0     # P&L total

        # Cache
        self._token_cache: dict  = {}       # addr → {data, ts}
        self._cache_ttl: int     = 60       # secondes

        # ── PumpPortal WebSocket + Event-driven pipeline ──────
        self._ws_task            = None     # asyncio Task du listener WS
        self._ws_connected: bool = False
        self._ws_sol_price: float = 0.0     # Prix SOL/USD mis en cache
        self._ws_sol_price_ts: float = 0.0

        # Queue événementielle : WS → workers (remplace le buffer passif)
        self._token_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        # Nouvelle architecture : séparation polling / scoring
        self._pending_polls: dict        = {}   # addr → token (tous les tokens en attente de MC)
        self._score_queue: asyncio.Queue = asyncio.Queue(maxsize=500)  # tokens prêts à scorer
        self._poll_task                  = None
        # Pool de workers concurrents pour enrichissement + scoring
        self._worker_tasks: list = []
        self._n_workers: int     = int(os.getenv("PUMP_WORKERS", "8"))
        # TTL : ignorer un token si pas traité dans ce délai
        self._ws_buffer_ttl: int = int(os.getenv("PUMP_TOKEN_TTL", "900"))  # 15min défaut

        # Cache de prix temps réel alimenté par le WS (addr -> {price, ts})
        self._ws_price_cache: dict = {}
        # Tokens actuellement souscrits via subscribeTokenTrade
        self._ws_subscribed_positions: set = set()
        # Référence au websocket actif (pour envoyer des souscriptions dynamiques)
        self._ws_ref = None
        # Délai de polling MC pour attendre la zone cible (en secondes)
        self._mc_poll_interval: float = float(os.getenv("PUMP_MC_POLL", "15"))
        self._mc_poll_max: int        = int(os.getenv("PUMP_MC_POLL_MAX", "12"))  # 12 polls × 15s = ~3min
        # Abandon précoce : si MC < X% de mc_min après N polls, on abandonne
        self._mc_poll_abandon_pct: float = float(os.getenv("PUMP_MC_ABANDON_PCT", "0.4"))  # <40% de mc_min
        self._mc_poll_abandon_after: int = int(os.getenv("PUMP_MC_ABANDON_AFTER", "3"))  # après 3 polls

        # Tracker de volume par token (addr → volume USD cumulé depuis le WS)
        # Alimenté par chaque transaction vue sur le WS avant/pendant le polling
        self._vol_tracker: dict = {}   # addr → float (USD)

        # Stats événementielles
        self._stats_received:  int = 0   # tokens reçus du WS
        self._stats_processed: int = 0   # tokens traités (enrichis)
        self._stats_filtered:  int = 0   # tokens passant les filtres
        self._stats_scored:    int = 0   # tokens scorés via Claude
        self._stats_alerted:   int = 0   # alertes envoyées

        # Ancien buffer conservé pour /pumpstatus compat
        self._ws_buffer: list    = []    # juste pour affichage queue size

        self._load_state()

    # ── JARVIS Interface ─────────────────────────────────────────

    def set_send_callback(self, callback):
        """Injecté par bot.py pour envoyer des messages Telegram proactifs"""
        self._send_callback = callback

    async def handle(self, command: str, args: str, context) -> str:
        self._context = context

        routes = {
            "pumpstart":      self._cmd_start,
            "pumpstop":       self._cmd_stop,
            "pumpstatus":     self._cmd_status,
            "pumpfilters":    self._cmd_filters,
            "pumplog":        self._cmd_log,
            "pumpblacklist":  self._cmd_blacklist,
            "pumptest":       self._cmd_test,
            "pumpmode":       self._cmd_mode,
            "pumptrademode":  self._cmd_trademode,
            "pumptrades":     self._cmd_trades,
            "pumpclose":      self._cmd_close,
            "pumppaperreset": self._cmd_paper_reset,
            "pumpconfig":     self._cmd_config,
            "pumpscanlog":    self._cmd_scanlog,
        }
        handler = routes.get(command)
        if handler:
            try:
                return await handler(args.strip(), context)
            except Exception as e:
                logger.error("Erreur commande %s: %s", command, e)
                return "❌ Erreur : %s" % str(e)[:100]
        return "❓ Commande inconnue : /%s" % command

    # ══════════════════════════════════════════════════════════════
    #   COMMANDES TELEGRAM
    # ══════════════════════════════════════════════════════════════

    async def _cmd_start(self, args: str, ctx: SkillContext) -> str:
        if self._running:
            return "⚠️ Scanner déjà actif. `/pumpstop` pour arrêter."
        if not self.anthropic_key:
            return (
                "❌ **ANTHROPIC_API_KEY manquant**\n"
                "Configure ta clé dans `.env` pour le scoring Claude.\n"
                "Le scan peut quand même tourner (score désactivé)."
            )
        self._running        = True
        self._notify_user_id = ctx.user_id   # stocker pour alertes TP/SL proactives
        self._token_queue    = asyncio.Queue(maxsize=2000)
        self._pending_polls  = {}
        self._score_queue    = asyncio.Queue(maxsize=500)
        self._worker_tasks   = []
        self._ws_task        = asyncio.create_task(self._ws_listener(ctx))
        self._loop_task      = asyncio.create_task(self._main_loop(ctx))
        self._poll_task      = asyncio.create_task(self._poll_dispatcher(ctx))
        # Lancer le pool de workers événementiels (scoring uniquement)
        for i in range(self._n_workers):
            t = asyncio.create_task(self._token_worker(i, ctx))
            self._worker_tasks.append(t)
        trade_mode_str = {"paper": "📄 PAPER", "live": "💸 LIVE", "off": "🚫 OFF"}.get(self.trade_mode, "?")
        poll_max_min   = self._mc_poll_interval * self._mc_poll_max / 60
        return (
            "⚡ **Scanner Pump.fun démarré — Mode événementiel !**\n"
            "━━━━━━━━━━━━━━━\n"
            "🔌 %d workers | WS PumpPortal temps réel\n"
            "🎯 Zone MC: $%s–$%s | poll %.0fs × %d max (%.1fmin)\n"
            "📋 Vol >$%s | Holders >%d | Snipers <%d | Dev <%.0f%%\n"
            "🏷 Alerte score >%d | HC >%d | Trading: %s\n"
            "   Mise $%.0f/$%.0f | TP x%.1f/x%.1f/x%.1f | SL -%.0f%%\n"
            "⏱ TTL: %ds\n\n"
            "💡 Latence cible: **10-30s** après création du token\n"
            "`/pumpstatus` pour les stats temps réel"
            % (
                self._n_workers,
                self._fmt_k(self.mc_min), self._fmt_k(self.mc_max),
                self._mc_poll_interval, self._mc_poll_max, poll_max_min,
                self._fmt_k(self.volume_min), self.holders_min,
                self.snipers_max, self.dev_hold_max,
                self.score_alert, self.score_high, trade_mode_str,
                self.buy_amount, self.buy_amount_high,
                self.tp1, self.tp2, self.tp3, self.sl * 100,
                self._ws_buffer_ttl,
            )
        )

    async def _cmd_stop(self, args: str, ctx: SkillContext) -> str:
        if not self._running:
            return "⚠️ Scanner pas actif."
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        for t in self._worker_tasks:
            t.cancel()
        self._worker_tasks = []
        self._save_state()
        return (
            "🛑 **Scanner arrêté.**\n"
            "━━━━━━━━━━━━━━━\n"
            "📊 %d reçus → %d traités → %d filtrés → %d scorés → %d alertes\n"
            "💡 `/pumpstart` pour reprendre"
            % (self._stats_received, self._stats_processed,
               self._stats_filtered, self._stats_scored, self._stats_alerted)
        )

    async def _cmd_status(self, args: str, ctx: SkillContext) -> str:
        status    = "🟢 ACTIF" if self._running else "🔴 ARRÊTÉ"
        ws_status = "🟢 connecté" if self._ws_connected else ("🔄 démarrage…" if self._running else "⚫ arrêté")
        recent = self._alerts_log[-5:][::-1]

        lines = [
            "📡 **Pump.fun Scanner Status**\n━━━━━━━━━━━━━━━",
            "🔘 Scanner: %s | WebSocket: %s" % (status, ws_status),
            "⚡ Mode: événementiel | %d workers | TTL: %ds" % (self._n_workers, self._ws_buffer_ttl),
            "📥 En attente poll: %d | Prêts scoring: %d" % (len(self._pending_polls), self._score_queue.qsize()),
            "📊 Stats: %d reçus → %d traités → %d filtrés → %d scorés → %d alertes" % (
                self._stats_received, self._stats_processed,
                self._stats_filtered, self._stats_scored, self._stats_alerted),
            "🔄 Scans effectués: %d" % self._scan_count,
            "🚨 Alertes totales: %d" % len(self._alerts_log),
            "🚫 Tokens blacklistés: %d" % len(self._blacklist),
            "⏱ Prochaine alerte dans: %ds" % max(0, self.scan_interval - (int(time.time()) % self.scan_interval)),
            "",
            "📋 **Filtres actifs :**",
            "  💰 MC: $%s–$%s" % (self._fmt_k(self.mc_min), self._fmt_k(self.mc_max)),
            "  📈 Volume: >$%s" % self._fmt_k(self.volume_min),
            "  👥 Holders: >%d" % self.holders_min,
            "  🎯 Snipers: <%d | Dev: <%d%% | Top10: <%d%%" % (self.snipers_max, self.dev_hold_max, self.top10_max),
            "  🕐 Âge max: %gh" % self.age_max_hours,
        ]

        if recent:
            lines.append("\n🔔 **Dernières alertes :**")
            for a in recent:
                score = a.get("score", 0)
                icon  = "🔴" if score >= self.score_high else "🟡"
                lines.append(
                    "%s **%s** ($%s) — Score: **%d/100** | %s"
                    % (icon, a.get("symbol","?"), self._fmt_k(a.get("mc",0)),
                       score, a.get("ts","?"))
                )
        else:
            lines.append("\n📭 Aucune alerte pour l'instant.")

        return "\n".join(lines)

    async def _cmd_filters(self, args: str, ctx: SkillContext) -> str:
        """Voir ou modifier un filtre : /pumpfilters mc_min 8000"""
        if not args:
            return (
                "🎛 **Filtres Pump.fun actuels :**\n"
                "━━━━━━━━━━━━━━━\n"
                "`mc_min`        = $%s\n"
                "`mc_max`        = $%s\n"
                "`volume_min`    = $%s\n"
                "`holders_min`   = %d\n"
                "`snipers_max`   = %d\n"
                "`dev_hold_max`  = %d%%\n"
                "`top10_max`     = %d%%\n"
                "`age_max_hours` = %.1fh\n"
                "`score_alert`   = %d/100\n"
                "`score_high`    = %d/100\n\n"
                "Pour modifier : `/pumpfilters <clé> <valeur>`\n"
                "Ex: `/pumpfilters mc_min 8000`"
                % (
                    self._fmt_k(self.mc_min), self._fmt_k(self.mc_max),
                    self._fmt_k(self.volume_min), self.holders_min,
                    self.snipers_max, self.dev_hold_max, self.top10_max,
                    self.age_max_hours, self.score_alert, self.score_high,
                )
            )

        parts = args.split()
        if len(parts) != 2:
            return "Usage: `/pumpfilters <clé> <valeur>`"

        key, val_str = parts
        filter_map = {
            "mc_min":        ("mc_min",        float),
            "mc_max":        ("mc_max",        float),
            "volume_min":    ("volume_min",    float),
            "holders_min":   ("holders_min",   int),
            "snipers_max":   ("snipers_max",   int),
            "dev_hold_max":  ("dev_hold_max",  float),
            "top10_max":     ("top10_max",     float),
            "age_max_hours": ("age_max_hours", float),
            "score_alert":   ("score_alert",   int),
            "score_high":    ("score_high",    int),
        }
        if key not in filter_map:
            return "❌ Filtre inconnu. Utilise `/pumpfilters` pour voir les clés disponibles."

        attr, cast = filter_map[key]
        try:
            setattr(self, attr, cast(val_str))
            self._save_state()
            return "✅ **%s** mis à jour → `%s`" % (key, val_str)
        except ValueError:
            return "❌ Valeur invalide : `%s`" % val_str

    async def _cmd_log(self, args: str, ctx: SkillContext) -> str:
        if not self._alerts_log:
            return "📭 Aucune alerte pour l'instant.\n\n`/pumpstart` pour lancer le scanner."

        lines = ["🔔 **Alertes Pump.fun (20 dernières)**\n━━━━━━━━━━━━━━━"]
        for a in self._alerts_log[-20:][::-1]:
            score    = a.get("score", 0)
            icon     = "🔴" if score >= self.score_high else "🟡"
            symbol   = a.get("symbol", "?")
            mc       = self._fmt_k(a.get("mc", 0))
            vol      = self._fmt_k(a.get("volume", 0))
            holders  = a.get("holders", 0)
            ts       = a.get("ts", "?")
            verdict  = a.get("verdict", "")
            addr     = a.get("address", "")
            link     = "https://pump.fun/%s" % addr if addr else ""

            lines.append(
                "%s **%s** | $%s MC | $%s vol | %d holders\n"
                "   Score: **%d/100** | %s\n"
                "   %s | _%s_"
                % (icon, symbol, mc, vol, holders, score, ts, link, verdict[:60])
            )

        return "\n\n".join(lines)

    async def _cmd_blacklist(self, args: str, ctx: SkillContext) -> str:
        """Gérer la blacklist : /pumpblacklist add <addr> | remove <addr> | list"""
        parts = args.split()
        if not parts or parts[0] == "list":
            if not self._blacklist:
                return "🚫 Blacklist vide."
            lines = ["🚫 **Tokens blacklistés (%d) :**" % len(self._blacklist)]
            for addr in list(self._blacklist)[:20]:
                lines.append("  `%s`" % addr)
            return "\n".join(lines)

        if parts[0] == "add" and len(parts) >= 2:
            addr = parts[1].strip()
            self._blacklist.add(addr)
            self._seen_tokens.add(addr)
            self._save_state()
            return "✅ `%s` ajouté à la blacklist." % addr

        if parts[0] == "remove" and len(parts) >= 2:
            addr = parts[1].strip()
            self._blacklist.discard(addr)
            self._save_state()
            return "✅ `%s` retiré de la blacklist." % addr

        return "Usage: `/pumpblacklist list|add <addr>|remove <addr>`"

    async def _cmd_test(self, args: str, ctx: SkillContext) -> str:
        """Analyser manuellement un token par son adresse"""
        addr = args.strip()
        if not addr:
            return "Usage: `/pumptest <adresse_token>`"

        await self._notify("🔍 Analyse de `%s`…" % addr[:20])

        # Récupérer les données
        token_data = await self._fetch_token_data(addr)
        if not token_data:
            return "❌ Impossible de récupérer les données pour `%s`\n\nVérifie l'adresse." % addr

        # Afficher les données brutes
        mc      = token_data.get("market_cap", 0)
        vol     = token_data.get("volume_24h", 0)
        holders = token_data.get("holders", 0)
        symbol  = token_data.get("symbol", "?")
        name    = token_data.get("name", "?")

        raw_info = (
            "📊 **%s** (%s)\n"
            "━━━━━━━━━━━━━━━\n"
            "💰 MC: $%s | Vol: $%s\n"
            "👥 Holders: %d | Snipers: %s\n"
            "🧑‍💻 Dev: %s%% | Top10: %s%%\n"
            "🕐 Âge: %s\n"
        ) % (
            name, symbol,
            self._fmt_k(mc), self._fmt_k(vol),
            holders, token_data.get("snipers", "?"),
            token_data.get("dev_holding_pct", "?"),
            token_data.get("top10_pct", "?"),
            token_data.get("age_str", "?"),
        )
        await self._notify(raw_info)

        # Scoring Claude
        if self.anthropic_key:
            result = await self._score_with_claude(token_data)
            score  = result.get("score", 0)
            return await self._format_alert(token_data, result, force=True)
        else:
            return raw_info + "\n⚠️ ANTHROPIC_API_KEY non configurée → scoring désactivé"

    async def _cmd_mode(self, args: str, ctx: SkillContext) -> str:
        """Changer la vitesse de scan"""
        modes = {
            "fast":   60,
            "normal": 120,
            "deep":   300,
        }
        mode = args.strip().lower()
        if mode not in modes:
            return (
                "Usage: `/pumpmode fast|normal|deep`\n"
                "  `fast`   → scan toutes les 60s (plus d'alertes, plus d'appels API)\n"
                "  `normal` → 120s (défaut)\n"
                "  `deep`   → 300s (analyse approfondie, moins d'appels)"
            )
        self.scan_interval = modes[mode]
        self._save_state()
        return "✅ Mode **%s** activé — scan toutes les **%ds**" % (mode, modes[mode])

    # ══════════════════════════════════════════════════════════════
    #   BOUCLE PRINCIPALE
    # ══════════════════════════════════════════════════════════════

    async def _main_loop(self, ctx: SkillContext):
        """
        Boucle principale :
        - Surveiller les positions ouvertes (TP/SL) toutes les 5s
          -> utilise le cache de prix WS si dispo, sinon appel API
        - Reset P&L journalier toutes les 60s
        - Synchroniser les souscriptions WS aux tokens en position
        Le traitement des tokens est gere par les workers evenementiels.
        """
        self._context = ctx
        logger.info("Main loop demarree — monitoring positions toutes les 5s")

        _last_daily_check = 0

        while self._running:
            try:
                await asyncio.sleep(5)

                if self._positions and self.trade_mode != "off":
                    await self._sync_ws_subscriptions()
                    await self._monitor_positions()

                now = time.time()
                if now - _last_daily_check > 60:
                    self._check_daily_reset()
                    _last_daily_check = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Erreur main loop: %s", e)
                await asyncio.sleep(5)

        logger.info("Main loop arretee")

    async def _ws_listener(self, ctx: SkillContext):
        """
        WebSocket PumpPortal — réception événementielle.
        Chaque token créé est immédiatement poussé dans _token_queue
        pour traitement par les workers concurrents.
        """
        RECONNECT_DELAY = 5

        if websockets is None:
            logger.error("websockets non installé — pip install websockets --break-system-packages")
            await self._notify("❌ `pip install websockets --break-system-packages`")
            return

        while self._running:
            try:
                logger.info("Connexion WebSocket PumpPortal → %s", PUMPPORTAL_WS)
                async with websockets.connect(
                    PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws_connected = True
                    self._ws_ref = ws
                    logger.info("✅ WebSocket PumpPortal connecté — mode événementiel")
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    # Pas de subscribeTokenTrade global — trop de messages
                    # Le volume est tracké via les messages "buy"/"sell" reçus naturellement

                    # Re-souscrire aux positions ouvertes après reconnexion
                    if self._positions:
                        addrs = list(self._positions.keys())
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": addrs}))
                        self._ws_subscribed_positions = set(addrs)
                        logger.info("Re-souscription WS pour %d positions", len(addrs))

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        tx_type = msg.get("txType", "")
                        mint    = msg.get("mint", "")

                        # Tracker le volume sur TOUS les trades (pas seulement create)
                        if mint and tx_type in ("create", "buy", "sell"):
                            sol_amt = float(msg.get("solAmount", 0) or 0)
                            if sol_amt > 0:
                                sol_px_vol            = await self._get_sol_price()
                                vol_usd               = sol_amt * sol_px_vol
                                self._vol_tracker[mint] = self._vol_tracker.get(mint, 0) + vol_usd

                            # Mettre à jour le cache de prix pour les positions ouvertes
                            if mint in self._positions:
                                mc_sol = float(msg.get("marketCapSol", 0) or 0)
                                if mc_sol > 0:
                                    sol_px = await self._get_sol_price()
                                    price  = (mc_sol * sol_px) / 1_000_000_000
                                    self._ws_price_cache[mint] = {"price": price, "ts": time.time()}

                        if tx_type != "create":
                            continue

                        if not mint or mint in self._blacklist:
                            continue
                        if mint in self._seen_tokens:
                            continue

                        sol_px = await self._get_sol_price()
                        mc_sol = float(msg.get("marketCapSol", 0) or 0)

                        token = {
                            "address":           mint,
                            "symbol":            msg.get("symbol", "?"),
                            "name":              msg.get("name", "?"),
                            "market_cap":        mc_sol * sol_px,
                            "mc_sol":            mc_sol,
                            "volume_24h":        float(msg.get("solAmount", 0) or 0) * sol_px,
                            "holders":           0,
                            "created_ts":        int(time.time()),
                            "age_hours":         0.0,
                            "age_str":           "< 1min",
                            "uri":               msg.get("uri", ""),
                            "bonding_curve_key": msg.get("bondingCurveKey", ""),
                            "v_sol_bonding":     float(msg.get("vSolInBondingCurve", 0) or 0),
                            "trader_key":        msg.get("traderPublicKey", ""),
                            "_source":           "pumpportal_ws",
                            "_queued_at":        time.time(),
                        }

                        self._stats_received += 1
                        try:
                            self._token_queue.put_nowait(token)
                            logger.info("Queue ← %s (%s) $%.0f | qsize=%d",
                                        token["symbol"], mint[:8],
                                        token["market_cap"], self._token_queue.qsize())
                        except asyncio.QueueFull:
                            logger.warning("Queue PLEINE (%d), token %s dropé",
                                           self._token_queue.maxsize, mint[:8])
                        except Exception as e:
                            logger.error("put_nowait ERREUR %s: %s", mint[:8], e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._ws_connected = False
                self._ws_ref = None
                self._ws_subscribed_positions = set()
                logger.warning("WS déconnecté: %s — reconnexion dans %ds", e, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

        self._ws_connected = False
        logger.info("WebSocket PumpPortal arrêté")

    async def _get_sol_price(self) -> float:
        """Prix SOL/USD mis en cache 5 minutes."""
        if (self._ws_sol_price > 0
                and time.time() - self._ws_sol_price_ts < 300):
            return self._ws_sol_price
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "%s/simple/price" % COINGECKO_API,
                    params={"ids": "solana", "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as r:
                    if r.status == 200:
                        data  = await r.json(content_type=None)
                        price = float(data.get("solana", {}).get("usd", 0) or 0)
                        if price > 0:
                            self._ws_sol_price    = price
                            self._ws_sol_price_ts = time.time()
                            return price
        except Exception as e:
            logger.debug("get_sol_price: %s", e)
        return self._ws_sol_price or 150.0

    async def _token_worker(self, worker_id: int, ctx: SkillContext):
        """
        Worker de scoring — nouvelle architecture découplée.

        Pioche dans _score_queue les tokens déjà dans la zone MC,
        puis enrichit, filtre et score. Plus de polling bloquant.
        Le polling est géré par _poll_dispatcher (tâche unique partagée).
        """
        self._context = ctx
        logger.info("W#%d démarré — scoring worker", worker_id)

        while self._running:
            try:
                try:
                    token_data = await asyncio.wait_for(self._score_queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                mint   = token_data.get("address", "")
                symbol = token_data.get("symbol", "?")

                self._stats_processed += 1

                # ── 3. Enrichissement complet ─────────────────────
                enriched = await self._enrich_token(token_data)
                if not enriched:
                    self._token_queue.task_done()
                    continue

                # ── 4. Filtres stricts ────────────────────────────
                ok, reason = self._apply_filters(enriched)

                if ok is None:
                    # Rejet temporaire (volume insuffisant) — requeue dans 60s
                    retry_count = enriched.get("_retry_count", 0) + 1
                    max_retries = 4  # max 4 retries = 4min supplémentaires
                    age_total   = time.time() - enriched.get("_queued_at", time.time())

                    if retry_count <= max_retries and age_total < self._ws_buffer_ttl:
                        enriched["_retry_count"] = retry_count
                        enriched["_retry_after"] = time.time() + 60
                        # Retirer de _seen_tokens pour permettre le retraitement
                        self._seen_tokens.discard(mint)
                        logger.info("W#%d RETRY %d/%d dans 60s: %s — %s",
                                    worker_id, retry_count, max_retries, symbol, reason)
                        await asyncio.sleep(60)
                        try:
                            self._token_queue.put_nowait(enriched)
                        except asyncio.QueueFull:
                            logger.warning("W#%d RETRY drop (queue pleine): %s", worker_id, symbol)
                    else:
                        logger.info("W#%d filtre définitif (max retries/TTL): %s — %s",
                                    worker_id, symbol, reason)
                    self._token_queue.task_done()
                    continue

                if not ok:
                    logger.info("W#%d filtre: %s — %s", worker_id, symbol, reason)
                    self._token_queue.task_done()
                    continue

                self._stats_filtered += 1
                logger.info("W#%d ✅ CANDIDAT: %s $%.0f %dh %ds",
                            worker_id, symbol,
                            enriched.get("market_cap", 0),
                            enriched.get("holders", 0),
                            enriched.get("snipers", 0))

                # ── 5. Scoring Claude ─────────────────────────────
                result = await self._score_with_claude(enriched)
                score  = result.get("score", 0)
                self._stats_scored += 1

                # Enregistrer dans scan_log (format compatible)
                self._scan_count += 1
                self._scan_log.append({
                    "num":        self._scan_count,
                    "ts":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "elapsed_s":  round(time.time() - token.get("_queued_at", time.time()), 1),
                    "buf_tokens": 1,
                    "candidates": 1,
                    "scored":     [{
                        "symbol":  enriched.get("symbol", "?"),
                        "address": mint,
                        "mc":      enriched.get("market_cap", 0),
                        "holders": enriched.get("holders", 0),
                        "score":   score,
                        "verdict": result.get("verdict", ""),
                        "rec":     result.get("recommendation", ""),
                        "alerted": score >= self.score_alert,
                    }],
                    "ws": self._ws_connected,
                    "worker": worker_id,
                })
                if len(self._scan_log) > 200:
                    self._scan_log = self._scan_log[-200:]

                # ── 6. Alerte ─────────────────────────────────────
                if score >= self.score_alert:
                    self._stats_alerted += 1
                    await self._send_alert(enriched, result)

                self._score_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("W#%d erreur: %s", worker_id, e)
                try:
                    self._score_queue.task_done()
                except Exception:
                    pass

        logger.info("Worker #%d arrêté", worker_id)

    async def _poll_dispatcher(self, ctx: SkillContext):
        """
        Tâche unique de polling MC — remplace le modèle 1-worker-1-token.

        Toutes les PUMP_MC_POLL secondes :
        - Dépile les tokens de _token_queue → _pending_polls
        - Poll le MC de TOUS les tokens en attente en parallèle
        - Ceux qui entrent dans la zone MC → poussés dans _score_queue
        - Ceux qui expirent (TTL) ou stagnent → supprimés

        Avantage : 1 seule tâche gère N tokens simultanément,
        le nombre de workers ne limite plus le débit de détection.
        """
        logger.info("Poll dispatcher démarré")
        while self._running:
            try:
                # Absorber tous les tokens disponibles dans _token_queue
                while not self._token_queue.empty():
                    try:
                        token = self._token_queue.get_nowait()
                        mint  = token.get("address", "")
                        if not mint or mint in self._seen_tokens or mint in self._pending_polls:
                            self._token_queue.task_done()
                            continue
                        age = time.time() - token.get("_queued_at", time.time())
                        if age > self._ws_buffer_ttl:
                            self._token_queue.task_done()
                            continue
                        self._seen_tokens.add(mint)
                        self._pending_polls[mint] = token
                        self._token_queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                if not self._pending_polls:
                    await asyncio.sleep(self._mc_poll_interval)
                    continue

                # Purger les tokens expirés (TTL)
                now = time.time()
                expired = [
                    addr for addr, tok in self._pending_polls.items()
                    if now - tok.get("_queued_at", now) > self._ws_buffer_ttl
                ]
                for addr in expired:
                    del self._pending_polls[addr]
                    logger.debug("Dispatcher TTL expiré: %s", addr[:8])

                # Poll MC en parallèle sur tous les tokens en attente
                addrs = list(self._pending_polls.keys())
                logger.info("Dispatcher: %d tokens en poll | score_q=%d",
                            len(addrs), self._score_queue.qsize())

                async def _check_one(addr):
                    token = self._pending_polls.get(addr)
                    if not token:
                        return
                    try:
                        pf = await self._fetch_pumpfun_coin(addr)
                        if not pf:
                            return
                        mc_usd = float(pf.get("usd_market_cap", 0) or 0)
                        token["market_cap"] = mc_usd
                        symbol = token.get("symbol", "?")

                        if mc_usd > self.mc_max * 2:
                            # Déjà trop haut, drop
                            del self._pending_polls[addr]
                            logger.info("Dispatcher ABANDON trop haut: %s $%.0f", symbol, mc_usd)
                            return

                        # Abandon stagnation : MC < 30% de mc_min après plusieurs polls
                        polls_done = token.get("_poll_count", 0) + 1
                        token["_poll_count"] = polls_done
                        self._pending_polls[addr] = token
                        if polls_done >= self._mc_poll_max and mc_usd < self.mc_min * 0.30:
                            del self._pending_polls[addr]
                            logger.info("Dispatcher ABANDON stagnation: %s $%.0f", symbol, mc_usd)
                            return

                        if self.mc_min <= mc_usd <= self.mc_max:
                            # Zone atteinte → enrichissement rapide et push vers scoring
                            sol_px  = await self._get_sol_price()
                            ws_vol  = self._vol_tracker.get(addr, 0)
                            moralis = await self._fetch_moralis_data(addr)
                            moralis_vol     = moralis.get("volume_24h", 0)
                            moralis_holders = moralis.get("holders", 0)
                            moralis_liq     = moralis.get("liquidity", 0)
                            best_vol        = moralis_vol if moralis_vol > 0 else ws_vol
                            if moralis_holders > 0:
                                pf["holder_count"] = moralis_holders
                            if moralis_liq > 0:
                                token["liquidity"] = moralis_liq
                            elapsed_s = time.time() - token.get("_queued_at", time.time())
                            token.update({
                                "holders":           int(pf.get("holder_count", 0) or 0),
                                "snipers":           int(pf.get("sniper_count", pf.get("bot_holder_count", 0)) or 0),
                                "dev_holding_pct":   float(pf.get("creator_percentage", 0) or 0),
                                "top10_pct":         float(pf.get("top10_pct", 0) or 0),
                                "reply_count":       int(pf.get("reply_count", 0) or 0),
                                "bonding_curve_pct": float(pf.get("bonding_curve_percentage", 0) or 0),
                                "description":       pf.get("description", token.get("description", "")),
                                "twitter":           pf.get("twitter", ""),
                                "telegram":          pf.get("telegram", ""),
                                "website":           pf.get("website", ""),
                                "volume_24h":        best_vol,
                                "real_sol_reserves": float(pf.get("real_sol_reserves", 0) or 0),
                                "_source":           "poll_dispatcher",
                            })
                            logger.info("Dispatcher ZONE ATTEINTE: %s $%.0f après %d polls (%.0fs)",
                                        symbol, mc_usd, polls_done, elapsed_s)
                            if self.notify_mc_zone:
                                bc_pct    = float(pf.get("bonding_curve_percentage", 0) or 0)
                                age_str   = self._fmt_duration(int(elapsed_s))
                                await self._notify(
                                    "👀 **Zone MC** — `%s`\n"
                                    "💰 MC: **$%s** | Holders: %d | BC: %.0f%%\n"
                                    "⏱ %s après création\n"
                                    "🔍 Analyse en cours...\n"
                                    "`%s`" % (
                                        symbol,
                                        self._fmt_k(mc_usd),
                                        int(pf.get("holder_count", 0) or 0),
                                        bc_pct, age_str, addr,
                                    )
                                )
                            del self._pending_polls[addr]
                            try:
                                self._score_queue.put_nowait(token)
                            except asyncio.QueueFull:
                                logger.warning("Dispatcher: score_queue pleine, token %s droppé", symbol)

                    except Exception as e:
                        logger.debug("Dispatcher check %s: %s", addr[:8], e)

                # Lancer tous les checks en parallèle
                await asyncio.gather(*[_check_one(addr) for addr in addrs], return_exceptions=True)
                await asyncio.sleep(self._mc_poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Erreur poll dispatcher: %s", e)
                await asyncio.sleep(5)

        logger.info("Poll dispatcher arrêté")

    async def _poll_until_mc_zone(self, token: dict, worker_id: int) -> Optional[dict]:
        """
        Interroge Pump.fun toutes les N secondes jusqu'à ce que le MC
        entre dans la zone cible [mc_min, mc_max], ou abandon après max polls.

        Retourne le token enrichi avec le MC actuel, ou None si hors zone.
        """
        mint   = token.get("address", "")
        symbol = token.get("symbol", "?")

        logger.info("W#%d POLL START %s | zone $%.0f-$%.0f | max %d polls x %.0fs",
                    worker_id, symbol, self.mc_min, self.mc_max,
                    self._mc_poll_max, self._mc_poll_interval)

        for poll_n in range(self._mc_poll_max):
            # Vérifier TTL global
            age = time.time() - token.get("_queued_at", time.time())
            if age > self._ws_buffer_ttl:
                logger.info("W#%d POLL TTL expiré %s (%.0fs > %ds)",
                            worker_id, symbol, age, self._ws_buffer_ttl)
                return None

            # Récupérer le MC actuel depuis Pump.fun
            pf = await self._fetch_pumpfun_coin(mint)
            if pf:
                mc_usd = float(pf.get("usd_market_cap", 0) or 0)
                token["market_cap"] = mc_usd
                logger.info("W#%d poll[%d/%d] %s MC=$%.0f (zone $%.0f-$%.0f)",
                            worker_id, poll_n + 1, self._mc_poll_max, symbol,
                            mc_usd, self.mc_min, self.mc_max)

                if self.mc_min <= mc_usd <= self.mc_max:
                    sol_px  = await self._get_sol_price()
                    # Pump.fun API ne retourne pas de volume — utiliser Moralis
                    ws_vol      = self._vol_tracker.get(mint, 0)
                    moralis     = await self._fetch_moralis_data(mint)
                    moralis_vol = moralis.get("volume_24h", 0)
                    moralis_liq = moralis.get("liquidity", 0)
                    moralis_holders = moralis.get("holders", 0)

                    # Volume : Moralis > WS tracker
                    best_vol = moralis_vol if moralis_vol > 0 else ws_vol

                    # Holders : Moralis (fiable) > pf.get (toujours 0)
                    if moralis_holders > 0:
                        pf["holder_count"] = moralis_holders

                    # Injecter liquidité + variation holders dans le token
                    if moralis_liq > 0:
                        token["liquidity"] = moralis_liq
                    if moralis.get("holders_change_1h") is not None:
                        token["holders_change_1h"]  = moralis["holders_change_1h"]
                        token["holders_change_24h"] = moralis.get("holders_change_24h", 0)
                    token.update({
                        "holders":           int(pf.get("holder_count", 0) or 0),
                        "snipers":           int(pf.get("sniper_count", pf.get("bot_holder_count", 0)) or 0),
                        "dev_holding_pct":   float(pf.get("creator_percentage", 0) or 0),
                        "top10_pct":         float(pf.get("top10_pct", 0) or 0),
                        "reply_count":       int(pf.get("reply_count", 0) or 0),
                        "bonding_curve_pct": float(pf.get("bonding_curve_percentage", 0) or 0),
                        "description":       pf.get("description", token.get("description", "")),
                        "twitter":           pf.get("twitter", ""),
                        "telegram":          pf.get("telegram", ""),
                        "website":           pf.get("website", ""),
                        "volume_24h":        best_vol,   # depuis Moralis ou WS tracker
                        "real_sol_reserves": float(pf.get("real_sol_reserves", 0) or 0),
                        "virtual_sol_reserves": float(pf.get("virtual_sol_reserves", 0) or 0),
                        "_source":           "pumpportal_ws",
                    })
                    elapsed_s = time.time() - token["_queued_at"]
                    logger.info("W#%d ✅ ZONE ATTEINTE: %s $%.0f après %d polls (%.0fs)",
                                worker_id, symbol, mc_usd, poll_n + 1, elapsed_s)

                    # Alerte Telegram immédiate dès l'entrée en zone MC
                    if self.notify_mc_zone:
                        try:
                            holders_now = int(pf.get("holder_count", 0) or 0)
                            bc_pct      = float(pf.get("bonding_curve_percentage", 0) or 0)
                            age_str     = self._fmt_duration(int(elapsed_s))
                            addr_short  = token.get("address", "")[:20]
                            notif_lines = [
                                "👀 **Zone MC** — `%s`" % symbol,
                                "💰 MC: **$%s** | Holders: %d | BC: %.0f%%" % (
                                    self._fmt_k(mc_usd), holders_now, bc_pct),
                                "⏱ %s apres creation" % age_str,
                                "🔍 Analyse en cours...",
                                "`%s`" % token.get("address", ""),
                            ]
                            msg = "\n".join(notif_lines)
                            await self._notify(msg)
                        except Exception as e:
                            logger.error("W#%d notif zone MC erreur: %s", worker_id, e)

                    return token

                elif mc_usd > self.mc_max * 2:
                    logger.info("W#%d ABANDON trop haut: %s $%.0f > $%.0f",
                                worker_id, symbol, mc_usd, self.mc_max * 2)
                    return None

                # Abandon précoce : stagne trop bas trop longtemps
                elif poll_n >= self._mc_poll_abandon_after and mc_usd < self.mc_min * self._mc_poll_abandon_pct:
                    logger.info("W#%d ABANDON stagnation: %s $%.0f < $%.0f après %d polls",
                                worker_id, symbol, mc_usd,
                                self.mc_min * self._mc_poll_abandon_pct, poll_n + 1)
                    return None

            else:
                logger.info("W#%d poll[%d/%d] %s — API Pump.fun indisponible",
                            worker_id, poll_n + 1, self._mc_poll_max, symbol)

            await asyncio.sleep(self._mc_poll_interval)

        logger.info("W#%d ABANDON max polls atteint: %s (%d polls sans zone)",
                    worker_id, symbol, self._mc_poll_max)
        return None


    async def _run_scan(self) -> list:
        """Obsolète en mode événementiel — conservé pour compatibilité /pumptest"""
        return []


    async def _fetch_pump_tokens(self) -> list:
        """Obsolète en mode événementiel — conservé pour compatibilité"""
        return []


    def _normalize_pumpfun_coin(self, c: dict) -> Optional[dict]:
        """Normalise un coin Pump.fun en format interne standard"""
        addr = c.get("mint", c.get("address", ""))
        if not addr:
            return None
        mc = float(c.get("usd_market_cap", c.get("market_cap", 0)) or 0)
        return {
            "address":       addr,
            "symbol":        c.get("symbol", "?"),
            "name":          c.get("name", "?"),
            "market_cap":    mc,
            "volume_24h":    float(c.get("volume", 0) or 0),
            "holders":       int(c.get("holder_count", 0) or 0),
            "snipers":       int(c.get("sniper_count", c.get("bot_holder_count", 0)) or 0),
            "dev_holding_pct": float(c.get("creator_percentage", c.get("dev_holding_pct", 0)) or 0),
            "top10_pct":     float(c.get("top10_pct", 0) or 0),
            "reply_count":   int(c.get("reply_count", 0) or 0),
            "bonding_curve_pct": float(c.get("bonding_curve_percentage", 0) or 0),
            "description":   c.get("description", ""),
            "image_uri":     c.get("image_uri", ""),
            "twitter":       c.get("twitter", ""),
            "telegram":      c.get("telegram", ""),
            "website":       c.get("website", ""),
            "created_ts":    int(c.get("created_timestamp", 0) or 0),
            "last_trade_ts": int(c.get("last_trade_timestamp", 0) or 0),
            "_source":       "pumpfun",
        }

    def _normalize_gmgn_pair(self, p: dict) -> Optional[dict]:
        """Normalise un pair GMGN en format interne standard"""
        addr = p.get("address", p.get("token_address", p.get("base_address", "")))
        if not addr:
            return None
        return {
            "address":    addr,
            "symbol":     p.get("symbol", "?"),
            "name":       p.get("name", "?"),
            "market_cap": float(p.get("market_cap", p.get("usd_market_cap", 0)) or 0),
            "volume_24h": float(p.get("volume", p.get("volume_24h", 0)) or 0),
            "holders":    int(p.get("holder_count", p.get("holder", 0)) or 0),
            "snipers":    int(p.get("sniper_count", 0) or 0),
            "dev_holding_pct": float(p.get("dev_hold", p.get("creator_percentage", 0)) or 0),
            "top10_pct":  float(p.get("top10_holder_rate", 0) or 0) * 100,
            "created_ts": int(p.get("open_timestamp", p.get("created_timestamp", 0)) or 0),
            "description": p.get("description", ""),
            "twitter":    p.get("twitter", ""),
            "telegram":   p.get("telegram", ""),
            "website":    p.get("website", ""),
            "_source":    "gmgn",
        }

    def _apply_filters(self, t: dict) -> tuple:
        """
        Applique les filtres Kabuki adaptés au mode événementiel.
        Retourne (True, "") si OK, (False, raison) si rejeté définitivement,
        ou (None, raison) si le token doit être retenté plus tard (ex: volume trop bas).
        """
        mc      = float(t.get("market_cap", 0) or 0)
        vol     = float(t.get("volume_24h", 0) or 0)
        holders = int(t.get("holders", 0) or 0)
        snipers = int(t.get("snipers", 0) or 0)
        dev_pct = float(t.get("dev_holding_pct", 0) or 0)
        top10   = float(t.get("top10_pct", 0) or 0)
        age_h   = float(t.get("age_hours", 0) or 0)
        # Temps depuis que le token a été mis dans la queue (en minutes)
        age_queued_min = (time.time() - t.get("_queued_at", time.time())) / 60

        # ── MC dans la zone ───────────────────────────────────
        if not (self.mc_min <= mc <= self.mc_max):
            return False, "MC $%.0f hors zone $%.0f-$%.0f" % (mc, self.mc_min, self.mc_max)

        # ── Volume — adapté à l'âge ───────────────────────────
        # Tokens < 10min : seuil réduit à 20% (les échanges s'accumulent)
        # Tokens 10-30min : seuil réduit à 50%
        # Tokens > 30min : seuil plein
        if age_queued_min < 10:
            vol_threshold = self.volume_min * 0.20
        elif age_queued_min < 30:
            vol_threshold = self.volume_min * 0.50
        else:
            vol_threshold = self.volume_min

        # Si volume = 0, essayer le vol WS comme dernier recours
        if vol == 0:
            ws_vol = self._vol_tracker.get(t.get("address", ""), 0)
            if ws_vol > 0:
                vol = ws_vol
                logger.info("filtre: vol WS fallback %s $%.0f", t.get("symbol","?"), ws_vol)

        if vol < vol_threshold:
            # None = pas définitif, le volume peut encore monter → retry
            return None, "Volume $%.0f < $%.0f (âge %.0fmin, seuil %.0f%%)" % (
                vol, vol_threshold, age_queued_min,
                vol_threshold / self.volume_min * 100)

        # ── Holders ───────────────────────────────────────────
        # Seuil réduit pour tokens très frais (< 5min)
        holders_threshold = max(20, self.holders_min) if age_queued_min < 5 else self.holders_min
        if holders < holders_threshold:
            return False, "Holders %d < %d" % (holders, holders_threshold)

        # ── Sécurité ──────────────────────────────────────────
        if snipers > self.snipers_max:
            return False, "Snipers %d > %d" % (snipers, self.snipers_max)
        if dev_pct > self.dev_hold_max:
            return False, "Dev %.1f%% > %.1f%%" % (dev_pct, self.dev_hold_max)
        if top10 > self.top10_max:
            return False, "Top10 %.1f%% > %.1f%%" % (top10, self.top10_max)
        if age_h > self.age_max_hours:
            return False, "Âge %.1fh > %.1fh" % (age_h, self.age_max_hours)

        # ── Anti wash-trading ─────────────────────────────────
        if holders > 0 and vol / holders > 5000:
            return False, "Wash trading suspect (vol/holder=$%.0f)" % (vol / holders)

        return True, ""

    async def _enrich_token(self, token: dict) -> Optional[dict]:
        """
        Enrichit un token avec les données manquantes :
        - Holders count (si manquant)
        - Snipers count
        - Dev holding %
        - Top 10 holders %
        - Age en heures
        - RugCheck score
        """
        addr = token.get("address", "")
        if not addr:
            return None

        # Vérifier le cache
        cached = self._token_cache.get(addr)
        if cached and time.time() - cached["ts"] < self._cache_ttl:
            return cached["data"]

        t = dict(token)  # Copie

        now = int(time.time())

        # ── Âge du token ─────────────────────────────────────
        created_ts = t.get("created_ts", 0)
        if created_ts:
            age_s = now - created_ts
            t["age_hours"] = age_s / 3600
            t["age_str"]   = self._fmt_duration(age_s)
        else:
            t["age_hours"] = 0
            t["age_str"]   = "?"

        # ── Pour les tokens WS, rafraîchir le MC depuis Pump.fun ─
        # Le marketCapSol reçu = MC à la CRÉATION (~$2-3K)
        # On veut le MC ACTUEL pour les filtres ($10K-$20K sweet spot)
        if t.get("_source") == "pumpportal_ws":
            pf_data = await self._fetch_pumpfun_coin(t["address"])
            if pf_data:
                mc_fresh = float(pf_data.get("usd_market_cap", 0) or 0)
                if mc_fresh > 0:
                    t["market_cap"] = mc_fresh
                    logger.debug("MC rafraîchi %s: $%.0f → $%.0f",
                                 t["address"][:8], t.get("mc_sol", 0) * 150, mc_fresh)

        # ── Données Pump.fun spécifiques ─────────────────────
        if not t.get("holders") or t.get("_source") != "pumpfun":
            pf_data = await self._fetch_pumpfun_coin(addr)
            if pf_data:
                t.update({
                    "holders":        pf_data.get("holder_count", t.get("holders", 0)),
                    "snipers":        pf_data.get("sniper_count", pf_data.get("bot_holder_count", 0)),
                    "dev_holding_pct": pf_data.get("dev_holding_pct", pf_data.get("creator_percentage", 0)),
                    "top10_pct":      pf_data.get("top10_pct", 0),
                    "reply_count":    pf_data.get("reply_count", 0),
                    "description":    pf_data.get("description", t.get("description", "")),
                    "twitter":        pf_data.get("twitter", t.get("twitter", "")),
                    "telegram":       pf_data.get("telegram", t.get("telegram", "")),
                    "website":        pf_data.get("website", t.get("website", "")),
                    "bonding_curve_pct": pf_data.get("bonding_curve_percentage", 0),
                    "king_of_hill_ts": pf_data.get("king_of_hill_timestamp", 0),
                    "market_cap":     pf_data.get("usd_market_cap", t.get("market_cap", 0)),
                })

        # ── RugCheck score ────────────────────────────────────
        if self.rugcheck_enabled:
            rug = await self._fetch_rugcheck(addr)
            if rug:
                t["rugcheck_score"] = rug.get("score", 0)
                t["rugcheck_risks"]  = rug.get("risks", [])
            else:
                t["rugcheck_score"] = None
                t["rugcheck_risks"]  = []

        # Defaults sécurisés
        t.setdefault("snipers", 0)
        t.setdefault("dev_holding_pct", 0)
        t.setdefault("top10_pct", 0)
        t.setdefault("bonding_curve_pct", 0)
        t.setdefault("reply_count", 0)
        t.setdefault("rugcheck_score", None)

        # Mettre en cache
        self._token_cache[addr] = {"data": t, "ts": time.time()}
        return t

    async def _fetch_pumpfun_coin(self, addr: str) -> Optional[dict]:
        """
        Récupère les détails d'un token depuis l'API Pump.fun.
        Essaie plusieurs endpoints en cascade car les APIs sont instables.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/",
        }
        # Endpoints par ordre de priorité
        urls = [
            "https://frontend-api-v3.pump.fun/coins/%s" % addr,
            "https://frontend-api-v2.pump.fun/coins/%s" % addr,
            "https://frontend-api.pump.fun/coins/%s" % addr,
        ]
        async with aiohttp.ClientSession() as session:
            for url in urls:
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=8),
                        headers=headers,
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            if data and isinstance(data, dict) and data.get("mint"):
                                return data
                        elif r.status not in (503, 530):
                            # Erreur inattendue — log et essayer suivant
                            logger.debug("fetch_pumpfun_coin %s HTTP %d via %s",
                                         addr[:12], r.status, url.split("/")[2])
                except Exception as e:
                    logger.debug("fetch_pumpfun_coin %s erreur %s: %s",
                                 addr[:12], url.split("/")[2], e)
        return None

    async def _fetch_moralis_data(self, addr: str) -> dict:
        """
        Récupère en parallèle depuis Moralis :
        - /token/mainnet/{addr}/pairs   → volume24h, liquidité, prix
        - /token/mainnet/holders/{addr} → totalHolders, holderChange 1h/24h

        Retourne un dict consolidé avec toutes les métriques.
        """
        if not self.moralis_key:
            return {}

        headers = {"accept": "application/json", "X-API-Key": self.moralis_key}
        timeout = aiohttp.ClientTimeout(total=8)
        result  = {}

        async def _get(session, url, key):
            try:
                async with session.get(url, headers=headers, timeout=timeout) as r:
                    if r.status == 200:
                        return key, await r.json(content_type=None)
                    logger.debug("Moralis %s HTTP %d", url.split("/")[-1], r.status)
            except Exception as e:
                logger.debug("Moralis %s: %s", key, e)
            return key, None

        async with aiohttp.ClientSession() as session:
            tasks = [
                _get(session, "%s/token/mainnet/%s/pairs?limit=10" % (MORALIS_SOL_GW, addr), "pairs"),
                _get(session, "%s/token/mainnet/holders/%s" % (MORALIS_SOL_GW, addr), "holders"),
            ]
            responses = await asyncio.gather(*tasks)

        for key, data in responses:
            if not data:
                continue

            if key == "pairs":
                total_vol = 0.0
                total_liq = 0.0
                best_price = 0.0
                for p in (data.get("pairs") or []):
                    if p.get("inactivePair"):
                        continue
                    total_vol  += float(p.get("volume24hrUsd", 0) or 0)
                    total_liq  += float(p.get("liquidityUsd",  0) or 0)
                    px = float(p.get("usdPrice", 0) or 0)
                    if px > best_price:
                        best_price = px
                result["volume_24h"] = total_vol
                result["liquidity"]  = total_liq
                result["price_usd"]  = best_price

            elif key == "holders":
                result["holders"]          = int(data.get("totalHolders", 0) or 0)
                hc = data.get("holderChange", {})
                result["holders_change_1h"]  = float((hc.get("1h")  or {}).get("change", 0) or 0)
                result["holders_change_24h"] = float((hc.get("24h") or {}).get("change", 0) or 0)
                # Distribution pour détecter la concentration
                dist = data.get("holdersByAcquisition", {})
                result["holders_by_swap"]    = int(dist.get("swap", 0) or 0)

        if result:
            logger.info("Moralis %s → vol=$%.0f holders=%d liq=$%.0f",
                        addr[:12],
                        result.get("volume_24h", 0),
                        result.get("holders", 0),
                        result.get("liquidity", 0))
        return result

    # Alias pour compatibilité
    async def _fetch_moralis_volume(self, addr: str) -> dict:
        return await self._fetch_moralis_data(addr)

    async def _fetch_rugcheck(self, addr: str) -> Optional[dict]:
        """Récupère le RugCheck score d'un token Solana"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "%s/tokens/%s/report/summary" % (RUGCHECK_API, addr),
                    timeout=aiohttp.ClientTimeout(total=8),
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        # RugCheck retourne un score et des risques
                        return {
                            "score": data.get("score", data.get("riskScore", 0)),
                            "risks": [
                                r.get("name", str(r)) for r in data.get("risks", [])
                            ][:5],
                        }
        except Exception as e:
            logger.debug("fetch_rugcheck %s: %s", addr[:16], e)
        return None

    async def _fetch_token_data(self, addr: str) -> Optional[dict]:
        """Fetch complet pour /pumptest"""
        token = {"address": addr, "_source": "manual"}
        pf    = await self._fetch_pumpfun_coin(addr)
        if pf:
            now    = int(time.time())
            age_s  = now - int(pf.get("created_timestamp", now) or now)
            token.update({
                "symbol":          pf.get("symbol", "?"),
                "name":            pf.get("name", "?"),
                "market_cap":      float(pf.get("usd_market_cap", 0) or 0),
                "volume_24h":      token.get("volume_24h", 0),  # hérité de Moralis via poll
                "holders":         int(pf.get("holder_count", 0) or 0),
                "snipers":         int(pf.get("sniper_count", pf.get("bot_holder_count", 0)) or 0),
                "dev_holding_pct": float(pf.get("creator_percentage", pf.get("dev_holding_pct", 0)) or 0),
                "top10_pct":       float(pf.get("top10_pct", 0) or 0),
                "reply_count":     int(pf.get("reply_count", 0) or 0),
                "bonding_curve_pct": float(pf.get("bonding_curve_percentage", 0) or 0),
                "description":     pf.get("description", ""),
                "twitter":         pf.get("twitter", ""),
                "telegram":        pf.get("telegram", ""),
                "website":         pf.get("website", ""),
                "image_uri":       pf.get("image_uri", ""),
                "created_ts":      int(pf.get("created_timestamp", 0) or 0),
                "age_hours":       age_s / 3600,
                "age_str":         self._fmt_duration(age_s),
                "_source":         "pumpfun",
            })
            if self.rugcheck_enabled:
                rug = await self._fetch_rugcheck(addr)
                if rug:
                    token["rugcheck_score"] = rug.get("score")
                    token["rugcheck_risks"]  = rug.get("risks", [])
            return token
        return None

    # ══════════════════════════════════════════════════════════════
    #   SCORING CLAUDE
    # ══════════════════════════════════════════════════════════════

    async def _score_with_claude(self, t: dict) -> dict:
        """
        Envoie les données du token à Claude pour analyse et scoring 0-100.
        Retourne : {score, verdict, breakdown, recommendation}
        """
        if not self.anthropic_key:
            return {
                "score": 0, "verdict": "API key manquante",
                "breakdown": {}, "recommendation": "Configurer ANTHROPIC_API_KEY"
            }

        # ── Construire le prompt structuré ────────────────────
        addr           = t.get("address", "")
        symbol         = t.get("symbol", "?")
        name           = t.get("name", "?")
        mc             = t.get("market_cap", 0)
        vol            = t.get("volume_24h", 0)
        holders        = t.get("holders", 0)
        age_str        = t.get("age_str", "?")
        bc_pct         = t.get("bonding_curve_pct", 0)
        reply_count    = t.get("reply_count", 0)
        description    = t.get("description", "")[:300]
        twitter        = t.get("twitter", "")
        telegram       = t.get("telegram", "")
        website        = t.get("website", "")
        vol_per_holder = vol / holders if holders > 0 else 0

        # ── Distinguer "donnée absente = vrai red flag" vs "API timeout = neutre" ──
        # Snipers, dev_holding, top10 viennent de RugCheck/Moralis — souvent timeout
        raw_snipers = t.get("snipers", None)
        raw_dev     = t.get("dev_holding_pct", None)
        raw_top10   = t.get("top10_pct", None)
        raw_rug_score  = t.get("rugcheck_score", None)
        raw_rug_risks  = t.get("rugcheck_risks", None)

        snipers_str   = str(raw_snipers)        if raw_snipers not in (None, "N/A") else "non récupéré (API timeout — traiter en NEUTRE)"
        dev_str       = f"{raw_dev}%%"           if raw_dev     not in (None, "N/A") else "non récupéré (API timeout — traiter en NEUTRE)"
        top10_str     = f"{raw_top10}%%"         if raw_top10   not in (None, "N/A") else "non récupéré (API timeout — traiter en NEUTRE)"
        rug_score_str = str(raw_rug_score)       if raw_rug_score not in (None, "N/A") else "non récupéré (API timeout — traiter en NEUTRE)"
        rug_risks_str = ", ".join(raw_rug_risks) if raw_rug_risks else "aucun détecté"

        # Les liens sociaux absents = vrai red flag (le token n'a tout simplement pas de socials)
        has_socials = bool(twitter or telegram or website)
        socials_str = (
            f"Twitter={twitter or 'non'} | TG={telegram or 'non'} | Site={website or 'non'}"
            if has_socials
            else "AUCUN lien social (red flag — pénaliser)"
        )
        # Description absente = vrai red flag
        desc_str = description if description else "AUCUNE description (red flag — pénaliser)"

        prompt = """Tu es un expert en analyse de memecoins sur Pump.fun (Solana). Tu dois scorer ce token de 0 à 100.

RÈGLE CRITIQUE sur les données manquantes :
- "non récupéré (API timeout)" = donnée indisponible pour raison technique → traiter en NEUTRE (ni bonus ni malus)
- "AUCUN lien social" ou "AUCUNE description" = le token n'a vraiment pas ces éléments → vrai red flag, pénaliser normalement

## Token à analyser
- **Ticker/Name**: %s / %s
- **Adresse**: %s
- **Âge**: %s
- **Market Cap**: $%s
- **Volume 24h**: $%s (ratio vol/holder: $%.0f)
- **Holders**: %d
- **Snipers**: %s
- **Dev Holdings**: %s
- **Top 10 Holders**: %s
- **Bonding Curve**: %.1f%%
- **Replies Pump.fun**: %d
- **RugCheck Score**: %s (risques: %s)
- **Liens**: %s
- **Description/Narrative**: %s

## Critères de scoring (total 100 pts)

### 1. Engagement organique vs bot/spam (30 pts)
Évalue si l'activité est réelle : ratio volume/holders, nombre de replies, diversité des wallets.
Si une donnée est "non récupéré (API timeout)", ignore-la complètement pour ce critère.

### 2. Potentiel viral / thème fort (30 pts)
Analyse le nom, ticker, description, narrative. Est-ce un thème d'actualité ? Timing bon ?
Absence de description ou de socials = pénaliser fort (max 10/30).

### 3. Risque rug / concentration wallets (20 pts)
Dev holdings, top 10 concentration, snipers, RugCheck risks.
Si les données sont "non récupéré (API timeout)" → attribuer 10/20 (neutre).
Si RugCheck détecte des risques réels → pénaliser normalement.

### 4. Momentum (20 pts)
Volume vs MC, bonding curve progression, replies récents.

## Format de réponse (JSON strict, aucun texte avant ou après)
{
  "score": <0-100>,
  "breakdown": {
    "engagement_organique": <0-30>,
    "potentiel_viral": <0-30>,
    "risque_rug": <0-20>,
    "momentum": <0-20>
  },
  "verdict": "<1 phrase résumant le verdict>",
  "points_forts": ["<point 1>", "<point 2>"],
  "points_faibles": ["<point 1>", "<point 2>"],
  "recommendation": "<BUY EARLY|WATCH|SKIP|HIGH RISK>",
  "tp_targets": ["x2", "x3"],
  "sl_suggestion": "-40%%",
  "raisonnement": "<2-3 phrases d'analyse clé>"
}""" % (
            symbol, name, addr[:20] + "...",
            age_str,
            self._fmt_k(mc), self._fmt_k(vol), vol_per_holder,
            holders, snipers_str, dev_str, top10_str,
            bc_pct, reply_count,
            rug_score_str, rug_risks_str,
            socials_str,
            desc_str,
        )

        # ── Appel API Anthropic ───────────────────────────────
        try:
            headers = {
                "x-api-key":         self.anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            }
            payload = {
                "model":      "claude-sonnet-4-6",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    ANTHROPIC_API,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        logger.error("Claude API %d: %s", r.status, body[:200])
                        return {"score": 0, "verdict": "Erreur API Claude", "breakdown": {}, "recommendation": "SKIP"}

                    data    = await r.json()
                    content = data.get("content", [{}])[0].get("text", "")

                    # Parser le JSON retourné par Claude
                    clean = content.strip()
                    if clean.startswith("```"):
                        clean = clean.split("```")[1]
                        if clean.startswith("json"):
                            clean = clean[4:]
                    clean = clean.strip()

                    result = json.loads(clean)
                    logger.info(
                        "Score Claude %s (%s): %d/100 — %s",
                        symbol, addr[:8], result.get("score", 0), result.get("verdict", "")
                    )
                    return result

        except json.JSONDecodeError as e:
            logger.error("JSON parse error Claude: %s | response: %s", e, content[:200])
            return {"score": 0, "verdict": "Erreur parsing réponse", "breakdown": {}, "recommendation": "SKIP"}
        except Exception as e:
            logger.error("Erreur appel Claude: %s", e)
            return {"score": 0, "verdict": "Erreur API", "breakdown": {}, "recommendation": "SKIP"}

    # ══════════════════════════════════════════════════════════════
    #   ALERTES
    # ══════════════════════════════════════════════════════════════

    async def _send_alert(self, token_data: dict, result: dict):
        """Envoie une alerte Telegram pour un token scoré"""

        # Anti-spam : max N alertes par heure
        now = time.time()
        self._alerts_this_hour = [t for t in self._alerts_this_hour if now - t < 3600]
        if len(self._alerts_this_hour) >= self.max_alerts_hour:
            logger.warning("Anti-spam: limite %d alertes/heure atteinte", self.max_alerts_hour)
            return

        self._alerts_this_hour.append(now)

        msg = await self._format_alert(token_data, result)

        # Logger
        addr = token_data.get("address", "")
        self._alerts_log.append({
            "ts":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "address": addr,
            "symbol":  token_data.get("symbol", "?"),
            "mc":      token_data.get("market_cap", 0),
            "volume":  token_data.get("volume_24h", 0),
            "holders": token_data.get("holders", 0),
            "score":   result.get("score", 0),
            "verdict": result.get("verdict", ""),
        })
        self._save_state()

        await self._notify(msg)

        # ── Trading automatique après alerte ─────────────────
        if self.trade_mode != "off":
            await self._auto_trade(token_data, result)

    async def _format_alert(self, t: dict, result: dict, force: bool = False) -> str:
        """Formate le message d'alerte complet"""
        score    = result.get("score", 0)
        verdict  = result.get("verdict", "")
        rec      = result.get("recommendation", "")
        points_f = result.get("points_forts", [])
        points_w = result.get("points_faibles", [])
        raison   = result.get("raisonnement", "")
        tp       = result.get("tp_targets", ["x2", "x3"])
        sl       = result.get("sl_suggestion", "-40%")
        breakdown = result.get("breakdown", {})

        addr    = t.get("address", "")
        symbol  = t.get("symbol", "?")
        name    = t.get("name", "?")
        mc      = t.get("market_cap", 0)
        vol     = t.get("volume_24h", 0)
        holders = t.get("holders", 0)
        snipers = t.get("snipers", "?")
        dev_pct = t.get("dev_holding_pct", "?")
        top10   = t.get("top10_pct", "?")
        age_str = t.get("age_str", "?")
        bc_pct  = t.get("bonding_curve_pct", 0)
        desc    = t.get("description", "")[:80]
        rug     = t.get("rugcheck_score", "N/A")

        # Icônes selon le score
        if score >= self.score_high:
            header_icon = "🔴🔴 HIGH CONVICTION 🔴🔴"
            score_bar   = "🟥" * (score // 10)
        elif score >= self.score_alert:
            header_icon = "🟡 GEM CANDIDATE 🟡"
            score_bar   = "🟨" * (score // 10)
        else:
            header_icon = "📊 Analyse"
            score_bar   = "⬜" * (score // 10)

        pump_link = "https://pump.fun/%s" % addr
        sol_link  = "https://solscan.io/token/%s" % addr
        dex_link  = "https://dexscreener.com/solana/%s" % addr

        # Breakdown formaté
        bd_str = ""
        if breakdown:
            bd_str = (
                "📊 **Breakdown:**\n"
                "  🤝 Engagement: %d/30 | 🔥 Viral: %d/30\n"
                "  🛡 Rug risk: %d/20 | 📈 Momentum: %d/20\n"
            ) % (
                breakdown.get("engagement_organique", 0),
                breakdown.get("potentiel_viral", 0),
                breakdown.get("risque_rug", 0),
                breakdown.get("momentum", 0),
            )

        # Points forts/faibles
        pf_str = ("\n".join("  ✅ " + p for p in points_f[:3])) if points_f else ""
        pw_str = ("\n".join("  ⚠️ " + p for p in points_w[:2])) if points_w else ""

        msg = (
            "%s\n"
            "━━━━━━━━━━━━━━━\n"
            "🪙 **%s** — _%s_\n"
            "🏷 Score Claude: **%d/100**  %s\n"
            "\n"
            "💰 MC: **$%s** | Vol 24h: **$%s**\n"
            "👥 Holders: **%d** | 🎯 Snipers: **%s**\n"
            "🧑‍💻 Dev: **%s%%** | Top10: **%s%%**\n"
            "📈 Bonding: **%.1f%%** | ⏱ Âge: **%s**\n"
            "🔍 RugCheck: **%s**\n"
            "\n"
            "%s"
            "\n"
            "💬 _%s_\n"
            "\n"
            "%s%s"
            "\n"
            "🤖 **Claude dit:** _%s_\n"
            "\n"
            "🎯 **%s** | TP: %s | SL: %s\n"
            "\n"
            "🔗 [Pump.fun](%s) | [DexScreener](%s) | [Solscan](%s)"
        ) % (
            header_icon,
            symbol, name,
            score, score_bar,
            self._fmt_k(mc), self._fmt_k(vol),
            holders, snipers,
            dev_pct, top10,
            bc_pct, age_str,
            str(rug) if rug else "N/A",
            bd_str,
            desc or "Pas de description",
            pf_str + "\n" if pf_str else "",
            pw_str + "\n" if pw_str else "",
            raison or verdict,
            rec, " / ".join(tp) if isinstance(tp, list) else str(tp), sl,
            pump_link, dex_link, sol_link,
        )
        return msg

    # ══════════════════════════════════════════════════════════════
    #   COMMANDES TRADING
    # ══════════════════════════════════════════════════════════════

    async def _cmd_trademode(self, args: str, ctx: SkillContext) -> str:
        """Activer/désactiver le trading auto : /pumptrademode paper|live|off"""
        mode = args.strip().lower()
        if mode not in ("paper", "live", "off"):
            mode_desc = {
                "paper": "📄 Simulation — trades fictifs, P&L calculé en temps réel",
                "live":  "💸 LIVE — trades réels sur Solana (nécessite PUMP_WALLET_KEY)",
                "off":   "🚫 Désactivé — alertes uniquement, aucun trade automatique",
            }
            current = self.trade_mode
            return (
                "💹 **Mode trading actuel: %s**\n\n"
                "Modes disponibles :\n"
                "  `paper` — %s\n"
                "  `live`  — %s\n"
                "  `off`   — %s\n\n"
                "Usage: `/pumptrademode paper|live|off`"
                % (current, mode_desc["paper"], mode_desc["live"], mode_desc["off"])
            )

        if mode == "live" and not self.wallet_key:
            return (
                "❌ **Mode LIVE impossible sans wallet configuré**\n\n"
                "Configure dans `.env` :\n"
                "`PUMP_WALLET_KEY=ta_clé_privée_base58`\n"
                "`PUMP_SOLANA_RPC=https://ton_rpc`\n\n"
                "⚠️ Ne partage JAMAIS ta clé privée."
            )

        old_mode      = self.trade_mode
        self.trade_mode = mode
        self._save_state()

        icons = {"paper": "📄", "live": "💸", "off": "🚫"}
        warnings = {
            "live":  "\n\n⚠️ **ATTENTION** : Les trades seront exécutés avec de vraies SOL !",
            "paper": "",
            "off":   "",
        }
        return (
            "%s **Mode trading → %s**\n"
            "(était: %s)%s\n\n"
            "💰 Mise: $%.0f (base) / $%.0f (high conviction)\n"
            "🎯 TP1: x%.1f | TP2: x%.1f | TP3: x%.1f | SL: -%.0f%%\n"
            "📊 Max positions simultanées: %d\n\n"
            "💡 `/pumpconfig` pour modifier les paramètres"
            % (
                icons.get(mode, ""), mode, old_mode, warnings.get(mode, ""),
                self.buy_amount, self.buy_amount_high,
                self.tp1, self.tp2, self.tp3, self.sl * 100,
                self.max_open_trades,
            )
        )

    async def _cmd_trades(self, args: str, ctx: SkillContext) -> str:
        """Voir les positions ouvertes + historique clôturés + P&L"""
        mode_icon = "📄" if self.trade_mode == "paper" else "💸"
        lines = ["%s **Positions %s**\n━━━━━━━━━━━━━━━" % (mode_icon, self.trade_mode.upper())]

        # ── Positions ouvertes ────────────────────────────────
        if not self._positions:
            lines.append("📭 Aucune position ouverte.")
        else:
            total_in  = 0.0
            total_val = 0.0
            for addr, pos in self._positions.items():
                symbol      = pos.get("symbol", "?")
                entry_price = pos.get("entry_price", 0)
                amount_usd  = pos.get("amount_usd", 0)
                shares      = pos.get("shares", 0)
                opened_at   = pos.get("opened_at", "?")
                score       = pos.get("score", 0)
                conviction  = pos.get("conviction", "normal")
                tp1_hit     = pos.get("tp1_hit", False)
                tp2_hit     = pos.get("tp2_hit", False)
                sl_price    = pos.get("sl_price", 0)

                # Récupérer le prix actuel
                cur_price = await self._get_token_price(addr)
                if cur_price <= 0:
                    cur_price = entry_price  # fallback

                cur_val  = shares * cur_price if cur_price > 0 else amount_usd
                pnl      = cur_val - amount_usd
                pnl_pct  = (pnl / amount_usd * 100) if amount_usd > 0 else 0
                mult     = cur_price / entry_price if entry_price > 0 else 1
                pnl_icon = "🟢" if pnl >= 0 else "🔴"

                tp_status = ""
                if tp1_hit and tp2_hit:
                    tp_status = " | TP1✅TP2✅"
                elif tp1_hit:
                    tp_status = " | TP1✅"

                conv_icon = "🔴" if conviction == "high" else "🟡"
                total_in  += amount_usd
                total_val += cur_val

                lines.append(
                    "%s **%s** (%s) | Score: %d%s\n"
                    "   Entrée: $%.4f → Actuel: $%.4f (x%.2f)\n"
                    "   $%.2f → $%.2f | %s **%+.2f$** (%+.1f%%)%s\n"
                    "   SL: $%.4f | %s"
                    % (
                        conv_icon, symbol, addr[:8], score, " 🔴HC" if conviction == "high" else "",
                        entry_price, cur_price, mult,
                        amount_usd, cur_val, pnl_icon, pnl, pnl_pct, tp_status,
                        sl_price, opened_at,
                    )
                )

            total_pnl = total_val - total_in
            pnl_icon  = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(
                "\n━━━━━━━━━━━━━━━\n"
                "💰 Investi: $%.2f → Valeur: $%.2f\n"
                "%s **P&L latent: $%+.2f**"
                % (total_in, total_val, pnl_icon, total_pnl)
            )

        # ── Trades clôturés récents ───────────────────────────
        if self._closed_trades:
            lines.append("\n📋 **Trades clôturés (5 derniers) :**")
            for t in self._closed_trades[-5:][::-1]:
                pnl  = t.get("pnl", 0)
                icon = "✅" if pnl >= 0 else "❌"
                lines.append(
                    "%s **%s** | %s | P&L: **$%+.2f** (x%.2f) | %s"
                    % (icon, t.get("symbol","?"), t.get("reason","?"),
                       pnl, t.get("multiplier", 1), t.get("closed_at","?"))
                )

        # ── P&L global ───────────────────────────────────────
        pnl_icon = "🟢" if self._total_pnl >= 0 else "🔴"
        lines.append(
            "\n%s **P&L réalisé total: $%+.2f** | Aujourd'hui: $%+.2f"
            % (pnl_icon, self._total_pnl, self._daily_pnl)
        )

        return "\n".join(lines)

    async def _cmd_close(self, args: str, ctx: SkillContext) -> str:
        """Fermer manuellement une position : /pumpclose <addr>"""
        addr = args.strip()
        if not addr:
            if self._positions:
                lines = ["📋 **Positions ouvertes (utilise /pumpclose <addr>) :**"]
                for a, p in self._positions.items():
                    lines.append("  `%s` — **%s**" % (a[:20], p.get("symbol","?")))
                return "\n".join(lines)
            return "📭 Aucune position ouverte."

        # Chercher la position (partiel OK)
        match = None
        for a in self._positions:
            if a.startswith(addr) or addr in a:
                match = a
                break

        if not match:
            return "❌ Position `%s` introuvable." % addr[:20]

        pos        = self._positions[match]
        cur_price  = await self._get_token_price(match)
        entry_price = pos.get("entry_price", 0)
        if cur_price <= 0:
            cur_price = entry_price

        pnl, mult = await self._close_position(match, cur_price, reason="Manuel")
        pnl_icon  = "🟢" if pnl >= 0 else "🔴"
        return (
            "✅ **Position %s fermée manuellement**\n"
            "   Prix sortie: $%.4f | Multiplicateur: x%.2f\n"
            "%s P&L: **$%+.2f**"
            % (pos.get("symbol","?"), cur_price, mult, pnl_icon, pnl)
        )

    async def _cmd_paper_reset(self, args: str, ctx: SkillContext) -> str:
        """Remettre à zéro le paper trading"""
        if args.strip().lower() != "confirm":
            nb_pos    = len([p for p in self._positions.values() if p.get("paper", True)])
            nb_closed = len([t for t in self._closed_trades if t.get("paper", True)])
            pnl       = self._total_pnl
            pnl_icon  = "🟢" if pnl >= 0 else "🔴"
            return (
                "⚠️ **Reset Paper Trading**\n"
                "━━━━━━━━━━━━━━━\n"
                "Ceci va effacer :\n"
                "  • %d position(s) ouverte(s)\n"
                "  • %d trade(s) clôturés\n"
                "  • %s P&L réalisé: $%+.2f\n\n"
                "Confirme : `/pumppaperreset confirm`"
                % (nb_pos, nb_closed, pnl_icon, pnl)
            )

        # Reset
        nb_pos    = len(self._positions)
        nb_closed = len(self._closed_trades)
        old_pnl   = self._total_pnl

        self._positions    = {}
        self._closed_trades = []
        self._total_pnl    = 0.0
        self._daily_pnl    = 0.0
        self._save_state()

        pnl_icon = "🟢" if old_pnl >= 0 else "🔴"
        return (
            "✅ **Paper Trading remis à zéro !**\n"
            "━━━━━━━━━━━━━━━\n"
            "Effacé :\n"
            "  • %d position(s)\n"
            "  • %d trade(s) clôturés\n"
            "  • %s P&L: $%+.2f\n\n"
            "Tout repart de zéro 🚀"
            % (nb_pos, nb_closed, pnl_icon, old_pnl)
        )

    async def _cmd_config(self, args: str, ctx: SkillContext) -> str:
        """Voir ou modifier la config trading"""
        if not args:
            return (
                "⚙️ **Config Trading Pump.fun**\n"
                "━━━━━━━━━━━━━━━\n"
                "Mode: **%s**\n\n"
                "💰 **Mises :**\n"
                "  `buy_amount`      = $%.0f (base)\n"
                "  `buy_amount_high` = $%.0f (high conviction)\n"
                "  `buy_score_min`   = %d/100 (score min pour buy)\n"
                "  `buy_score_high`  = %d/100 (score high conviction)\n\n"
                "🎯 **Take Profits :**\n"
                "  `tp1` = x%.1f → vendre 40%%\n"
                "  `tp2` = x%.1f → vendre 35%%\n"
                "  `tp3` = x%.1f → vendre 25%%\n\n"
                "🛡 **Stop Loss :**\n"
                "  `sl` = %.0f%% de perte\n\n"
                "📊 **Limites :**\n"
                "  `max_open_trades` = %d\n"
                "  `max_daily_loss`  = $%.0f\n\n"
                "Pour modifier : `/pumpconfig <clé> <valeur>`\n"
                "Ex: `/pumpconfig buy_amount 25`"
                % (
                    self.trade_mode,
                    self.buy_amount, self.buy_amount_high,
                    self.buy_score_min, self.buy_score_high,
                    self.tp1, self.tp2, self.tp3,
                    self.sl * 100,
                    self.max_open_trades, self.max_daily_loss,
                )
            )

        parts = args.split()
        if len(parts) != 2:
            return "Usage: `/pumpconfig <clé> <valeur>`"

        key, val_str = parts
        config_map = {
            "buy_amount":      ("buy_amount",      float),
            "buy_amount_high": ("buy_amount_high",  float),
            "buy_score_min":   ("buy_score_min",    int),
            "buy_score_high":  ("buy_score_high",   int),
            "tp1":             ("tp1",              float),
            "tp2":             ("tp2",              float),
            "tp3":             ("tp3",              float),
            "sl":              ("sl",               float),
            "max_open_trades": ("max_open_trades",  int),
            "max_daily_loss":  ("max_daily_loss",   float),
        }
        if key not in config_map:
            return "❌ Clé inconnue. `/pumpconfig` pour voir les clés disponibles."

        attr, cast = config_map[key]
        try:
            val = cast(val_str)
            # Normaliser sl (accepter 50 = 50% ou 0.50)
            if key == "sl" and val > 1:
                val = val / 100
            setattr(self, attr, val)
            self._save_state()
            return "✅ **%s** → `%s`" % (key, val_str)
        except ValueError:
            return "❌ Valeur invalide : `%s`" % val_str

    # ══════════════════════════════════════════════════════════════
    #   MOTEUR DE TRADING AUTOMATIQUE
    # ══════════════════════════════════════════════════════════════

    async def _auto_trade(self, token_data: dict, score_result: dict):
        """
        Décide et exécute automatiquement un trade selon le score.

        Logique :
        - score >= buy_score_high → HIGH CONVICTION → mise x2
        - score >= buy_score_min  → NORMAL → mise standard
        - Vérifications : max_open_trades, max_daily_loss, déjà en position
        """
        score    = score_result.get("score", 0)
        addr     = token_data.get("address", "")
        symbol   = token_data.get("symbol", "?")

        # Vérifier si déjà en position
        if addr in self._positions:
            logger.debug("Auto-trade ignoré: déjà en position sur %s", symbol)
            return

        # Vérifier score minimum
        if score < self.buy_score_min:
            logger.debug("Auto-trade ignoré: score %d < %d pour %s", score, self.buy_score_min, symbol)
            return

        # Vérifier limite positions simultanées
        if len(self._positions) >= self.max_open_trades:
            logger.warning("Auto-trade bloqué: max positions (%d) atteint", self.max_open_trades)
            await self._notify(
                "⚠️ **Auto-trade bloqué** : %d/%d positions déjà ouvertes\n"
                "Token ignoré : **%s** (score %d/100)\n"
                "Ferme une position avec `/pumpclose`"
                % (len(self._positions), self.max_open_trades, symbol, score)
            )
            return

        # Vérifier perte journalière max
        self._check_daily_reset()
        if self._daily_pnl <= -self.max_daily_loss:
            logger.warning("Auto-trade bloqué: perte journalière max $%.2f atteinte", self.max_daily_loss)
            await self._notify(
                "🛑 **Trading pausé : perte journalière max atteinte**\n"
                "Perte aujourd'hui: $%.2f / limite $%.2f\n"
                "Trading reprendra demain automatiquement."
                % (self._daily_pnl, self.max_daily_loss)
            )
            return

        # Déterminer conviction et mise
        conviction = "high" if score >= self.buy_score_high else "normal"
        amount_usd = self.buy_amount_high if conviction == "high" else self.buy_amount

        # Récupérer le prix d'entrée
        entry_price = await self._get_token_price(addr)
        if entry_price <= 0:
            # Fallback sur MC / supply approximative
            mc = token_data.get("market_cap", 0)
            entry_price = mc / 1_000_000_000 if mc > 0 else 0  # Estimation
        if entry_price <= 0:
            logger.warning("Prix introuvable pour %s, trade annulé", symbol)
            return

        shares    = amount_usd / entry_price
        sl_price  = entry_price * (1 - self.sl)

        # ── PAPER MODE ────────────────────────────────────────
        if self.trade_mode == "paper":
            self._positions[addr] = {
                "symbol":       symbol,
                "name":         token_data.get("name", "?"),
                "entry_price":  entry_price,
                "amount_usd":   amount_usd,
                "shares":       shares,
                "sl_price":     sl_price,
                "opened_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "timestamp":    int(time.time()),
                "score":        score,
                "conviction":   conviction,
                "tp1_hit":      False,
                "tp2_hit":      False,
                "tp3_hit":      False,
                "paper":        True,
                "tp1_price":    entry_price * self.tp1,
                "tp2_price":    entry_price * self.tp2,
                "tp3_price":    entry_price * self.tp3,
                "remaining_shares": shares,
            }
            self._save_state()
            logger.info(
                "PAPER BUY %s: $%.2f @ $%.6f | SL: $%.6f | Score: %d/100",
                symbol, amount_usd, entry_price, sl_price, score
            )
            conv_badge = "🔴 HIGH CONVICTION" if conviction == "high" else "🟡 Normal"
            await self._notify(
                "📄 **AUTO-TRADE PAPER — BUY**\n"
                "━━━━━━━━━━━━━━━\n"
                "🪙 **%s** | Score: **%d/100** | %s\n"
                "💰 Mise: **$%.2f** @ `$%.6f`\n"
                "📈 TP1: $%.6f (x%.1f) | TP2: $%.6f (x%.1f) | TP3: $%.6f (x%.1f)\n"
                "🛡 SL: $%.6f (-%.0f%%)\n"
                "🔗 https://pump.fun/%s"
                % (
                    symbol, score, conv_badge,
                    amount_usd, entry_price,
                    entry_price * self.tp1, self.tp1,
                    entry_price * self.tp2, self.tp2,
                    entry_price * self.tp3, self.tp3,
                    sl_price, self.sl * 100,
                    addr,
                )
            )

        # ── LIVE MODE ─────────────────────────────────────────
        elif self.trade_mode == "live":
            if not self.wallet_key:
                await self._notify("❌ LIVE trade impossible : PUMP_WALLET_KEY manquant")
                return

            tx_hash = await self._execute_live_buy(addr, amount_usd, entry_price)
            if not tx_hash:
                await self._notify(
                    "❌ **AUTO-TRADE LIVE ÉCHOUÉ**\n"
                    "Token: **%s** | Tentative: $%.2f\n"
                    "Vérifie les logs pour détails." % (symbol, amount_usd)
                )
                return

            # Enregistrer la position
            self._positions[addr] = {
                "symbol":       symbol,
                "name":         token_data.get("name", "?"),
                "entry_price":  entry_price,
                "amount_usd":   amount_usd,
                "shares":       shares,
                "sl_price":     sl_price,
                "opened_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "timestamp":    int(time.time()),
                "score":        score,
                "conviction":   conviction,
                "tp1_hit":      False,
                "tp2_hit":      False,
                "tp3_hit":      False,
                "paper":        False,
                "tx_hash_buy":  tx_hash,
                "tp1_price":    entry_price * self.tp1,
                "tp2_price":    entry_price * self.tp2,
                "tp3_price":    entry_price * self.tp3,
                "remaining_shares": shares,
            }
            self._save_state()
            conv_badge = "🔴 HIGH CONVICTION" if conviction == "high" else "🟡 Normal"
            await self._notify(
                "💸 **AUTO-TRADE LIVE — BUY EXÉCUTÉ**\n"
                "━━━━━━━━━━━━━━━\n"
                "🪙 **%s** | Score: **%d/100** | %s\n"
                "💰 Mise: **$%.2f** @ `$%.6f`\n"
                "📈 TP1: x%.1f | TP2: x%.1f | TP3: x%.1f | SL: -%.0f%%\n"
                "🔗 TX: `%s`"
                % (
                    symbol, score, conv_badge,
                    amount_usd, entry_price,
                    self.tp1, self.tp2, self.tp3, self.sl * 100,
                    tx_hash[:20] + "...",
                )
            )

    async def _sync_ws_subscriptions(self):
        """
        S'assure que toutes les positions ouvertes sont souscrites via WS
        pour recevoir les prix en temps réel sans appel API.
        """
        if not self._ws_ref or not self._positions:
            return
        current_addrs = set(self._positions.keys())
        # Nouvelles positions à souscrire
        to_sub = current_addrs - self._ws_subscribed_positions
        if to_sub:
            try:
                await self._ws_ref.send(json.dumps({
                    "method": "subscribeTokenTrade",
                    "keys": list(to_sub)
                }))
                self._ws_subscribed_positions |= to_sub
                logger.info("WS souscription prix: %s", [a[:8] for a in to_sub])
            except Exception as e:
                logger.debug("sync_ws_subscriptions: %s", e)
        # Positions fermées à désouscrire
        to_unsub = self._ws_subscribed_positions - current_addrs
        if to_unsub:
            try:
                await self._ws_ref.send(json.dumps({
                    "method": "unsubscribeTokenTrade",
                    "keys": list(to_unsub)
                }))
                self._ws_subscribed_positions -= to_unsub
                logger.info("WS désouscription prix: %s", [a[:8] for a in to_unsub])
            except Exception as e:
                logger.debug("sync_ws_unsubscribe: %s", e)

    async def _monitor_positions(self):
        """
        Vérifie le prix actuel de chaque position ouverte.
        Utilise le cache WS (temps réel) en priorité, API en fallback si cache > 10s.
        Déclenche TP partiels ou SL selon les niveaux atteints.
        Appelé toutes les 5s depuis _main_loop.
        """
        if not self._positions:
            return

        for addr in list(self._positions.keys()):
            pos = self._positions.get(addr)
            if not pos:
                continue

            # Utiliser le cache WS en priorité (mis à jour en temps réel)
            # Fallback API si cache absent ou trop vieux (>10s sans trade)
            cached = self._ws_price_cache.get(addr)
            if cached and (time.time() - cached["ts"]) < 10:
                cur_price = cached["price"]
            else:
                cur_price = await self._get_token_price(addr)
            if cur_price <= 0:
                continue

            entry_price      = pos.get("entry_price", 0)
            sl_price         = pos.get("sl_price", 0)
            symbol           = pos.get("symbol", "?")
            remaining_shares = pos.get("remaining_shares", pos.get("shares", 0))

            if entry_price <= 0:
                continue

            mult = cur_price / entry_price

            # ── STOP LOSS ─────────────────────────────────────
            if cur_price <= sl_price and sl_price > 0:
                tp1_was_hit   = pos.get("tp1_hit", False)
                tp2_was_hit   = pos.get("tp2_hit", False)
                pnl_already   = self._total_pnl  # snapshot avant fermeture
                pnl, _        = await self._close_position(addr, cur_price, reason="Stop Loss")
                # Si TP1 ou TP2 avaient déjà été touchés, le SL est au breakeven ou TP1
                # → afficher le P&L net total de la position
                if tp1_was_hit or tp2_was_hit:
                    pnl_total_pos = (self._total_pnl - pnl_already) + pnl
                    amount_init   = pos.get("amount_usd", 0)
                    tp_status     = "après TP1+TP2" if tp2_was_hit else "après TP1"
                    await self._notify(
                        "🛑 **STOP LOSS déclenché** (%s)\n"
                        "🪙 **%s** | Prix: $%.6f\n"
                        "📉 x%.2f | P&L restant: **$%+.2f**\n"
                        "💰 P&L net total position: **$%+.2f** (mise $%.2f)\n"
                        "✅ Capital protégé grâce aux TP partiels"
                        % (tp_status, symbol, cur_price, mult, pnl, pnl_total_pos, amount_init)
                    )
                else:
                    await self._notify(
                        "🛑 **STOP LOSS déclenché**\n"
                        "🪙 **%s** | Prix: $%.6f\n"
                        "📉 x%.2f | P&L: **$%+.2f**\n"
                        "SL atteint à -%.0f%%"
                        % (symbol, cur_price, mult, pnl, self.sl * 100)
                    )
                continue

            # ── TAKE PROFIT 1 (x2 → vendre 40%) ──────────────
            if not pos.get("tp1_hit") and cur_price >= pos.get("tp1_price", float("inf")):
                total_shares   = pos.get("shares", remaining_shares)
                shares_sell    = total_shares * 0.40
                # Coût réel des shares vendues (proportionnel à la mise initiale)
                cost_sell      = pos.get("amount_usd", 0) * (shares_sell / total_shares) if total_shares > 0 else 0
                pnl_partial    = (shares_sell * cur_price) - cost_sell
                remaining_after = remaining_shares - shares_sell
                # Coût restant pour les shares encore en jeu
                cost_remaining = pos.get("amount_usd", 0) * (remaining_after / total_shares) if total_shares > 0 else 0
                self._positions[addr]["tp1_hit"]           = True
                self._positions[addr]["remaining_shares"]  = remaining_after
                self._positions[addr]["amount_usd_remaining"] = cost_remaining
                # Remonter le SL au breakeven
                self._positions[addr]["sl_price"] = entry_price * 1.01
                self._record_partial_pnl(pnl_partial)
                await self._notify(
                    "✅ **TP1 atteint !** 🎉\n"
                    "🪙 **%s** x%.1f @ $%.6f\n"
                    "💰 Vendu 40%% → P&L partiel: **$%+.2f**\n"
                    "📊 SL remonté au breakeven | 60%% encore en jeu"
                    % (symbol, mult, cur_price, pnl_partial)
                )
                if self.trade_mode == "live":
                    await self._execute_live_sell(addr, shares_sell, cur_price)

            # ── TAKE PROFIT 2 (x3 → vendre 35%) ──────────────
            elif pos.get("tp1_hit") and not pos.get("tp2_hit") and cur_price >= pos.get("tp2_price", float("inf")):
                remaining_now  = self._positions[addr].get("remaining_shares", remaining_shares)
                total_shares   = pos.get("shares", remaining_now)
                shares_sell    = remaining_now * 0.583  # ~35% du total original
                # Coût réel des shares vendues depuis amount_usd_remaining (post-TP1)
                cost_remaining_tp1 = self._positions[addr].get("amount_usd_remaining",
                    pos.get("amount_usd", 0) * (remaining_now / total_shares) if total_shares > 0 else 0)
                cost_sell      = cost_remaining_tp1 * (shares_sell / remaining_now) if remaining_now > 0 else 0
                pnl_partial    = (shares_sell * cur_price) - cost_sell
                remaining_after = remaining_now - shares_sell
                cost_remaining2 = cost_remaining_tp1 - cost_sell
                self._positions[addr]["tp2_hit"]              = True
                self._positions[addr]["remaining_shares"]     = remaining_after
                self._positions[addr]["amount_usd_remaining"] = cost_remaining2
                # Remonter le SL à TP1
                self._positions[addr]["sl_price"] = pos.get("tp1_price", entry_price * self.tp1) * 0.95
                self._record_partial_pnl(pnl_partial)
                await self._notify(
                    "✅✅ **TP2 atteint !** 🚀\n"
                    "🪙 **%s** x%.1f @ $%.6f\n"
                    "💰 Vendu 35%% de plus → P&L partiel: **$%+.2f**\n"
                    "📊 SL remonté à TP1 | 25%% en mode moon"
                    % (symbol, mult, cur_price, pnl_partial)
                )
                if self.trade_mode == "live":
                    await self._execute_live_sell(addr, shares_sell, cur_price)

            # ── TAKE PROFIT 3 (x5 → vendre tout) ─────────────
            elif pos.get("tp2_hit") and not pos.get("tp3_hit") and cur_price >= pos.get("tp3_price", float("inf")):
                # P&L déjà réalisé sur TP1 + TP2 (pour affichage total)
                pnl_already   = self._total_pnl  # snapshot avant fermeture
                pnl, mult_final = await self._close_position(addr, cur_price, reason="TP3 x%.1f" % self.tp3)
                pnl_total_pos = (self._total_pnl - pnl_already) + pnl  # TP1+TP2+TP3
                amount_init   = pos.get("amount_usd", 0)
                roi_pct       = (pnl_total_pos / amount_init * 100) if amount_init > 0 else 0
                await self._notify(
                    "🏆🏆 **TP3 — MOON ATTEINT !**\n"
                    "🪙 **%s** x%.1f @ $%.6f\n"
                    "💰 TP3 final: **$%+.2f** | P&L total position: **$%+.2f** (+%.0f%%)\n"
                    "📊 Mise initiale: $%.2f\n"
                    "🎉 LFG !"
                    % (symbol, mult_final, cur_price, pnl, pnl_total_pos, roi_pct, amount_init)
                )

    async def _close_position(self, addr: str, cur_price: float, reason: str = "") -> tuple:
        """
        Ferme une position complètement.
        Retourne (pnl, multiplicateur).
        """
        pos = self._positions.get(addr)
        if not pos:
            return 0.0, 1.0

        entry_price  = pos.get("entry_price", cur_price)
        amount_usd   = pos.get("amount_usd", 0)
        shares       = pos.get("remaining_shares", pos.get("shares", 0))
        symbol       = pos.get("symbol", "?")
        opened_at    = pos.get("opened_at", "?")
        held_h       = (time.time() - pos.get("timestamp", time.time())) / 3600
        paper        = pos.get("paper", True)

        # Utiliser le coût restant réel si des TP partiels ont déjà eu lieu
        # Evite de soustraire la mise initiale entière alors qu'une partie a déjà été récupérée
        cost_basis   = pos.get("amount_usd_remaining", amount_usd)

        payout = shares * cur_price
        pnl    = payout - cost_basis
        mult   = cur_price / entry_price if entry_price > 0 else 1.0

        # Enregistrer
        self._closed_trades.append({
            "symbol":      symbol,
            "address":     addr,
            "entry_price": entry_price,
            "exit_price":  cur_price,
            "amount_usd":  amount_usd,   # mise initiale (pour référence)
            "cost_basis":  cost_basis,   # coût réel des shares restantes
            "payout":      payout,
            "pnl":         pnl,
            "multiplier":  mult,
            "reason":      reason,
            "opened_at":   opened_at,
            "closed_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "held_hours":  round(held_h, 1),
            "paper":       paper,
        })

        self._total_pnl  += pnl
        self._daily_pnl  += pnl

        del self._positions[addr]

        if self.trade_mode == "live" and not paper:
            await self._execute_live_sell(addr, shares, cur_price)

        self._save_state()
        logger.info(
            "CLOSE %s %s @ $%.6f | x%.2f | P&L $%+.2f | %s",
            "PAPER" if paper else "LIVE", symbol, cur_price, mult, pnl, reason
        )
        return pnl, mult

    def _record_partial_pnl(self, pnl: float):
        """Enregistre un P&L de TP partiel"""
        self._total_pnl += pnl
        self._daily_pnl += pnl
        self._save_state()

    def _check_daily_reset(self):
        """Remet à zéro le P&L journalier à minuit + nettoie le vol_tracker"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl_date = today
            self._daily_pnl      = 0.0
            # Nettoyer le tracker de volume (garder seulement les tokens en position)
            active = set(self._positions.keys()) | self._seen_tokens
            before = len(self._vol_tracker)
            self._vol_tracker = {k: v for k, v in self._vol_tracker.items() if k in active}
            logger.info("vol_tracker nettoyé: %d → %d entrées", before, len(self._vol_tracker))

    # ══════════════════════════════════════════════════════════════
    #   PRIX EN TEMPS RÉEL
    # ══════════════════════════════════════════════════════════════

    async def _get_token_price(self, addr: str) -> float:
        """
        Récupère le prix actuel d'un token Solana.
        Sources : DexScreener → Birdeye → Pump.fun.
        """
        if not addr:
            return 0.0

        headers = {"User-Agent": "Mozilla/5.0"}

        # ── DexScreener token-pairs/v1 (endpoint correct) ───
        try:
            async with aiohttp.ClientSession() as session:
                # Essayer d'abord le nouveau endpoint v1
                for url in [
                    "%s/%s" % (DEXSCREENER_V1, addr),
                    "%s/%s" % (DEXSCREENER_PAIRS, addr),
                ]:
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            # token-pairs/v1 retourne une liste directement
                            pairs = data if isinstance(data, list) else (data.get("pairs") or []) if isinstance(data, dict) else []
                            if pairs:
                                # Prendre la paire avec le plus de liquidité
                                best = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), default=pairs[0])
                                p = float(best.get("priceUsd", 0) or 0)
                                if p > 0:
                                    return p
                        await asyncio.sleep(0.1)
        except Exception as e:
            logger.debug("get_price DexScreener %s: %s", addr[:8], e)

        # ── Birdeye (si clé dispo) ────────────────────────────
        birdeye_key = os.getenv("BIRDEYE_API_KEY", "")
        if birdeye_key:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "%s/public/price" % BIRDEYE_API,
                        params={"address": addr},
                        headers={**headers, "X-API-KEY": birdeye_key, "x-chain": "solana"},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            p    = float(data.get("data", {}).get("value", 0) or 0)
                            if p > 0:
                                return p
            except Exception as e:
                logger.debug("get_price Birdeye %s: %s", addr[:8], e)

        # ── Pump.fun (MC / supply) ────────────────────────────
        try:
            pf = await self._fetch_pumpfun_coin(addr)
            if pf:
                mc = float(pf.get("usd_market_cap", 0) or 0)
                # Pump.fun tokens ont une supply de 1B
                if mc > 0:
                    return mc / 1_000_000_000
        except Exception as e:
            logger.debug("get_price PumpFun %s: %s", addr[:8], e)

        return 0.0

    # ══════════════════════════════════════════════════════════════
    #   EXÉCUTION LIVE (SOLANA)
    # ══════════════════════════════════════════════════════════════

    async def _execute_live_buy(self, addr: str, amount_usd: float, price: float) -> Optional[str]:
        """
        Exécute un achat réel sur Pump.fun via Jupiter Aggregator ou Pump.fun API.
        Retourne le tx_hash ou None si échec.

        ⚠️  IMPORTANT : Cette fonction requiert PUMP_WALLET_KEY configuré.
        Elle construit et signe une transaction Solana.
        """
        if not self.wallet_key:
            logger.error("LIVE BUY impossible: PUMP_WALLET_KEY non configuré")
            return None

        logger.info(
            "LIVE BUY %s: $%.2f @ $%.6f",
            addr[:16], amount_usd, price
        )

        # ── Tentative via Jupiter API (swap USDC → token) ────
        try:
            # Convertir USD en lamports USDC (6 decimals)
            USDC_MINT    = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            usdc_amount  = int(amount_usd * 1_000_000)  # 6 decimals

            async with aiohttp.ClientSession() as session:
                # 1. Obtenir le quote Jupiter
                async with session.get(
                    "https://quote-api.jup.ag/v6/quote",
                    params={
                        "inputMint":  USDC_MINT,
                        "outputMint": addr,
                        "amount":     usdc_amount,
                        "slippageBps": 500,  # 5% slippage max
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning("Jupiter quote failed: %d", r.status)
                        return None
                    quote = await r.json()

                # 2. Construire la transaction
                async with session.post(
                    "https://quote-api.jup.ag/v6/swap",
                    json={
                        "quoteResponse":        quote,
                        "userPublicKey":        self._get_public_key(),
                        "wrapAndUnwrapSol":     True,
                        "computeUnitPriceMicroLamports": 100000,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning("Jupiter swap build failed: %d", r.status)
                        return None
                    swap_data   = await r.json()
                    swap_tx_b64 = swap_data.get("swapTransaction", "")

                # 3. Signer et envoyer via RPC Solana
                tx_hash = await self._sign_and_send_transaction(swap_tx_b64)
                if tx_hash:
                    logger.info("LIVE BUY OK: tx=%s", tx_hash[:20])
                    return tx_hash

        except Exception as e:
            logger.error("Live buy error %s: %s", addr[:16], e)

        return None

    async def _execute_live_sell(self, addr: str, shares: float, price: float) -> Optional[str]:
        """
        Exécute une vente réelle sur Jupiter.
        Retourne le tx_hash ou None.
        """
        if not self.wallet_key:
            logger.error("LIVE SELL impossible: PUMP_WALLET_KEY non configuré")
            return None

        logger.info("LIVE SELL %s: %.4f shares @ $%.6f", addr[:16], shares, price)

        try:
            USDC_MINT   = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            # Estimation du montant en plus petite unité (assume 6 decimals)
            token_amount = int(shares * 1_000_000)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://quote-api.jup.ag/v6/quote",
                    params={
                        "inputMint":  addr,
                        "outputMint": USDC_MINT,
                        "amount":     token_amount,
                        "slippageBps": 500,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        return None
                    quote = await r.json()

                async with session.post(
                    "https://quote-api.jup.ag/v6/swap",
                    json={
                        "quoteResponse":  quote,
                        "userPublicKey":  self._get_public_key(),
                        "wrapAndUnwrapSol": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        return None
                    swap_data   = await r.json()
                    swap_tx_b64 = swap_data.get("swapTransaction", "")

                tx_hash = await self._sign_and_send_transaction(swap_tx_b64)
                if tx_hash:
                    logger.info("LIVE SELL OK: tx=%s", tx_hash[:20])
                    return tx_hash

        except Exception as e:
            logger.error("Live sell error %s: %s", addr[:16], e)

        return None

    def _get_public_key(self) -> str:
        """Dérive la clé publique depuis la clé privée"""
        try:
            import base58
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            key_bytes = base58.b58decode(self.wallet_key)
            if len(key_bytes) == 64:
                key_bytes = key_bytes[:32]
            priv = Ed25519PrivateKey.from_private_bytes(key_bytes)
            pub  = priv.public_key().public_bytes_raw()
            return base58.b58encode(pub).decode()
        except ImportError:
            logger.error("base58 ou cryptography non installé. pip install base58 cryptography")
            return ""
        except Exception as e:
            logger.error("get_public_key: %s", e)
            return ""

    async def _sign_and_send_transaction(self, tx_b64: str) -> Optional[str]:
        """
        Signe une transaction versioning Solana et l'envoie au RPC.
        Retourne le tx_hash ou None.
        """
        if not tx_b64 or not self.wallet_key:
            return None
        try:
            import base64, base58
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            # Décoder la transaction
            tx_bytes  = base64.b64decode(tx_b64)
            key_bytes = base58.b58decode(self.wallet_key)
            if len(key_bytes) == 64:
                key_bytes = key_bytes[:32]
            priv_key = Ed25519PrivateKey.from_private_bytes(key_bytes)

            # Signer le message (bytes 65+ pour versioned tx)
            # Format simplifié — en production utiliser solana-py pour la gestion complète
            signature  = priv_key.sign(tx_bytes)
            signed_tx  = base64.b64encode(signature + tx_bytes).decode()

            # Envoyer au RPC Solana
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.solana_rpc,
                    json={
                        "jsonrpc": "2.0",
                        "id":      1,
                        "method":  "sendTransaction",
                        "params":  [
                            signed_tx,
                            {"encoding": "base64", "skipPreflight": False,
                             "preflightCommitment": "confirmed"},
                        ],
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                    if "result" in data:
                        return data["result"]
                    logger.error("sendTransaction error: %s", data.get("error", data))
                    return None

        except ImportError:
            logger.error(
                "Dépendances manquantes pour live trading. "
                "Installe : pip install base58 cryptography"
            )
            return None
        except Exception as e:
            logger.error("sign_and_send_transaction: %s", e)
            return None

    async def _cmd_scanlog(self, args: str, ctx: SkillContext) -> str:
        """Affiche le log détaillé des scans : /pumpscanlog [N=10]"""
        try:
            n = int(args.strip()) if args.strip() else 10
            n = max(1, min(n, 50))
        except ValueError:
            n = 10

        if not self._scan_log:
            return "📋 Aucun scan enregistré.\n`/pumpstart` pour lancer le scanner."

        lines = ["📋 **Scan Log — %d derniers scans**\n━━━━━━━━━━━━━━━" % n]

        for scan in self._scan_log[-n:][::-1]:
            num        = scan.get("num", 0)
            ts         = scan.get("ts", "?")
            elapsed    = scan.get("elapsed_s", 0)
            buf        = scan.get("buf_tokens", 0)
            candidates = scan.get("candidates", 0)
            scored     = scan.get("scored", [])
            ws         = "🟢" if scan.get("ws") else "🔴"
            alerted    = sum(1 for s in scored if s.get("alerted"))

            header = (
                "\n**Scan #%d** — %s %s\n"
                "⏱ %.1fs | 📥 %d buffered → %d candidats → %d scorés → %d alertes"
            ) % (num, ts, ws, elapsed, buf, candidates, len(scored), alerted)
            lines.append(header)

            if scored:
                for s in sorted(scored, key=lambda x: x.get("score", 0), reverse=True):
                    score   = s.get("score", 0)
                    symbol  = s.get("symbol", "?")
                    mc      = self._fmt_k(s.get("mc", 0))
                    holders = s.get("holders", 0)
                    rec     = s.get("rec", "")
                    verdict = s.get("verdict", "")[:50]
                    addr    = s.get("address", "")

                    if score >= self.score_high:
                        icon = "🔴"
                    elif score >= self.score_alert:
                        icon = "🟡"
                    else:
                        icon = "⚪"

                    alerted_tag = " 🔔" if s.get("alerted") else ""
                    row = "  %s **%s** $%s | %d h | **%d/100** %s%s\n     _%s_ %s" % (
                        icon, symbol, mc, holders, score, rec, alerted_tag,
                        verdict, addr[:20]
                    )
                    lines.append(row)
            else:
                lines.append("  _(aucun candidat)_")

        total_candidates = sum(s.get("candidates", 0) for s in self._scan_log)
        total_alerted    = sum(
            sum(1 for sc in s.get("scored", []) if sc.get("alerted"))
            for s in self._scan_log
        )
        lines.append(
            "\n━━━━━━━━━━━━━━━\n📊 **Stats** — %d scans | %d candidats | %d alertes"
            % (len(self._scan_log), total_candidates, total_alerted)
        )
        return "\n".join(lines)


    # ══════════════════════════════════════════════════════════════
    #   PERSISTENCE
    # ══════════════════════════════════════════════════════════════

    def _save_state(self):
        try:
            state = {
                "scan_interval":   self.scan_interval,
                "mc_min":          self.mc_min,
                "mc_max":          self.mc_max,
                "volume_min":      self.volume_min,
                "holders_min":     self.holders_min,
                "snipers_max":     self.snipers_max,
                "dev_hold_max":    self.dev_hold_max,
                "top10_max":       self.top10_max,
                "age_max_hours":   self.age_max_hours,
                "score_alert":     self.score_alert,
                "score_high":      self.score_high,
                "blacklist":       list(self._blacklist),
                "alerts_log":      self._alerts_log[-100:],
                "scan_count":      self._scan_count,
                # Trading
                "trade_mode":      self.trade_mode,
                "buy_amount":      self.buy_amount,
                "buy_amount_high": self.buy_amount_high,
                "buy_score_min":   self.buy_score_min,
                "buy_score_high":  self.buy_score_high,
                "tp1":             self.tp1,
                "tp2":             self.tp2,
                "tp3":             self.tp3,
                "sl":              self.sl,
                "max_open_trades": self.max_open_trades,
                "max_daily_loss":  self.max_daily_loss,
                "positions":       self._positions,
                "closed_trades":   self._closed_trades[-200:],
                "total_pnl":       self._total_pnl,
                "daily_pnl":       self._daily_pnl,
                "daily_pnl_date":  self._daily_pnl_date,
                "scan_log":        [
                    {k: v for k, v in s.items() if k != "scored"}
                    for s in self._scan_log[-50:]
                ],
            }
            with open("/tmp/jarvis_pumpfun_state.json", "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.error("save_state: %s", e)

    def _load_state(self):
        try:
            with open("/tmp/jarvis_pumpfun_state.json") as f:
                state = json.load(f)
            self.scan_interval   = state.get("scan_interval",  self.scan_interval)
            self.mc_min          = state.get("mc_min",         self.mc_min)
            self.mc_max          = state.get("mc_max",         self.mc_max)
            self.volume_min      = state.get("volume_min",     self.volume_min)
            self.holders_min     = state.get("holders_min",    self.holders_min)
            self.snipers_max     = state.get("snipers_max",    self.snipers_max)
            self.dev_hold_max    = state.get("dev_hold_max",   self.dev_hold_max)
            self.top10_max       = state.get("top10_max",      self.top10_max)
            self.age_max_hours   = state.get("age_max_hours",  self.age_max_hours)
            self.score_alert     = state.get("score_alert",    self.score_alert)
            self.score_high      = state.get("score_high",     self.score_high)
            self._blacklist      = set(state.get("blacklist",  []))
            self._alerts_log     = state.get("alerts_log",     [])
            self._scan_count     = state.get("scan_count",     0)
            # Trading
            self.trade_mode      = state.get("trade_mode",     self.trade_mode)
            self.buy_amount      = state.get("buy_amount",     self.buy_amount)
            self.buy_amount_high = state.get("buy_amount_high", self.buy_amount_high)
            self.buy_score_min   = state.get("buy_score_min",  self.buy_score_min)
            self.buy_score_high  = state.get("buy_score_high", self.buy_score_high)
            self.tp1             = state.get("tp1",            self.tp1)
            self.tp2             = state.get("tp2",            self.tp2)
            self.tp3             = state.get("tp3",            self.tp3)
            self.sl              = state.get("sl",             self.sl)
            self.max_open_trades = state.get("max_open_trades", self.max_open_trades)
            self.max_daily_loss  = state.get("max_daily_loss", self.max_daily_loss)
            self._positions      = state.get("positions",      {})
            self._closed_trades  = state.get("closed_trades",  [])
            self._total_pnl      = state.get("total_pnl",      0.0)
            self._daily_pnl      = state.get("daily_pnl",      0.0)
            self._daily_pnl_date = state.get("daily_pnl_date", "")
            # Note: scored details non persistés (trop lourd), juste les méta
            saved_scan_log = state.get("scan_log", [])
            self._scan_log = [{**s, "scored": []} for s in saved_scan_log]
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error("load_state: %s", e)

    # ══════════════════════════════════════════════════════════════
    #   HELPERS
    # ══════════════════════════════════════════════════════════════

    async def _notify(self, text: str):
        """Envoie une notification proactive via Telegram"""
        if not self._send_callback:
            return
        # Priorité : user_id stocké au /pumpstart, fallback sur le contexte courant
        uid = self._notify_user_id or (self._context.user_id if self._context else 0)
        if uid:
            try:
                await self._send_callback(uid, text)
            except Exception as e:
                logger.error("notify: %s", e)
        else:
            logger.warning("_notify: aucun user_id disponible, message perdu: %s", text[:50])

    @staticmethod
    def _fmt_k(n) -> str:
        """Formater un nombre : 25000 → '25K', 1500000 → '1.5M'"""
        try:
            n = float(n)
        except (TypeError, ValueError):
            return "?"
        if n >= 1_000_000:
            return "%.1fM" % (n / 1_000_000)
        if n >= 1_000:
            return "%.0fK" % (n / 1_000)
        return "%.0f" % n

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        """Formater une durée : 3723 → '1h2m'"""
        if seconds < 60:
            return "%ds" % seconds
        if seconds < 3600:
            return "%dm%ds" % (seconds // 60, seconds % 60)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return "%dh%dm" % (h, m) if m else "%dh" % h


# ══════════════════════════════════════════════════════════════════
#   POINT D'ENTRÉE AUTONOME (test sans JARVIS)
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Test autonome : lance un scan unique et affiche les résultats.
    Usage : python skill_pumpfun.py
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _test():
        skill = PumpFunSkill()
        print("\n=== PUMP.FUN SCANNER — TEST ===\n")
        print("Filtres actifs :")
        print("  MC: $%s–$%s" % (skill._fmt_k(skill.mc_min), skill._fmt_k(skill.mc_max)))
        print("  Volume: >$%s" % skill._fmt_k(skill.volume_min))
        print("  Holders: >%d" % skill.holders_min)
        print("  Snipers: <%d | Dev: <%d%% | Top10: <%d%%" % (
            skill.snipers_max, skill.dev_hold_max, skill.top10_max))
        print()

        print("Récupération des tokens...")
        raw   = await skill._fetch_pump_tokens()
        print("%d tokens bruts récupérés" % len(raw))

        candidates = []
        for token in raw[:20]:  # Limiter à 20 pour le test
            enriched = await skill._enrich_token(token)
            if not enriched:
                continue
            ok, reason = skill._apply_filters(enriched)
            if ok:
                candidates.append(enriched)
                print("✅ %s (%s) | MC:$%s Vol:$%s H:%d" % (
                    enriched.get("symbol","?"),
                    enriched.get("address","")[:8],
                    skill._fmt_k(enriched.get("market_cap",0)),
                    skill._fmt_k(enriched.get("volume_24h",0)),
                    enriched.get("holders",0),
                ))
            else:
                print("❌ %s — %s" % (token.get("symbol","?"), reason))

        print("\n%d candidats passent les filtres" % len(candidates))

        if candidates and skill.anthropic_key:
            print("\nScoring du premier candidat via Claude...")
            result = await skill._score_with_claude(candidates[0])
            print("Score: %d/100" % result.get("score", 0))
            print("Verdict: %s" % result.get("verdict", ""))
            print("Recommendation: %s" % result.get("recommendation", ""))
        elif not skill.anthropic_key:
            print("\n⚠️  ANTHROPIC_API_KEY non configurée → scoring désactivé")

    asyncio.run(_test())