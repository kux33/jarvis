"""
Cœur de l'agent JARVIS
Gestion de la mémoire, des outils et de la logique de l'agent
"""

import logging
import asyncio
import subprocess
import platform
import psutil
from datetime import datetime
from typing import Optional
from collections import deque

from llm.router import LLMRouter
from skills.manager import SkillManager
from skills.base import SkillContext

logger = logging.getLogger("Jarvis.Agent")


class Memory:
    """Mémoire de conversation avec fenêtre glissante"""
    
    def __init__(self, max_messages: int = 20):
        self.max_messages = max_messages
        self.messages = deque(maxlen=max_messages)
        self.session_start = datetime.now()

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})

    def get(self) -> list:
        return list(self.messages)

    def clear(self):
        self.messages.clear()
        self.session_start = datetime.now()

    def __len__(self):
        return len(self.messages)


class JarvisAgent:
    """Agent principal JARVIS"""

    def __init__(self, settings):
        self.settings = settings
        self.llm_router = LLMRouter(settings)
        self.memory = Memory(settings.memory_max_messages)
        self.skill_manager = SkillManager(settings, agent=self)
        
        # Charger l'état précédent si disponible
        try:
            settings.load_state()
        except Exception:
            pass

        logger.info(f"🧠 Agent JARVIS initialisé | LLM: {settings.active_llm}")

    async def initialize(self):
        """Initialisation asynchrone (skills, etc.)"""
        await self.skill_manager.load_all()

    # ──────────────────────────────────────────
    # Point d'entrée principal
    # ──────────────────────────────────────────

    async def process(self, user_input: str, user_id: int = 0, username: str = "") -> str:
        """Traite un message utilisateur et retourne la réponse"""
        
        # Commandes spéciales (slash commands)
        if user_input.startswith("/"):
            cmd_parts = user_input.strip().split(maxsplit=1)
            cmd = cmd_parts[0].lstrip("/").lower()
            args = cmd_parts[1] if len(cmd_parts) > 1 else ""
            
            # Commandes de gestion des skills
            if cmd == "skill":
                return await self._handle_skill_cmd(args, user_id, username)
            
            # Router vers une skill si elle gère cette commande
            if self.skill_manager.has_command(cmd):
                ctx = SkillContext(user_id=user_id, username=username, settings=self.settings, agent=self)
                return await self.skill_manager.dispatch(cmd, args, ctx)
            
            # Commandes internes JARVIS
            return await self._handle_command(user_input, user_id)

        # Ajouter à la mémoire
        self.memory.add("user", user_input)

        try:
            llm = self.llm_router.get()
            response = await llm.chat(self.memory.get())
            self.memory.add("assistant", response)
            return response

        except Exception as e:
            logger.error(f"Erreur LLM: {e}")
            return f"⚠️ Erreur avec {self.settings.active_llm.upper()}: {str(e)}"

    async def _handle_skill_cmd(self, args: str, user_id: int, username: str) -> str:
        """Gestion des commandes /skill"""
        parts = args.strip().split(maxsplit=1) if args.strip() else []
        sub = parts[0].lower() if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if not sub or sub == "list":
            return self.skill_manager.list_skills()
        elif sub == "on":
            return await self.skill_manager.enable(target)
        elif sub == "off":
            return await self.skill_manager.disable(target)
        elif sub == "reload":
            return await self.skill_manager.reload_skill(target)
        elif sub == "help":
            return self.skill_manager.skill_help(target)
        else:
            return (
                "📦 **Commandes Skills**\n━━━━━━━━━━━━━━━\n"
                "`/skill list` — Lister les skills\n"
                "`/skill on <nom>` — Activer\n"
                "`/skill off <nom>` — Désactiver\n"
                "`/skill reload <nom>` — Recharger à chaud\n"
                "`/skill help <nom>` — Aide d'une skill"
            )

    # ──────────────────────────────────────────
    # Commandes internes
    # ──────────────────────────────────────────

    async def _handle_command(self, cmd: str, user_id: int) -> str:
        parts = cmd.strip().split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handlers = {
            "/llm":     self._cmd_llm,
            "/status":  self._cmd_status,
            "/memory":  self._cmd_memory,
            "/clear":   self._cmd_clear,
            "/sysinfo": self._cmd_sysinfo,
            "/help":    self._cmd_help,
        }

        # Commandes shell (si activé)
        if self.settings.enable_shell and command == "/shell":
            return await self._cmd_shell(args)

        handler = handlers.get(command)
        if handler:
            return await handler(args)
        
        return f"❓ Commande inconnue: `{command}`\nTape /help pour la liste."

    async def _cmd_llm(self, args: str) -> str:
        """Change ou affiche le LLM actif"""
        if not args:
            current = self.settings.active_llm
            available = self.llm_router.available()
            status = "\n".join(
                f"{'✅' if llm == current else '⬜'} `{llm}`"
                for llm in available
            )
            return f"🤖 **LLM actif:** `{current}`\n\n{status}\n\nUtilise `/llm <nom>` pour changer."
        
        try:
            llm = self.llm_router.switch(args.strip())
            return f"🔄 LLM changé → **{llm.get_name()}**"
        except ValueError as e:
            return f"❌ {e}"

    async def _cmd_status(self, args: str) -> str:
        """Statut de JARVIS"""
        llm = self.llm_router.get()
        uptime = datetime.now() - self.memory.session_start
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes = rem // 60
        
        return (
            f"🤖 **JARVIS Status**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🧠 LLM: `{llm.get_name()}`\n"
            f"💬 Messages en mémoire: `{len(self.memory)}`\n"
            f"⏱ Session: `{hours}h {minutes}m`\n"
            f"🖥 Plateforme: `{platform.machine()}`\n"
            f"📅 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
        )

    async def _cmd_sysinfo(self, args: str) -> str:
        """Infos système (CPU, RAM, température)"""
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Température (Raspberry Pi)
        temp_str = "N/A"
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ("cpu_thermal", "coretemp", "acpitz"):
                    if key in temps:
                        temp_str = f"{temps[key][0].current:.1f}°C"
                        break
        except Exception:
            try:
                with open("/sys/class/thermal/thermal_zone0/temp") as f:
                    temp_str = f"{int(f.read()) / 1000:.1f}°C"
            except Exception:
                pass

        return (
            f"🖥 **Infos Système**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💻 CPU: `{cpu}%`\n"
            f"🌡 Température: `{temp_str}`\n"
            f"🧠 RAM: `{ram.used // 1024**2}MB / {ram.total // 1024**2}MB ({ram.percent}%)`\n"
            f"💾 Disque: `{disk.used // 1024**3}GB / {disk.total // 1024**3}GB ({disk.percent}%)`\n"
            f"🏗 OS: `{platform.system()} {platform.release()}`\n"
            f"⚙️ Arch: `{platform.machine()}`"
        )

    async def _cmd_memory(self, args: str) -> str:
        """Affiche la mémoire de conversation"""
        if not self.memory.messages:
            return "💭 Mémoire vide."
        
        lines = [f"💭 **Mémoire** ({len(self.memory)} messages)\n━━━━━━━━━━━━━━━"]
        for msg in list(self.memory.messages)[-6:]:  # 6 derniers
            role = "👤" if msg["role"] == "user" else "🤖"
            content = msg["content"][:100] + "..." if len(msg["content"]) > 100 else msg["content"]
            lines.append(f"{role} {content}")
        return "\n".join(lines)

    async def _cmd_clear(self, args: str) -> str:
        """Efface la mémoire"""
        self.memory.clear()
        return "🧹 Mémoire effacée. Nouvelle session démarrée."

    async def _cmd_shell(self, args: str) -> str:
        """Exécute une commande shell (si activé)"""
        if not args:
            return "Usage: /shell <commande>"
        try:
            result = await asyncio.create_subprocess_shell(
                args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=30)
            output = stdout.decode() or stderr.decode()
            return f"```\n{output[:2000]}\n```"
        except asyncio.TimeoutError:
            return "⏱ Timeout (30s)"
        except Exception as e:
            return f"❌ Erreur: {e}"

    async def _cmd_help(self, args: str) -> str:
        """Aide"""
        shell_line = "\n`/shell <cmd>` — Exécuter une commande shell" if self.settings.enable_shell else ""
        skills_cmds = ""
        if self.skill_manager._skills:
            all_cmds = []
            for name, skill in self.skill_manager._skills.items():
                if name not in self.skill_manager._disabled:
                    for cmd in skill.SKILL_COMMANDS:
                        all_cmds.append(f"`/{cmd}`")
            if all_cmds:
                skills_cmds = f"\n\n🔧 **Skills actives:**\n" + " ".join(all_cmds)
        return (
            f"🤖 **JARVIS — Aide**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"`/llm` — Voir/changer le LLM actif\n"
            f"`/llm claude|grok|openai|kimi` — Changer de LLM\n"
            f"`/status` — Statut de JARVIS\n"
            f"`/sysinfo` — Infos système & température\n"
            f"`/memory` — Voir la mémoire de conversation\n"
            f"`/clear` — Effacer la mémoire\n"
            f"`/skill list` — Gérer les skills"
            f"{shell_line}"
            f"{skills_cmds}\n\n"
            f"💬 Sinon, parle-moi directement !"
        )
