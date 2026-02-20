#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║          JARVIS - AI Agent               ║
║   Compatible Raspberry Pi 5              ║
║   Multi-LLM | Telegram Control           ║
╚══════════════════════════════════════════╝
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from core.agent import JarvisAgent
from telegram.bot import JarvisTelegramBot
from config.settings import Settings

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/jarvis.log"),
    ]
)
logger = logging.getLogger("Jarvis.Main")


async def main():
    logger.info("🤖 Démarrage de JARVIS...")
    
    # Charger la configuration
    settings = Settings()
    
    # Créer l'agent IA
    agent = JarvisAgent(settings)
    
    # Initialisation asynchrone (chargement des skills)
    await agent.initialize()
    
    # Démarrer le bot Telegram
    bot = JarvisTelegramBot(agent, settings)
    
    logger.info(f"✅ JARVIS opérationnel | LLM actif: {settings.active_llm.upper()}")
    
    # Gestion propre de l'arrêt
    loop = asyncio.get_running_loop()
    
    def shutdown():
        logger.info("🔴 Arrêt de JARVIS...")
        loop.stop()
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
