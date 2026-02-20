"""
╔══════════════════════════════════════════════════════════════╗
║              SKILL JARVIS : PolyJarvis                       ║
║         Trading sur Polymarket via CLOB + Gamma API          ║
║                                                              ║
║  Dépendances:                                                ║
║    pip install py-clob-client web3 aiohttp                   ║
║                                                              ║
║  Variables .env requises:                                    ║
║    POLY_PRIVATE_KEY   = clé privée wallet Polygon            ║
║    POLY_FUNDER        = adresse wallet (funder address)      ║
║    POLY_SIG_TYPE      = 0 (EOA/MetaMask) | 1 (Magic/email)  ║
║                         | 2 (Browser proxy)                  ║
╚══════════════════════════════════════════════════════════════╝

COMMANDES TELEGRAM:
  /polymarkets [n]          — Marchés tendance par volume
  /polysearch <mots>        — Recherche de marchés
  /polymarket <condID>      — Détail d'un marché
  /polybuy <condID> YES|NO <$>  — Acheter une position
  /polypositions            — Positions ouvertes + P&L
  /polyhedge                — Scanner les opportunités de hedge
  /polywallet               — Soldes POL + USDC.e
  /polyapprove              — Approuver les contrats (one-time)
  /polyorders               — Ordres ouverts
  /polycancel <orderID>     — Annuler un ordre
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.PolyJarvis")

# ── Constantes Polymarket ──────────────────────────────────────
CLOB_HOST      = "https://clob.polymarket.com"
GAMMA_API      = "https://gamma-api.polymarket.com"
CHAIN_ID       = 137   # Polygon mainnet

# Contrats Polygon (mainnet)
USDC_ADDRESS        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS         = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS    = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADDRESS    = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER    = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Polygon RPC public
POLYGON_RPC = "https://polygon-rpc.com"

# Fichier de stockage des positions locales
POSITIONS_FILE = Path("config/poly_positions.json")

# ERC20 ABI minimal (balanceOf + approve)
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "allowance", "type": "function",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "approve", "type": "function",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
]

# CTF ABI minimal (isApprovedForAll + setApprovalForAll)
CTF_ABI = [
    {"name": "isApprovedForAll", "type": "function",
     "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view"},
    {"name": "setApprovalForAll", "type": "function",
     "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "outputs": [], "stateMutability": "nonpayable"},
]


# ══════════════════════════════════════════════════════════════
#   SKILL PRINCIPALE
# ══════════════════════════════════════════════════════════════

class PolyJarvisSkill(BaseSkill):
    SKILL_NAME    = "polyjarvis"
    SKILL_DESC    = "Trading Polymarket (marchés, trades, positions, hedge)"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR  = "JARVIS"

    SKILL_COMMANDS = {
        "polymarkets":   "Marchés tendance (`/polymarkets` ou `/polymarkets 20`)",
        "polysearch":    "Rechercher des marchés (`/polysearch bitcoin`)",
        "polymarket":    "Détail d'un marché (`/polymarket <conditionID>`)",
        "polybuy":       "Acheter YES/NO (`/polybuy <condID> YES 50`)",
        "polypositions": "Positions ouvertes + P&L",
        "polyhedge":     "Scanner les opportunités de hedge",
        "polywallet":    "Soldes POL et USDC.e",
        "polyapprove":   "Approuver les contrats (one-time setup)",
        "polyorders":    "Ordres ouverts sur le CLOB",
        "polycancel":    "Annuler un ordre (`/polycancel <orderID>`)",
    }

    # ── Init ────────────────────────────────────────────────────

    def __init__(self, settings=None):
        super().__init__(settings)
        self._clob   = None   # ClobClient
        self._w3     = None   # Web3
        self._ready  = False
        self._positions: dict = {}   # {condition_id: position_data}

    async def setup(self) -> bool:
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        if not private_key:
            logger.warning("⚠️ POLY_PRIVATE_KEY non défini — PolyJarvis en mode lecture seule")
            self._ready = True   # Mode read-only quand même
            return True

        try:
            from py_clob_client.client import ClobClient
            funder   = os.getenv("POLY_FUNDER", "")
            sig_type = int(os.getenv("POLY_SIG_TYPE", "0"))

            self._clob = ClobClient(
                CLOB_HOST,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=sig_type,
                funder=funder if funder else None,
            )
            self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
            logger.info(f"✅ ClobClient connecté | adresse: {self._clob.get_address()}")
        except ImportError:
            logger.error("py-clob-client non installé. `pip install py-clob-client`")
            return False
        except Exception as e:
            logger.error(f"Erreur init ClobClient: {e}")
            return False

        # Init Web3 (pour balances on-chain)
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
            logger.info(f"🔗 Web3 connecté | bloc: {self._w3.eth.block_number}")
        except ImportError:
            logger.warning("web3 non installé (pip install web3) — fonctions wallet limitées")
        except Exception as e:
            logger.warning(f"Web3 non disponible: {e}")

        self._load_positions()
        self._ready = True
        return True

    async def teardown(self):
        self._save_positions()
        self._ready = False

    # ── Router ──────────────────────────────────────────────────

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        routes = {
            "polymarkets":   self._cmd_markets,
            "polysearch":    self._cmd_search,
            "polymarket":    self._cmd_market_detail,
            "polybuy":       self._cmd_buy,
            "polypositions": self._cmd_positions,
            "polyhedge":     self._cmd_hedge,
            "polywallet":    self._cmd_wallet,
            "polyapprove":   self._cmd_approve,
            "polyorders":    self._cmd_orders,
            "polycancel":    self._cmd_cancel,
        }
        handler = routes.get(command)
        if handler:
            try:
                return await handler(args.strip(), context)
            except Exception as e:
                logger.error(f"Erreur PolyJarvis /{command}: {e}", exc_info=True)
                return f"❌ Erreur: `{e}`\n\nVérifiez vos logs JARVIS pour plus de détails."
        return "❓ Commande inconnue."

    # ══════════════════════════════════════════════════════════════
    #   1. MARCHÉS TENDANCE
    # ══════════════════════════════════════════════════════════════

    async def _cmd_markets(self, args: str, ctx: SkillContext) -> str:
        limit = int(args) if args.isdigit() else 10
        limit = min(limit, 25)

        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "limit": limit, "order": "volume24hr", "ascending": "false"},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            markets = await resp.json(content_type=None)

        if not markets:
            return "📭 Aucun marché trouvé."

        lines = [f"📈 **Marchés Polymarket — Top {len(markets)} par volume 24h**\n━━━━━━━━━━━━━━━"]
        for i, m in enumerate(markets, 1):
            vol24  = float(m.get("volume24hr") or 0)
            vol    = float(m.get("volume") or 0)
            liq    = float(m.get("liquidity") or 0)
            tokens = m.get("tokens", [])
            yes_p  = _get_token_price(tokens, "Yes")
            no_p   = _get_token_price(tokens, "No")
            cid    = m.get("conditionId", "?")[:14] + "…"

            lines.append(
                f"\n`{i}.` **{_truncate(m.get('question','?'), 60)}**\n"
                f"   🟢 YES `{yes_p:.2f}$` | 🔴 NO `{no_p:.2f}$`\n"
                f"   📊 Vol24h: `${vol24:,.0f}` | Total: `${vol:,.0f}` | Liq: `${liq:,.0f}`\n"
                f"   🔑 `{cid}`"
            )

        lines.append(f"\n💡 `/polymarket <conditionID>` pour le détail")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #   2. RECHERCHE
    # ══════════════════════════════════════════════════════════════

    async def _cmd_search(self, args: str, ctx: SkillContext) -> str:
        if not args:
            return "Usage: `/polysearch <mots-clés>`\nEx: `/polysearch bitcoin ETF`"

        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "limit": 15, "q": args},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            markets = await resp.json(content_type=None)

        if not markets:
            return f"🔍 Aucun marché trouvé pour `{args}`"

        lines = [f"🔍 **Résultats pour « {args} »** ({len(markets)} marchés)\n━━━━━━━━━━━━━━━"]
        for m in markets:
            tokens = m.get("tokens", [])
            yes_p  = _get_token_price(tokens, "Yes")
            no_p   = _get_token_price(tokens, "No")
            vol24  = float(m.get("volume24hr") or 0)
            cid    = m.get("conditionId", "")

            lines.append(
                f"\n📌 **{_truncate(m.get('question','?'), 70)}**\n"
                f"   🟢 YES `{yes_p:.2f}$` | 🔴 NO `{no_p:.2f}$` | Vol24h: `${vol24:,.0f}`\n"
                f"   🔑 `{cid}`"
            )

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #   3. DÉTAIL D'UN MARCHÉ
    # ══════════════════════════════════════════════════════════════

    async def _cmd_market_detail(self, args: str, ctx: SkillContext) -> str:
        if not args:
            return "Usage: `/polymarket <conditionID>`"
        cond_id = args.strip()

        # Récupérer depuis Gamma
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{GAMMA_API}/markets",
                params={"condition_ids": cond_id},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            data = await resp.json(content_type=None)

        if not data:
            return f"❌ Marché `{cond_id}` introuvable."

        m = data[0] if isinstance(data, list) else data
        tokens = m.get("tokens", [])
        yes_p  = _get_token_price(tokens, "Yes")
        no_p   = _get_token_price(tokens, "No")

        # Récupérer orderbook CLOB si possible
        clob_info = ""
        yes_token = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), None)
        if yes_token and self._clob:
            try:
                token_id = yes_token.get("token_id", "")
                mid  = self._clob.get_midpoint(token_id)
                bid  = self._clob.get_price(token_id, side="BUY")
                ask  = self._clob.get_price(token_id, side="SELL")
                spread = float(ask) - float(bid) if bid and ask else None
                clob_info = (
                    f"\n📖 **CLOB (token YES)**\n"
                    f"   Midpoint: `{float(mid):.4f}$` | Bid: `{float(bid):.4f}$` | Ask: `{float(ask):.4f}$`"
                )
                if spread is not None:
                    clob_info += f" | Spread: `{spread:.4f}`"
            except Exception as e:
                clob_info = f"\n⚠️ CLOB non disponible: {e}"

        end_date = m.get("endDate", m.get("end_date_iso", "?"))
        vol24  = float(m.get("volume24hr") or 0)
        vol    = float(m.get("volume") or 0)
        liq    = float(m.get("liquidity") or 0)
        status = "🟢 Actif" if m.get("active") else "🔴 Fermé"

        return (
            f"📊 **Détail du marché**\n━━━━━━━━━━━━━━━\n"
            f"❓ {m.get('question','?')}\n\n"
            f"🟢 YES: `{yes_p:.4f}$` ({yes_p*100:.1f}%)\n"
            f"🔴 NO:  `{no_p:.4f}$` ({no_p*100:.1f}%)\n\n"
            f"📊 Vol 24h: `${vol24:,.2f}`\n"
            f"📊 Vol total: `${vol:,.2f}`\n"
            f"💧 Liquidité: `${liq:,.2f}`\n"
            f"📅 Clôture: `{end_date}`\n"
            f"🔘 Statut: {status}\n"
            f"🔑 CondID: `{m.get('conditionId','?')}`"
            f"{clob_info}\n\n"
            f"💡 `/polybuy {cond_id} YES 50` pour acheter 50$ de YES"
        )

    # ══════════════════════════════════════════════════════════════
    #   4. ACHETER UNE POSITION (market order FOK)
    # ══════════════════════════════════════════════════════════════

    async def _cmd_buy(self, args: str, ctx: SkillContext) -> str:
        if not self._clob:
            return "🔒 Wallet non configuré. Ajoute `POLY_PRIVATE_KEY` dans ton `.env`"

        # Parsing: /polybuy <condID> YES|NO <montant$>
        parts = args.split()
        if len(parts) < 3:
            return (
                "Usage: `/polybuy <conditionID> YES|NO <montant$>`\n"
                "Ex: `/polybuy 0xabc...def YES 50`"
            )

        cond_id = parts[0]
        side_str = parts[1].upper()
        try:
            amount_usd = float(parts[2])
        except ValueError:
            return "❌ Montant invalide. Ex: `50` ou `25.5`"

        if side_str not in ("YES", "NO"):
            return "❌ Côté invalide. Utilise `YES` ou `NO`."
        if amount_usd < 1:
            return "❌ Montant minimum: 1 USDC"
        if amount_usd > 10000:
            return "❌ Montant maximum par ordre: 10 000 USDC (sécurité)"

        # Récupérer le token_id depuis Gamma
        token_id = await self._get_token_id(cond_id, side_str)
        if not token_id:
            return f"❌ Impossible de trouver le token `{side_str}` pour le marché `{cond_id}`"

        # Récupérer le prix actuel
        try:
            from py_clob_client.order_builder.constants import BUY as CLOB_BUY
            price_raw = self._clob.get_price(token_id, side="BUY")
            current_price = float(price_raw)
        except Exception as e:
            return f"❌ Impossible de récupérer le prix: {e}"

        if current_price <= 0 or current_price >= 1:
            return f"⚠️ Prix anormal: `{current_price}`. Vérifie le marché."

        estimated_shares = amount_usd / current_price

        # Confirmation avant d'exécuter (afficher un résumé)
        confirm_msg = (
            f"🛒 **Récapitulatif de l'ordre**\n━━━━━━━━━━━━━━━\n"
            f"📌 Marché: `{cond_id[:20]}…`\n"
            f"🎯 Côté: **{side_str}**\n"
            f"💵 Montant: **${amount_usd:.2f} USDC**\n"
            f"💹 Prix actuel: `{current_price:.4f}$` ({current_price*100:.1f}%)\n"
            f"📦 Shares estimées: `~{estimated_shares:.2f}`\n\n"
            f"⏳ Exécution en cours (FOK — Fill or Kill)…"
        )

        # Passer l'ordre market FOK
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY as CLOB_BUY

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
                side=CLOB_BUY,
                order_type=OrderType.FOK,
            )
            signed = self._clob.create_market_order(mo)
            resp   = self._clob.post_order(signed, OrderType.FOK)

            if resp and resp.get("status") in ("matched", "filled", "MATCHED", "FILLED"):
                # Enregistrer la position localement
                self._record_position(
                    cond_id=cond_id,
                    side=side_str,
                    token_id=token_id,
                    amount_usd=amount_usd,
                    entry_price=current_price,
                    shares=estimated_shares,
                    order_resp=resp
                )
                return (
                    f"✅ **Ordre exécuté avec succès !**\n━━━━━━━━━━━━━━━\n"
                    f"{confirm_msg}\n\n"
                    f"📋 Order ID: `{resp.get('orderID','?')}`\n"
                    f"🔗 TX: `{resp.get('transactionsHashes', ['?'])[0]}`\n\n"
                    f"💡 `/polypositions` pour voir ton P&L"
                )
            else:
                return (
                    f"⚠️ **Ordre non rempli (FOK)**\n"
                    f"{confirm_msg}\n\n"
                    f"Réponse: `{resp}`\n\n"
                    f"💡 Essaie avec un montant plus petit ou réessaie plus tard."
                )

        except Exception as e:
            logger.error(f"Erreur post_order: {e}", exc_info=True)
            return f"❌ Erreur lors de l'envoi de l'ordre:\n`{e}`"

    # ══════════════════════════════════════════════════════════════
    #   5. POSITIONS + P&L
    # ══════════════════════════════════════════════════════════════

    async def _cmd_positions(self, args: str, ctx: SkillContext) -> str:
        if not self._positions:
            return (
                "📭 **Aucune position enregistrée**\n\n"
                "Les positions sont trackées localement après chaque `/polybuy`.\n"
                "💡 `/polybuy <condID> YES 50` pour ouvrir une position."
            )

        lines = ["💼 **Positions ouvertes PolyJarvis**\n━━━━━━━━━━━━━━━"]
        total_invested = 0.0
        total_value    = 0.0

        for cond_id, pos in self._positions.items():
            token_id    = pos.get("token_id", "")
            entry_price = pos.get("entry_price", 0)
            shares      = pos.get("shares", 0)
            invested    = pos.get("amount_usd", 0)
            side        = pos.get("side", "?")
            opened_at   = pos.get("opened_at", "?")

            # Prix actuel
            current_price = entry_price  # fallback
            try:
                if token_id and self._clob:
                    cp = self._clob.get_midpoint(token_id)
                    current_price = float(cp)
            except Exception:
                pass

            current_value = shares * current_price
            pnl_usd       = current_value - invested
            pnl_pct       = (pnl_usd / invested * 100) if invested else 0
            pnl_icon      = "🟢" if pnl_usd >= 0 else "🔴"

            total_invested += invested
            total_value    += current_value

            lines.append(
                f"\n📌 `{_truncate(cond_id, 20)}…` | **{side}**\n"
                f"   💰 Investi: `${invested:.2f}` | Entrée: `{entry_price:.4f}$`\n"
                f"   💹 Actuel: `{current_price:.4f}$` | Valeur: `${current_value:.2f}`\n"
                f"   {pnl_icon} P&L: `${pnl_usd:+.2f}` ({pnl_pct:+.1f}%)\n"
                f"   📅 Ouvert le: `{opened_at}`"
            )

        total_pnl = total_value - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0
        pnl_icon  = "🟢" if total_pnl >= 0 else "🔴"

        lines.append(
            f"\n━━━━━━━━━━━━━━━\n"
            f"📊 **Total investi:** `${total_invested:.2f}`\n"
            f"📊 **Valeur actuelle:** `${total_value:.2f}`\n"
            f"{pnl_icon} **P&L total:** `${total_pnl:+.2f}` ({total_pnl_pct:+.1f}%)"
        )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #   6. HEDGE SCANNER
    # ══════════════════════════════════════════════════════════════

    async def _cmd_hedge(self, args: str, ctx: SkillContext) -> str:
        await asyncio.sleep(0)  # yield

        # Récupérer les top marchés
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "limit": 50, "order": "volume24hr", "ascending": "false"},
                timeout=aiohttp.ClientTimeout(total=15)
            )
            markets = await resp.json(content_type=None)

        if not markets:
            return "❌ Impossible de charger les marchés."

        msg_scanning = (
            f"🔍 **PolyJarvis — Hedge Scanner**\n━━━━━━━━━━━━━━━\n"
            f"Analyse de {len(markets)} marchés en cours…\n\n"
        )

        opportunities = []

        # Analyse des opportunités de hedge :
        # - Marchés avec YES+NO > 1.00 (spread arbitrage)
        # - Paires liées sémantiquement (analyse LLM si dispo)
        for m in markets:
            tokens = m.get("tokens", [])
            yes_p = _get_token_price(tokens, "Yes")
            no_p  = _get_token_price(tokens, "No")
            total = yes_p + no_p
            vol24 = float(m.get("volume24hr") or 0)
            liq   = float(m.get("liquidity") or 0)

            # Type 1: Spread arbitrage (YES+NO != 1.00 significativement)
            if total > 0.01:
                spread_dev = abs(total - 1.0)
                if spread_dev > 0.03 and vol24 > 5000 and liq > 1000:
                    opp_type = "📉 Sous-évalué" if total < 1.0 else "📈 Sur-évalué"
                    profit_potential = spread_dev * 100
                    opportunities.append({
                        "type": "spread",
                        "score": spread_dev,
                        "market": m,
                        "yes_p": yes_p,
                        "no_p": no_p,
                        "total": total,
                        "opp_type": opp_type,
                        "profit_pct": profit_potential,
                        "vol24": vol24,
                    })

        # Trier par opportunité (écart au 1.00)
        opportunities.sort(key=lambda x: x["score"], reverse=True)

        if not opportunities:
            result = msg_scanning + "✅ Aucune anomalie de prix détectée sur les marchés actifs.\n\n"
        else:
            result = msg_scanning + f"⚡ **{len(opportunities)} opportunité(s) détectée(s)**\n\n"
            for i, opp in enumerate(opportunities[:5], 1):
                m     = opp["market"]
                level = _hedge_tier(opp["score"])
                result += (
                    f"**{i}. {level['icon']} {level['name']}** — {_truncate(m.get('question','?'), 55)}\n"
                    f"   🟢 YES `{opp['yes_p']:.4f}$` + 🔴 NO `{opp['no_p']:.4f}$` = `{opp['total']:.4f}$`\n"
                    f"   {opp['opp_type']} | Écart: `{opp['score']*100:.2f}%` | Potentiel: `~{opp['profit_pct']:.1f}%`\n"
                    f"   📊 Vol24h: `${opp['vol24']:,.0f}` | 🔑 `{m.get('conditionId','?')[:20]}…`\n\n"
                )

        # Analyse LLM si contexte agent disponible
        if ctx.agent and opportunities:
            try:
                llm = ctx.agent.llm_router.get()
                prompt = (
                    "Tu es un analyste de marchés prédictifs. Voici des marchés Polymarket avec des anomalies de prix:\n\n" +
                    "\n".join(
                        f"- {o['market'].get('question','?')} | YES={o['yes_p']:.3f} NO={o['no_p']:.3f} total={o['total']:.3f}"
                        for o in opportunities[:3]
                    ) +
                    "\n\nAnalyse brièvement (2-3 phrases) si ces anomalies représentent de vraies opportunités "
                    "de hedge ou d'arbitrage, et lesquelles éviter."
                )
                llm_analysis = await llm.chat([{"role": "user", "content": prompt}])
                result += f"🤖 **Analyse JARVIS:**\n_{llm_analysis[:400]}_"
            except Exception as e:
                logger.warning(f"LLM hedge analysis failed: {e}")

        result += f"\n\n━━━━━━━━━━━━━━━\n💡 `/polymarket <condID>` pour détailler un marché\n💡 `/polybuy <condID> YES|NO <$>` pour trader"
        return result

    # ══════════════════════════════════════════════════════════════
    #   7. WALLET — SOLDES ON-CHAIN
    # ══════════════════════════════════════════════════════════════

    async def _cmd_wallet(self, args: str, ctx: SkillContext) -> str:
        if not self._clob:
            return "🔒 Wallet non configuré. Ajoute `POLY_PRIVATE_KEY` dans ton `.env`"

        address = self._clob.get_address()
        lines = [f"💼 **Wallet PolyJarvis**\n━━━━━━━━━━━━━━━\n📍 Adresse: `{address}`\n"]

        if self._w3:
            try:
                from web3 import Web3
                addr_cs = Web3.to_checksum_address(address)

                # POL (gas token, natif Polygon)
                pol_wei = self._w3.eth.get_balance(addr_cs)
                pol_bal = pol_wei / 1e18

                # USDC.e (6 decimales)
                usdc_contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI
                )
                usdc_raw = usdc_contract.functions.balanceOf(addr_cs).call()
                usdc_bal = usdc_raw / 1e6

                # Allowances USDC vers exchange
                allow_exchange = usdc_contract.functions.allowance(
                    addr_cs, Web3.to_checksum_address(EXCHANGE_ADDRESS)
                ).call() / 1e6

                pol_icon   = "✅" if pol_bal > 0.05 else "⚠️"
                usdc_icon  = "✅" if usdc_bal > 1 else "⚠️"
                allow_icon = "✅" if allow_exchange > 1000 else "❌"

                lines.append(
                    f"{pol_icon} **POL (gas):** `{pol_bal:.6f} POL`"
                    + (" ⚠️ Peu de gas !" if pol_bal < 0.05 else "") + "\n"
                    f"{usdc_icon} **USDC.e:** `{usdc_bal:.4f} USDC`\n\n"
                    f"**Approbations contrats:**\n"
                    f"{allow_icon} Exchange principal: `{allow_exchange:,.0f} USDC` autorisé"
                )

                if allow_exchange < 100:
                    lines.append("\n⚠️ Approbation insuffisante → `/polyapprove`")

            except Exception as e:
                lines.append(f"❌ Erreur lecture on-chain: `{e}`")
        else:
            lines.append("⚠️ Web3 non disponible — impossible de lire les soldes on-chain\n`pip install web3`")

        lines.append(f"\n🔗 [Voir sur Polygonscan](https://polygonscan.com/address/{address})")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #   8. APPROVE — APPROBATION ONE-TIME
    # ══════════════════════════════════════════════════════════════

    async def _cmd_approve(self, args: str, ctx: SkillContext) -> str:
        if not self._clob:
            return "🔒 Wallet non configuré."
        if not self._w3:
            return "❌ Web3 requis pour les approbations on-chain. `pip install web3`"

        try:
            from web3 import Web3
            MAX_UINT256 = 2**256 - 1
            addr_cs  = Web3.to_checksum_address(self._clob.get_address())
            pk       = os.getenv("POLY_PRIVATE_KEY", "")
            results  = []

            spenders = [
                (EXCHANGE_ADDRESS, "Exchange principal"),
                (NEG_RISK_ADDRESS, "Neg-Risk Exchange"),
                (NEG_RISK_ADAPTER, "Neg-Risk Adapter"),
            ]

            usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI
            )
            ctf  = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI
            )

            nonce = self._w3.eth.get_transaction_count(addr_cs)

            for spender_addr, name in spenders:
                spender_cs = Web3.to_checksum_address(spender_addr)

                # USDC approve
                current = usdc.functions.allowance(addr_cs, spender_cs).call()
                if current < MAX_UINT256 // 2:
                    tx = usdc.functions.approve(spender_cs, MAX_UINT256).build_transaction({
                        "from": addr_cs, "nonce": nonce,
                        "gas": 100000,
                        "gasPrice": self._w3.to_wei("50", "gwei"),
                    })
                    signed = self._w3.eth.account.sign_transaction(tx, pk)
                    tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                    results.append(f"✅ USDC → {name}: `{tx_hash.hex()[:20]}…`")
                    nonce += 1
                else:
                    results.append(f"✅ USDC → {name}: déjà approuvé")

                # CTF setApprovalForAll
                is_approved = ctf.functions.isApprovedForAll(addr_cs, spender_cs).call()
                if not is_approved:
                    tx = ctf.functions.setApprovalForAll(spender_cs, True).build_transaction({
                        "from": addr_cs, "nonce": nonce,
                        "gas": 100000,
                        "gasPrice": self._w3.to_wei("50", "gwei"),
                    })
                    signed = self._w3.eth.account.sign_transaction(tx, pk)
                    tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                    results.append(f"✅ CTF → {name}: `{tx_hash.hex()[:20]}…`")
                    nonce += 1
                else:
                    results.append(f"✅ CTF → {name}: déjà approuvé")

            return (
                "🔐 **Approbations on-chain**\n━━━━━━━━━━━━━━━\n" +
                "\n".join(results) +
                "\n\n✅ Ton wallet est prêt pour trader !"
            )

        except Exception as e:
            logger.error(f"Erreur approve: {e}", exc_info=True)
            return f"❌ Erreur lors de l'approbation:\n`{e}`"

    # ══════════════════════════════════════════════════════════════
    #   9. ORDRES OUVERTS
    # ══════════════════════════════════════════════════════════════

    async def _cmd_orders(self, args: str, ctx: SkillContext) -> str:
        if not self._clob:
            return "🔒 Wallet non configuré."

        try:
            from py_clob_client.clob_types import OpenOrderParams
            orders = self._clob.get_orders(OpenOrderParams())
        except Exception as e:
            return f"❌ Erreur récupération ordres: `{e}`"

        if not orders:
            return "📭 Aucun ordre ouvert sur le CLOB."

        lines = [f"📋 **Ordres ouverts** ({len(orders)})\n━━━━━━━━━━━━━━━"]
        for o in orders[:10]:
            side   = o.get("side", "?")
            price  = float(o.get("price", 0))
            size   = float(o.get("size", 0))
            filled = float(o.get("size_matched", 0))
            oid    = o.get("id", "?")[:16] + "…"
            asset  = o.get("asset_id", "?")[:14] + "…"
            side_icon = "🟢" if side == "BUY" else "🔴"

            lines.append(
                f"{side_icon} **{side}** `{size:.2f}` @ `{price:.4f}$` (rempli: `{filled:.2f}`)\n"
                f"   Token: `{asset}` | Order: `{oid}`"
            )

        lines.append(f"\n💡 `/polycancel <orderID>` pour annuler")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #   10. ANNULER UN ORDRE
    # ══════════════════════════════════════════════════════════════

    async def _cmd_cancel(self, args: str, ctx: SkillContext) -> str:
        if not self._clob:
            return "🔒 Wallet non configuré."
        if not args:
            return "Usage: `/polycancel <orderID>`"

        try:
            resp = self._clob.cancel(args.strip())
            return f"✅ Ordre `{args.strip()[:20]}…` annulé !\n\nRéponse: `{resp}`"
        except Exception as e:
            return f"❌ Erreur annulation: `{e}`"

    # ══════════════════════════════════════════════════════════════
    #   HELPERS INTERNES
    # ══════════════════════════════════════════════════════════════

    async def _get_token_id(self, cond_id: str, side: str) -> Optional[str]:
        """Récupère le token_id YES ou NO depuis Gamma"""
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{GAMMA_API}/markets",
                    params={"condition_ids": cond_id},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json(content_type=None)

            if not data:
                return None
            m = data[0] if isinstance(data, list) else data
            tokens = m.get("tokens", [])
            for t in tokens:
                if t.get("outcome", "").lower() == side.lower():
                    return t.get("token_id")
        except Exception as e:
            logger.error(f"Erreur _get_token_id: {e}")
        return None

    def _record_position(self, cond_id, side, token_id, amount_usd, entry_price, shares, order_resp):
        """Enregistre une position dans le fichier JSON local"""
        self._positions[f"{cond_id}_{side}_{int(time.time())}"] = {
            "cond_id":     cond_id,
            "side":        side,
            "token_id":    token_id,
            "amount_usd":  amount_usd,
            "entry_price": entry_price,
            "shares":      shares,
            "order_id":    order_resp.get("orderID", "?"),
            "opened_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._save_positions()

    def _save_positions(self):
        POSITIONS_FILE.parent.mkdir(exist_ok=True)
        with open(POSITIONS_FILE, "w") as f:
            json.dump(self._positions, f, indent=2, ensure_ascii=False)

    def _load_positions(self):
        if POSITIONS_FILE.exists():
            try:
                with open(POSITIONS_FILE) as f:
                    self._positions = json.load(f)
            except Exception:
                self._positions = {}


# ══════════════════════════════════════════════════════════════
#   HELPERS GLOBAUX
# ══════════════════════════════════════════════════════════════

def _get_token_price(tokens: list, outcome: str) -> float:
    """Extrait le prix d'un token YES ou NO depuis la liste tokens Gamma"""
    for t in tokens:
        if t.get("outcome", "").lower() == outcome.lower():
            price = t.get("price") or t.get("last_trade_price") or t.get("best_bid")
            if price is not None:
                return float(price)
    return 0.0


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len] + "…"


def _hedge_tier(score: float) -> dict:
    """Classe une opportunité de hedge en T1–T4"""
    if score >= 0.15:
        return {"name": "T1 — Critique", "icon": "🔥"}
    elif score >= 0.08:
        return {"name": "T2 — Fort",     "icon": "⭐"}
    elif score >= 0.05:
        return {"name": "T3 — Modéré",   "icon": "💡"}
    else:
        return {"name": "T4 — Faible",   "icon": "📌"}
