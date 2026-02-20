"""
╔══════════════════════════════════════════════════════════════╗
║           SKILL JARVIS : CopyTrading Polymarket              ║
║                                                              ║
║  Surveille le leaderboard Polymarket, copie les trades       ║
║  des top 10 traders sur les marchés crypto/politique.        ║
║                                                              ║
║  Dépendances: py-clob-client, web3, aiohttp                  ║
║  (déjà présentes si skill_polyjarvis.py est installée)       ║
║                                                              ║
║  Variables .env optionnelles:                                ║
║    COPY_MAX_POSITION   = 10       (USDC max par trade)       ║
║    COPY_INTERVAL_MIN   = 15       (minutes entre cycles)     ║
║    COPY_PAPER_MODE     = true     (paper trading par défaut) ║
║    COPY_LEADERBOARD_WINDOW = 30d  (1d | 7d | 30d | all)     ║
╚══════════════════════════════════════════════════════════════╝

COMMANDES TELEGRAM:
  /copystart          — Démarrer la surveillance automatique
  /copystop           — Arrêter la surveillance
  /copystatus         — Positions copiées + P&L
  /copyleaders        — Voir le top 10 traders suivis
  /copymode paper     — Passer en paper trading (simulation)
  /copymode live      — Passer en live trading (réel)
  /copylog            — Derniers trades loggés
  /copyreset          — Réinitialiser le circuit breaker
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp

from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.CopyTrading")

# ── Constantes API ─────────────────────────────────────────────
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Catégories Polymarket à copier (crypto + politique)
TARGET_TAGS = {
    "crypto", "bitcoin", "ethereum", "politics", "politique",
    "elections", "election", "us-politics", "crypto-markets",
    "defi", "blockchain", "president", "congress", "government",
    "trump", "fed", "economy", "finance",
}

# Fichiers de persistance
COPY_LOG_FILE      = Path("config/copy_trades.json")
COPY_STATE_FILE    = Path("config/copy_state.json")
COPY_LEADERS_FILE  = Path("config/copy_leaders.json")

# Circuit breaker
MAX_CONSECUTIVE_LOSSES = 3


# ══════════════════════════════════════════════════════════════
#   SKILL PRINCIPALE
# ══════════════════════════════════════════════════════════════

class CopyTradingSkill(BaseSkill):
    SKILL_NAME    = "copytrading"
    SKILL_DESC    = "Copy trading automatique des top traders Polymarket"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR  = "JARVIS"

    SKILL_COMMANDS = {
        "copystart":   "Démarrer la surveillance automatique",
        "copystop":    "Arrêter la surveillance",
        "copystatus":  "Positions copiées + P&L en temps réel",
        "copyleaders": "Top 10 traders actuellement suivis",
        "copymode":    "Changer le mode (`/copymode paper` ou `/copymode live`)",
        "copylog":     "Voir les 10 derniers trades loggés",
        "copyreset":   "Réinitialiser le circuit breaker",
    }

    # ── Init ────────────────────────────────────────────────────

    def __init__(self, settings=None):
        super().__init__(settings)

        # Config depuis .env
        self.max_position   = float(os.getenv("COPY_MAX_POSITION", "10"))
        self.interval_min   = int(os.getenv("COPY_INTERVAL_MIN", "15"))
        self.paper_mode     = os.getenv("COPY_PAPER_MODE", "true").lower() != "false"
        self.lb_window      = os.getenv("COPY_LEADERBOARD_WINDOW", "30d")

        # État runtime
        self._running        = False
        self._loop_task: Optional[asyncio.Task] = None
        self._leaders: list  = []          # Top 10 wallets trackés
        self._seen_trades: set = set()     # IDs de trades déjà copiés
        self._trades_log: list = []        # Historique des copies
        self._context: Optional[SkillContext] = None
        self._send_callback  = None        # Callback Telegram

        # Circuit breaker
        self._consecutive_losses = 0
        self._circuit_open       = False

        # Stats paper trading
        self._paper_positions: dict = {}
        self._paper_pnl_total = 0.0

        self._load_state()

    async def setup(self) -> bool:
        self._ready = True
        mode = "📄 PAPER" if self.paper_mode else "💸 LIVE"
        logger.info(
            f"✅ CopyTrading prêt | Mode: {mode} | "
            f"Max: {self.max_position}$ | Cycle: {self.interval_min}min"
        )
        return True

    async def teardown(self):
        await self._stop_loop()
        self._save_state()
        self._ready = False

    def set_send_callback(self, callback):
        self._send_callback = callback

    # ── Router ──────────────────────────────────────────────────

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        self._context = context
        routes = {
            "copystart":   self._cmd_start,
            "copystop":    self._cmd_stop,
            "copystatus":  self._cmd_status,
            "copyleaders": self._cmd_leaders,
            "copymode":    self._cmd_mode,
            "copylog":     self._cmd_log,
            "copyreset":   self._cmd_reset,
        }
        handler = routes.get(command)
        if handler:
            try:
                return await handler(args.strip(), context)
            except Exception as e:
                logger.error(f"Erreur CopyTrading /{command}: {e}", exc_info=True)
                return f"❌ Erreur: `{e}`"
        return "❓ Commande inconnue."

    # ══════════════════════════════════════════════════════════════
    #   COMMANDES TELEGRAM
    # ══════════════════════════════════════════════════════════════

    async def _cmd_start(self, args: str, ctx: SkillContext) -> str:
        if self._running:
            return (
                f"⚠️ CopyTrading déjà actif !\n"
                f"Mode: {'📄 PAPER' if self.paper_mode else '💸 LIVE'} | "
                f"Cycle: {self.interval_min}min\n\n"
                f"💡 `/copystop` pour arrêter"
            )

        if self._circuit_open:
            return (
                f"🔴 **Circuit breaker actif !**\n"
                f"{MAX_CONSECUTIVE_LOSSES} pertes consécutives détectées.\n\n"
                f"💡 `/copyreset` pour réinitialiser manuellement"
            )

        # Charger les leaders avant de démarrer
        leaders = await self._fetch_leaderboard()
        if not leaders:
            return "❌ Impossible de charger le leaderboard. Réessaie dans quelques instants."

        self._leaders = leaders
        self._save_leaders()

        mode_str = "📄 PAPER (simulation)" if self.paper_mode else "💸 LIVE (fonds réels)"
        self._running = True
        self._loop_task = asyncio.create_task(self._main_loop(ctx))

        return (
            f"🚀 **CopyTrading démarré !**\n━━━━━━━━━━━━━━━\n"
            f"Mode: **{mode_str}**\n"
            f"👥 Traders suivis: **{len(self._leaders)}**\n"
            f"💰 Position max: **{self.max_position} USDC** par trade\n"
            f"⏱ Cycle: toutes les **{self.interval_min} minutes**\n"
            f"🏷 Catégories: **crypto + politique**\n"
            f"🛑 Circuit breaker: après **{MAX_CONSECUTIVE_LOSSES} pertes** consécutives\n\n"
            f"{'⚠️ Mode PAPER : aucun fonds réel utilisé.' if self.paper_mode else '⚠️ Mode LIVE : trades réels sur Polygon !'}\n\n"
            f"💡 `/copystatus` pour suivre | `/copyleaders` pour voir les traders"
        )

    async def _cmd_stop(self, args: str, ctx: SkillContext) -> str:
        if not self._running:
            return "ℹ️ CopyTrading n'est pas actif."

        await self._stop_loop()
        total = len(self._trades_log)
        return (
            f"⏹ **CopyTrading arrêté.**\n━━━━━━━━━━━━━━━\n"
            f"📊 Trades copiés durant la session: **{total}**\n\n"
            f"💡 `/copystatus` pour voir le bilan final\n"
            f"💡 `/copystart` pour redémarrer"
        )

    async def _cmd_status(self, args: str, ctx: SkillContext) -> str:
        status_icon = "🟢 Actif" if self._running else "🔴 Arrêté"
        mode_str    = "📄 PAPER" if self.paper_mode else "💸 LIVE"
        cb_str      = f"🔴 OUVERT ({self._consecutive_losses} pertes)" if self._circuit_open else f"🟢 OK ({self._consecutive_losses}/{MAX_CONSECUTIVE_LOSSES})"

        lines = [
            f"📊 **CopyTrading Status**\n━━━━━━━━━━━━━━━",
            f"🔘 État: {status_icon}",
            f"🎮 Mode: {mode_str}",
            f"⚡ Circuit breaker: {cb_str}",
            f"👥 Traders suivis: {len(self._leaders)}",
            f"📋 Trades copiés (total): {len(self._trades_log)}",
            f"💰 Position max: {self.max_position} USDC",
            f"⏱ Cycle: {self.interval_min} min",
            "",
        ]

        # Résumé P&L paper
        if self.paper_mode and self._paper_positions:
            lines.append("📄 **Positions Paper Trading**\n━━━━━━━━━━━━━━━")
            total_in   = 0.0
            total_val  = 0.0

            for key, pos in list(self._paper_positions.items())[-8:]:
                invested  = pos.get("amount_usd", 0)
                entry     = pos.get("entry_price", 0)
                shares    = pos.get("shares", 0)
                side      = pos.get("side", "?")

                # Récupérer prix actuel via Gamma
                cur_price = await self._get_current_price(
                    pos.get("cond_id", ""), side
                )
                cur_price = cur_price if cur_price > 0 else entry

                cur_val  = shares * cur_price
                pnl      = cur_val - invested
                pnl_pct  = (pnl / invested * 100) if invested else 0
                icon     = "🟢" if pnl >= 0 else "🔴"

                total_in  += invested
                total_val += cur_val

                lines.append(
                    f"{icon} **{side}** `{pos.get('market_title','?')[:35]}…`\n"
                    f"   ${invested:.2f} → ${cur_val:.2f} | {icon} {pnl:+.2f}$ ({pnl_pct:+.1f}%)"
                )

            total_pnl     = total_val - total_in
            total_pnl_pct = (total_pnl / total_in * 100) if total_in else 0
            pnl_icon      = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(
                f"\n━━━━━━━━━━━━━━━\n"
                f"💰 Investi (paper): `${total_in:.2f}`\n"
                f"💹 Valeur actuelle: `${total_val:.2f}`\n"
                f"{pnl_icon} **P&L total: `${total_pnl:+.2f}` ({total_pnl_pct:+.1f}%)**"
            )

        return "\n".join(lines)

    async def _cmd_leaders(self, args: str, ctx: SkillContext) -> str:
        if not self._leaders:
            leaders = await self._fetch_leaderboard()
            if not leaders:
                return "❌ Impossible de charger le leaderboard."
            self._leaders = leaders
            self._save_leaders()

        lines = [f"🏆 **Top {len(self._leaders)} Traders Polymarket** (fenêtre: {self.lb_window})\n━━━━━━━━━━━━━━━"]
        for i, trader in enumerate(self._leaders, 1):
            profit  = float(trader.get("profit", 0))
            volume  = float(trader.get("volume", 0))
            name    = trader.get("name") or trader.get("pseudonym") or "Anonyme"
            wallet  = trader.get("proxyWallet", trader.get("address", "?"))
            icon    = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"`{i}.`"

            lines.append(
                f"{icon} **{name}**\n"
                f"   💰 Profit: `${profit:,.0f}` | Vol: `${volume:,.0f}`\n"
                f"   📍 `{wallet[:20]}…`"
            )

        lines.append(f"\n🔄 Mis à jour à chaque démarrage | `/copystart` pour démarrer")
        return "\n".join(lines)

    async def _cmd_mode(self, args: str, ctx: SkillContext) -> str:
        if args.lower() == "paper":
            self.paper_mode = True
            self._save_state()
            return (
                "📄 **Mode PAPER activé**\n"
                "Les trades sont simulés — aucun fonds réel utilisé.\n\n"
                "💡 `/copymode live` pour passer en trading réel"
            )
        elif args.lower() == "live":
            # Vérifier que PolyJarvis est configuré
            polyjarvis = ctx.agent.skill_manager._skills.get("polyjarvis") if ctx.agent else None
            if not polyjarvis or not polyjarvis._clob:
                return (
                    "❌ **Mode LIVE impossible**\n"
                    "La skill PolyJarvis n'est pas configurée avec un wallet.\n\n"
                    "Configure `POLY_PRIVATE_KEY` et `POLY_FUNDER` dans ton `.env`\n"
                    "puis relance JARVIS."
                )
            self.paper_mode = False
            self._save_state()
            return (
                "⚠️ **Mode LIVE activé !**\n"
                "━━━━━━━━━━━━━━━\n"
                "Les trades seront exécutés avec de **vrais fonds** sur Polygon.\n"
                f"Position max: **{self.max_position} USDC** par trade.\n\n"
                "💡 `/copymode paper` pour revenir en simulation"
            )
        return "Usage: `/copymode paper` ou `/copymode live`"

    async def _cmd_log(self, args: str, ctx: SkillContext) -> str:
        if not self._trades_log:
            return "📭 Aucun trade copié pour l'instant."

        lines = [f"📋 **Derniers trades copiés** ({len(self._trades_log)} total)\n━━━━━━━━━━━━━━━"]
        for trade in reversed(self._trades_log[-10:]):
            mode_icon = "📄" if trade.get("paper") else "💸"
            side_icon = "🟢" if trade.get("side") == "YES" else "🔴"
            ts        = trade.get("timestamp", "?")
            trader    = trade.get("trader_name", "?")[:12]
            title     = trade.get("market_title", "?")[:40]
            amount    = trade.get("amount_usd", 0)
            result    = trade.get("result", "pending")

            result_icon = "✅" if result == "executed" else "📄" if result == "simulated" else "⚠️"

            lines.append(
                f"{mode_icon} {side_icon} **{trade.get('side','?')}** `${amount:.0f}` — _{title}…_\n"
                f"   👤 {trader} | {ts} | {result_icon} {result}"
            )

        return "\n".join(lines)

    async def _cmd_reset(self, args: str, ctx: SkillContext) -> str:
        was_open = self._circuit_open
        self._consecutive_losses = 0
        self._circuit_open       = False
        self._save_state()

        if was_open:
            return (
                "✅ **Circuit breaker réinitialisé !**\n"
                "Le trading peut reprendre.\n\n"
                "💡 `/copystart` pour redémarrer la surveillance"
            )
        return f"ℹ️ Circuit breaker était déjà OK ({self._consecutive_losses}/{MAX_CONSECUTIVE_LOSSES} pertes)."

    # ══════════════════════════════════════════════════════════════
    #   BOUCLE PRINCIPALE
    # ══════════════════════════════════════════════════════════════

    async def _main_loop(self, ctx: SkillContext):
        """Boucle principale — tourne toutes les N minutes"""
        logger.info(f"🔄 CopyTrading loop démarrée (cycle: {self.interval_min}min)")

        cycle = 0
        while self._running:
            cycle += 1
            try:
                await self._run_cycle(ctx, cycle)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur cycle #{cycle}: {e}", exc_info=True)
                await self._notify(f"⚠️ CopyTrading — erreur cycle #{cycle}:\n`{e}`")

            # Attendre le prochain cycle
            try:
                await asyncio.sleep(self.interval_min * 60)
            except asyncio.CancelledError:
                break

        logger.info("⏹ CopyTrading loop arrêtée")

    async def _run_cycle(self, ctx: SkillContext, cycle: int):
        """Un cycle complet : leaderboard → trades → copie"""
        logger.info(f"🔄 Cycle #{cycle} — {datetime.now().strftime('%H:%M:%S')}")

        # 1. Vérifier circuit breaker
        if self._circuit_open:
            await self._notify(
                f"🔴 **Circuit breaker ACTIF** — trading suspendu.\n"
                f"{self._consecutive_losses} pertes consécutives.\n"
                f"💡 `/copyreset` pour réinitialiser."
            )
            return

        # 2. Rafraîchir le leaderboard toutes les 3 heures (12 cycles de 15 min)
        if cycle % 12 == 1:
            leaders = await self._fetch_leaderboard()
            if leaders:
                self._leaders = leaders
                self._save_leaders()
                logger.info(f"👥 Leaderboard rafraîchi: {len(leaders)} traders")

        if not self._leaders:
            logger.warning("Leaderboard vide, skip cycle")
            return

        # 3. Récupérer les trades récents des top traders
        new_trades_found = 0
        for trader in self._leaders[:10]:
            wallet = trader.get("proxyWallet") or trader.get("address", "")
            if not wallet:
                continue

            try:
                recent_trades = await self._fetch_trader_activity(wallet)
                for trade in recent_trades:
                    copied = await self._process_trade(trade, trader, ctx)
                    if copied:
                        new_trades_found += 1
            except Exception as e:
                logger.warning(f"Erreur fetch trader {wallet[:10]}…: {e}")

            # Petite pause entre les traders pour respecter le rate limit
            await asyncio.sleep(0.5)

        if new_trades_found > 0:
            logger.info(f"✅ Cycle #{cycle} — {new_trades_found} nouveau(x) trade(s) copié(s)")
        else:
            logger.info(f"✅ Cycle #{cycle} — aucun nouveau trade détecté")

    async def _process_trade(self, trade: dict, trader: dict, ctx: SkillContext) -> bool:
        """Traite un trade candidat et le copie si éligible"""

        trade_id = trade.get("transactionHash") or f"{trade.get('asset','')}-{trade.get('timestamp','')}"

        # Déduplication
        if trade_id in self._seen_trades:
            return False
        self._seen_trades.add(trade_id)

        # Filtrer : seulement les achats (BUY), pas les ventes
        if trade.get("side", "").upper() != "BUY":
            return False

        # Filtrer : uniquement crypto + politique
        title = (trade.get("title") or trade.get("slug") or "").lower()
        tags  = (trade.get("tags") or [])
        tags_lower = [str(t).lower() for t in tags]

        is_target = any(kw in title for kw in TARGET_TAGS) or \
                    any(any(kw in tag for kw in TARGET_TAGS) for tag in tags_lower)

        if not is_target:
            logger.debug(f"Ignoré (hors catégorie): {title[:40]}")
            return False

        # Récupérer les infos du marché
        cond_id    = trade.get("conditionId", "")
        outcome    = trade.get("outcome", "Yes").upper()
        side       = "YES" if outcome in ("YES", "YES") else "NO"
        entry_p    = float(trade.get("price") or trade.get("avgPrice") or 0)
        mkt_title  = trade.get("title", "Marché inconnu")
        trader_name = trader.get("name") or trader.get("pseudonym") or trader.get("proxyWallet", "?")[:10]

        if not cond_id or entry_p <= 0:
            return False

        # Prix trop élevé pour un trade intéressant (>0.95 = peu de valeur)
        if entry_p > 0.95:
            logger.debug(f"Ignoré (prix trop haut {entry_p:.2f}): {mkt_title[:30]}")
            return False

        logger.info(
            f"📋 Trade à copier: {trader_name} | {side} @ {entry_p:.3f}$ | "
            f"{mkt_title[:40]} | condID: {cond_id[:16]}…"
        )

        # Exécuter ou simuler
        amount = min(self.max_position, 10.0)
        result = await self._execute_or_simulate(cond_id, side, amount, entry_p, ctx)

        # Logger le trade
        log_entry = {
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "trader_name":  trader_name,
            "trader_wallet": trader.get("proxyWallet", "?"),
            "cond_id":      cond_id,
            "market_title": mkt_title,
            "side":         side,
            "entry_price":  entry_p,
            "amount_usd":   amount,
            "paper":        self.paper_mode,
            "result":       result,
            "tx_hash":      trade_id,
        }
        self._trades_log.append(log_entry)
        self._save_log()

        # Notification Telegram
        mode_icon = "📄" if self.paper_mode else "💸"
        side_icon = "🟢" if side == "YES" else "🔴"
        await self._notify(
            f"{mode_icon} **CopyTrade** {'(Paper)' if self.paper_mode else '(Live)'}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 Trader: **{trader_name}**\n"
            f"{side_icon} Côté: **{side}** @ `{entry_p:.3f}$`\n"
            f"💰 Montant: **${amount:.2f} USDC**\n"
            f"📌 _{mkt_title[:55]}_\n"
            f"{'✅ Simulé' if self.paper_mode else '✅ Exécuté sur Polygon'}"
        )

        return True

    async def _execute_or_simulate(
        self, cond_id: str, side: str, amount: float, entry_price: float, ctx: SkillContext
    ) -> str:
        """Exécute le trade (live) ou le simule (paper)"""
        shares = amount / entry_price if entry_price > 0 else 0

        if self.paper_mode:
            # Stocker la position simulée
            key = f"{cond_id}_{side}_{int(time.time())}"
            self._paper_positions[key] = {
                "cond_id":     cond_id,
                "side":        side,
                "amount_usd":  amount,
                "entry_price": entry_price,
                "shares":      shares,
                "opened_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                "market_title": "",  # rempli après si dispo
            }
            self._save_state()
            return "simulated"

        else:
            # Exécution réelle via PolyJarvis
            polyjarvis = ctx.agent.skill_manager._skills.get("polyjarvis") if ctx.agent else None
            if not polyjarvis:
                logger.error("PolyJarvis non disponible pour l'exécution live !")
                return "error_no_polyjarvis"

            try:
                result = await polyjarvis._cmd_buy(
                    f"{cond_id} {side} {amount}", ctx
                )
                # Évaluer si succès ou échec d'après la réponse
                if "✅" in result:
                    self._consecutive_losses = 0  # reset sur succès
                    return "executed"
                else:
                    self._handle_loss()
                    return "failed"
            except Exception as e:
                logger.error(f"Erreur exécution trade live: {e}")
                self._handle_loss()
                return f"error: {e}"

    # ══════════════════════════════════════════════════════════════
    #   CIRCUIT BREAKER
    # ══════════════════════════════════════════════════════════════

    def _handle_loss(self):
        self._consecutive_losses += 1
        logger.warning(f"⚠️ Perte #{self._consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}")

        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self._circuit_open = True
            self._running = False
            self._save_state()
            logger.error(f"🔴 CIRCUIT BREAKER OUVERT — {MAX_CONSECUTIVE_LOSSES} pertes consécutives !")
            asyncio.create_task(self._notify(
                f"🔴 **CIRCUIT BREAKER DÉCLENCHÉ !**\n"
                f"━━━━━━━━━━━━━━━\n"
                f"**{MAX_CONSECUTIVE_LOSSES} pertes consécutives** détectées.\n"
                f"Le copy trading a été **automatiquement arrêté**.\n\n"
                f"💡 `/copyreset` pour réinitialiser\n"
                f"💡 `/copystatus` pour voir le bilan"
            ))

    # ══════════════════════════════════════════════════════════════
    #   APPELS API
    # ══════════════════════════════════════════════════════════════

    async def _fetch_leaderboard(self) -> list:
        """Récupère le top 10 du leaderboard Polymarket (profit, fenêtre configurée)"""
        # Mapping window → paramètre API
        window_map = {"1d": "DAY", "7d": "WEEK", "30d": "MONTH", "all": "ALL"}
        window_param = window_map.get(self.lb_window, "MONTH")

        endpoints_to_try = [
            # Data API officielle
            f"{DATA_API}/leaderboard?limit=20&window={window_param}&sortBy=PROFIT&sortDirection=DESC",
            # Fallback Gamma API
            f"{GAMMA_API}/leaderboard?limit=20&window={self.lb_window}&sortBy=profit",
        ]

        for url in endpoints_to_try:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            leaders = data if isinstance(data, list) else data.get("data", data.get("results", []))
                            if leaders:
                                logger.info(f"✅ Leaderboard chargé: {len(leaders)} traders (via {url[:40]}…)")
                                return leaders[:10]
            except Exception as e:
                logger.warning(f"Leaderboard endpoint échoué ({url[:40]}…): {e}")

        logger.error("❌ Tous les endpoints leaderboard ont échoué")
        return []

    async def _fetch_trader_activity(self, wallet: str, since_minutes: int = None) -> list:
        """Récupère l'activité récente d'un trader"""
        since_minutes = since_minutes or (self.interval_min + 2)
        since_ts      = int(time.time()) - (since_minutes * 60)

        url = (
            f"{DATA_API}/activity"
            f"?user={wallet}"
            f"&type=TRADE"
            f"&side=BUY"
            f"&start={since_ts}"
            f"&sortBy=TIMESTAMP"
            f"&sortDirection=DESC"
            f"&limit=10"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.warning(f"Erreur activity {wallet[:12]}…: {e}")

        return []

    async def _get_current_price(self, cond_id: str, side: str) -> float:
        """Récupère le prix actuel d'un token depuis Gamma"""
        if not cond_id:
            return 0.0
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{GAMMA_API}/markets",
                    params={"condition_ids": cond_id},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        m = data[0] if isinstance(data, list) and data else {}
                        tokens = m.get("tokens", [])
                        for t in tokens:
                            if t.get("outcome", "").lower() == side.lower():
                                price = t.get("price") or t.get("last_trade_price")
                                if price:
                                    return float(price)
        except Exception:
            pass
        return 0.0

    # ══════════════════════════════════════════════════════════════
    #   HELPERS
    # ══════════════════════════════════════════════════════════════

    async def _stop_loop(self):
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._loop_task = None

    async def _notify(self, text: str):
        """Envoie une notification Telegram proactive"""
        if self._send_callback and self._context:
            try:
                await self._send_callback(self._context.user_id, text)
            except Exception as e:
                logger.error(f"Erreur notification: {e}")

    def _save_state(self):
        COPY_STATE_FILE.parent.mkdir(exist_ok=True)
        state = {
            "paper_mode":           self.paper_mode,
            "consecutive_losses":   self._consecutive_losses,
            "circuit_open":         self._circuit_open,
            "paper_positions":      self._paper_positions,
            "seen_trades":          list(self._seen_trades)[-500:],  # garder les 500 derniers
        }
        with open(COPY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def _load_state(self):
        if COPY_STATE_FILE.exists():
            try:
                with open(COPY_STATE_FILE) as f:
                    state = json.load(f)
                self.paper_mode           = state.get("paper_mode", True)
                self._consecutive_losses  = state.get("consecutive_losses", 0)
                self._circuit_open        = state.get("circuit_open", False)
                self._paper_positions     = state.get("paper_positions", {})
                self._seen_trades         = set(state.get("seen_trades", []))
            except Exception as e:
                logger.warning(f"Erreur chargement état: {e}")

        if COPY_LOG_FILE.exists():
            try:
                with open(COPY_LOG_FILE) as f:
                    self._trades_log = json.load(f)
            except Exception:
                self._trades_log = []

    def _save_log(self):
        COPY_LOG_FILE.parent.mkdir(exist_ok=True)
        # Garder les 1000 derniers trades max
        log_to_save = self._trades_log[-1000:]
        with open(COPY_LOG_FILE, "w") as f:
            json.dump(log_to_save, f, indent=2, ensure_ascii=False)

    def _save_leaders(self):
        COPY_LEADERS_FILE.parent.mkdir(exist_ok=True)
        with open(COPY_LEADERS_FILE, "w") as f:
            json.dump(self._leaders, f, indent=2, ensure_ascii=False)
