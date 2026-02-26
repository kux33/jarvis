"""
Interface Telegram pour JARVIS
"""

import logging
import asyncio
from typing import Optional
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction, ParseMode

logger = logging.getLogger("Jarvis.Telegram")


class JarvisTelegramBot:
    def __init__(self, agent, settings):
        self.agent = agent
        self.settings = settings
        
        self.app = Application.builder().token(settings.telegram_token).build()
        self._register_handlers()

    def _register_handlers(self):
        """Enregistre tous les handlers Telegram"""
        self.app.add_handler(CommandHandler("start",   self._on_start))
        self.app.add_handler(CommandHandler("help",    self._on_help))
        self.app.add_handler(CommandHandler("status",  self._on_status))
        self.app.add_handler(CommandHandler("sysinfo", self._on_sysinfo))
        self.app.add_handler(CommandHandler("llm",     self._on_llm))
        self.app.add_handler(CommandHandler("clear",   self._on_clear))
        self.app.add_handler(CommandHandler("memory",  self._on_memory))
        self.app.add_handler(CommandHandler("skill",   self._on_skill))
        
        if self.settings.enable_shell:
            self.app.add_handler(CommandHandler("shell", self._on_shell))

        # Handler générique pour TOUTES les autres commandes (→ skills)
        self.app.add_handler(MessageHandler(filters.COMMAND, self._on_any_command))
        # Handler messages texte
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

    def _is_authorized(self, update: Update) -> bool:
        """Vérifie que l'utilisateur est autorisé"""
        if not self.settings.telegram_allowed_users:
            return True  # Si aucun whitelist, tout le monde peut utiliser
        return update.effective_user.id in self.settings.telegram_allowed_users

    async def _check_auth(self, update: Update) -> bool:
        """Vérifie l'auth et répond si non autorisé"""
        if not self._is_authorized(update):
            await update.message.reply_text("🚫 Accès refusé.")
            logger.warning(f"Accès refusé: user_id={update.effective_user.id}")
            return False
        return True

    async def _send_typing(self, update: Update):
        await update.effective_chat.send_action(ChatAction.TYPING)

    # ──────────────────────────────────────────
    # Handlers Telegram
    # ──────────────────────────────────────────

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        user = update.effective_user.first_name
        await update.message.reply_text(
            f"👋 Bonjour {user} ! Je suis **JARVIS**, votre assistant IA personnel.\n\n"
            f"🤖 LLM actif: `{self.settings.active_llm.upper()}`\n\n"
            f"Posez-moi une question ou utilisez /help pour les commandes.",
            parse_mode=ParseMode.MARKDOWN
        )

    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        response = await self.agent.process("/help")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        response = await self.agent.process("/status")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_sysinfo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        await self._send_typing(update)
        response = await self.agent.process("/sysinfo")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_llm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        args = " ".join(ctx.args) if ctx.args else ""
        response = await self.agent.process(f"/llm {args}")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_clear(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        response = await self.agent.process("/clear")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        response = await self.agent.process("/memory")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_skill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        args = " ".join(ctx.args) if ctx.args else ""
        response = await self.agent.process(f"/skill {args}", update.effective_user.id, update.effective_user.first_name)
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_any_command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handler générique pour les commandes de skills"""
        if not await self._check_auth(update):
            return
        # Reconstruire la commande complète
        text = update.message.text
        await self._send_typing(update)
        response = await self.agent.process(
            text,
            update.effective_user.id,
            update.effective_user.first_name
        )
        await self._reply(update, response)

    async def _reply(self, update: Update, text: str):
        """Envoie une réponse en découpant si nécessaire"""
        if len(text) > 4000:
            for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def send_message(self, user_id: int, text: str):
        """Envoie un message proactif (utilisé par les skills: rappels, alertes...)"""
        try:
            await self.app.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Erreur envoi message à {user_id}: {e}")

    async def _on_shell(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        cmd = " ".join(ctx.args) if ctx.args else ""
        await self._send_typing(update)
        response = await self.agent.process(f"/shell {cmd}")
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handler principal pour les messages texte libres"""
        if not await self._check_auth(update):
            return
        
        user_text = update.message.text
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        
        logger.info(f"Message de {user_name} ({user_id}): {user_text[:50]}")
        await self._send_typing(update)
        
        try:
            response = await self.agent.process(user_text, user_id, user_name)
            await self._reply(update, response)
        except Exception as e:
            logger.error(f"Erreur traitement message: {e}")
            await update.message.reply_text(f"⚠️ Erreur interne: {str(e)}")

    # ──────────────────────────────────────────
    # Démarrage
    # ──────────────────────────────────────────

    async def run(self):
        """Démarre le bot Telegram"""
        commands = [
            BotCommand("start",   "Démarrer JARVIS"),
            BotCommand("help",    "Aide et commandes"),
            BotCommand("status",  "Statut de JARVIS"),
            BotCommand("sysinfo", "Infos système"),
            BotCommand("llm",     "Voir/changer le LLM"),
            BotCommand("skill",   "Gérer les skills"),
            BotCommand("clear",   "Effacer la mémoire"),
            BotCommand("memory",  "Voir la mémoire"),
        ]
        
        # Injecter le callback d'envoi dans la skill rappel (si chargée)
        rappel_skill = self.agent.skill_manager._skills.get("rappel")
        if rappel_skill and hasattr(rappel_skill, "set_send_callback"):
            rappel_skill.set_send_callback(self.send_message)
        
        async with self.app:
            await self.app.bot.set_my_commands(commands)
            logger.info("🚀 Bot Telegram démarré — En attente de messages...")
            await self.app.start()
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await asyncio.Event().wait()
