"""applications/cog.py – Discord Cog for the application system (EXTENDED)."""

import discord
from discord.ext import commands
from discord import app_commands

from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .views import (
    ApplicationSetupView, ApplicationPanelView, ApplicationChannelView,
    get_server_config_cached,
)
from .app_edit_views import AppEditMainView
from .manager import (
    ApplicationManager, load_application, update_application,
    mark_app_message_deleted, append_app_message_edit,
    append_app_message,
)

logger = get_logger("applications")


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_views_restored = False

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_panel_view()
        # on_ready feuert bei jedem Reconnect erneut – die Kanal-Views
        # (inkl. Abgleich der Control-Nachrichten) nur einmal pro Prozess.
        if self._channel_views_restored:
            return
        self._channel_views_restored = True
        await self._restore_channel_views()

    async def _restore_panel_view(self):
        self.bot.add_view(ApplicationPanelView(bot=self.bot))
        logger.info("✅ ApplicationPanelView wiederhergestellt")

    async def _restore_channel_views(self):
        """Registriert die persistenten Views aller laufenden Bewerbungen.

        Wichtig:
          * Es müssen ALLE nicht abgeschlossenen Bewerbungen geladen werden –
            auch die bereits angenommenen ("accepted"). Nur dort gibt es den
            "🔒 Ticket schließen"-Button; fehlte die View, war er nach einem
            Neustart ohne Funktion.
          * tolerant=True registriert zusätzlich alle Custom-IDs, die auf der
            bereits gesendeten Control-Nachricht stehen können.
          * Ein Fehler bei einer Bewerbung darf die restlichen nicht verhindern.
        """
        from bot.core.supabase_client import get_supabase
        try:
            supabase = get_supabase()
            apps = supabase.table("applications").select("*") \
                .in_("status", ["open", "accepted"]).execute().data or []
            count = 0
            for app in apps:
                try:
                    await self._restore_single_app(app)
                    count += 1
                except Exception as inner_e:
                    logger.error(
                        f"[_restore_channel_views] Bewerbung "
                        f"{app.get('server_id')}/{app.get('app_id')}: {inner_e}"
                    )
            logger.info(f"✅ {count} ApplicationChannelView(s) wiederhergestellt")
        except Exception as e:
            logger.error(f"[_restore_channel_views] {e}")

    async def _restore_single_app(self, app: dict):
        server_id = str(app.get("server_id") or "")
        app_id    = app.get("app_id")
        if not server_id or app_id is None:
            return

        # Config laden und für die Klick-Pfade mit cachen: Supabase-Aufrufe sind
        # synchron – ein DB-Aufruf beim Button-Klick kann das 3-Sekunden-Fenster
        # der Interaktion sprengen (10062 Unknown interaction).
        cfg = await get_server_config_cached(server_id)

        row    = load_application(server_id, app_id) or app
        status = row.get("status") or app.get("status") or "open"

        view = ApplicationChannelView(
            app_id=app_id,
            server_id=server_id,
            applicant_id=str(row.get("creator_id") or ""),
            cfg=cfg,
            bot=self.bot,
            status=status,
            claimed_by=row.get("claimed_by"),
            tolerant=True,
        )
        self.bot.add_view(view)
        # Die Custom-IDs sind jetzt registriert (der Store hält die Items);
        # ab hier wird wieder normal gerendert.
        view._tolerant = False

        await self._sync_control_message(server_id, app_id, row, cfg)

    async def _sync_control_message(self, server_id: str, app_id: int, app: dict, cfg: dict):
        """Bringt die Buttons der Control-Nachricht mit dem DB-Status in Einklang.

        Ältere Bewerbungen haben keine gespeicherte control_message_id. Wurde
        eine solche Bewerbung angenommen, blieben in der Nachricht die alten
        Buttons (Übernehmen/Annehmen/Ablehnen) stehen – ein "Schließen"-Button
        fehlt dort komplett. Das wird hier nachgeholt.
        """
        status  = app.get("status") or "open"
        channel = None
        if app.get("channel_id"):
            try:
                channel = self.bot.get_channel(int(app["channel_id"]))
            except (TypeError, ValueError):
                channel = None
        if channel is None:
            return   # Kanal existiert nicht mehr

        message = await self._find_control_message(channel, app_id, app.get("control_message_id"))
        if message is None:
            return

        if status == "accepted":
            expected = {f"app_close_{app_id}"}
        else:
            expected = {
                f"app_accept_{app_id}",
                f"app_reject_{app_id}",
                f"app_unclaim_{app_id}" if app.get("claimed_by") else f"app_claim_{app_id}",
            }

        if not expected.issubset(self._component_custom_ids(message)):
            view = ApplicationChannelView(
                app_id=app_id,
                server_id=server_id,
                applicant_id=str(app.get("creator_id") or ""),
                cfg=cfg,
                bot=self.bot,
                status=status,
                claimed_by=app.get("claimed_by"),
            )
            await message.edit(view=view)
            logger.info(
                f"[_restore_channel_views] Buttons von Bewerbung #{app_id} "
                f"({server_id}) an Status '{status}' angepasst"
            )

        if str(app.get("control_message_id") or "") != str(message.id):
            update_application(server_id, app_id, {"control_message_id": str(message.id)})

    @staticmethod
    def _component_custom_ids(message: discord.Message) -> set:
        out = set()
        for action_row in getattr(message, "components", []) or []:
            for child in getattr(action_row, "children", []) or []:
                cid = getattr(child, "custom_id", None)
                if cid:
                    out.add(cid)
        return out

    async def _find_control_message(self, channel, app_id: int, message_id):
        """Findet die Control-Nachricht der Bewerbung (Buttons des Bots)."""
        if message_id:
            try:
                return await channel.fetch_message(int(message_id))
            except Exception as e:
                logger.warning(
                    f"[_restore_channel_views] Control-Nachricht {message_id} "
                    f"nicht abrufbar ({e}) – suche im Kanal"
                )

        wanted = {
            f"app_claim_{app_id}", f"app_unclaim_{app_id}",
            f"app_accept_{app_id}", f"app_reject_{app_id}", f"app_close_{app_id}",
        }
        try:
            async for msg in channel.history(limit=100, oldest_first=True):
                if self.bot.user and msg.author.id != self.bot.user.id:
                    continue
                if wanted & self._component_custom_ids(msg):
                    return msg
        except Exception as e:
            logger.warning(f"[_restore_channel_views] Suche in Kanal {getattr(channel, 'id', '?')}: {e}")
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        from bot.core.supabase_client import get_supabase
        try:
            supabase  = get_supabase()
            server_id = str(member.guild.id)
            cfg_r = supabase.table("application_servers").select("newbie_role_id")\
                .eq("server_id", server_id).execute()
            if not cfg_r.data:
                return
            newbie_role_id = cfg_r.data[0].get("newbie_role_id")
            if not newbie_role_id:
                return
            role = member.guild.get_role(int(newbie_role_id))
            if role:
                await member.add_roles(role, reason="Automatisch beim Beitreten vergeben")
        except Exception as e:
            logger.error(f"[on_member_join] {e}")

    # ── Message Logging ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Log messages in application channels."""
        if message.author.bot or not message.guild:
            return
        try:
            parts = message.channel.name.split("-")
            if len(parts) < 3 or not parts[0].isdigit() or parts[-1] != "bewerbung":
                return
            app_id    = int(parts[0])
            server_id = str(message.guild.id)
            append_app_message(
                server_id=server_id, app_id=app_id,
                user=message.author.display_name,
                user_id=str(message.author.id),
                content=message.content or "",
                attachments=[a.url for a in message.attachments],
                discord_message_id=str(message.id),
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Track message edits in application channels."""
        if after.author.bot or not after.guild:
            return
        if before.content == after.content:
            return
        try:
            parts = after.channel.name.split("-")
            if len(parts) < 3 or not parts[0].isdigit() or parts[-1] != "bewerbung":
                return
            app_id    = int(parts[0])
            server_id = str(after.guild.id)
            append_app_message_edit(
                server_id=server_id,
                app_id=app_id,
                discord_message_id=str(after.id),
                old_content=before.content or "",
                new_content=after.content or "",
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Track message deletions in application channels."""
        if message.author.bot or not message.guild:
            return
        try:
            parts = message.channel.name.split("-")
            if len(parts) < 3 or not parts[0].isdigit() or parts[-1] != "bewerbung":
                return
            app_id    = int(parts[0])
            server_id = str(message.guild.id)
            mark_app_message_deleted(
                server_id=server_id,
                app_id=app_id,
                discord_message_id=str(message.id),
            )
        except Exception:
            pass

    # ── Commands ──────────────────────────────────────────────────────────────

    bewerbung = app_commands.Group(
        name="bewerbung",
        description="Bewerbungs System",
    )

    @bewerbung.command(name="setup", description="Richte das Bewerbungs-System ein")
    async def bewerbung_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        from .setup_wizard import start_application_wizard

        # Bestehende Konfiguration laden (falls vorhanden)
        from bot.core.supabase_client import get_supabase
        sb = get_supabase()
        existing = sb.table("application_servers").select("*").eq("server_id", str(interaction.guild_id)).execute()
        existing_config = existing.data[0] if existing.data else None

        await start_application_wizard(
            interaction=interaction,
            bot=self.bot,
            existing_config=existing_config,
        )

    @bewerbung.command(name="bearbeiten", description="Bearbeite das Bewerbungs-System")
    async def bewerbung_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        from bot.core.supabase_client import get_supabase
        import os

        sb = get_supabase()
        if not sb.table("application_servers").select("server_id") \
                .eq("server_id", str(interaction.guild_id)).execute().data:
            await interaction.response.send_message(
                "❌ Das Bewerbungs-System ist noch nicht eingerichtet.\n"
                "Nutze zuerst `/bewerbung_setup`.", ephemeral=True
            )
            return

        web_base = os.getenv("WEB_BASE_URL", "http://localhost:5000")

        view = AppEditMainView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction

        embed = view.build_embed()
        embed.add_field(
            name="🌐 Web-Dashboard",
            value=(
                f"[→ Bewerbungs-Setup im Browser öffnen]"
                f"({web_base}/dashboard/setup/applications?server_id={interaction.guild_id})"
            ),
            inline=False,
        )

        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))