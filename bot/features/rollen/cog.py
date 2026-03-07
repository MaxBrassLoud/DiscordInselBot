import discord
from discord.ext import commands
from discord import app_commands

from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger

logger = get_logger("rollen")
MAX_ROLE_MODULES = 5


class RoleAssignView(discord.ui.View):
    def __init__(self, role_id: int, module_db_id: int):
        super().__init__(timeout=None)
        self.role_id      = role_id
        self.module_db_id = module_db_id

        accept_btn = discord.ui.Button(
            label="✅ Rolle annehmen", style=discord.ButtonStyle.success,
            custom_id=f"role_accept_{module_db_id}",
        )
        accept_btn.callback = self.accept_callback
        self.add_item(accept_btn)

        decline_btn = discord.ui.Button(
            label="❌ Rolle ablehnen", style=discord.ButtonStyle.danger,
            custom_id=f"role_decline_{module_db_id}",
        )
        decline_btn.callback = self.decline_callback
        self.add_item(decline_btn)

    async def accept_callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("ℹ️ Du hast diese Rolle bereits.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Rollenvergabe Bot")
            await interaction.response.send_message(f"✅ Du hast die Rolle **{role.name}** erhalten!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Ich habe keine Berechtigung diese Rolle zu vergeben.", ephemeral=True)

    async def decline_callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message("ℹ️ Du hast diese Rolle nicht.", ephemeral=True)
            return
        try:
            await interaction.user.remove_roles(role, reason="Rollenvergabe Bot")
            await interaction.response.send_message(f"🔕 Die Rolle **{role.name}** wurde entfernt.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Ich habe keine Berechtigung.", ephemeral=True)


class AddRoleModuleModal(discord.ui.Modal, title="Rollenmodul hinzufügen"):
    role_name = discord.ui.TextInput(label="Rollenname", placeholder="z.B. Gamer, ...", required=True, max_length=100)
    role_desc = discord.ui.TextInput(label="Beschreibung", placeholder="Was bekommt man?", required=True,
                                     style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, setup_view: "SetupRoleView"):
        super().__init__()
        self.setup_view = setup_view

    async def on_submit(self, interaction: discord.Interaction):
        for mod in self.setup_view.modules:
            if mod["display_name"].lower() == self.role_name.value.lower():
                await interaction.response.send_message("❌ Ein Modul mit diesem Namen existiert bereits.", ephemeral=True)
                return
        view = RolePickerView(display_name=self.role_name.value, role_desc=self.role_desc.value, setup_view=self.setup_view)
        await interaction.response.send_message(
            embed=discord.Embed(title="🎭 Rolle auswählen",
                                description=f"Modul **{self.role_name.value}** wird angelegt.\n\nWähle nun die **Discord-Rolle**.",
                                color=discord.Color.blurple()),
            view=view, ephemeral=True
        )


class RolePickerView(discord.ui.View):
    def __init__(self, display_name: str, role_desc: str, setup_view: "SetupRoleView"):
        super().__init__(timeout=120)
        self.display_name = display_name
        self.role_desc    = role_desc
        self.setup_view   = setup_view
        role_sel = discord.ui.RoleSelect(placeholder="Wähle die Discord-Rolle...", min_values=1, max_values=1)
        role_sel.callback = self.role_selected
        self.add_item(role_sel)

    async def role_selected(self, interaction: discord.Interaction):
        role_id   = interaction.data["values"][0]
        role_name = interaction.guild.get_role(int(role_id)).name
        for mod in self.setup_view.modules:
            if mod["role_id"] == role_id:
                await interaction.response.send_message(f"❌ Diese Rolle ist bereits zugewiesen.", ephemeral=True)
                return
        self.setup_view.modules.append({"display_name": self.display_name, "role_desc": self.role_desc, "role_id": role_id, "role_name": role_name})
        self.setup_view._rebuild()
        await interaction.response.edit_message(
            embed=discord.Embed(title="✅ Modul hinzugefügt", description=f"**{self.display_name}** → {role_name}", color=discord.Color.green()),
            view=None
        )
        try:
            await self.setup_view._original_interaction.edit_original_response(
                embed=self.setup_view._build_embed(), view=self.setup_view
            )
        except Exception:
            pass


