"""
Skill JARVIS : Notes persistantes
Commandes: /note, /notes, /supprimenote
"""

import json
import logging
from pathlib import Path
from datetime import datetime

from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.Notes")

NOTES_FILE = Path("config/notes.json")


class NotesSkill(BaseSkill):
    SKILL_NAME = "notes"
    SKILL_DESC = "Notes persistantes par utilisateur"
    SKILL_VERSION = "1.0.0"
    SKILL_COMMANDS = {
        "note":         "Ajouter une note (`/note Faire les courses`)",
        "notes":        "Lister mes notes",
        "supprimenote": "Supprimer une note (`/supprimenote 2`)",
        "effacenotes":  "Supprimer toutes mes notes",
    }

    def __init__(self, settings=None):
        super().__init__(settings)
        self._data: dict = {}   # {str(user_id): [{id, text, date}]}

    async def setup(self) -> bool:
        self._load()
        self._ready = True
        return True

    def _load(self):
        if NOTES_FILE.exists():
            try:
                with open(NOTES_FILE) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        NOTES_FILE.parent.mkdir(exist_ok=True)
        with open(NOTES_FILE, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        uid = str(context.user_id)
        if uid not in self._data:
            self._data[uid] = []

        if command == "note":
            return self._add(uid, args.strip())
        elif command == "notes":
            return self._list(uid)
        elif command == "supprimenote":
            return self._delete(uid, args.strip())
        elif command == "effacenotes":
            return self._clear(uid)
        return ""

    def _add(self, uid: str, text: str) -> str:
        if not text:
            return "Usage: `/note Mon texte`"
        notes = self._data[uid]
        nid = (max((n["id"] for n in notes), default=0) + 1)
        notes.append({"id": nid, "text": text, "date": datetime.now().strftime("%d/%m %H:%M")})
        self._save()
        return f"📝 Note #{nid} enregistrée !"

    def _list(self, uid: str) -> str:
        notes = self._data.get(uid, [])
        if not notes:
            return "📭 Aucune note. Utilise `/note <texte>` pour en créer une."
        lines = [f"📝 **Mes notes** ({len(notes)})\n━━━━━━━━━━━━━━━"]
        for n in notes:
            lines.append(f"`#{n['id']}` {n['text']} _(le {n['date']})_")
        return "\n".join(lines)

    def _delete(self, uid: str, nid_str: str) -> str:
        try:
            nid = int(nid_str)
        except ValueError:
            return "Usage: `/supprimenote <numéro>`"
        notes = self._data.get(uid, [])
        new_notes = [n for n in notes if n["id"] != nid]
        if len(new_notes) == len(notes):
            return f"❌ Note #{nid} introuvable."
        self._data[uid] = new_notes
        self._save()
        return f"🗑️ Note #{nid} supprimée."

    def _clear(self, uid: str) -> str:
        count = len(self._data.get(uid, []))
        self._data[uid] = []
        self._save()
        return f"🧹 {count} note(s) supprimée(s)."
