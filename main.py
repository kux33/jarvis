#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║          JARVIS - AI Agent               ║
║   Compatible Windows / Raspberry Pi 5    ║
║   Multi-LLM | Telegram Control           ║
╚══════════════════════════════════════════╝
"""

import asyncio
import logging
import sys
import platform
from pathlib import Path
from dotenv import load_dotenv

# Charger le .env AVANT tout le reste
load_dotenv()

# ── Logging UTF-8 (fix Windows cp1252) ────────────────────────
Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(
            stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
        ),
        logging.FileHandler("logs/jarvis.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("Jarvis.Main")

# ── Imports JARVIS ─────────────────────────────────────────────
from core.agent import JarvisAgent
from tgbot.bot import JarvisTelegramBot
from config.settings import Settings


async def main():
    logger.info("JARVIS - Demarrage...")

    settings = Settings()
    agent = JarvisAgent(settings)
    await agent.initialize()

    bot = JarvisTelegramBot(agent, settings)

    logger.info(f"JARVIS operationnel | LLM actif: {settings.active_llm.upper()}")

    # Gestion arret compatible Windows + Linux
    if platform.system() != "Windows":
        import signal
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("JARVIS - Arret demande (Ctrl+C)")
    finally:
        logger.info("JARVIS - Arret propre.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
