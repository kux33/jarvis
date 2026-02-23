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

# Cookie navigateur (bypass géoblocage Polymarket)
POLY_BROWSER_COOKIE = os.getenv("POLY_BROWSER_COOKIE", "")

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
        "copyreset":      "Réinitialiser le circuit breaker",
        "copypaperreset": "Remettre à zéro le paper trading (positions + P&L)",
        "copywatch":   "Surveiller un wallet spécifique (`/copywatch 0x...`)",
        "copyunwatch": "Arrêter de surveiller un wallet (`/copyunwatch 0x...`)",
        "copywatched": "Lister les wallets surveillés",
    }

    # ── Init ────────────────────────────────────────────────────

    def __init__(self, settings=None):
        super().__init__(settings)

        # Config depuis .env
        self.max_position   = float(os.getenv("COPY_MAX_POSITION", "10"))
        self.interval_min   = int(os.getenv("COPY_INTERVAL_MIN", "15"))
        self.paper_mode     = os.getenv("COPY_PAPER_MODE", "true").lower() != "false"
        self.budget_total   = float(os.getenv("COPY_BUDGET", "100"))  # $ total alloué au copytrading
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

        # Wallets custom à surveiller
        self._watched_wallets: dict = {}  # {address: label}

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
            "copyreset":      self._cmd_reset,
            "copypaperreset": self._cmd_paper_reset,
            "copywatch":   self._cmd_watch,
            "copyunwatch": self._cmd_unwatch,
            "copywatched": self._cmd_watched,
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


        # Charger les leaders (non bloquant si wallets custom configurés)
        leaders = await self._fetch_leaderboard()
        if leaders:
            self._leaders = leaders
            self._save_leaders()

        # Vérifier qu'on a au moins quelque chose à surveiller
        total_watchable = len(self._watched_wallets) + len(self._leaders)
        if total_watchable == 0:
            return (
                "❌ **Rien à surveiller !**\n━━━━━━━━━━━━━━━\n"
                "Leaderboard indisponible et aucun wallet custom configuré.\n\n"
                "Ajoute un wallet puis relance :\n"
                "`/copywatch 0xTonTrader MonLabel`\n"
                "`/copystart`"
            )

        mode_str = "📄 PAPER (simulation)" if self.paper_mode else "💸 LIVE (fonds réels)"
        self._running = True
        self._loop_task = asyncio.create_task(self._main_loop(ctx))

        custom_count = len(self._watched_wallets)
        leader_count = len(self._leaders)
        lb_note = "" if leader_count else "\n⚠️ Leaderboard indisponible — wallets custom uniquement."

        return (
            f"🚀 **CopyTrading démarré !**\n━━━━━━━━━━━━━━━\n"
            f"Mode: **{mode_str}**\n"
            f"👤 Wallets custom: **{custom_count}** | 🏆 Leaderboard: **{leader_count}**{lb_note}\n"
            f"💰 Position max: **{self.max_position} USDC** par trade\n"
            f"⏱ Cycle: toutes les **{self.interval_min} minutes**\n"
            f"🛑 Circuit breaker: après **{MAX_CONSECUTIVE_LOSSES} pertes**\n\n"
            f"{'⚠️ Mode PAPER : simulation uniquement.' if self.paper_mode else '⚠️ Mode LIVE : trades réels !'}\n\n"
            f"💡 `/copywatched` pour voir les wallets surveillés"
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
            nb_pos = len(self._paper_positions)
            lines.append(f"📄 **Positions Paper Trading ({nb_pos})**\n━━━━━━━━━━━━━━━")
            total_in  = 0.0
            total_val = 0.0

            # Trier par timestamp décroissant (les plus récentes en premier)
            sorted_pos = sorted(
                self._paper_positions.items(),
                key=lambda x: x[1].get("timestamp", 0),
                reverse=True
            )

            for key, pos in sorted_pos[:8]:
                invested   = pos.get("amount_usd", 0)
                entry      = pos.get("entry_price", 0)
                shares     = pos.get("shares", 0)
                side       = pos.get("side", "?")
                cond_id_p  = pos.get("cond_id", "")
                title      = pos.get("market_title") or cond_id_p[:20] or "?"
                opened     = pos.get("opened_at", "?")

                if invested <= 0 or entry <= 0:
                    continue

                # Recalculer shares si manquant (bug sur anciennes positions)
                if shares <= 0 and entry > 0:
                    shares = invested / entry

                # Prix actuel depuis Gamma
                cur_price = await self._get_current_price(cond_id_p, side)

                # Fallback : si l'API échoue ou retourne 0, on garde entry_price
                if cur_price <= 0 or cur_price > 1.0:
                    cur_price = entry
                    price_tag = " (estimé)"
                else:
                    price_tag = ""

                cur_val = shares * cur_price
                pnl     = cur_val - invested
                pnl_pct = (pnl / invested * 100) if invested > 0 else 0
                icon    = "🟢" if pnl >= 0 else "🔴"

                total_in  += invested
                total_val += cur_val

                lines.append(
                    f"{icon} **{side}** — _{title[:40]}_\n"
                    f"   Entrée `{entry:.4f}` → Actuel `{cur_price:.4f}`{price_tag}\n"
                    f"   `${invested:.2f}` → `${cur_val:.2f}` | **{pnl:+.2f}$** ({pnl_pct:+.1f}%) | {opened}"
                )

            if nb_pos > 8:
                lines.append(f"_… et {nb_pos - 8} autres positions_")

            total_pnl     = total_val - total_in
            total_pnl_pct = (total_pnl / total_in * 100) if total_in > 0 else 0
            pnl_icon      = "🟢" if total_pnl >= 0 else "🔴"
            realized_icon = "🟢" if self._paper_pnl_total >= 0 else "🔴"
            lines.append(
                "\n━━━━━━━━━━━━━━━\n"
                "💰 Investi (paper): `$" + ("%.2f" % total_in) + "`\n"
                "💹 Valeur actuelle: `$" + ("%.2f" % total_val) + "`\n"
                + pnl_icon + " **P&L latent: `$" + ("%+.2f" % total_pnl) + "` (" + ("%.1f" % total_pnl_pct) + "%)**\n"
                + realized_icon + " **P&L réalisé (résolutions): `$" + ("%+.2f" % self._paper_pnl_total) + "`**"
            )

        return "\n".join(lines)


    async def _cmd_leaders(self, args: str, ctx: SkillContext) -> str:
        if not self._leaders:
            leaders = await self._fetch_leaderboard()
            if leaders:
                self._leaders = leaders
                self._save_leaders()

        if not self._leaders and not self._watched_wallets:
            return (
                "❌ Leaderboard indisponible et aucun wallet custom.\n\n"
                "💡 `/copywatch 0x... Label` pour ajouter un wallet à surveiller"
            )

        if not self._leaders and self._watched_wallets:
            lines2 = ["👀 **Wallets surveillés (custom)** — leaderboard indisponible\n━━━━━━━━━━━━━━━"]
            for addr, label in self._watched_wallets.items():
                lines2.append(f"\n👤 **{label}**\n   📍 `{addr}`")
            return "\n".join(lines2)


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

        sections = ["📋 **CopyTrading Log**\n━━━━━━━━━━━━━━━"]

        # 1. Résolutions (positions clôturées)
        resolutions = [t for t in self._trades_log if t.get("action") == "RESOLUTION"]
        if resolutions:
            pnl_icon = "🟢" if self._paper_pnl_total >= 0 else "🔴"
            sections.append("🏁 **Positions résolues (%d) :**" % len(resolutions))
            for r in resolutions[-5:][::-1]:
                icon  = "✅" if r.get("won") else "❌"
                title = r.get("market_title", "?")[:38]
                pnl   = r.get("real_pnl", 0)
                held  = r.get("held_hours", 0)
                ts    = r.get("timestamp", "?")
                sections.append(
                    "%s **%s**\n   P&L: **$%+.2f** | Tenu %.1fh | %s" % (icon, title, pnl, held, ts)
                )
            sections.append(
                "%s P&L réalisé total: **$%+.2f**" % (pnl_icon, self._paper_pnl_total)
            )
            sections.append("")

        # 2. Trades copiés récents
        trades = [t for t in self._trades_log if t.get("action") != "RESOLUTION"]
        if trades:
            sections.append("📋 **Trades copiés (10 derniers) :**")
            for trade in reversed(trades[-10:]):
                mode_icon   = "📄" if trade.get("paper") else "💸"
                side_icon   = "🟢" if trade.get("side") == "YES" else "🔴"
                ts          = trade.get("timestamp", "?")
                trader      = trade.get("trader_name", "?")[:12]
                title       = trade.get("market_title", "?")[:38]
                amount      = trade.get("amount_usd", 0)
                result      = trade.get("result", "pending")
                result_icon = "✅" if result == "executed" else "📄" if result == "simulated" else "⚠️"
                sections.append(
                    "%s %s **%s** $%.0f — _%s_\n   👤 %s | %s | %s %s"
                    % (mode_icon, side_icon, trade.get("side","?"), amount, title, trader, ts, result_icon, result)
                )

        return "\n".join(sections)


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

    async def _cmd_paper_reset(self, args: str, ctx: SkillContext) -> str:
        """Remet à zéro toutes les données paper trading avec confirmation"""

        # Demande de confirmation : /copypaperreset confirm
        if args.strip().lower() != "confirm":
            nb_pos  = len(self._paper_positions)
            nb_log  = len([t for t in self._trades_log if t.get("paper")])
            pnl     = self._paper_pnl_total
            pnl_icon = "🟢" if pnl >= 0 else "🔴"
            return (
                "⚠️ **Remise à zéro du Paper Trading**\n"
                "━━━━━━━━━━━━━━━\n"
                f"Ceci va effacer :\n"
                f"  • **{nb_pos}** position(s) ouverte(s)\n"
                f"  • **{nb_log}** trade(s) dans le log\n"
                f"  • {pnl_icon} P&L réalisé: **${pnl:+.2f}**\n\n"
                "Pour confirmer :\n"
                "`/copypaperreset confirm`"
            )

        # Snapshot avant reset pour le message de confirmation
        nb_pos        = len(self._paper_positions)
        nb_trades     = len([t for t in self._trades_log if t.get("paper")])
        nb_resol      = len([t for t in self._trades_log if t.get("action") == "RESOLUTION"])
        old_pnl       = self._paper_pnl_total

        # Reset
        self._paper_positions  = {}
        self._paper_pnl_total  = 0.0
        # Retirer les entrées paper du log (garder les live si mixte)
        self._trades_log = [t for t in self._trades_log if not t.get("paper")]

        self._save_state()
        self._save_log()

        pnl_icon = "🟢" if old_pnl >= 0 else "🔴"
        return (
            "✅ **Paper Trading remis à zéro !**\n"
            "━━━━━━━━━━━━━━━\n"
            f"Effacé :\n"
            f"  • {nb_pos} position(s) ouverte(s)\n"
            f"  • {nb_trades} trade(s) du log\n"
            f"  • {nb_resol} résolution(s)\n"
            f"  • {pnl_icon} P&L réalisé: ${old_pnl:+.2f}\n\n"
            "Le paper trading repart de zéro.\n"
            "💡 `/copystart` pour reprendre la surveillance"
        )


    # ══════════════════════════════════════════════════════════════
    #   GESTION DES WALLETS PERSONNALISÉS
    # ══════════════════════════════════════════════════════════════

    async def _cmd_watch(self, args: str, ctx: SkillContext) -> str:
        """Ajouter un wallet à surveiller"""
        parts = args.strip().split(None, 1)
        if not parts:
            return (
                "Usage: `/copywatch <adresse> [label]`\n"
                "Ex: `/copywatch 0xabc...def MonTrader`\n"
                "Ex: `/copywatch 0xabc...def`"
            )

        address = parts[0].strip()
        label   = parts[1].strip() if len(parts) > 1 else address[:6] + "..." + address[-4:]

        # Validation basique de l'adresse Ethereum
        if not address.startswith("0x") or len(address) < 20:
            return "❌ Adresse invalide. Elle doit commencer par `0x` et faire 42 caractères."

        address = address.lower()

        if address in self._watched_wallets:
            old_label = self._watched_wallets[address]
            self._watched_wallets[address] = label
            self._save_state()
            return (
                f"✏️ Wallet mis à jour\n"
                f"📍 `{address}`\n"
                f"🏷 Label: `{old_label}` → `{label}`"
            )

        self._watched_wallets[address] = label
        self._save_state()

        total = len(self._watched_wallets)
        return (
            f"✅ **Wallet ajouté à la surveillance !**\n━━━━━━━━━━━━━━━\n"
            f"📍 `{address}`\n"
            f"🏷 Label: `{label}`\n"
            f"👥 Total wallets surveillés: `{total}`\n\n"
            f"{'▶️ Lance `/copystart` pour démarrer la surveillance.' if not self._running else '🟢 Surveillance déjà active — ce wallet sera inclus au prochain cycle.'}"
        )

    async def _cmd_unwatch(self, args: str, ctx: SkillContext) -> str:
        """Retirer un wallet de la surveillance"""
        if not args.strip():
            return "Usage: `/copyunwatch <adresse>`\nEx: `/copyunwatch 0xabc...def`"

        address = args.strip().lower()

        # Recherche partielle si adresse incomplète
        if address not in self._watched_wallets:
            matches = [a for a in self._watched_wallets if a.startswith(address) or a.endswith(address)]
            if len(matches) == 1:
                address = matches[0]
            elif len(matches) > 1:
                lines = ["⚠️ Plusieurs wallets correspondent :\n"]
                for a in matches:
                    lines.append(f"• `{a}` ({self._watched_wallets[a]})")
                lines.append("\nPrécise l'adresse complète.")
                return "\n".join(lines)
            else:
                return f"❌ Wallet `{address}` non trouvé dans la liste.\n\n💡 `/copywatched` pour voir la liste complète."

        label = self._watched_wallets.pop(address)
        self._save_state()

        return (
            f"🗑 **Wallet retiré**\n"
            f"📍 `{address}` (`{label}`)"
        )

    async def _cmd_watched(self, args: str, ctx: SkillContext) -> str:
        """Lister tous les wallets surveillés"""
        if not self._watched_wallets:
            return (
                "📭 **Aucun wallet en surveillance**\n\n"
                "💡 `/copywatch 0x... [label]` pour en ajouter un\n"
                "💡 `/copystart` lance aussi le leaderboard automatique"
            )

        status = "🟢 Surveillance active" if self._running else "🔴 Surveillance arrêtée"
        lines  = [f"👀 **Wallets surveillés** ({len(self._watched_wallets)}) | {status}\n━━━━━━━━━━━━━━━"]

        for addr, label in self._watched_wallets.items():
            # Compter les trades déjà copiés pour ce wallet
            nb_trades = sum(
                1 for t in self._trades_log
                if t.get("trader_wallet", "").lower() == addr
            )
            lines.append(
                f"\n👤 **{label}**\n"
                f"   📍 `{addr}`\n"
                f"   📋 Trades copiés: `{nb_trades}`\n"
                f"   🔗 [Voir sur Polygonscan](https://polygonscan.com/address/{addr})"
            )

        lines.append(
            f"\n━━━━━━━━━━━━━━━\n"
            f"💡 `/copywatch 0x... label` pour ajouter\n"
            f"💡 `/copyunwatch 0x...` pour retirer"
        )
        return "\n".join(lines)

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

        # 2. Vérifier les résolutions des positions paper
        if self.paper_mode and self._paper_positions:
            await self._check_paper_resolutions(ctx)

        # 3. Rafraîchir le leaderboard toutes les 3 heures (12 cycles de 15 min)
        if cycle % 12 == 1:
            leaders = await self._fetch_leaderboard()
            if leaders:
                self._leaders = leaders
                self._save_leaders()
                logger.info(f"Leaderboard rafraichi: {len(leaders)} traders")

        # 4. Construire la liste complète des wallets à surveiller
        #    = leaderboard automatique + wallets custom ajoutés par l'user
        wallets_to_watch: list = []

        # Wallets custom (priorité haute — toujours inclus)
        for addr, label in self._watched_wallets.items():
            wallets_to_watch.append({
                "proxyWallet": addr,
                "address":     addr,
                "name":        label,
                "custom":      True,
            })

        # Leaderboard automatique (si disponible)
        for trader in self._leaders[:10]:
            wallet = trader.get("proxyWallet") or trader.get("address", "")
            if wallet and wallet.lower() not in self._watched_wallets:
                wallets_to_watch.append(trader)

        if not wallets_to_watch:
            logger.warning("Aucun wallet a surveiller — ajoute un wallet avec /copywatch ou configure le leaderboard")
            await self._notify(
                "⚠️ **Aucun wallet à surveiller**\n\n"
                "Ajoute un wallet manuellement :\n"
                "`/copywatch 0xTonTrader MonTrader`\n\n"
                "Ou attends que le leaderboard se construise."
            )
            return

        # 5. Récupérer les trades récents de chaque wallet
        new_trades_found = 0
        custom_count = len(self._watched_wallets)
        logger.info(f"Surveillance: {len(wallets_to_watch)} wallets ({custom_count} custom + {len(wallets_to_watch)-custom_count} leaderboard)")

        for trader in wallets_to_watch:
            wallet = trader.get("proxyWallet") or trader.get("address", "")
            if not wallet:
                continue

            try:
                # Charger le portfolio du trader pour le sizing dynamique
                if "portfolio_value" not in trader:
                    pv = await self._fetch_trader_portfolio_value(wallet)
                    trader["portfolio_value"] = pv
                    if pv > 0:
                        logger.info("Portfolio %s: $%.2f", wallet[:10], pv)

                recent_trades = await self._fetch_trader_activity(wallet)
                for trade in recent_trades:
                    copied = await self._process_trade(trade, trader, ctx)
                    if copied:
                        new_trades_found += 1
            except Exception as e:
                logger.warning(f"Erreur fetch trader {wallet[:10]}: {e}")

            await asyncio.sleep(0.5)

        if new_trades_found > 0:
            logger.info(f"✅ Cycle #{cycle} — {new_trades_found} nouveau(x) trade(s) copié(s)")
        else:
            logger.info(f"✅ Cycle #{cycle} — aucun nouveau trade détecté")

    async def _process_trade(self, trade: dict, trader: dict, ctx: SkillContext) -> bool:
        """
        Traite un trade du trader copié.
        - BUY  → ouvrir une position (comme avant)
        - SELL → fermer la position correspondante si on l'a
        """
        trade_id = trade.get("transactionHash") or \
                   f"{trade.get('asset','')}-{trade.get('timestamp','')}-{trade.get('side','')}"

        # Déduplication
        if trade_id in self._seen_trades:
            return False
        self._seen_trades.add(trade_id)

        # Infos communes
        raw_side    = trade.get("side", "").upper()
        cond_id     = trade.get("conditionId", "")
        outcome     = trade.get("outcome", "Yes").upper()
        side        = "YES" if outcome in ("YES", "1", "TRUE") else "NO"
        price       = float(trade.get("price") or trade.get("avgPrice") or 0)
        mkt_title   = trade.get("title", "Marché inconnu")
        trader_name = (trader.get("name") or trader.get("pseudonym") or
                       trader.get("proxyWallet", "?")[:10])

        if not cond_id or price <= 0:
            return False

        # Filtrer par catégorie (crypto + politique)
        title_l    = (trade.get("title") or trade.get("slug") or "").lower()
        tags_lower = [str(t).lower() for t in (trade.get("tags") or [])]
        is_target  = (any(kw in title_l for kw in TARGET_TAGS) or
                      any(any(kw in tag for kw in TARGET_TAGS) for tag in tags_lower))
        if not is_target:
            logger.debug("Ignoré (hors catégorie): %s", title_l[:40])
            return False

        # ── VENTE : le trader clôture sa position ────────────────
        if raw_side in ("SELL", "REDEEM", "SELL_OUTCOME"):
            return await self._process_sell(
                cond_id=cond_id,
                side=side,
                sell_price=price,
                mkt_title=mkt_title,
                trader_name=trader_name,
                ctx=ctx,
            )

        # ── ACHAT : le trader ouvre une position ─────────────────
        if raw_side not in ("BUY", "BUY_OUTCOME", ""):
            logger.debug("Side inconnu ignoré: %s", raw_side)
            return False

        # Prix trop élevé → peu de valeur résiduelle
        if price > 0.95:
            logger.debug("Ignoré (prix trop haut %.2f): %s", price, mkt_title[:30])
            return False

        logger.info(
            "📋 CopyBUY: %s | %s @ %.3f$ | %s | %s…",
            trader_name, side, price, mkt_title[:35], cond_id[:16]
        )

        # Sizing dynamique proportionnel au trader copié
        amount = await self._compute_position_size(
            trade=trade,
            trader=trader,
        )
        result = await self._execute_or_simulate(cond_id, side, amount, price, ctx)

        self._trades_log.append({
            "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "action":        "BUY",
            "trader_name":   trader_name,
            "trader_wallet": trader.get("proxyWallet", "?"),
            "cond_id":       cond_id,
            "market_title":  mkt_title,
            "side":          side,
            "entry_price":   price,
            "amount_usd":    amount,
            "trader_usdc":   trade.get("trader_usdc_size", 0),
            "paper":         self.paper_mode,
            "result":        result,
            "tx_hash":       trade_id,
        })
        self._save_log()

        mode_icon   = "📄" if self.paper_mode else "💸"
        side_icon   = "🟢" if side == "YES" else "🔴"
        trader_usdc_v = float(trade.get("trader_usdc_size") or 0)
        portfolio_v   = float(trader.get("portfolio_value") or 0)
        if trader_usdc_v > 0 and portfolio_v > 0:
            ratio_pct = trader_usdc_v / portfolio_v * 100
            size_note = " (trader: $%.0f = %.1f%% de son portfolio)" % (trader_usdc_v, ratio_pct)
        elif trader_usdc_v > 0:
            size_note = " (trader a mis $%.0f)" % trader_usdc_v
        else:
            size_note = ""
        await self._notify(
            "%s **CopyTrade BUY** %s\n"
            "━━━━━━━━━━━━━━━\n"
            "👤 Trader: **%s**\n"
            "%s Côté: **%s** @ `%.3f$`\n"
            "💰 Notre mise: **$%.2f USDC**%s\n"
            "📌 _%s_\n"
            "%s"
            % (
                mode_icon,
                "(Paper)" if self.paper_mode else "(Live)",
                trader_name,
                side_icon, side, price,
                amount, size_note,
                mkt_title[:55],
                "✅ Simulé" if self.paper_mode else "✅ Exécuté sur Polygon",
            )
        )
        return True

    async def _process_sell(
        self,
        cond_id: str,
        side: str,
        sell_price: float,
        mkt_title: str,
        trader_name: str,
        ctx: SkillContext,
    ) -> bool:
        """
        Le trader copié vient de vendre sa position sur cond_id/side.
        → Chercher si on a une position paper correspondante et la fermer.
        → En live, passer un ordre de vente via PolyJarvis.
        """
        # ── Trouver la/les position(s) paper correspondante(s) ───
        matching_keys = [
            k for k, pos in self._paper_positions.items()
            if pos.get("cond_id") == cond_id and pos.get("side") == side
        ]

        if not matching_keys:
            logger.info(
                "SELL détecté (%s %s %.3f$) mais aucune position ouverte → ignoré",
                side, cond_id[:16], sell_price
            )
            return False

        logger.info(
            "📋 CopySELL: %s | %s @ %.3f$ | %s | %d position(s) à fermer",
            trader_name, side, sell_price, mkt_title[:35], len(matching_keys)
        )

        total_pnl   = 0.0
        total_in    = 0.0
        total_out   = 0.0
        closed_msgs = []

        for key in matching_keys:
            pos      = self._paper_positions[key]
            invested = pos.get("amount_usd", 0)
            entry    = pos.get("entry_price", 0)
            shares   = pos.get("shares", invested / entry if entry > 0 else 0)
            title    = pos.get("market_title") or mkt_title
            opened   = pos.get("opened_at", "?")
            held_h   = (time.time() - pos.get("timestamp", time.time())) / 3600

            # Valeur de sortie = shares × prix de vente du trader
            payout   = shares * sell_price
            pnl      = payout - invested
            pnl_pct  = (pnl / invested * 100) if invested > 0 else 0
            icon     = "🟢" if pnl >= 0 else "🔴"

            total_in  += invested
            total_out += payout
            total_pnl += pnl

            logger.info(
                "FERMETURE paper %s | entrée %.3f$ → sortie %.3f$ | P&L $%+.2f (%.1f%%)",
                key[:20], entry, sell_price, pnl, pnl_pct
            )

            # Enregistrer dans le log
            self._trades_log.append({
                "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "action":       "SELL_COPY",
                "trader_name":  trader_name,
                "cond_id":      cond_id,
                "market_title": title[:80],
                "side":         side,
                "entry_price":  entry,
                "sell_price":   sell_price,
                "amount_usd":   invested,
                "payout":       payout,
                "real_pnl":     pnl,
                "held_hours":   round(held_h, 1),
                "paper":        self.paper_mode,
                "result":       "sell_copied",
            })

            self._paper_pnl_total += pnl
            del self._paper_positions[key]

            closed_msgs.append(
                "%s `$%.2f` → `$%.2f` | **$%+.2f** (%.1f%%) | %.1fh"
                % (icon, invested, payout, pnl, pnl_pct, held_h)
            )

        self._save_state()
        self._save_log()

        # ── Live : tenter de passer l'ordre de vente ─────────────
        live_result = ""
        if not self.paper_mode:
            pj = ctx.agent.skill_manager._skills.get("polyjarvis") if ctx.agent else None
            if pj:
                try:
                    # Polymarket : vendre YES = acheter NO au prix inverse
                    # On utilise /polycancel + ordre inverse ou le CLOB sell
                    res = await pj._cmd_buy(
                        "%s %s %s" % (cond_id, "NO" if side == "YES" else "YES", total_in),
                        ctx
                    )
                    live_result = "\n✅ Ordre de vente envoyé" if "✅" in res else "\n⚠️ Vente live échouée"
                except Exception as e:
                    live_result = "\n⚠️ Vente live: %s" % str(e)[:40]
            else:
                live_result = "\n⚠️ PolyJarvis non disponible pour la vente"

        # ── Notification Telegram ─────────────────────────────────
        mode_icon   = "📄" if self.paper_mode else "💸"
        pnl_icon    = "🟢" if total_pnl >= 0 else "🔴"
        pnl_tot_icon = "🟢" if self._paper_pnl_total >= 0 else "🔴"

        msg = (
            "%s **CopyTrade SELL** %s\n"
            "━━━━━━━━━━━━━━━\n"
            "👤 Trader: **%s** a vendu\n"
            "📌 _%s_\n"
            "💱 %s @ `%.3f$`\n\n"
            "%s\n\n"
            "💰 Investi: `$%.2f` → Récupéré: `$%.2f`\n"
            "%s **P&L trade: `$%+.2f`**\n"
            "%s P&L total paper: `$%+.2f`"
            "%s"
        ) % (
            mode_icon,
            "(Paper)" if self.paper_mode else "(Live)",
            trader_name,
            mkt_title[:55],
            side, sell_price,
            "\n".join(closed_msgs),
            total_in, total_out,
            pnl_icon, total_pnl,
            pnl_tot_icon, self._paper_pnl_total,
            live_result,
        )
        await self._notify(msg)
        return True

    async def _compute_position_size(self, trade: dict, trader: dict) -> float:
        """
        Calcule la mise proportionnelle au % du portfolio que le trader a engagé.

        Sources de données (par priorité) :
        A. trade["trader_usdc_size"] + trader["portfolio_value"]  → ratio exact
        B. trade["trader_usdc_size"] seul                         → ratio estimé (÷10 conservateur)
        C. trader["portfolio_value"] seul (via positions ouvertes) → ratio estimé (÷20)
        D. Fallback plat                                           → max_position × 0.10

        Notre mise = ratio × self.budget_total, plafonné par self.max_position.
        """
        max_pos = float(self.max_position)
        budget  = float(self.budget_total)
        min_pos = 1.0

        trader_usdc      = float(trade.get("trader_usdc_size") or 0)
        portfolio_trader = float(trader.get("portfolio_value") or 0)

        # ── A. Ratio exact ─────────────────────────────────────────
        if trader_usdc > 0 and portfolio_trader > 0:
            ratio  = min(trader_usdc / portfolio_trader, 1.0)
            amount = min(ratio * budget, max_pos)
            logger.info(
                "Sizing A (exact): trader $%.2f / $%.2f = %.1f%% → nous $%.2f",
                trader_usdc, portfolio_trader, ratio * 100, amount
            )

        # ── B. Taille connue, portfolio inconnu ────────────────────
        elif trader_usdc > 0:
            # Hypothèse conservatrice : le trade représente ~10% de son portfolio
            ratio  = 0.10
            amount = min(ratio * budget, max_pos)
            logger.info(
                "Sizing B (estimé, portfolio inconnu): trader $%.2f → ratio 10%% → nous $%.2f",
                trader_usdc, amount
            )

        # ── C. Portfolio connu, taille trade inconnue ──────────────
        elif portfolio_trader > 0:
            # On ne sait pas combien il a mis → très conservateur : 5%
            ratio  = 0.05
            amount = min(ratio * budget, max_pos)
            logger.info(
                "Sizing C (portfolio connu, taille inconnue): portfolio $%.2f → ratio 5%% → nous $%.2f",
                portfolio_trader, amount
            )

        # ── D. Fallback ────────────────────────────────────────────
        else:
            amount = min(budget * 0.10, max_pos)
            logger.info("Sizing D (fallback): 10%% budget → nous $%.2f", amount)

        return round(max(min_pos, min(amount, max_pos)), 2)

    
    async def _execute_or_simulate(
        self, cond_id: str, side: str, amount: float, entry_price: float, ctx: SkillContext
    ) -> str:
        """Exécute le trade (live) ou le simule (paper)"""
        shares = amount / entry_price if entry_price > 0 else 0

        if self.paper_mode:
            # Stocker la position simulée
            key = f"{cond_id}_{side}_{int(time.time())}"
            self._paper_positions[key] = {
                "cond_id":      cond_id,
                "side":         side,
                "amount_usd":   amount,
                "entry_price":  entry_price,
                "shares":       shares,
                "opened_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "timestamp":    int(time.time()),
                "market_title": "",  # sera rempli par _enrich_paper_position
            }
            # Enrichir le titre en background (non bloquant)
            asyncio.create_task(self._enrich_paper_position(key, cond_id, side))
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

    def _get_headers(self, extra: dict = None) -> dict:
        """
        Construit les headers HTTP avec cookie navigateur si disponible.
        Le cookie permet de contourner le géoblocage Polymarket depuis la Belgique.
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
            "Origin":          "https://polymarket.com",
            "Referer":         "https://polymarket.com/",
        }
        if POLY_BROWSER_COOKIE:
            headers["Cookie"] = POLY_BROWSER_COOKIE
        if extra:
            headers.update(extra)
        return headers

    async def _fetch_trader_portfolio_value(self, wallet: str) -> float:
        """
        Récupère la valeur totale du portefeuille USDC d'un trader.
        Utilisé pour calculer le % qu'il a alloué sur un trade.
        Retourne 0.0 si indisponible.
        """
        headers = self._get_headers()
        endpoints = [
            (f"{DATA_API}/portfolio",   {"user": wallet}),
            (f"{DATA_API}/portfolio",   {"address": wallet}),
            (f"{GAMMA_API}/portfolios", {"user": wallet}),
            (f"{DATA_API}/balance",     {"user": wallet}),
        ]
        for url, params in endpoints:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status != 200:
                            continue
                        data = await r.json(content_type=None)
                        # Chercher la valeur totale dans différents formats
                        for key in ("value", "totalValue", "portfolioValue", "balance",
                                    "totalBalance", "usdcBalance", "collateral", "total"):
                            val = data.get(key) if isinstance(data, dict) else None
                            if val and float(val) > 0:
                                logger.debug("Portfolio %s: $%.2f via %s", wallet[:10], float(val), key)
                                return float(val)
                        # Format liste de positions : sommer les valeurs
                        items = data if isinstance(data, list) else data.get("data", [])
                        if items and isinstance(items, list):
                            total = sum(
                                float(i.get("value") or i.get("currentValue") or 0)
                                for i in items if isinstance(i, dict)
                            )
                            if total > 0:
                                logger.debug("Portfolio %s (sum positions): $%.2f", wallet[:10], total)
                                return total
            except Exception as e:
                logger.debug("fetch_trader_portfolio %s: %s", wallet[:10], e)
        return 0.0

    async def _fetch_trader_activity_via_gamma(self, wallet: str, since_ts: int) -> list:
        """
        Scrape l'activite d'un wallet via l'API Gamma profiles — endpoint public
        qui retourne les positions ouvertes et trades d'un utilisateur.
        """
        results = []
        headers = self._get_headers({"Content-Type": "application/json"})

        endpoints = [
            f"{GAMMA_API}/profiles?address={wallet}",
            f"{GAMMA_API}/positions?user={wallet}&limit=50",
            f"https://polymarket.com/api/profile/{wallet}",
            f"https://polymarket.com/api/activity?user={wallet}&limit=50",
        ]

        for url in endpoints:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        logger.debug(f"Gamma profile {wallet[:10]} [{url.split('/')[-1][:20]}] → HTTP {r.status}")
                        if r.status != 200:
                            continue
                        data = await r.json(content_type=None)

                        # Normaliser selon le format retourné
                        items = []
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            for key in ("positions", "activity", "trades", "data", "results"):
                                if key in data and isinstance(data[key], list):
                                    items = data[key]
                                    break

                        if not items:
                            continue

                        logger.info(f"  {url.split('/')[-1]}: {len(items)} items pour {wallet[:10]}")
                        for item in items:
                            ts = int(item.get("timestamp", item.get("createdAt", 0)) or 0)
                            if ts and ts < since_ts:
                                continue
                            cond_id = item.get("conditionId", item.get("market", item.get("marketId", "")))
                            outcome = item.get("outcome", item.get("side", "Yes"))
                            side    = "YES" if str(outcome).upper() in ("YES", "0", "BUY") else "NO"
                            price   = float(item.get("price", item.get("avgPrice", 0)) or 0)
                            title   = item.get("title", item.get("question", item.get("name", cond_id[:30])))
                            tags    = item.get("tags", [])

                            if price > 0 and cond_id:
                                results.append({
                                    "transactionHash": item.get("txHash", item.get("id", f"{wallet}-{ts}")),
                                    "conditionId":     cond_id,
                                    "outcome":         side,
                                    "side":            "BUY",
                                    "price":           price,
                                    "avgPrice":        price,
                                    "title":           str(title)[:60],
                                    "tags":            tags if isinstance(tags, list) else [],
                                    "timestamp":       ts,
                                })

                        if results:
                            return results
            except Exception as e:
                logger.debug(f"Gamma profile endpoint {url[-30:]}: {e}")

        return results

    async def _fetch_trader_activity_via_clob_data(self, wallet: str, since_ts: int) -> list:
        """
        Appel direct à l'endpoint CLOB /data/trades avec cookie navigateur.
        Fonctionne si POLY_BROWSER_COOKIE est configuré.
        """
        results  = []
        headers  = self._get_headers()

        # Paramètres à essayer pour identifier un wallet
        param_variants = [
            {"maker": wallet},
            {"taker": wallet},
            {"maker_address": wallet},
        ]

        async with aiohttp.ClientSession(headers=headers) as session:
            for params in param_variants:
                try:
                    async with session.get(
                        "https://clob.polymarket.com/data/trades",
                        params={**params, "limit": "50"},
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        logger.debug(f"CLOB /data/trades {list(params.keys())[0]}={wallet[:10]} → HTTP {r.status}")
                        if r.status != 200:
                            continue
                        data   = await r.json(content_type=None)
                        trades = data if isinstance(data, list) else data.get("data", [])
                        if not trades:
                            continue
                        logger.info(f"CLOB /data/trades: {len(trades)} trades pour {wallet[:10]}")
                        for t in trades:
                            mt = str(t.get("match_time", "0"))
                            try:
                                ts = int(float(mt)) if mt.replace(".", "").isdigit() else 0
                            except Exception:
                                ts = 0
                            if ts and ts < since_ts:
                                continue
                            if t.get("side", "").upper() not in ("BUY", ""):
                                continue
                            results.append({
                                "transactionHash": t.get("transaction_hash", t.get("id", "")),
                                "conditionId":     t.get("market", ""),
                                "outcome":         t.get("outcome", "Yes"),
                                "side":            "BUY",
                                "price":           float(t.get("price", 0)),
                                "avgPrice":        float(t.get("price", 0)),
                                "title":           t.get("outcome", "")[:40],
                                "tags":            [],
                                "timestamp":       ts,
                            })
                        if results:
                            return results
                except Exception as e:
                    logger.debug(f"CLOB variant {e}")

        return results

    async def _fetch_leaderboard(self) -> list:
        """
        Construit le leaderboard via le Subgraph public Polymarket (The Graph).
        Endpoint : https://api.thegraph.com/subgraphs/name/polymarket/polymarket-matic
        Recupere les AccountMerged (positions) tries par montant investis.
        """
        headers = self._get_headers({"Content-Type": "application/json"})

        # Fenetre temporelle
        window_map = {"1d": 1, "7d": 7, "30d": 30, "all": 365}
        days  = window_map.get(self.lb_window, 30)
        since = int(time.time()) - (days * 86400)

        # Query GraphQL — top traders par volume d'achat sur Polymarket
        query = """
        {
          positionsMergeds(
            first: 200
            orderBy: amount
            orderDirection: desc
            where: { timestamp_gt: %d }
          ) {
            stakeholder { id }
            amount
            timestamp
          }
        }
        """ % since

        subgraph_urls = [
            "https://api.thegraph.com/subgraphs/name/polymarket/polymarket-matic",
            "https://gateway-arbitrum.network.thegraph.com/api/subgraphs/id/81Dm16JjuFSrqz813HysXoUPvzTwE7fsfPk2RTf66nyC",
        ]

        traders: dict = {}

        for url in subgraph_urls:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.post(
                        url,
                        json={"query": query},
                        timeout=aiohttp.ClientTimeout(total=20)
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            positions = (data.get("data") or {}).get("positionsMergeds", [])
                            logger.info(f"Subgraph positions: {len(positions)} via {url[:50]}")

                            for pos in positions:
                                addr = (pos.get("stakeholder") or {}).get("id", "")
                                if not addr:
                                    continue
                                amount = float(pos.get("amount", 0)) / 1e6  # USDC 6 decimales
                                if addr not in traders:
                                    traders[addr] = {
                                        "proxyWallet": addr,
                                        "address":     addr,
                                        "name":        addr[:6] + "..." + addr[-4:],
                                        "volume":      0.0,
                                        "trade_count": 0,
                                        "profit":      0.0,
                                    }
                                traders[addr]["volume"]      += amount
                                traders[addr]["trade_count"] += 1

                            if traders:
                                break  # succes, pas besoin du fallback
            except Exception as e:
                logger.warning(f"Subgraph {url[:40]} echoue: {e}")

        # Fallback : construire depuis les trades de tes propres marches trending
        if not traders:
            logger.info("Fallback subgraph echoue — construction depuis trades Gamma")
            traders = await self._build_leaderboard_from_gamma()

        if not traders:
            logger.error("Leaderboard impossible a construire — aucune source disponible")
            return []

        sorted_traders = sorted(traders.values(), key=lambda x: x["volume"], reverse=True)
        top10 = sorted_traders[:10]
        logger.info(f"Leaderboard OK: {len(top10)} traders | top volume: ${top10[0]['volume']:,.2f}")
        return top10

    async def _build_leaderboard_from_gamma(self) -> dict:
        """
        Fallback ultime : recupere les trades des top marches via CLOB authentifie
        et extrait les makers. Necessite POLY_PRIVATE_KEY.
        """
        clob = self._get_clob_client()
        if not clob:
            logger.warning("Pas de ClobClient disponible — configure POLY_PRIVATE_KEY")
            return {}

        traders: dict = {}
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

        try:
            # Recuperer les marches trending
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    f"{GAMMA_API}/markets",
                    params={"active": "true", "limit": 15,
                            "order": "volume24hr", "ascending": "false"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    markets = await r.json(content_type=None) if r.status == 200 else []

            for mkt in markets[:8]:
                cond_id = mkt.get("conditionId", "")
                if not cond_id:
                    continue
                try:
                    from py_clob_client.clob_types import TradeParams
                    trades = clob.get_trades(TradeParams(market=cond_id)) or []
                    for t in trades:
                        self._aggregate_trader(traders, t)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.debug(f"get_trades {cond_id[:16]}: {e}")
        except Exception as e:
            logger.warning(f"_build_leaderboard_from_gamma: {e}")

        return traders

    def _aggregate_trader(self, traders: dict, trade: dict):
        """Agregation d'un trade dans le dict traders"""
        maker_orders = trade.get("maker_orders", [])
        addresses = [m.get("maker_address", "") for m in maker_orders if m.get("maker_address")]
        if not addresses:
            addr = trade.get("maker_address", "")
            if addr:
                addresses = [addr]
        for addr in addresses:
            if not addr or len(addr) < 10:
                continue
            size  = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
            vol   = size * price
            if addr not in traders:
                traders[addr] = {
                    "proxyWallet": addr,
                    "address":     addr,
                    "name":        addr[:6] + "..." + addr[-4:],
                    "volume":      0.0,
                    "trade_count": 0,
                    "profit":      0.0,
                }
            traders[addr]["volume"]      += vol
            traders[addr]["trade_count"] += 1

    def _get_clob_client(self):
        """Recupere le ClobClient depuis PolyJarvis si configure"""
        try:
            if self._context and self._context.agent:
                pj = self._context.agent.skill_manager._skills.get("polyjarvis")
                if pj and pj._clob:
                    return pj._clob
        except Exception:
            pass
        return None

    async def _fetch_trader_activity(self, wallet: str, since_minutes: int = None) -> list:
        """
        Recupere les trades recents d'un wallet.
        Strategie en cascade :
        1. data-api.polymarket.com/activity (endpoint qui fonctionne depuis le navigateur)
        2. CLOB authentifie py-clob-client (maker_address)
        3. Autres fallbacks
        """
        since_minutes = since_minutes or (self.interval_min + 2)
        since_ts = int(time.time()) - (since_minutes * 60)
        headers  = self._get_headers()
        results  = []

        # ── Methode 1 : data-api.polymarket.com/activity ─────────────
        # C'est l'endpoint qui fonctionne depuis le navigateur !
        try:
            url = f"{DATA_API}/activity"
            params = {
                "user":  wallet,
                "limit": 50,
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    logger.info(f"data-api/activity {wallet[:10]} → HTTP {r.status}")
                    if r.status == 200:
                        data  = await r.json(content_type=None)
                        items = data if isinstance(data, list) else data.get("data", [])
                        logger.info(f"  → {len(items)} activites brutes")

                        for item in items:
                            # Filtrer par fenetre temporelle
                            ts_raw = item.get("timestamp", item.get("createdAt", 0))
                            try:
                                ts = int(float(str(ts_raw))) if ts_raw else 0
                            except Exception:
                                ts = 0
                            if ts and ts < since_ts:
                                continue

                            # Garder seulement les BUY
                            side = str(item.get("side", item.get("type", "BUY"))).upper()
                            if side not in ("BUY", "TRADE", ""):
                                continue

                            outcome  = item.get("outcome", item.get("outcomeIndex", "Yes"))
                            cond_id  = item.get("conditionId", item.get("market", item.get("marketId", "")))
                            price    = float(item.get("price", item.get("avgPrice", 0)) or 0)
                            title    = item.get("title", item.get("question", item.get("name", "")))
                            tags     = item.get("tags", [])
                            tx_hash  = item.get("transactionHash", item.get("txHash", item.get("id", "")))

                            if not cond_id:
                                continue

                            # Taille USDC du trade du trader
                            usdc_size = float(
                                item.get("usdcSize") or
                                item.get("size")     or
                                item.get("amount")   or
                                item.get("usdSize")  or
                                item.get("collateralAmount") or
                                0
                            )
                            raw_side_api = str(item.get("side", item.get("type", "BUY"))).upper()
                            results.append({
                                "transactionHash": tx_hash,
                                "conditionId":     cond_id,
                                "outcome":         str(outcome),
                                "side":            raw_side_api if raw_side_api in ("BUY","SELL","REDEEM","SELL_OUTCOME") else "BUY",
                                "price":           price,
                                "avgPrice":        price,
                                "title":           str(title)[:60],
                                "tags":            tags if isinstance(tags, list) else [],
                                "timestamp":       ts,
                                "trader_usdc_size": usdc_size,
                                "_raw":            item,
                            })

                        if results:
                            logger.info(f"  → {len(results)} trades recents pour {wallet[:10]}")
                            return results
                        else:
                            logger.info(f"  → 0 trades dans la fenetre {since_minutes}min pour {wallet[:10]}")
                            return []  # Pas de trades récents = normal, pas une erreur
        except Exception as e:
            logger.warning(f"data-api/activity {wallet[:10]}: {e}")

        # ── Methode 2 : CLOB authentifie (fallback) ──────────────────
        clob = self._get_clob_client()
        if clob:
            try:
                from py_clob_client.clob_types import TradeParams
                raw = clob.get_trades(TradeParams(maker_address=wallet)) or []
                logger.info(f"CLOB fallback {wallet[:10]}: {len(raw)} trades")
                for t in raw:
                    mt = str(t.get("match_time", "0"))
                    try:
                        ts = int(float(mt)) if mt.replace(".", "").isdigit() else 0
                    except Exception:
                        ts = 0
                    if ts and ts < since_ts:
                        continue
                    if t.get("side", "BUY").upper() != "BUY":
                        continue
                    results.append({
                        "transactionHash": t.get("transaction_hash", t.get("id", "")),
                        "conditionId":     t.get("market", ""),
                        "outcome":         t.get("outcome", "Yes"),
                        "side":            "BUY",
                        "price":           float(t.get("price", 0)),
                        "avgPrice":        float(t.get("price", 0)),
                        "title":           t.get("outcome", "")[:40],
                        "tags":            [],
                        "timestamp":       ts,
                    })
                if results:
                    return results
            except Exception as e:
                logger.warning(f"CLOB fallback {wallet[:10]}: {e}")

        # ── Methode 3 : autres fallbacks ─────────────────────────────
        clob_results = await self._fetch_trader_activity_via_clob_data(wallet, since_ts)
        if clob_results:
            return clob_results

        gamma_results = await self._fetch_trader_activity_via_gamma(wallet, since_ts)
        if gamma_results:
            return gamma_results

        logger.warning(f"Aucun trade detecte pour {wallet[:10]}")
        return []

    async def _get_current_price(self, cond_id: str, side: str) -> float:
        """
        Prix actuel via data-api (fonctionne depuis Belgique) avec fallback Gamma.
        """
        if not cond_id:
            return 0.0
        side_l = side.lower()
        yes_set = {"yes", "1", "true"}
        no_set  = {"no",  "0", "false"}
        headers = self._get_headers()
        endpoints = [
            (DATA_API  + "/markets", {"condition_id":  cond_id}),
            (GAMMA_API + "/markets", {"condition_ids": cond_id}),
        ]
        for url, params in endpoints:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status != 200:
                            continue
                        data = await r.json(content_type=None)
                        mkts = data if isinstance(data, list) else data.get("data", [])
                        if not mkts:
                            continue
                        m = mkts[0] if isinstance(mkts, list) else mkts
                        for t in m.get("tokens", []):
                            outcome = t.get("outcome", "").lower()
                            match = (side_l in yes_set and outcome in yes_set) or \
                                    (side_l in no_set  and outcome in no_set)
                            if match:
                                for pk in ("price", "last_trade_price", "bestAsk", "midpoint"):
                                    p = t.get(pk)
                                    if p and 0 < float(p) <= 1.0:
                                        logger.debug("Prix %s %s: %.4f", cond_id[:16], side, float(p))
                                        return float(p)
                        op = m.get("outcomePrices")
                        if isinstance(op, list) and len(op) >= 2:
                            idx = 0 if side_l in yes_set else 1
                            try:
                                p = float(op[idx])
                                if 0 < p <= 1.0:
                                    return p
                            except Exception:
                                pass
            except Exception as e:
                logger.debug("_get_current_price %s %s: %s", cond_id[:16], side, e)
        return 0.0

    async def _get_market_info(self, cond_id: str) -> dict:
        """Infos completes d'un marche (titre, resolution, prix)"""
        if not cond_id:
            return {}
        headers = self._get_headers()
        for url, params in [
            (DATA_API  + "/markets", {"condition_id":  cond_id}),
            (GAMMA_API + "/markets", {"condition_ids": cond_id}),
        ]:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            mkts = data if isinstance(data, list) else data.get("data", [])
                            if mkts:
                                return mkts[0] if isinstance(mkts, list) else mkts
            except Exception as e:
                logger.debug("_get_market_info %s: %s", cond_id[:16], e)
        return {}

    async def _enrich_paper_position(self, key: str, cond_id: str, side: str):
        """Recupere le titre du marche en background et met a jour la position"""
        if not cond_id:
            return
        try:
            await asyncio.sleep(1)
            info = await self._get_market_info(cond_id)
            title = info.get("question") or info.get("title") or ""
            if key in self._paper_positions and title:
                self._paper_positions[key]["market_title"] = title[:80]
                self._save_state()
        except Exception as e:
            logger.debug("enrich_paper_position: %s", e)

    async def _check_paper_resolutions(self, ctx: SkillContext):
        """
        Verifie si des positions paper sont resolues sur Polymarket.
        Appele a chaque cycle depuis _run_cycle.
        """
        if not self._paper_positions:
            return

        to_close = []
        resolved_msgs = []

        for key, pos in list(self._paper_positions.items()):
            cond_id = pos.get("cond_id", "")
            if not cond_id:
                continue

            info = await self._get_market_info(cond_id)
            if not info:
                continue

            # Detecter si resolu
            is_closed = bool(info.get("closed") or info.get("resolved"))
            if not is_closed:
                end_date = info.get("endDate") or info.get("endDateIso", "")
                if end_date:
                    try:
                        from datetime import timezone
                        if end_date.endswith("Z"):
                            end_date = end_date[:-1] + "+00:00"
                        end_dt  = datetime.fromisoformat(end_date)
                        now_utc = datetime.now(timezone.utc)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        is_closed = end_dt < now_utc
                    except Exception:
                        pass

            # Mettre a jour le titre si manquant
            if not pos.get("market_title"):
                t = info.get("question") or info.get("title") or ""
                if t:
                    self._paper_positions[key]["market_title"] = t[:80]

            if not is_closed:
                continue

            # Determiner le gagnant
            winner = None
            for t in info.get("tokens", []):
                p = float(t.get("price", 0) or 0)
                if p >= 0.99:
                    winner = t.get("outcome", "")
                    break
            if not winner:
                rp = info.get("resolutionPrice")
                if rp is not None:
                    winner = "YES" if float(rp) >= 0.5 else "NO"

            side     = pos.get("side", "YES")
            entry    = pos.get("entry_price", 0)
            invested = pos.get("amount_usd", 0)
            shares   = pos.get("shares", invested / entry if entry > 0 else 0)
            title    = pos.get("market_title") or cond_id[:30]
            held_h   = (time.time() - pos.get("timestamp", time.time())) / 3600

            won      = bool(winner and winner.upper() == side.upper())
            payout   = shares * 1.0 if won else 0.0
            real_pnl = payout - invested
            pnl_pct  = (real_pnl / invested * 100) if invested > 0 else 0
            icon     = "✅" if won else "❌"

            logger.info(
                "RESOLUTION PAPER %s %s | Gagnant: %s | Cote: %s | P&L: $%.2f",
                icon, title[:40], winner, side, real_pnl
            )

            self._trades_log.append({
                "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "action":       "RESOLUTION",
                "market_title": title,
                "cond_id":      cond_id,
                "side":         side,
                "winner":       winner or "?",
                "won":          won,
                "entry_price":  entry,
                "amount_usd":   invested,
                "payout":       payout,
                "real_pnl":     real_pnl,
                "held_hours":   round(held_h, 1),
                "paper":        True,
            })
            self._paper_pnl_total += real_pnl
            to_close.append(key)

            w = winner or "?"
            msg = (
                icon + " **" + title[:45] + "**\n"
                "   Gagnant: **" + w + "** | Cote: **" + side + "**\n"
                "   Investi: $" + ("%.2f" % invested) + " -> Payout: $" + ("%.2f" % payout) + "\n"
                "   P&L: **$" + ("%+.2f" % real_pnl) + "** (" + ("%.1f" % pnl_pct) + "%) | Tenu " + ("%.1f" % held_h) + "h"
            )
            resolved_msgs.append(msg)

        for key in to_close:
            del self._paper_positions[key]

        self._save_state()
        self._save_log()

        if resolved_msgs:
            pnl_icon = "🟢" if self._paper_pnl_total >= 0 else "🔴"
            header   = "🏁 **" + str(len(resolved_msgs)) + " resolution(s) Paper Trading**\n━━━━━━━━━━━━━━━\n"
            footer   = "\n\n━━━━━━━━━━━━━━━\n" + pnl_icon + " P&L total paper: **$" + ("%+.2f" % self._paper_pnl_total) + "**"
            await self._notify(header + "\n\n".join(resolved_msgs) + footer)

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
            "paper_pnl_total":      self._paper_pnl_total,
            "seen_trades":          list(self._seen_trades)[-500:],
            "watched_wallets":      self._watched_wallets,
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
                self._paper_pnl_total     = state.get("paper_pnl_total", 0.0)
                self._seen_trades         = set(state.get("seen_trades", []))
                self._watched_wallets     = state.get("watched_wallets", {})
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
