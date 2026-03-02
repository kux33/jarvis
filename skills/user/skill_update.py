"""
skill_update.py — JARVIS Skill : Mise à jour depuis GitHub

Commandes :
  /update          — git pull + pip install + restart
  /updatecheck     — vérifier si une mise à jour est disponible (sans l'appliquer)
  /updatelog       — afficher les derniers commits
  /updatestatus    — version actuelle et état du repo

⚠️  Prérequis :
  1. Le repo GitHub doit être cloné avec git (pas téléchargé en zip)
  2. L'utilisateur système doit pouvoir redémarrer le service sans mot de passe :
     Crée /etc/sudoers.d/jarvis-update avec :
       pi ALL=(ALL) NOPASSWD: /bin/systemctl restart jarvis
       pi ALL=(ALL) NOPASSWD: /bin/systemctl status jarvis
  3. Variable d'env optionnelle :
     JARVIS_SERVICE_NAME=jarvis  (nom du service systemd, défaut: jarvis)
     JARVIS_DIR=/home/pi/jarvis  (chemin du projet, défaut: auto-détecté)
     JARVIS_VENV=/home/pi/jarvis/venv  (chemin du venv, défaut: auto-détecté)
"""

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from skills.base import BaseSkill, SkillContext

logger = logging.getLogger("Jarvis.Skill.Update")


