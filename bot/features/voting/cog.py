"""
bot/features/voting/cog.py
===========================
Abstimmungssystem – nur für den Bot-Inhaber (MBL) + Admins dürfen Links versenden.

FEATURES:
  - /abstimmung erstellen   – Startet eine neue Abstimmung aus einer JSON-Datei
  - /abstimmung status      – Zeigt aktive Abstimmungen
  - /abstimmung beenden     – Beendet eine Abstimmung
  - /abstimmung link        – Sendet den Abstimmungs-Link (auch für Admins)
  - /abstimmung beispiel    – Zeigt ein Beispiel-JSON

SUPABASE SQL (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS votings (
        id              TEXT PRIMARY KEY,
        server_id       TEXT NOT NULL,
        title           TEXT NOT NULL,
        description     TEXT,
        categories      JSONB NOT NULL DEFAULT '[]',
        questions       JSONB NOT NULL DEFAULT '[]',
        allowed_users   TEXT,        -- 'ALL' oder komma-separierte User-IDs
        public_key      TEXT,        -- RSA Public Key für Verschlüsselung (optional)
        is_active       BOOLEAN DEFAULT TRUE,
        created_by      TEXT NOT NULL,
        created_at      TIMESTAMPTZ DEFAULT now(),
        ends_at         TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS voting_responses (
        id              BIGSERIAL PRIMARY KEY,
        voting_id       TEXT NOT NULL REFERENCES votings(id),
        voter_hash      TEXT NOT NULL,  -- SHA256(user_id + voting_id + secret)
        answers         TEXT NOT NULL,  -- JSON oder verschlüsseltes JSON
        is_encrypted    BOOLEAN DEFAULT FALSE,
        submitted_at    TIMESTAMPTZ DEFAULT now(),
        UNIQUE (voting_id, voter_hash)
    );

    CREATE INDEX IF NOT EXISTS idx_voting_responses_voting_id
        ON voting_responses (voting_id);
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("voting")

_MBL_ID = os.getenv("MBL", "")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")

# Salt für anonymen Voter-Hash – sollte in .env gesetzt sein
VOTER_SALT = os.getenv("VOTER_SALT", "")
if not VOTER_SALT:
    logger.warning("[voting] VOTER_SALT nicht gesetzt! Verwende stabilen Fallback.")
    VOTER_SALT = "insel-bot-voter-salt-fallback-2024"


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _is_mbl(user: discord.User | discord.Member) -> bool:
    return bool(_MBL_ID and str(user.id) == _MBL_ID)


def _voter_hash(user_id: str, voting_id: str) -> str:
    """Erstellt einen anonymen, nicht rückführbaren Hash für den Voter."""
    raw = f"{user_id}:{voting_id}:{VOTER_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _validate_voting_json(data: dict) -> list[str]:
    """Validiert die Abstimmungs-JSON-Struktur. Gibt Fehler-Liste zurück."""
    errors = []
    if "Kategorie" not in data:
        errors.append("'Kategorie' fehlt")
    if "Beschreibung" not in data:
        errors.append("'Beschreibung' fehlt")
    if "Zur_Auswahl" not in data:
        errors.append("'Zur_Auswahl' fehlt (Liste von User-IDs oder '--All')")
    if "Fragen" not in data or not isinstance(data.get("Fragen"), list):
        errors.append("'Fragen' fehlt oder ist keine Liste")
    else:
        for i, frage in enumerate(data["Fragen"]):
            if not isinstance(frage, dict):
                errors.append(f"Frage {i+1} ist kein Objekt")
                continue
            if "Frage" not in frage:
                errors.append(f"Frage {i+1}: 'Frage' fehlt")
            if "Typ" not in frage:
                errors.append(f"Frage {i+1}: 'Typ' fehlt (text/choice/rating/person)")
            if frage.get("Typ") in ("choice", "person") and "Optionen" not in frage:
                errors.append(f"Frage {i+1}: 'Optionen' fehlt für Typ '{frage.get('Typ')}'")
    return errors


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class VotingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    abstimmung = app_commands.Group(
        name="abstimmung",
        description="Abstimmungssystem (nur Bot-Inhaber & Administratoren dürfen Links senden)",
    )

    # ── /abstimmung erstellen ─────────────────────────────────────────────────

    @abstimmung.command(
        name="erstellen",
        description="[MBL] Erstelle eine neue Abstimmung aus einer JSON-Datei"
    )
    @app_commands.describe(
        json_pfad="Pfad zur Abstimmungs-JSON-Datei (relativ zum Bot-Verzeichnis)",
        server_id="Discord-Server-ID für die Abstimmung",
    )
    async def abstimmung_erstellen(
        self,
        interaction: discord.Interaction,
        json_pfad: str,
        server_id: str,
    ):
        if not _is_mbl(interaction.user):
            await interaction.response.send_message("❌ Nur der Bot-Inhaber.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # JSON laden
        try:
            json_path = Path(json_pfad)
            if not json_path.is_absolute():
                # Relativ zum Bot-Verzeichnis
                bot_root = Path(__file__).resolve().parent.parent.parent.parent
                json_path = bot_root / json_pfad
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            await interaction.followup.send(f"❌ Datei nicht gefunden: `{json_pfad}`", ephemeral=True)
            return
        except json.JSONDecodeError as e:
            await interaction.followup.send(f"❌ JSON-Fehler: {e}", ephemeral=True)
            return

        # Validierung
        errors = _validate_voting_json(data)
        if errors:
            err_text = "\n".join(f"• {e}" for e in errors)
            await interaction.followup.send(
                f"❌ Ungültige JSON-Struktur:\n{err_text}", ephemeral=True
            )
            return

        # Abstimmungs-ID generieren
        voting_id = str(uuid.uuid4())[:12].replace("-", "")

        # Zur_Auswahl verarbeiten
        zur_auswahl = data.get("Zur_Auswahl", "--All")
        if isinstance(zur_auswahl, list):
            allowed_users = ",".join(str(u) for u in zur_auswahl)
        elif str(zur_auswahl).strip().lower() in ("--all", "-all", "all"):
            allowed_users = "ALL"
        else:
            allowed_users = str(zur_auswahl)

        # Public Key (optional)
        public_key = data.get("RSA_Public_Key", None)
        if public_key:
            public_key = public_key.strip()

        # Fragen verarbeiten – Person-Fragen mit --All mit Server-Mitgliedern füllen
        questions = data.get("Fragen", [])
        for frage in questions:
            if frage.get("Typ") == "person":
                optionen = frage.get("Optionen", [])
                if isinstance(optionen, str) and optionen.strip().lower() in ("--all", "-all", "all"):
                    frage["Optionen"] = "--All"  # Wird beim Rendern dynamisch geladen

        # In Supabase speichern
        sb = get_supabase()
        voting_row = {
            "id":           voting_id,
            "server_id":    server_id,
            "title":        data["Kategorie"],
            "description":  data.get("Beschreibung", ""),
            "questions":    questions,
            "allowed_users": allowed_users,
            "public_key":   public_key,
            "is_active":    True,
            "created_by":   str(interaction.user.id),
            "created_at":   datetime.now(timezone.utc).isoformat(),
        }
        try:
            sb.table("votings").insert(voting_row).execute()
        except Exception as e:
            await interaction.followup.send(f"❌ DB-Fehler: {e}", ephemeral=True)
            return

        # Link generieren
        voting_url = f"{WEB_BASE_URL}/vote/{voting_id}"

        embed = discord.Embed(
            title="✅ Abstimmung erstellt!",
            color=discord.Color.green(),
        )
        embed.add_field(name="📋 Titel",      value=data["Kategorie"],    inline=True)
        embed.add_field(name="🆔 ID",         value=f"`{voting_id}`",     inline=True)
        embed.add_field(name="👥 Zugriff",    value=f"{'Alle' if allowed_users == 'ALL' else f'{len(allowed_users.split(chr(44)))} User'}", inline=True)
        embed.add_field(name="❓ Fragen",     value=str(len(questions)),  inline=True)
        embed.add_field(name="🔐 Verschlüsselt", value="✅ Ja" if public_key else "❌ Nein", inline=True)
        embed.add_field(
            name="🔗 Abstimmungs-Link",
            value=f"[→ Abstimmung öffnen]({voting_url})\n`{voting_url}`",
            inline=False,
        )
        embed.set_footer(text="Der Link kann jetzt geteilt werden.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"[voting] Abstimmung erstellt: {voting_id} von {interaction.user}")

    # ── /abstimmung status ────────────────────────────────────────────────────

    @abstimmung.command(
        name="status",
        description="[MBL] Zeige aktive Abstimmungen"
    )
    async def abstimmung_status(self, interaction: discord.Interaction):
        if not _is_mbl(interaction.user):
            await interaction.response.send_message("❌ Nur der Bot-Inhaber.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        sb = get_supabase()
        rows = sb.table("votings").select("*").eq("is_active", True).execute().data or []

        if not rows:
            await interaction.followup.send("ℹ️ Keine aktiven Abstimmungen.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📊 Aktive Abstimmungen ({len(rows)})",
            color=discord.Color.blurple(),
        )
        for v in rows[:10]:
            # Antworten zählen
            try:
                resp_count = len(
                    sb.table("voting_responses").select("id").eq("voting_id", v["id"]).execute().data or []
                )
            except Exception:
                resp_count = "?"

            voting_url = f"{WEB_BASE_URL}/vote/{v['id']}"
            embed.add_field(
                name=f"📋 {v['title']}",
                value=(
                    f"ID: `{v['id']}`\n"
                    f"Server: `{v['server_id']}`\n"
                    f"Antworten: **{resp_count}**\n"
                    f"[Link]({voting_url})"
                ),
                inline=True,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /abstimmung beenden ───────────────────────────────────────────────────

    @abstimmung.command(
        name="beenden",
        description="[MBL] Beende eine aktive Abstimmung"
    )
    @app_commands.describe(voting_id="Die ID der Abstimmung")
    async def abstimmung_beenden(self, interaction: discord.Interaction, voting_id: str):
        if not _is_mbl(interaction.user):
            await interaction.response.send_message("❌ Nur der Bot-Inhaber.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            await interaction.followup.send(f"❌ Abstimmung `{voting_id}` nicht gefunden.", ephemeral=True)
            return

        sb.table("votings").update({"is_active": False}).eq("id", voting_id).execute()

        # Antworten zählen
        resp = sb.table("voting_responses").select("id").eq("voting_id", voting_id).execute()
        count = len(resp.data or [])

        await interaction.followup.send(
            embed=discord.Embed(
                title="🔒 Abstimmung beendet",
                description=(
                    f"Abstimmung **{r.data[0]['title']}** (`{voting_id}`) wurde beendet.\n"
                    f"**{count}** Antworten wurden abgegeben.\n\n"
                    f"Die Ergebnisse sind unter:\n"
                    f"`{WEB_BASE_URL}/vote/{voting_id}/results`"
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    # ── /abstimmung link ──────────────────────────────────────────────────────
    # 🔓 ERWEITERT: Auch Server-Administratoren dürfen den Link senden

    @abstimmung.command(
        name="link",
        description="[MBL/Admin] Sende den Abstimmungs-Link in diesen Kanal"
    )
    @app_commands.describe(
        voting_id="Die ID der Abstimmung",
        ephemeral="Nur du siehst den Link (Standard: Nein)",
    )
    async def abstimmung_link(
        self,
        interaction: discord.Interaction,
        voting_id: str,
        ephemeral: bool = False,
    ):
        # 1) Bot-Inhaber darf immer
        if _is_mbl(interaction.user):
            pass  # Erlaubt
        else:
            # 2) Prüfe, ob Benutzer Administrator auf dem Server der Abstimmung ist
            sb = get_supabase()
            voting_data = sb.table("votings").select("server_id, title, is_active, description").eq("id", voting_id).execute()
            if not voting_data.data:
                await interaction.response.send_message(f"❌ Abstimmung `{voting_id}` nicht gefunden.", ephemeral=True)
                return
            server_id_str = voting_data.data[0]["server_id"]
            try:
                guild_id = int(server_id_str)
            except ValueError:
                await interaction.response.send_message("❌ Ungültige Server-ID in der Abstimmung.", ephemeral=True)
                return

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await interaction.response.send_message(
                    "❌ Bot ist nicht auf dem Server, für den diese Abstimmung erstellt wurde. Link kann nicht autorisiert werden.",
                    ephemeral=True
                )
                return

            # Mitgliedsobjekt holen (Cache + Fallback fetch)
            member = guild.get_member(interaction.user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except discord.NotFound:
                    member = None

            if not member or not member.guild_permissions.administrator:
                await interaction.response.send_message(
                    "❌ Nur der Bot-Inhaber oder Server-Administratoren dürfen den Abstimmungs-Link versenden.",
                    ephemeral=True
                )
                return

        # Berechtigung bestanden – jetzt den Link senden
        sb = get_supabase()
        r  = sb.table("votings").select("title, is_active, description").eq("id", voting_id).execute()
        if not r.data:
            await interaction.response.send_message(f"❌ Abstimmung `{voting_id}` nicht gefunden.", ephemeral=True)
            return

        v = r.data[0]
        voting_url = f"{WEB_BASE_URL}/vote/{voting_id}"

        embed = discord.Embed(
            title=f"📊 {v['title']}",
            description=(
                f"{v.get('description', '')}\n\n"
                f"**[→ Zur Abstimmung]({voting_url})**\n\n"
                f"{'✅ Abstimmung ist aktiv' if v['is_active'] else '🔒 Abstimmung geschlossen'}"
            ),
            color=discord.Color.blurple() if v["is_active"] else discord.Color.grayed(),
            url=voting_url,
        )
        embed.set_footer(text=f"ID: {voting_id}")
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    # ── /abstimmung json_beispiel ─────────────────────────────────────────────

    @abstimmung.command(
        name="beispiel",
        description="[MBL] Zeige ein Beispiel-JSON für eine Abstimmung"
    )
    async def abstimmung_beispiel(self, interaction: discord.Interaction):
        if not _is_mbl(interaction.user):
            await interaction.response.send_message("❌ Nur der Bot-Inhaber.", ephemeral=True)
            return

        beispiel = {
            "Kategorie": "Server-Abstimmung Q1 2026",
            "Beschreibung": "Quartalsbefragung aller Servermitglieder",
            "RSA_Public_Key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----",
            "Zur_Auswahl": "--All",
            "Fragen": [
                {
                    "Frage": "Wie zufrieden bist du mit dem Server?",
                    "Typ": "rating",
                    "Min": 1,
                    "Max": 5,
                    "Pflicht": True
                },
                {
                    "Frage": "Was soll als nächstes verbessert werden?",
                    "Typ": "choice",
                    "Mehrfach": False,
                    "Optionen": ["Events", "Regeln", "Bot-Features", "Design", "Anderes"],
                    "Pflicht": True
                },
                {
                    "Frage": "Wer soll als neues Teammitglied aufgenommen werden?",
                    "Typ": "person",
                    "Optionen": "--All",
                    "Mehrfach": False,
                    "Pflicht": False
                },
                {
                    "Frage": "Hast du Anmerkungen?",
                    "Typ": "text",
                    "Pflicht": False
                }
            ]
        }

        json_str = json.dumps(beispiel, indent=2, ensure_ascii=False)

        # Als Datei senden wenn zu lang
        if len(json_str) > 1900:
            import io
            file = discord.File(
                fp=io.BytesIO(json_str.encode("utf-8")),
                filename="abstimmung_beispiel.json"
            )
            await interaction.response.send_message(
                "📄 Beispiel-JSON (als Datei):", file=file, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"📄 Beispiel-JSON:\n```json\n{json_str}\n```",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(VotingCog(bot))