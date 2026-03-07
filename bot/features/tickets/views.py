import discord
from bot.utils.logger import get_logger
from .manager import TicketManager
from .storage import update_ticket, load_ticket, append_message

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
            # ── Ticket-Limit prüfen ───────────────────────────────────────────
            max_tickets = self.module.get("max_tickets", 1)
            open_count  = await TicketManager.get_open_tickets_for_user(
                str(interaction.guild_id), str(interaction.user.id), self.module["name"]
            )
            if open_count >= max_tickets:
                await interaction.followup.send(
                    f"❌ Du hast bereits **{open_count}/{max_tickets}** offene Tickets für dieses Modul.",
                    ephemeral=True
                )
                return

            channel, ticket_id = await TicketManager.create_ticket(
                guild=interaction.guild,
                creator=interaction.user,
                module=self.module,
                description=self.beschreibung.value,
                category_id=self.category_id,
            )

            # ── Erste Nachricht im Ticket-Kanal ───────────────────────────────
            embed = discord.Embed(
                title=f"🎫 Ticket #{ticket_id}",
                color=discord.Color.blurple(),
            )
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
# TICKET PANEL – Dropdown Menü
# ══════════════════════════════════════════════════════════════════════════════

class TicketPanelView(discord.ui.View):
    def __init__(self, modules: list[dict], category_id: int, bot: discord.Client):
        super().__init__(timeout=None)
        self.modules     = modules
        self.category_id = category_id
        self.bot         = bot
        self._build_select()

    def _build_select(self):
        options = [
            discord.SelectOption(
                label=mod["name"][:100],
                description=(mod.get("description") or "")[:100],
                value=str(mod["id"]),
                emoji="🎫",
            )
            for mod in self.modules[:25]
        ]
        select = discord.ui.Select(
            placeholder="📂 Wähle ein Ticket-Modul…",
            options=options,
            custom_id="ticket_panel_select",
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        module_id = int(interaction.data["values"][0])
        module    = await TicketManager.get_module(module_id)
        if not module:
            await interaction.response.send_message("❌ Modul nicht gefunden.", ephemeral=True)
            return
        modal = TicketDescriptionModal(module=module, category_id=self.category_id, bot=self.bot)
        await interaction.response.send_modal(modal)


# ══════════════════════════════════════════════════════════════════════════════
# TICKET CHANNEL BUTTONS
# ══════════════════════════════════════════════════════════════════════════════

class TicketChannelView(discord.ui.View):
    def __init__(self, ticket_id: int, server_id: str, creator_id: str, module: dict, bot: discord.Client):
        super().__init__(timeout=None)
        self.ticket_id  = ticket_id
        self.server_id  = server_id
        self.creator_id = creator_id
        self.module     = module
        self.bot        = bot
        self._claimed_by_id: str | None = None

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        staff_roles = self.module.get("staff_role_ids", [])
        return any(str(r.id) in staff_roles for r in member.roles)

    @discord.ui.button(label="📥 Ticket übernehmen", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff kann Tickets übernehmen.", ephemeral=True)
            return
        if self._claimed_by_id:
            await interaction.response.send_message(
                f"❌ Ticket wird bereits von <@{self._claimed_by_id}> bearbeitet.", ephemeral=True
            )
            return
        self._claimed_by_id = str(interaction.user.id)
        update_ticket(self.server_id, self.ticket_id, {"claimed_by": str(interaction.user.id)})
        button.label    = "🔄 Ticket abgeben"
        button.style    = discord.ButtonStyle.secondary
        button.custom_id = "ticket_unclaim"
        button.callback = self.unclaim_button
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"✅ {interaction.user.mention} hat das Ticket übernommen.")

    async def unclaim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self._claimed_by_id and not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur der Bearbeiter kann das Ticket abgeben.", ephemeral=True)
            return
        self._claimed_by_id = None
        update_ticket(self.server_id, self.ticket_id, {"claimed_by": None})
        button.label    = "📥 Ticket übernehmen"
        button.style    = discord.ButtonStyle.primary
        button.custom_id = "ticket_claim"
        button.callback = self.claim_button
        await interaction.message.edit(view=self)
        await interaction.response.send_message("🔄 Ticket wurde freigegeben.")

    @discord.ui.button(label="🔒 Ticket schließen", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = load_ticket(self.server_id, self.ticket_id)
        if not ticket:
            await interaction.response.send_message("❌ Ticket nicht gefunden.", ephemeral=True)
            return

        if self._is_staff(interaction.user):
            # Staff schließt direkt nach Bestätigung
            view = TicketCloseConfirmView(
                ticket=ticket, channel=interaction.channel, closer=interaction.user,
                module=self.module, bot=self.bot,
            )
            await interaction.response.send_message(
                embed=discord.Embed(title="⚠️ Ticket schließen?",
                                    description="Bist du sicher? Das Ticket wird exportiert und der Kanal gelöscht.",
                                    color=discord.Color.red()),
                view=view, ephemeral=True
            )
        else:
            # User stellt Schließanfrage
            if str(interaction.user.id) != str(ticket.get("creator_id")):
                await interaction.response.send_message("❌ Nur der Ersteller oder Staff kann Tickets schließen.", ephemeral=True)
                return
            view = TicketCloseRequestView(
                ticket=ticket, channel=interaction.channel,
                requester=interaction.user, module=self.module, bot=self.bot,
            )
            await interaction.channel.send(
                embed=discord.Embed(
                    title="🙋 Schließanfrage",
                    description=f"{interaction.user.mention} möchte das Ticket schließen.",
                    color=discord.Color.orange()
                ),
                view=view
            )
            await interaction.response.send_message("✅ Schließanfrage gesendet.", ephemeral=True)

    @discord.ui.button(label="➕ Benutzer hinzufügen", style=discord.ButtonStyle.secondary, custom_id="ticket_add_user")
    async def add_user_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        view = AddUserView(ticket_id=self.ticket_id, server_id=self.server_id, channel=interaction.channel)
        await interaction.response.send_message("Wähle einen oder mehrere Benutzer:", view=view, ephemeral=True)


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self, ticket: dict, channel: discord.TextChannel, closer: discord.Member, module: dict, bot: discord.Client):
        super().__init__(timeout=60)
        self.ticket  = ticket
        self.channel = channel
        self.closer  = closer
        self.module  = module
        self.bot     = bot

    @discord.ui.button(label="✅ Bestätigen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await TicketManager.close_ticket(interaction.guild, self.channel, self.ticket, self.closer)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Abgebrochen.", ephemeral=True)
        self.stop()


class TicketCloseRequestView(discord.ui.View):
    def __init__(self, ticket: dict, channel: discord.TextChannel, requester: discord.Member, module: dict, bot: discord.Client):
        super().__init__(timeout=None)
        self.ticket    = ticket
        self.channel   = channel
        self.requester = requester
        self.module    = module
        self.bot       = bot

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        staff_roles = self.module.get("staff_role_ids", [])
        return any(str(r.id) in staff_roles for r in member.roles)

    @discord.ui.button(label="✅ Genehmigen", style=discord.ButtonStyle.success, custom_id="ticket_close_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        await interaction.response.defer()
        await TicketManager.close_ticket(interaction.guild, self.channel, self.ticket, interaction.user)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger, custom_id="ticket_close_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        await interaction.message.delete()
        await interaction.response.send_message("🚫 Schließanfrage abgelehnt.")
        self.stop()


class AddUserView(discord.ui.View):
    def __init__(self, ticket_id: int, server_id: str, channel: discord.TextChannel):
        super().__init__(timeout=60)
        self.ticket_id = ticket_id
        self.server_id = server_id
        self.channel   = channel

        user_select = discord.ui.UserSelect(placeholder="Benutzer auswählen...", min_values=1, max_values=5)
        user_select.callback = self.user_selected
        self.add_item(user_select)

    async def user_selected(self, interaction: discord.Interaction):
        for user_id in interaction.data["values"]:
            member = interaction.guild.get_member(int(user_id))
            if member:
                await self.channel.set_permissions(
                    member,
                    view_channel=True, send_messages=True, read_message_history=True
                )
        added = ", ".join(f"<@{uid}>" for uid in interaction.data["values"])
        await interaction.response.send_message(f"✅ Hinzugefügt: {added}", ephemeral=True)
        await self.channel.send(f"👤 {added} wurde zum Ticket hinzugefügt.")
        self.stop()