class SetupRoleView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id              = guild_id
        self.bot                   = bot
        self.modules: list         = []
        self.target_channel_id: str | None = None
        self._original_interaction = None
        self._rebuild()

    def _rebuild(self):
        for item in [i for i in self.children if not isinstance(i, discord.ui.ChannelSelect) or True]:
            self.remove_item(item) if item in self.children else None
        self.clear_items()

        if len(self.modules) < MAX_ROLE_MODULES:
            add_btn = discord.ui.Button(label=f"➕ Modul hinzufügen ({len(self.modules)}/{MAX_ROLE_MODULES})", style=discord.ButtonStyle.primary)
            async def add_cb(interaction: discord.Interaction):
                await interaction.response.send_modal(AddRoleModuleModal(self))
            add_btn.callback = add_cb
            self.add_item(add_btn)

        if self.modules:
            remove_btn = discord.ui.Button(label="🗑️ Letztes Modul entfernen", style=discord.ButtonStyle.secondary)
            async def remove_cb(interaction: discord.Interaction):
                if self.modules:
                    self.modules.pop()
                self._rebuild()
                await interaction.response.edit_message(embed=self._build_embed(), view=self)
            remove_btn.callback = remove_cb
            self.add_item(remove_btn)

        ch_sel = discord.ui.ChannelSelect(placeholder="📢 Kanal für die Rollenvergabe-Nachricht", min_values=1, max_values=1, channel_types=[discord.ChannelType.text])
        async def ch_cb(interaction: discord.Interaction):
            self.target_channel_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        ch_sel.callback = ch_cb
        self.add_item(ch_sel)

        send_btn = discord.ui.Button(
            label="🚀 Senden & speichern", style=discord.ButtonStyle.success,
            disabled=not (self.modules and self.target_channel_id),
        )
        send_btn.callback = self.send_callback
        self.add_item(send_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚙️ Rollenvergabe Setup", color=discord.Color.blurple())
        if self.modules:
            for i, mod in enumerate(self.modules, 1):
                embed.add_field(name=f"Modul {i}: {mod['display_name']} → @{mod['role_name']}", value=mod["role_desc"][:200], inline=False)
        else:
            embed.add_field(name="Module", value="*Noch keine Module*", inline=False)
        embed.add_field(name="📢 Kanal", value=f"<#{self.target_channel_id}>" if self.target_channel_id else "*Nicht ausgewählt*", inline=False)
        return embed

    async def send_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            channel = self.bot.get_channel(int(self.target_channel_id))
            if not channel:
                await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
                return
            await channel.send(embed=discord.Embed(title="🎭 Rollenvergabe",
                description="Wähle deine Rollen! Drücke auf **✅ Rolle annehmen** oder **❌ Rolle ablehnen**.",
                color=discord.Color.blurple()))
            for mod in self.modules:
                role = interaction.guild.get_role(int(mod["role_id"]))
                if not role:
                    continue
                db_data = {"guild_id": str(self.guild_id), "role_id": str(role.id), "role_name": mod["role_name"],
                           "display_name": mod["display_name"], "role_desc": mod["role_desc"], "channel_id": str(self.target_channel_id)}
                result = supabase.table("role_modules").insert(db_data).execute()
                db_id  = result.data[0]["id"] if result.data else 0
                view   = RoleAssignView(role.id, db_id)
                self.bot.add_view(view)
                sent = await channel.send(embed=discord.Embed(title=f"🏷️ {mod['display_name']}", description=mod["role_desc"], color=discord.Color.blurple()), view=view)
                supabase.table("role_modules").update({"message_id": str(sent.id)}).eq("id", db_id).execute()
            await interaction.followup.send(f"✅ {len(self.modules)} Module gesendet!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class RollenCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            supabase = get_supabase()
            result = supabase.table("role_modules").select("*").execute()
            for mod in result.data:
                view = RoleAssignView(int(mod["role_id"]), mod["id"])
                self.bot.add_view(view)
            logger.info(f"✅ {len(result.data)} Rollenvergabe-Views registriert")
        except Exception as e:
            logger.error(f"[register_role_views] Fehler: {e}")

    @app_commands.command(name="setup_rollen", description="Richte die Rollenvergabe ein")
    async def setup_rollen(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = SetupRoleView(interaction.guild_id, self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="rollen_bearbeiten", description="Bearbeite oder lösche ein Rollenvergabe-Modul")
    async def rollen_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            result = supabase.table("role_modules").select("*").eq("guild_id", str(interaction.guild_id)).execute()
            if not result.data:
                await interaction.followup.send("❌ Keine Module gefunden. Nutze `/setup_rollen` zuerst.", ephemeral=True)
                return
            embed = discord.Embed(title="✏️ Rollenvergabe bearbeiten", color=discord.Color.blurple())
            for m in result.data:
                display = m.get("display_name") or m.get("role_name", "?")
                role    = interaction.guild.get_role(int(m["role_id"]))
                embed.add_field(name=f"ID `{m['id']}` — {display}",
                                value=f"Rolle: {role.mention if role else '❓'} | {m.get('role_desc','')[:80]}", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RollenCog(bot))