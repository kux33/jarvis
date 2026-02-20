"""
Skill JARVIS : Rappels & Timers
Commandes: /rappel, /timer, /rappels
"""

import asyncio
import re
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.Rappel")


@dataclass
class Reminder:
    id: int
    user_id: int
    text: str
    fire_at: datetime
    task: Optional[asyncio.Task] = None


class RappelSkill(BaseSkill):
    SKILL_NAME = "rappel"
    SKILL_DESC = "Rappels et timers"
    SKILL_VERSION = "1.0.0"
    SKILL_COMMANDS = {
        "rappel":   "Créer un rappel (`/rappel 10m Appeler le médecin`)",
        "timer":    "Lancer un timer (`/timer 25m`)",
        "rappels":  "Lister les rappels actifs",
        "annuler":  "Annuler un rappel (`/annuler 3`)",
    }

    TIME_UNITS = {
        "s": 1, "sec": 1, "seconde": 1, "secondes": 1,
        "m": 60, "min": 60, "minute": 60, "minutes": 60,
        "h": 3600, "heure": 3600, "heures": 3600,
        "j": 86400, "jour": 86400, "jours": 86400,
    }

    def __init__(self, settings=None):
        super().__init__(settings)
        self._reminders: dict[int, Reminder] = {}
        self._counter = 0
        self._send_callback = None  # Injecté par le bot Telegram

    async def setup(self) -> bool:
        self._ready = True
        return True

    async def teardown(self):
        for r in self._reminders.values():
            if r.task:
                r.task.cancel()
        self._reminders.clear()
        self._ready = False

    def set_send_callback(self, callback):
        """Inject le callback pour envoyer des messages Telegram"""
        self._send_callback = callback

    def _parse_duration(self, text: str) -> tuple[Optional[int], str]:
        """Parse '10m faire la vaisselle' → (600, 'faire la vaisselle')"""
        match = re.match(r"(\d+)\s*([a-zé]+)\s*(.*)", text.strip(), re.IGNORECASE)
        if not match:
            return None, text
        amount, unit, rest = match.groups()
        unit = unit.lower()
        seconds = self.TIME_UNITS.get(unit)
        if not seconds:
            return None, text
        return int(amount) * seconds, rest.strip()

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        if command == "rappel":
            return await self._create_reminder(args, context)
        elif command == "timer":
            return await self._create_timer(args, context)
        elif command == "rappels":
            return self._list_reminders(context.user_id)
        elif command == "annuler":
            return self._cancel_reminder(args.strip(), context.user_id)
        return "Commande inconnue."

    async def _create_reminder(self, args: str, context: SkillContext) -> str:
        if not args:
            return "Usage: `/rappel 10m Texte du rappel`\nExemple: `/rappel 1h30m Réunion`"

        seconds, text = self._parse_duration(args)
        if not seconds:
            return "❌ Durée invalide. Exemples: `5m`, `1h`, `30s`, `2j`"
        if not text:
            text = "⏰ Rappel JARVIS"

        self._counter += 1
        rid = self._counter
        fire_at = datetime.now() + timedelta(seconds=seconds)

        reminder = Reminder(id=rid, user_id=context.user_id, text=text, fire_at=fire_at)

        async def _fire():
            await asyncio.sleep(seconds)
            if rid in self._reminders:
                del self._reminders[rid]
                if self._send_callback:
                    await self._send_callback(
                        context.user_id,
                        f"⏰ **Rappel #{rid}**\n{text}"
                    )

        reminder.task = asyncio.create_task(_fire())
        self._reminders[rid] = reminder

        human = self._human_duration(seconds)
        return f"✅ Rappel #{rid} créé !\n⏱ Dans **{human}** : _{text}_"

    async def _create_timer(self, args: str, context: SkillContext) -> str:
        if not args:
            return "Usage: `/timer 25m`"

        seconds, _ = self._parse_duration(args.strip() + " x")
        if not seconds:
            return "❌ Durée invalide."

        return await self._create_reminder(f"{args} ⏰ Timer terminé !", context)

    def _list_reminders(self, user_id: int) -> str:
        user_reminders = [r for r in self._reminders.values() if r.user_id == user_id]
        if not user_reminders:
            return "📭 Aucun rappel actif."
        lines = ["📋 **Rappels actifs**\n━━━━━━━━━━━━━━━"]
        for r in user_reminders:
            delta = r.fire_at - datetime.now()
            remaining = max(0, int(delta.total_seconds()))
            lines.append(f"#{r.id} — _{r.text}_ (dans {self._human_duration(remaining)})")
        return "\n".join(lines)

    def _cancel_reminder(self, rid_str: str, user_id: int) -> str:
        try:
            rid = int(rid_str)
        except ValueError:
            return "Usage: `/annuler <numéro>` (ex: `/annuler 3`)"

        if rid not in self._reminders:
            return f"❌ Rappel #{rid} introuvable."
        r = self._reminders.pop(rid)
        if r.task:
            r.task.cancel()
        return f"🗑️ Rappel #{rid} annulé."

    def _human_duration(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            m, s = divmod(seconds, 60)
            return f"{m}m{s}s" if s else f"{m}m"
        elif seconds < 86400:
            h, rem = divmod(seconds, 3600)
            m = rem // 60
            return f"{h}h{m}m" if m else f"{h}h"
        else:
            d, rem = divmod(seconds, 86400)
            h = rem // 3600
            return f"{d}j{h}h" if h else f"{d}j"
