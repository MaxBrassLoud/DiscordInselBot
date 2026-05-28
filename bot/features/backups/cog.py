from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.database_backup import (
    create_database_backup,
    get_backup_interval_hours,
    get_backup_keep_count,
    list_database_backups,
    prune_database_backups,
    restore_database_backup,
)
from bot.utils.logger import get_logger

logger = get_logger("backup_cog")


def _is_mbl(interaction: discord.Interaction) -> bool:
    return str(interaction.user.id) == str(os.getenv("MBL", "")).strip()


async def backup_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if not _is_mbl(interaction):
        return []

    current_lower = current.lower()
    backups = list_database_backups()
    choices = []
    for path in backups:
        if current_lower and current_lower not in path.name.lower():
            continue
        choices.append(app_commands.Choice(name=path.name[:100], value=path.name))
        if len(choices) >= 25:
            break
    return choices


class BackupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_result = None
        self._last_error: str | None = None
        self._backup_lock = asyncio.Lock()
        self.automatic_backup.change_interval(hours=get_backup_interval_hours())
        self.automatic_backup.start()

    def cog_unload(self):
        self.automatic_backup.cancel()

    backup = app_commands.Group(
        name="backup",
        description="Datenbank-Backups verwalten",
    )

    async def _run_backup(self):
        async with self._backup_lock:
            result = await asyncio.to_thread(create_database_backup)
            await asyncio.to_thread(prune_database_backups)
            self._last_result = result
            self._last_error = None
            return result

    async def _run_restore(self, backup_name: str | None):
        async with self._backup_lock:
            result = await asyncio.to_thread(restore_database_backup, backup_name)
            self._last_error = None
            return result

    @tasks.loop(hours=24)
    async def automatic_backup(self):
        try:
            await self._run_backup()
        except Exception as exc:
            self._last_error = str(exc)
            logger.error(f"[backup] Automatisches Backup fehlgeschlagen: {exc}")

    @automatic_backup.before_loop
    async def before_automatic_backup(self):
        await self.bot.wait_until_ready()

    @backup.command(name="create")
    @app_commands.describe(
        send_file="Wenn aktiviert, wird die Backup-ZIP direkt in Discord hochgeladen."
    )
    async def backup_create(self, interaction: discord.Interaction, send_file: bool = False):
        if not _is_mbl(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await self._run_backup()
        except Exception as exc:
            self._last_error = str(exc)
            await interaction.followup.send(
                f"Backup fehlgeschlagen: `{exc}`",
                ephemeral=True,
            )
            return

        description = (
            f"Backup erstellt: `{result.path}`\n"
            f"Tabellen: `{len(result.table_counts)}`\n"
            f"Zeilen: `{result.total_rows}`"
        )
        embed = discord.Embed(
            title="Datenbank-Backup erstellt",
            description=description,
            color=discord.Color.green(),
        )

        if send_file:
            await interaction.followup.send(
                embed=embed,
                file=discord.File(result.path),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @backup.command(name="status")
    async def backup_status(self, interaction: discord.Interaction):
        if not _is_mbl(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return

        backups = list_database_backups()
        newest = backups[0] if backups else None
        interval = get_backup_interval_hours()
        keep_count = get_backup_keep_count()

        lines = [
            f"Intervall: `{interval:g}` Stunden",
            f"Aufbewahrung: `{keep_count}` Backups",
            f"Vorhandene Backups: `{len(backups)}`",
        ]
        if newest:
            created = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
            lines.append(f"Neuestes Backup: `{newest}`")
            lines.append(f"Geaendert: `{created.isoformat()}`")
        if self._last_error:
            lines.append(f"Letzter Fehler: `{self._last_error}`")

        embed = discord.Embed(
            title="Backup-Status",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @backup.command(name="load")
    @app_commands.describe(
        backup_name="Name der Backup-ZIP aus dem Backup-Ordner. Leer lassen fuer das neueste Backup.",
        confirm="Zum Laden exakt LADEN eingeben.",
    )
    @app_commands.autocomplete(backup_name=backup_name_autocomplete)
    async def backup_load(
        self,
        interaction: discord.Interaction,
        confirm: str,
        backup_name: str = "",
    ):
        if not _is_mbl(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        if confirm != "LADEN":
            await interaction.response.send_message(
                "Bitte bestaetige den Restore mit `LADEN`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await self._run_restore(backup_name or None)
        except Exception as exc:
            self._last_error = str(exc)
            await interaction.followup.send(
                f"Backup konnte nicht geladen werden: `{exc}`",
                ephemeral=True,
            )
            return

        self._last_error = None
        embed = discord.Embed(
            title="Datenbank-Backup geladen",
            description=(
                f"Backup: `{result.path}`\n"
                f"Tabellen: `{len(result.table_counts)}`\n"
                f"Zeilen: `{result.total_rows}`\n"
                "Modus: `upsert`"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))
