import discord
from bot.utils.logger import get_logger
from .manager import TicketManager
from .storage import update_ticket, load_ticket

logger = get_logger("tickets.views")


# ══════════════════════════════════════════════════════════════════════════════
# TICKET CREATION MODAL
# ══════════════════════════════════════════════════════════════════════════════

class TicketDescriptionModal(discord.ui.Modal, title="Ticket erstellen"):
    beschreibung = discord.ui.TextInput(
        label="Beschreibung",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, module: dict, category_id: int, bot: discord.Client):
        super().__init__(title=f"Ticket: {module['name']}")
        self.module      = module
        self.category_id = category_id
        self.bot         = bot
        self.beschreibung.placeholder = module.get("modal_question", "Bitte beschreibe dein Anliegen.")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            max_tickets = self.module.get("max_tickets", 1)
            open_count  = await TicketManager.get_open_tickets_for_user(
                str(interaction.guild_id), str(interaction.user.id), self.module["name"]
            )
            if open_count >= max_tickets:
                await interaction.followup.send(
                    f"❌ Du hast bereits **{open_count}/{max_tickets}** offene Tickets für dieses Modul.",
                    ephemeral=True,
                )
                return

            channel, ticket_id = await TicketManager.create_ticket(
                guild=interaction.guild,
                creator=interaction.user,
                module=self.module,
                description=self.beschreibung.value,
                category_id=self.category_id,
            )

            embed = discord.Embed(title=f"🎫 Ticket #{ticket_id}", color=discord.Color.blurple())
            embed.add_field(name="👤 Erstellt von", value=interaction.user.mention, inline=True)
            embed.add_field(name="📂 Modul",        value=self.module["name"],      inline=True)
            embed.add_field(name="📝 Beschreibung", value=self.beschreibung.value,  inline=False)

            view = TicketChannelView(
                ticket_id=ticket_id,
                server_id=str(interaction.guild_id),
                creator_id=str(interaction.user.id),
                module=self.module,
                bot=self.bot,
            )
            await channel.send(embed=embed, view=view)
            await interaction.followup.send(f"✅ Ticket erstellt: {channel.mention}", ephemeral=True)
        except Exception as e:
            logger.error(f"[TicketDescriptionModal] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# PROXY TICKET MODAL  –  Staff erstellt Ticket im Namen eines anderen Mitglieds
# ══════════════════════════════════════════════════════════════════════════════

class ProxyTicketModal(discord.ui.Modal):
    """
    Öffnet sich wenn ein Staff-Mitglied `/ticket_fuer @Mitglied Modul` ausführt.

    Felder:
      • grund       – Warum wird dieses Ticket im Namen des Mitglieds erstellt?
                      (interner Staff-Hinweis, wird im Ticket-Header angezeigt)
      • beschreibung – Eigentlicher Ticket-Inhalt (so als würde das Mitglied selbst schreiben)
    """

    grund = discord.ui.TextInput(
        label="Grund (warum erstellst du das Ticket?)",
        placeholder="z.B. Mitglied hat mich per DM kontaktiert und kann nicht selbst…",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    beschreibung = discord.ui.TextInput(
        label="Ticket-Beschreibung (Anliegen des Mitglieds)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(
        self,
        module: dict,
        category_id: int,
        bot: discord.Client,
        behalf_of: discord.Member,
    ):
        super().__init__(title=f"Ticket für {behalf_of.display_name[:20]}: {module['name']}")
        self.module      = module
        self.category_id = category_id
        self.bot         = bot
        self.behalf_of   = behalf_of
        # Placeholder aus dem Modul übernehmen
        self.beschreibung.placeholder = module.get(
            "modal_question", "Beschreibe das Anliegen des Mitglieds."
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            guild      = interaction.guild
            staff      = interaction.user       # wer den Command ausgeführt hat
            behalf_of  = self.behalf_of         # für wen das Ticket erstellt wird

            # Vollständige Beschreibung: Anliegen + Staff-Hinweis
            full_description = (
                f"{self.beschreibung.value}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 *Dieses Ticket wurde von {staff.display_name} "
                f"im Namen von {behalf_of.display_name} erstellt.*\n"
                f"**Grund:** {self.grund.value}"
            )

            # Ticket anlegen – als Ersteller gilt das Ziel-Mitglied (behalf_of),
            # damit es Zugang zum Kanal erhält und im Dashboard als Ersteller erscheint.
            channel, ticket_id = await TicketManager.create_ticket(
                guild=guild,
                creator=behalf_of,          # Ticket gehört dem Mitglied
                module=self.module,
                description=full_description,
                category_id=self.category_id,
            )

            # Ticket-Embed im Kanal
            embed = discord.Embed(
                title=f"🎫 Ticket #{ticket_id}",
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="👤 Mitglied",
                value=behalf_of.mention,
                inline=True,
            )
            embed.add_field(
                name="📂 Modul",
                value=self.module["name"],
                inline=True,
            )
            embed.add_field(
                name="👮 Erstellt von Staff",
                value=staff.mention,
                inline=True,
            )
            embed.add_field(
                name="📋 Grund",
                value=self.grund.value,
                inline=False,
            )
            embed.add_field(
                name="📝 Anliegen",
                value=self.beschreibung.value,
                inline=False,
            )
            embed.set_footer(
                text=f"Erstellt von {staff.display_name} im Namen von {behalf_of.display_name}"
            )

            # Staff-Mitglied ebenfalls zum Kanal hinzufügen
            # (er erstellt das Ticket, soll aber auch direkt drin sein)
            try:
                await channel.set_permissions(
                    staff,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )
            except Exception as e:
                logger.warning(f"[ProxyTicketModal] Staff-Permission: {e}")

            view = TicketChannelView(
                ticket_id=ticket_id,
                server_id=str(guild.id),
                creator_id=str(behalf_of.id),
                module=self.module,
                bot=self.bot,
            )
            await channel.send(embed=embed, view=view)

            # Mitglied per DM benachrichtigen (best-effort)
            try:
                dm_embed = discord.Embed(
                    title=f"🎫 Ein Ticket wurde für dich erstellt – #{ticket_id}",
                    description=(
                        f"**{staff.display_name}** hat im Modul **{self.module['name']}** "
                        f"ein Ticket in deinem Namen erstellt.\n\n"
                        f"Du kannst es hier einsehen: {channel.mention}"
                    ),
                    color=discord.Color.blurple(),
                )
                dm_embed.set_footer(text=f"Server: {guild.name}")
                await behalf_of.send(embed=dm_embed)
            except discord.Forbidden:
                pass  # DMs gesperrt – kein Problem

            await interaction.followup.send(
                f"✅ Ticket #{ticket_id} für {behalf_of.mention} erstellt: {channel.mention}",
                ephemeral=True,
            )

        except Exception as e:
            logger.error(f"[ProxyTicketModal] {e}")
            await interaction.followup.send(f"❌ Fehler beim Erstellen des Tickets: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# TICKET PANEL  –  Ein Button pro Modul
# ══════════════════════════════════════════════════════════════════════════════

class TicketPanelView(discord.ui.View):
    """Ein Button pro Modul – persistent, custom_id = ticket_open_<module_id>."""

    def __init__(self, modules: list[dict], category_id: int, bot: discord.Client):
        super().__init__(timeout=None)
        self.modules     = modules
        self.category_id = category_id
        self.bot         = bot
        self._build_buttons()

    def _build_buttons(self):
        for mod in self.modules[:25]:
            raw_emoji = mod.get("button_emoji") or "🎫"
            if raw_emoji.startswith("<") and raw_emoji.endswith(">"):
                try:
                    animated = raw_emoji.startswith("<a:")
                    inner    = raw_emoji[3:].rstrip(">") if animated else raw_emoji[2:].rstrip(">")
                    parts    = inner.rsplit(":", 1)
                    parsed_emoji = discord.PartialEmoji(name=parts[0], id=int(parts[1]), animated=animated)
                except Exception:
                    parsed_emoji = "🎫"
            else:
                parsed_emoji = raw_emoji

            btn = discord.ui.Button(
                label=mod["name"][:80],
                emoji=parsed_emoji,
                style=discord.ButtonStyle.primary,
                custom_id=f"ticket_open_{mod['id']}",
            )
            async def callback(interaction: discord.Interaction, mid=mod["id"]):
                await self._handle_open(interaction, mid)
            btn.callback = callback
            self.add_item(btn)

    async def _handle_open(self, interaction: discord.Interaction, module_id: int):
        module = await TicketManager.get_module(module_id)
        if not module:
            await interaction.response.send_message("❌ Modul nicht gefunden.", ephemeral=True)
            return

        server_cfg  = await TicketManager.get_server_config(str(interaction.guild_id))
        global_cat  = int(server_cfg["category_id"]) if server_cfg else self.category_id
        modal = TicketDescriptionModal(
            module=module,
            category_id=global_cat,
            bot=self.bot,
        )
        await interaction.response.send_modal(modal)


# ══════════════════════════════════════════════════════════════════════════════
# TICKET CHANNEL BUTTONS
# ══════════════════════════════════════════════════════════════════════════════

class TicketChannelView(discord.ui.View):
    def __init__(self, ticket_id: int, server_id: str, creator_id: str,
                 module: dict, bot: discord.Client):
        super().__init__(timeout=None)
        self.ticket_id       = ticket_id
        self.server_id       = server_id
        self.creator_id      = creator_id
        self.module          = module
        self.bot             = bot
        self._claimed_by_id: str | None = None
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        tid = self.ticket_id

        if self._claimed_by_id:
            b = discord.ui.Button(label="🔄 Ticket abgeben",
                                  style=discord.ButtonStyle.secondary,
                                  custom_id=f"ticket_unclaim_{tid}")
            b.callback = self._unclaim_callback
        else:
            b = discord.ui.Button(label="📥 Ticket übernehmen",
                                  style=discord.ButtonStyle.primary,
                                  custom_id=f"ticket_claim_{tid}")
            b.callback = self._claim_callback
        self.add_item(b)

        close_btn = discord.ui.Button(label="🔒 Ticket schließen",
                                      style=discord.ButtonStyle.danger,
                                      custom_id=f"ticket_close_{tid}")
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

        add_btn = discord.ui.Button(label="➕ Benutzer hinzufügen",
                                    style=discord.ButtonStyle.secondary,
                                    custom_id=f"ticket_adduser_{tid}")
        add_btn.callback = self._adduser_callback
        self.add_item(add_btn)

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        return any(str(r.id) in self.module.get("staff_role_ids", []) for r in member.roles)

    def _get_ticket(self) -> dict:
        t = load_ticket(self.server_id, self.ticket_id)
        if t:
            return t
        return {
            "ticket_id":    self.ticket_id,
            "server_id":    self.server_id,
            "module":       self.module.get("name", "?"),
            "creator_id":   self.creator_id,
            "creator_name": "Unbekannt",
            "description":  "",
            "status":       "open",
            "claimed_by":   self._claimed_by_id,
        }

    async def _claim_callback(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff kann Tickets übernehmen.", ephemeral=True)
            return
        if self._claimed_by_id:
            await interaction.response.send_message(
                f"❌ Wird bereits von <@{self._claimed_by_id}> bearbeitet.", ephemeral=True
            )
            return
        self._claimed_by_id = str(interaction.user.id)
        update_ticket(self.server_id, self.ticket_id, {"claimed_by": str(interaction.user.id)})
        self._build_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"✅ {interaction.user.mention} hat das Ticket übernommen.")

    async def _unclaim_callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self._claimed_by_id and not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur der Bearbeiter kann das Ticket abgeben.", ephemeral=True)
            return
        self._claimed_by_id = None
        update_ticket(self.server_id, self.ticket_id, {"claimed_by": None})
        self._build_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.channel.send("🔄 Ticket wurde freigegeben.")

    async def _close_callback(self, interaction: discord.Interaction):
        is_staff   = self._is_staff(interaction.user)
        is_creator = str(interaction.user.id) == str(self.creator_id)

        if not is_staff and not is_creator:
            await interaction.response.send_message(
                "❌ Nur der Ersteller oder Staff kann Tickets schließen.", ephemeral=True
            )
            return

        ticket = self._get_ticket()

        if is_staff:
            view = TicketCloseConfirmView(
                ticket=ticket, channel=interaction.channel,
                closer=interaction.user, module=self.module, bot=self.bot,
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Ticket schließen?",
                    description="Bist du sicher? Das Ticket wird exportiert und der Kanal gelöscht.",
                    color=discord.Color.red(),
                ),
                view=view, ephemeral=True,
            )
        else:
            view = TicketCloseRequestView(
                ticket=ticket, channel=interaction.channel,
                requester=interaction.user, module=self.module, bot=self.bot,
            )
            await interaction.channel.send(
                embed=discord.Embed(
                    title="🙋 Schließanfrage",
                    description=f"{interaction.user.mention} möchte das Ticket schließen.",
                    color=discord.Color.orange(),
                ),
                view=view,
            )
            await interaction.response.send_message("✅ Schließanfrage gesendet.", ephemeral=True)

    async def _adduser_callback(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        view = AddUserView(ticket_id=self.ticket_id, server_id=self.server_id, channel=interaction.channel)
        await interaction.response.send_message("Wähle einen oder mehrere Benutzer:", view=view, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# CLOSE CONFIRM
# ══════════════════════════════════════════════════════════════════════════════

class TicketCloseConfirmView(discord.ui.View):
    def __init__(self, ticket: dict, channel: discord.TextChannel,
                 closer: discord.Member, module: dict, bot: discord.Client):
        super().__init__(timeout=60)
        self.ticket  = ticket
        self.channel = channel
        self.closer  = closer
        self.module  = module
        self.bot     = bot

    @discord.ui.button(label="✅ Bestätigen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Ticket wird geschlossen…", ephemeral=True)
        self.stop()
        try:
            await TicketManager.close_ticket(
                interaction.guild, self.channel, self.ticket, self.closer
            )
        except Exception as e:
            logger.error(f"[TicketCloseConfirmView] {e}")

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Abgebrochen.", ephemeral=True)
        self.stop()


class TicketCloseRequestView(discord.ui.View):
    def __init__(self, ticket: dict, channel: discord.TextChannel,
                 requester: discord.Member, module: dict, bot: discord.Client):
        super().__init__(timeout=None)
        self.ticket    = ticket
        self.channel   = channel
        self.requester = requester
        self.module    = module
        self.bot       = bot

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        return any(str(r.id) in self.module.get("staff_role_ids", []) for r in member.roles)

    @discord.ui.button(label="✅ Genehmigen", style=discord.ButtonStyle.success,
                       custom_id="ticket_close_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Ticket wird geschlossen…", ephemeral=True)
        self.stop()
        try:
            await TicketManager.close_ticket(
                interaction.guild, self.channel, self.ticket, interaction.user
            )
        except Exception as e:
            logger.error(f"[TicketCloseRequestView.approve] {e}")

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger,
                       custom_id="ticket_close_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.response.send_message("🚫 Schließanfrage abgelehnt.", ephemeral=True)
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
# ADD USER
# ══════════════════════════════════════════════════════════════════════════════

class AddUserView(discord.ui.View):
    def __init__(self, ticket_id: int, server_id: str, channel: discord.TextChannel):
        super().__init__(timeout=60)
        self.ticket_id = ticket_id
        self.server_id = server_id
        self.channel   = channel

        sel = discord.ui.UserSelect(placeholder="Benutzer auswählen...", min_values=1, max_values=5)
        sel.callback = self.user_selected
        self.add_item(sel)

    async def user_selected(self, interaction: discord.Interaction):
        added_ids = interaction.data["values"]

        for user_id in added_ids:
            member = interaction.guild.get_member(int(user_id))
            if member:
                await self.channel.set_permissions(
                    member, view_channel=True, send_messages=True, read_message_history=True,
                )

        try:
            from bot.features.tickets.storage import load_ticket, update_ticket
            from bot.core.supabase_client import get_supabase

            ticket = load_ticket(self.server_id, self.ticket_id) or {}
            existing = ticket.get("added_users") or []
            merged = list(set(existing + added_ids))
            update_ticket(self.server_id, self.ticket_id, {"added_users": merged})

            supabase = get_supabase()
            supabase.table("tickets").update({"added_users": merged})\
                .eq("ticket_id", self.ticket_id)\
                .eq("server_id", self.server_id)\
                .execute()
        except Exception as e:
            import logging
            logging.getLogger("tickets.views").error(f"[AddUserView] persist added_users: {e}")

        added = ", ".join(f"<@{uid}>" for uid in added_ids)
        await interaction.response.send_message(f"✅ Hinzugefügt: {added}", ephemeral=True)
        await self.channel.send(f"👤 {added} wurde zum Ticket hinzugefügt.")
        self.stop()