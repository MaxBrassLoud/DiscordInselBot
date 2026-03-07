import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("tickets.setup")

MAX_MODULES = 10


class AddTicketModuleModal(discord.ui.Modal, title="Ticket-Modul hinzufügen"):
    name = discord.ui.TextInput(label="Modul-Name", placeholder="z.B. Support", required=True, max_length=50)
    description = discord.ui.TextInput(label="Beschreibung", placeholder="z.B. Hilfe bei Problemen",
                                       required=True, max_length=200)
    max_tickets = discord.ui.TextInput(label="Max. Tickets pro User", placeholder="z.B. 2",
                                       required=True, max_length=2, default="1")
    modal_question = discord.ui.TextInput(label="Modal-Anweisung",
                                          placeholder="Was soll der User beschreiben?",
                                          style=discord.TextStyle.paragraph, required=True, max_length=300)

    def __init__(self, setup_view: "TicketSetupView"):
        super().__init__()
        self.setup_view = setup_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_t = int(self.max_tickets.value)
        except ValueError:
            max_t = 1

        view = ModuleRolePickerView(
            name=self.name.value,
            description=self.description.value,
            max_tickets=max_t,
            modal_question=self.modal_question.value,
            setup_view=self.setup_view,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎭 Staff-Rollen wählen",
                description=f"Modul **{self.name.value}** – Wähle die Staff-Rollen die Zugriff haben sollen.",
                color=discord.Color.blurple()
            ),
            view=view, ephemeral=True
        )


class ModuleRolePickerView(discord.ui.View):
    def __init__(self, name: str, description: str, max_tickets: int, modal_question: str, setup_view: "TicketSetupView"):
        super().__init__(timeout=120)
        self.mod_data   = {"name": name, "description": description, "max_tickets": max_tickets, "modal_question": modal_question}
        self.setup_view = setup_view

        role_sel = discord.ui.RoleSelect(placeholder="Staff-Rollen wählen...", min_values=1, max_values=10)
        role_sel.callback = self.roles_selected
        self.add_item(role_sel)

    async def roles_selected(self, interaction: discord.Interaction):
        self.mod_data["staff_role_ids"] = interaction.data["values"]
        self.setup_view.pending_modules.append(self.mod_data)
        self.setup_view._rebuild()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Modul gespeichert",
                description=f"**{self.mod_data['name']}** mit {len(self.mod_data['staff_role_ids'])} Staff-Rolle(n) hinzugefügt.",
                color=discord.Color.green()
            ), view=None
        )
        try:
            await self.setup_view._original_interaction.edit_original_response(
                embed=self.setup_view._build_embed(), view=self.setup_view
            )
        except Exception:
            pass