class UpdateSkill(BaseSkill):
    SKILL_NAME    = "update"
    SKILL_DESC    = "Mise à jour JARVIS depuis GitHub"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR  = "JARVIS"
    SKILL_COMMANDS = {
        "update":       "Mettre à jour et redémarrer JARVIS",
        "updatecheck":  "Vérifier si une mise à jour est disponible",
        "updatelog":    "Voir les derniers commits GitHub",
        "updatestatus": "Version actuelle et état du repo",
    }

    def __init__(self, settings=None):
        super().__init__(settings)

        # Chemin du projet : auto-détecté depuis l'emplacement de ce fichier
        self._jarvis_dir = Path(
            os.getenv("JARVIS_DIR", str(Path(__file__).parent.parent.parent.resolve()))
        )
        # Nom du service systemd
        self._service_name = os.getenv("JARVIS_SERVICE_NAME", "jarvis")
        # Chemin du venv Python
        self._venv_dir = Path(
            os.getenv("JARVIS_VENV", str(self._jarvis_dir / "venv"))
        )
        # pip du venv
        self._pip = str(self._venv_dir / "bin" / "pip")
        if not Path(self._pip).exists():
            # Pas de venv → pip système
            self._pip = sys.executable.replace("python", "pip").replace("python3", "pip3")

        # Lock pour éviter deux updates simultanés
        self._update_lock = asyncio.Lock()
        self._last_update: Optional[datetime] = None

    async def setup(self) -> bool:
        # Vérifier que le dossier est bien un repo git
        git_dir = self._jarvis_dir / ".git"
        if not git_dir.exists():
            logger.warning(
                "UpdateSkill: %s n'est pas un repo git. "
                "La commande /update ne fonctionnera pas.",
                self._jarvis_dir
            )
        self._ready = True
        return True

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        cmd = command.lower().strip()
        if cmd == "update":
            return await self._cmd_update(context)
        elif cmd == "updatecheck":
            return await self._cmd_check()
        elif cmd == "updatelog":
            return await self._cmd_log(args)
        elif cmd == "updatestatus":
            return await self._cmd_status()
        return f"❓ Commande inconnue : `/{command}`"

    # ──────────────────────────────────────────────────────────────────
    # Commandes
    # ──────────────────────────────────────────────────────────────────

    async def _cmd_update(self, context: SkillContext) -> str:
        """
        Exécute la mise à jour complète :
          1. git fetch + comparaison commits
          2. git pull
          3. pip install -r requirements.txt (si requirements.txt modifié)
          4. systemctl restart jarvis

        Le restart tue le process → la réponse Telegram est envoyée AVANT le restart.
        """
        if self._update_lock.locked():
            return "⏳ Une mise à jour est déjà en cours, patiente..."

        async with self._update_lock:
            lines = ["🔄 **Mise à jour JARVIS en cours...**\n━━━━━━━━━━━━━━━"]

            # ── 1. Vérifier l'état du repo ─────────────────────────
            if not (self._jarvis_dir / ".git").exists():
                return (
                    "❌ **Impossible de mettre à jour**\n"
                    "Le dossier `%s` n'est pas un repo git.\n"
                    "Clone le projet avec `git clone` pour utiliser cette fonctionnalité."
                    % self._jarvis_dir
                )

            # ── 2. git fetch ───────────────────────────────────────
            fetch_ok, fetch_out = await self._run("git fetch origin", cwd=self._jarvis_dir)
            if not fetch_ok:
                return "❌ **git fetch échoué**\n```\n%s\n```\nVérifie la connexion réseau et les droits GitHub." % fetch_out

            # ── 3. Comparer local vs remote ────────────────────────
            _, local_hash  = await self._run("git rev-parse HEAD", cwd=self._jarvis_dir)
            _, remote_hash = await self._run("git rev-parse @{u}", cwd=self._jarvis_dir)
            local_hash  = local_hash.strip()
            remote_hash = remote_hash.strip()

            if local_hash == remote_hash:
                return (
                    "✅ **JARVIS est déjà à jour !**\n"
                    "Commit : `%s`" % local_hash[:10]
                )

            # Récupérer les commits entrants
            _, new_commits = await self._run(
                "git log HEAD..@{u} --oneline --no-merges",
                cwd=self._jarvis_dir
            )
            commit_lines = [l.strip() for l in new_commits.strip().splitlines() if l.strip()]
            nb_commits = len(commit_lines)
            lines.append("📦 **%d nouveau(x) commit(s) détecté(s)**" % nb_commits)
            for c in commit_lines[:5]:
                lines.append("  • `%s`" % c)
            if nb_commits > 5:
                lines.append("  _...et %d de plus_" % (nb_commits - 5))

            # ── 4. git pull ────────────────────────────────────────
            lines.append("\n⬇️ Téléchargement des modifications...")
            pull_ok, pull_out = await self._run(
                "git pull --rebase origin HEAD",
                cwd=self._jarvis_dir
            )
            if not pull_ok:
                return (
                    "❌ **git pull échoué**\n```\n%s\n```\n"
                    "Possible conflit avec des fichiers locaux.\n"
                    "Connecte-toi en SSH et fais `git status`."
                    % pull_out[:500]
                )
            lines.append("✅ Code mis à jour")

            # ── 5. pip install si requirements modifié ─────────────
            _, diff_req = await self._run(
                "git diff HEAD~1 HEAD --name-only",
                cwd=self._jarvis_dir
            )
            needs_pip = "requirements.txt" in diff_req
            if needs_pip:
                lines.append("\n📚 Mise à jour des dépendances Python...")
                req_file = self._jarvis_dir / "requirements.txt"
                pip_ok, pip_out = await self._run(
                    "%s install -r %s -q" % (self._pip, req_file)
                )
                if pip_ok:
                    lines.append("✅ Dépendances mises à jour")
                else:
                    lines.append("⚠️ pip install échoué (non bloquant) :\n```\n%s\n```" % pip_out[:300])
            else:
                lines.append("📚 requirements.txt inchangé — pip skippé")

            # ── 6. Nouveau commit après pull ───────────────────────
            _, new_hash = await self._run("git rev-parse --short HEAD", cwd=self._jarvis_dir)
            lines.append("\n🏷 Version : `%s`" % new_hash.strip())

            # ── 7. Restart systemd ─────────────────────────────────
            lines.append("\n🔁 Redémarrage de JARVIS...")
            lines.append("_Tu recevras un message de confirmation après le restart._")

            # Construire la réponse AVANT le restart
            # (après le restart le process sera tué)
            response = "\n".join(lines)

            # Planifier le restart dans 2s (temps que le message parte)
            asyncio.create_task(self._delayed_restart())

            self._last_update = datetime.now()
            return response

    async def _cmd_check(self) -> str:
        """Vérifie si une mise à jour est disponible sans l'appliquer."""
        if not (self._jarvis_dir / ".git").exists():
            return "❌ Pas un repo git."

        fetch_ok, fetch_out = await self._run("git fetch origin", cwd=self._jarvis_dir)
        if not fetch_ok:
            return "❌ git fetch échoué : %s" % fetch_out

        _, local_hash  = await self._run("git rev-parse HEAD", cwd=self._jarvis_dir)
        _, remote_hash = await self._run("git rev-parse @{u}", cwd=self._jarvis_dir)
        local_hash  = local_hash.strip()
        remote_hash = remote_hash.strip()

        if local_hash == remote_hash:
            return (
                "✅ **JARVIS est à jour**\n"
                "Commit local : `%s`" % local_hash[:10]
            )

        _, new_commits = await self._run(
            "git log HEAD..@{u} --oneline --no-merges",
            cwd=self._jarvis_dir
        )
        commit_lines = [l.strip() for l in new_commits.strip().splitlines() if l.strip()]

        lines = [
            "🔔 **Mise à jour disponible !** (%d commit(s))" % len(commit_lines),
            "━━━━━━━━━━━━━━━",
            "Local  : `%s`" % local_hash[:10],
            "Remote : `%s`" % remote_hash[:10],
            "",
        ]
        for c in commit_lines[:8]:
            lines.append("• `%s`" % c)
        if len(commit_lines) > 8:
            lines.append("_...et %d de plus_" % (len(commit_lines) - 8))
        lines.append("\n👉 `/update` pour mettre à jour")
        return "\n".join(lines)

    async def _cmd_log(self, args: str) -> str:
        """Affiche les N derniers commits (défaut 8)."""
        try:
            n = int(args.strip()) if args.strip() else 8
            n = max(1, min(n, 20))
        except ValueError:
            n = 8

        ok, out = await self._run(
            'git log --oneline --no-merges -n %d --format="%%h %%s (%%ar)"' % n,
            cwd=self._jarvis_dir
        )
        if not ok or not out.strip():
            return "❌ Impossible de lire le log git."

        lines = ["📋 **%d derniers commits**\n━━━━━━━━━━━━━━━" % n]
        for line in out.strip().splitlines():
            lines.append("• `%s`" % line.strip())
        return "\n".join(lines)

    async def _cmd_status(self) -> str:
        """Affiche la version actuelle, la branche et l'état du service."""
        lines = ["ℹ️ **JARVIS Update Status**\n━━━━━━━━━━━━━━━"]

        # Git info
        _, branch  = await self._run("git rev-parse --abbrev-ref HEAD", cwd=self._jarvis_dir)
        _, commit  = await self._run("git rev-parse --short HEAD", cwd=self._jarvis_dir)
        _, subject = await self._run('git log -1 --format="%s"', cwd=self._jarvis_dir)
        _, commit_date = await self._run('git log -1 --format="%ar"', cwd=self._jarvis_dir)

        lines.append("🌿 Branche : `%s`" % branch.strip())
        lines.append("🏷 Commit  : `%s` — %s" % (commit.strip(), subject.strip()))
        lines.append("🕐 Date    : %s" % commit_date.strip())

        # Repo propre ?
        _, status_out = await self._run("git status --porcelain", cwd=self._jarvis_dir)
        if status_out.strip():
            lines.append("⚠️ Fichiers locaux modifiés (git status non propre)")
        else:
            lines.append("✅ Repo propre")

        # Service systemd
        svc_ok, svc_out = await self._run(
            "systemctl is-active %s" % self._service_name
        )
        svc_state = svc_out.strip()
        svc_icon = "🟢" if svc_state == "active" else "🔴"
        lines.append("\n%s Service `%s` : **%s**" % (svc_icon, self._service_name, svc_state))

        # Dernier update
        if self._last_update:
            lines.append("🔄 Dernier update : %s" % self._last_update.strftime("%d/%m/%Y %H:%M"))

        # Dossier
        lines.append("\n📁 `%s`" % self._jarvis_dir)
        lines.append("🐍 `%s`" % self._pip)

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    async def _run(self, cmd: str, cwd: Optional[Path] = None) -> tuple[bool, str]:
        """
        Exécute une commande shell de manière asynchrone.
        Retourne (succès, stdout+stderr).
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd) if cwd else None,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace").strip()
            success = proc.returncode == 0
            if not success:
                logger.warning("Commande échouée (%d): %s\n%s", proc.returncode, cmd, output[:200])
            return success, output
        except asyncio.TimeoutError:
            logger.error("Timeout (120s) sur commande: %s", cmd)
            return False, "Timeout (120s)"
        except Exception as e:
            logger.error("Erreur commande '%s': %s", cmd, e)
            return False, str(e)

    async def _delayed_restart(self):
        """Attend 2s puis redémarre le service systemd."""
        await asyncio.sleep(2)
        logger.info("Restart systemd : %s", self._service_name)
        ok, out = await self._run("sudo systemctl restart %s" % self._service_name)
        if not ok:
            # Si le restart échoue (ex: sudoers pas configuré), logger l'erreur
            # On ne peut plus envoyer de Telegram ici (pas de contexte)
            logger.error("Restart échoué : %s\nConfigure sudoers : voir INSTALL ci-dessous", out)
