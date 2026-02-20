"""
Skill JARVIS : Météo
Source: wttr.in (pas de clé API requise)
Commandes: /meteo <ville>, /previsions <ville>
"""

import aiohttp
import json
from skills.base import BaseSkill, SkillContext


class MeteoSkill(BaseSkill):
    SKILL_NAME = "meteo"
    SKILL_DESC = "Météo en temps réel (sans clé API)"
    SKILL_VERSION = "1.0.0"
    SKILL_AUTHOR = "JARVIS"
    SKILL_COMMANDS = {
        "meteo":      "Météo actuelle d'une ville (`/meteo Paris`)",
        "previsions": "Prévisions 3 jours (`/previsions Lyon`)",
    }

    ICONS = {
        "sunny": "☀️", "clear": "☀️", "cloud": "☁️", "overcast": "🌥️",
        "mist": "🌫️", "fog": "🌫️", "rain": "🌧️", "drizzle": "🌦️",
        "snow": "❄️", "sleet": "🌨️", "thunder": "⛈️", "blizzard": "🌨️",
    }

    async def handle(self, command: str, args: str, context: SkillContext) -> str:
        city = args.strip() if args.strip() else "Paris"

        if command == "meteo":
            return await self._current(city)
        elif command == "previsions":
            return await self._forecast(city)
        return "Usage: `/meteo <ville>` ou `/previsions <ville>`"

    async def _fetch(self, city: str) -> dict:
        url = f"https://wttr.in/{city}?format=j1&lang=fr"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json(content_type=None)

    def _icon(self, desc: str) -> str:
        desc = desc.lower()
        for key, icon in self.ICONS.items():
            if key in desc:
                return icon
        return "🌡️"

    async def _current(self, city: str) -> str:
        try:
            data = await self._fetch(city)
            cur = data["current_condition"][0]
            area = data["nearest_area"][0]
            city_name = area["areaName"][0]["value"]
            country = area["country"][0]["value"]
            desc = cur["lang_fr"][0]["value"] if cur.get("lang_fr") else cur["weatherDesc"][0]["value"]
            icon = self._icon(cur["weatherDesc"][0]["value"])

            temp_c = cur["temp_C"]
            feels = cur["FeelsLikeC"]
            humidity = cur["humidity"]
            wind = cur["windspeedKmph"]
            wind_dir = cur["winddir16Point"]
            uv = cur["uvIndex"]
            visibility = cur["visibility"]

            return (
                f"{icon} **Météo — {city_name}, {country}**\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🌡️ Température: **{temp_c}°C** (ressenti {feels}°C)\n"
                f"🌤️ Conditions: {desc}\n"
                f"💧 Humidité: {humidity}%\n"
                f"💨 Vent: {wind} km/h ({wind_dir})\n"
                f"👁️ Visibilité: {visibility} km\n"
                f"☀️ UV: {uv}"
            )
        except Exception as e:
            return f"❌ Impossible de récupérer la météo pour `{city}`: {e}"

    async def _forecast(self, city: str) -> str:
        try:
            data = await self._fetch(city)
            area = data["nearest_area"][0]
            city_name = area["areaName"][0]["value"]
            days_data = data["weather"]

            lines = [f"📅 **Prévisions 3 jours — {city_name}**\n━━━━━━━━━━━━━━━"]
            for day in days_data[:3]:
                date = day["date"]
                max_t = day["maxtempC"]
                min_t = day["mintempC"]
                desc = day["hourly"][4]["lang_fr"][0]["value"] if day["hourly"][4].get("lang_fr") else day["hourly"][4]["weatherDesc"][0]["value"]
                icon = self._icon(day["hourly"][4]["weatherDesc"][0]["value"])
                rain = day["hourly"][4].get("chanceofrain", "?")
                lines.append(f"\n{icon} **{date}**\n   🌡️ {min_t}°C → {max_t}°C | {desc}\n   🌧️ Pluie: {rain}%")

            return "\n".join(lines)
        except Exception as e:
            return f"❌ Prévisions indisponibles pour `{city}`: {e}"