class TicketSetupView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=600)
        self.guild_id              = guild_id
        self.bot                   = bot
        self.pending_modules       = []
        self.panel_channel_id: str | None  = None
        self.category_id: str | None       = None
        self.log_channel_id: str | None    = None   # NEW: Ticket-Log Kanal
        self.staff_ping_channel_id: str | None = None  # NEW: Staff-Ping Kanal
        self._original_interaction = None
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        # Panel-Kanal
        ch_sel = discord.ui.ChannelSelect(
            placeholder="📢 Panel-Kanal (wo das Ticket-Panel erscheint)",
            min_values=1, max_values=1, channel_types=[discord.ChannelType.text]
        )
        async def ch_cb(interaction: discord.Interaction):
            self.panel_channel_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        ch_sel.callback = ch_cb
        self.add_item(ch_sel)

        # Kategorie
        cat_sel = discord.ui.ChannelSelect(
            placeholder="📁 Kategorie für neue Ticket-Kanäle",
            min_values=1, max_values=1, channel_types=[discord.ChannelType.category]
        )
        async def cat_cb(interaction: discord.Interaction):
            self.category_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        cat_sel.callback = cat_cb
        self.add_item(cat_sel)

        # Log-Kanal (NEU)
        log_sel = discord.ui.ChannelSelect(
            placeholder="📋 Ticket-Log Kanal (Links zu allen Tickets)",
            min_values=1, max_values=1, channel_types=[discord.ChannelType.text]
        )
        async def log_cb(interaction: discord.Interaction):
            self.log_channel_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        log_sel.callback = log_cb
        self.add_item(log_sel)

        # Staff-Ping Kanal (NEU)
        ping_sel = discord.ui.ChannelSelect(
            placeholder="🔔 Staff-Ping Kanal (Benachrichtigung bei neuem Ticket)",
            min_values=1, max_values=1, channel_types=[discord.ChannelType.text]
        )
        async def ping_cb(interaction: discord.Interaction):
            self.staff_ping_channel_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        ping_sel.callback = ping_cb
        self.add_item(ping_sel)

        # Modul hinzufügen
        if len(self.pending_modules) < MAX_MODULES:
            add_btn = discord.ui.Button(
                label=f"➕ Modul hinzufügen ({len(self.pending_modules)}/{MAX_MODULES})",
                style=discord.ButtonStyle.primary
            )
            async def add_cb(interaction: discord.Interaction):
                await interaction.response.send_modal(AddTicketModuleModal(self))
            add_btn.callback = add_cb
            self.add_item(add_btn)

        # Letztes Modul entfernen
        if self.pending_modules:
            rem_btn = discord.ui.Button(label="🗑️ Letztes Modul entfernen", style=discord.ButtonStyle.secondary)
            async def rem_cb(interaction: discord.Interaction):
                if self.pending_modules:
                    self.pending_modules.pop()
                self._rebuild()
                await interaction.response.edit_message(embed=self._build_embed(), view=self)
            rem_btn.callback = rem_cb
            self.add_item(rem_btn)

        # Speichern & Panel senden
        save_btn = discord.ui.Button(
            label="🚀 Setup abschließen & Panel senden",
            style=discord.ButtonStyle.success,
            disabled=not (self.panel_channel_id and self.category_id and self.pending_modules)
        )
        save_btn.callback = self.save_callback
        self.add_item(save_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚙️ Ticket-System Setup", color=discord.Color.blurple(),
                              description="Konfiguriere das Ticket-System für diesen Server.")
        embed.add_field(name="📢 Panel-Kanal",
                        value=f"<#{self.panel_channel_id}>" if self.panel_channel_id else "*Nicht ausgewählt*", inline=True)
        embed.add_field(name="📁 Kategorie",
                        value=f"<#{self.category_id}>" if self.category_id else "*Nicht ausgewählt*", inline=True)
        embed.add_field(name="📋 Log-Kanal",
                        value=f"<#{self.log_channel_id}>" if self.log_channel_id else "*Nicht ausgewählt*", inline=True)
        embed.add_field(name="🔔 Staff-Ping Kanal",
                        value=f"<#{self.staff_ping_channel_id}>" if self.staff_ping_channel_id else "*Nicht ausgewählt*", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        if self.pending_modules:
            for i, mod in enumerate(self.pending_modules, 1):
                roles = ", ".join(f"<@&{r}>" for r in mod.get("staff_role_ids", [])) or "*keine*"
                embed.add_field(
                    name=f"📂 Modul {i}: {mod['name']}",
                    value=f"Beschreibung: {mod['description']}\nMax Tickets: {mod['max_tickets']}\nStaff: {roles}",
                    inline=False
                )
        else:
            embed.add_field(name="📂 Module", value="*Noch keine Module hinzugefügt*", inline=False)
        return embed

    async def save_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            guild_id = str(self.guild_id)

            # ── ticket_servers upsert ─────────────────────────────────────────
            existing = supabase.table("ticket_servers").select("server_id").eq("server_id", guild_id).execute()
            server_data = {
                "server_id":             guild_id,
                "category_id":           self.category_id,
                "panel_channel_id":      self.panel_channel_id,
                "log_channel_id":        self.log_channel_id,        # NEW
                "staff_ping_channel_id": self.staff_ping_channel_id, # NEW
            }
            if existing.data:
                supabase.table("ticket_servers").update(server_data).eq("server_id", guild_id).execute()
            else:
                server_data["ticket_counter"] = 0
                supabase.table("ticket_servers").insert(server_data).execute()

            # ── Alte Module löschen ───────────────────────────────────────────
            old_mods = supabase.table("ticket_modules").select("id").eq("server_id", guild_id).execute()
            for mod in (old_mods.data or []):
                supabase.table("ticket_module_roles").delete().eq("module_id", mod["id"]).execute()
            supabase.table("ticket_modules").delete().eq("server_id", guild_id).execute()

            # ── Neue Module speichern ─────────────────────────────────────────
            from .views import TicketPanelView
            db_modules = []
            for mod in self.pending_modules:
                result = supabase.table("ticket_modules").insert({
                    "server_id":      guild_id,
                    "name":           mod["name"],
                    "description":    mod["description"],
                    "max_tickets":    mod["max_tickets"],
                    "modal_question": mod["modal_question"],
                }).execute()
                if result.data:
                    mod_id = result.data[0]["id"]
                    mod["id"] = mod_id
                    for role_id in mod.get("staff_role_ids", []):
                        supabase.table("ticket_module_roles").insert({
                            "module_id": mod_id, "role_id": str(role_id)
                        }).execute()
                db_modules.append(mod)

            # ── Panel senden ──────────────────────────────────────────────────
            panel_channel = self.bot.get_channel(int(self.panel_channel_id))
            if not panel_channel:
                await interaction.followup.send("❌ Panel-Kanal nicht gefunden!", ephemeral=True)
                return

            embed = discord.Embed(
                title="🎫 Support-Tickets",
                description="Wähle ein Modul aus dem Dropdown um ein Ticket zu erstellen.",
                color=discord.Color.blurple()
            )
            for mod in db_modules:
                embed.add_field(name=f"📂 {mod['name']}", value=mod["description"], inline=False)

            panel_view = TicketPanelView(modules=db_modules, category_id=int(self.category_id), bot=self.bot)
            await panel_channel.send(embed=embed, view=panel_view)

            for item in self.children:
                item.disabled = True
            await interaction.followup.send(
                f"✅ Ticket-System eingerichtet! Panel wurde in <#{self.panel_channel_id}> gesendet.", ephemeral=True
            )
        except Exception as e:
            logger.error(f"[TicketSetupView.save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)