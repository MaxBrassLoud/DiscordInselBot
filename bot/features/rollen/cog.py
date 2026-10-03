import discord
from discord.ext import commands
from discord import app_commands

from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger

logger = get_logger("rollen")
MAX_ROLE_MODULES = 5
HEADER_TITLE = "Hol dir deine Rolle"


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────
def _member_count(guild: discord.Guild, role: discord.Role) -> int:
    return sum(1 for m in guild.members if role in m.roles)


def _build_roles_embed(guild: discord.Guild, modules: list[dict]) -> discord.Embed:
    """Eine Embed-Nachricht mit allen Modulen inkl. Beschreibung."""
    embed = discord.Embed(
        title=HEADER_TITLE,
        description="Reagiere mit dem passenden Emoji, um die Rolle zu erhalten oder zu entfernen.",
        color=discord.Color.blurple(),
    )

    if not modules:
        embed.add_field(name="Hinweis", value="*Keine Rollen konfiguriert.*", inline=False)
        return embed

    lines: list[str] = []
    for mod in modules:
        role = guild.get_role(int(mod["role_id"]))
        if not role:
            continue

        emoji = mod.get("emoji", "🎭")
        desc  = (mod.get("role_desc") or "").strip()

        # Hauptzeile: "🔔 = @Server-Ping"
        lines.append(f"{emoji} = {role.mention}")

        # Beschreibung darunter eingerückt
        if desc:
            for dl in desc.splitlines():
                if dl.strip():
                    lines.append(f"   └ *{dl.strip()}*")

        lines.append("")  # Leerzeile zwischen Modulen

    embed.description += "\n\n" + "\n".join(lines).rstrip()
    return embed


# ──────────────────────────────────────────────────────────────
#  Modal: neues Modul anlegen
# ──────────────────────────────────────────────────────────────
class AddRoleModuleModal(discord.ui.Modal, title="Rollenmodul hinzufügen"):
    role_name = discord.ui.TextInput(
        label="Anzeigename",
        placeholder="z.B. Server-Ping",
        required=True, max_length=100,
    )
    role_emoji = discord.ui.TextInput(
        label="Emoji (Reaction)",
        placeholder="z.B. 🔔  oder  <:name:1234567890>",
        required=True, max_length=60,
    )
    role_desc = discord.ui.TextInput(
        label="Beschreibung",
        placeholder="Was bekommt man durch diese Rolle?",
        required=True, style=discord.TextStyle.paragraph, max_length=300,
    )

    def __init__(self, setup_view: "SetupRoleView"):
        super().__init__()
        self.setup_view = setup_view

    async def on_submit(self, interaction: discord.Interaction):
        # Duplikat-Check: Name
        for mod in self.setup_view.modules:
            if mod["display_name"].lower() == self.role_name.value.lower():
                await interaction.response.send_message(
                    "❌ Ein Modul mit diesem Namen existiert bereits.", ephemeral=True
                )
                return
            # Duplikat-Check: Emoji
            if mod.get("emoji") == self.role_emoji.value.strip():
                await interaction.response.send_message(
                    "❌ Dieses Emoji wird bereits verwendet.", ephemeral=True
                )
                return

        view = RolePickerView(
            display_name=self.role_name.value,
            role_desc=self.role_desc.value.strip(),
            emoji=self.role_emoji.value.strip(),
            setup_view=self.setup_view,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎭 Rolle auswählen",
                description=(
                    f"Modul **{self.role_name.value}** wird angelegt.\n"
                    f"Emoji: {self.role_emoji.value}\n"
                    f"Beschreibung: *{self.role_desc.value.strip()}*\n\n"
                    f"Wähle nun die **Discord-Rolle**."
                ),
                color=discord.Color.blurple(),
            ),
            view=view, ephemeral=True,
        )


class RolePickerView(discord.ui.View):
    def __init__(self, display_name: str, role_desc: str, emoji: str, setup_view: "SetupRoleView"):
        super().__init__(timeout=120)
        self.display_name = display_name
        self.role_desc    = role_desc
        self.emoji        = emoji
        self.setup_view   = setup_view

        role_sel = discord.ui.RoleSelect(placeholder="Wähle die Discord-Rolle...", min_values=1, max_values=1)
        role_sel.callback = self.role_selected
        self.add_item(role_sel)

    async def role_selected(self, interaction: discord.Interaction):
        role_id  = interaction.data["values"][0]
        role_obj = interaction.guild.get_role(int(role_id))
        role_name = role_obj.name if role_obj else "?"

        for mod in self.setup_view.modules:
            if mod["role_id"] == role_id:
                await interaction.response.send_message(
                    "❌ Diese Rolle ist bereits zugewiesen.", ephemeral=True
                )
                return

        self.setup_view.modules.append({
            "display_name": self.display_name,
            "role_desc":    self.role_desc,
            "emoji":        self.emoji,
            "role_id":      role_id,
            "role_name":    role_name,
        })
        self.setup_view._rebuild()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Modul hinzugefügt",
                description=f"{self.emoji} **{self.display_name}** → {role_name}",
                color=discord.Color.green(),
            ),
            view=None,
        )
        try:
            await self.setup_view._original_interaction.edit_original_response(
                embed=self.setup_view._build_embed(), view=self.setup_view
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
#  Setup-View
# ──────────────────────────────────────────────────────────────
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
        self.clear_items()

        if len(self.modules) < MAX_ROLE_MODULES:
            add_btn = discord.ui.Button(
                label=f"➕ Modul hinzufügen ({len(self.modules)}/{MAX_ROLE_MODULES})",
                style=discord.ButtonStyle.primary,
            )

            async def add_cb(interaction: discord.Interaction):
                await interaction.response.send_modal(AddRoleModuleModal(self))
            add_btn.callback = add_cb
            self.add_item(add_btn)

        if self.modules:
            remove_btn = discord.ui.Button(
                label="🗑️ Letztes Modul entfernen",
                style=discord.ButtonStyle.secondary,
            )

            async def remove_cb(interaction: discord.Interaction):
                if self.modules:
                    self.modules.pop()
                self._rebuild()
                await interaction.response.edit_message(embed=self._build_embed(), view=self)
            remove_btn.callback = remove_cb
            self.add_item(remove_btn)

        ch_sel = discord.ui.ChannelSelect(
            placeholder="📢 Kanal für die Rollenvergabe",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )

        async def ch_cb(interaction: discord.Interaction):
            self.target_channel_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        ch_sel.callback = ch_cb
        self.add_item(ch_sel)

        send_btn = discord.ui.Button(
            label="🚀 Senden & speichern",
            style=discord.ButtonStyle.success,
            disabled=not (self.modules and self.target_channel_id),
        )
        send_btn.callback = self.send_callback
        self.add_item(send_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚙️ Rollenvergabe Setup", color=discord.Color.blurple())
        if self.modules:
            for i, mod in enumerate(self.modules, 1):
                embed.add_field(
                    name=f"{mod.get('emoji','🎭')} Modul {i}: {mod['display_name']} → @{mod['role_name']}",
                    value=(mod.get("role_desc") or "*keine Beschreibung*")[:200],
                    inline=False,
                )
        else:
            embed.add_field(name="Module", value="*Noch keine Module*", inline=False)
        embed.add_field(
            name="📢 Kanal",
            value=f"<#{self.target_channel_id}>" if self.target_channel_id else "*Nicht ausgewählt*",
            inline=False,
        )
        return embed

    async def send_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        try:
            supabase = get_supabase()
            guild    = interaction.guild
            channel  = self.bot.get_channel(int(self.target_channel_id))
            if not channel:
                await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
                return

            # 1) Alte Module dieser Gilde löschen und neue speichern
            supabase.table("role_modules").delete().eq(
                "guild_id", str(guild.id)
            ).execute()

            saved_modules = []
            for mod in self.modules:
                role = guild.get_role(int(mod["role_id"]))
                if not role:
                    continue
                db_data = {
                    "guild_id":     str(guild.id),
                    "role_id":      str(role.id),
                    "role_name":    mod["role_name"],
                    "display_name": mod["display_name"],
                    "role_desc":    mod.get("role_desc", ""),
                    "emoji":        mod.get("emoji", "🎭"),
                    "channel_id":   str(self.target_channel_id),
                }
                supabase.table("role_modules").insert(db_data).execute()
                saved_modules.append(db_data)

            if not saved_modules:
                await interaction.followup.send("❌ Keine gültigen Module vorhanden.", ephemeral=True)
                return

            # 2) Eine Übersichts-Nachricht senden
            embed = _build_roles_embed(guild, saved_modules)
            msg = await channel.send(embed=embed)

            # 3) Bot reagiert mit ALLEN Emojis → initialisiert Reactions
            for mod in saved_modules:
                try:
                    await msg.add_reaction(mod["emoji"])
                except discord.HTTPException as e:
                    logger.warning(f"Konnte Emoji {mod['emoji']} nicht hinzufügen: {e}")

            # 4) role_message speichern (eine Nachricht pro Gilde)
            supabase.table("role_message").upsert({
                "guild_id":   str(guild.id),
                "channel_id": str(self.target_channel_id),
                "message_id": str(msg.id),
            }).execute()

            await interaction.followup.send(
                f"✅ Rollenvergabe gesendet mit {len(saved_modules)} Modulen!", ephemeral=True
            )
        except Exception as e:
            logger.exception("send_callback")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ──────────────────────────────────────────────────────────────
#  Cog
# ──────────────────────────────────────────────────────────────
class RollenCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────────────────
    #  Reaktion hinzugefügt
    # ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        supabase = get_supabase()
        rm = supabase.table("role_message").select("*").eq(
            "guild_id", str(guild.id)
        ).execute()
        if not rm.data or str(rm.data[0]["message_id"]) != str(payload.message_id):
            return

        mods = supabase.table("role_modules").select("*").eq(
            "guild_id", str(guild.id)
        ).execute()
        mod = next((m for m in mods.data if m.get("emoji") == str(payload.emoji)), None)
        if not mod:
            return

        role = guild.get_role(int(mod["role_id"]))
        if not role:
            return

        member = payload.member or guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        try:
            if role not in member.roles:
                await member.add_roles(role, reason="Rollenvergabe (Reaction)")
        except discord.Forbidden:
            logger.warning(f"Keine Berechtigung, {member} die Rolle {role} zu geben.")

    # ──────────────────────────────────────────────────────────
    #  Reaktion entfernt
    # ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        supabase = get_supabase()
        rm = supabase.table("role_message").select("*").eq(
            "guild_id", str(guild.id)
        ).execute()
        if not rm.data or str(rm.data[0]["message_id"]) != str(payload.message_id):
            return

        mods = supabase.table("role_modules").select("*").eq(
            "guild_id", str(guild.id)
        ).execute()
        mod = next((m for m in mods.data if m.get("emoji") == str(payload.emoji)), None)
        if not mod:
            return

        role = guild.get_role(int(mod["role_id"]))
        if not role:
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Rollenvergabe (Reaction entfernt)")
        except discord.Forbidden:
            logger.warning(f"Keine Berechtigung, {member} die Rolle {role} zu nehmen.")

    # ──────────────────────────────────────────────────────────
    #  on_ready: Nachricht prüfen, Reactions nachsetzen,
    #  verpasste Reaktionen aufarbeiten
    # ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        try:
            supabase = get_supabase()
            rm_all = supabase.table("role_message").select("*").execute()
            if not rm_all.data:
                logger.info("Keine Rollenvergabe-Nachrichten konfiguriert.")
                return

            for rm in rm_all.data:
                guild = self.bot.get_guild(int(rm["guild_id"]))
                if not guild:
                    continue
                channel = guild.get_channel(int(rm["channel_id"]))
                if not channel:
                    continue

                try:
                    msg = await channel.fetch_message(int(rm["message_id"]))
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"Rollenvergabe-Nachricht {rm['message_id']} nicht auffindbar.")
                    continue

                mods = supabase.table("role_modules").select("*").eq(
                    "guild_id", str(guild.id)
                ).execute().data
                if not mods:
                    continue

                # 1) Embed aktualisieren (falls Rollen umbenannt / Beschreibungen geändert)
                try:
                    await msg.edit(embed=_build_roles_embed(guild, mods))
                except Exception as e:
                    logger.warning(f"Embed-Update fehlgeschlagen: {e}")

                # 2) Sicherstellen, dass der Bot mit ALLEN Emojis reagiert hat
                existing = {str(r.emoji) for r in msg.reactions}
                wanted   = {m.get("emoji", "🎭") for m in mods}
                for emoji in wanted - existing:
                    try:
                        await msg.add_reaction(emoji)
                    except discord.HTTPException as e:
                        logger.warning(f"Konnte Emoji {emoji} nicht setzen: {e}")

                # 3) Verpasste Reaktionen aufarbeiten
                await self._sync_reactions(guild, msg, mods)

            logger.info("✅ Rollenvergabe-Nachrichten synchronisiert")
        except Exception as e:
            logger.error(f"[on_ready] Fehler: {e}")

    async def _sync_reactions(self, guild: discord.Guild, msg: discord.Message, mods: list[dict]):
        """Gleicht Reactions auf der Nachricht mit den tatsächlichen Rollen ab.
        Deckt auch Reaktionen ab, die während der Offline-Zeit gesetzt wurden."""
        emoji_to_mod = {m.get("emoji", "🎭"): m for m in mods}

        reacted_users: dict[str, set[int]] = {e: set() for e in emoji_to_mod}
        for reaction in msg.reactions:
            emoji_str = str(reaction.emoji)
            if emoji_str not in emoji_to_mod:
                continue
            async for user in reaction.users():
                if user.bot:
                    continue
                reacted_users[emoji_str].add(user.id)

        for emoji, mod in emoji_to_mod.items():
            role = guild.get_role(int(mod["role_id"]))
            if not role:
                continue

            users_who_reacted = reacted_users.get(emoji, set())

            # Reaktion da, aber Rolle fehlt → hinzufügen
            for uid in users_who_reacted:
                member = guild.get_member(uid)
                if member and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Rollenvergabe (Sync)")
                        logger.info(f"Sync: {member} bekam Rolle {role.name}")
                    except discord.Forbidden:
                        pass

    # ──────────────────────────────────────────────────────────
    #  Slash-Gruppe
    # ──────────────────────────────────────────────────────────
    rollen = app_commands.Group(name="rollen", description="Rollen System")

    @rollen.command(name="setup", description="Richte die Rollenvergabe ein")
    async def setup_rollen(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = SetupRoleView(interaction.guild_id, self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True
        )

    @rollen.command(name="bearbeiten", description="Zeigt alle Rollen-Module")
    async def rollen_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            result = supabase.table("role_modules").select("*").eq(
                "guild_id", str(interaction.guild_id)
            ).execute()
            if not result.data:
                await interaction.followup.send(
                    "❌ Keine Module gefunden. Nutze `/rollen setup` zuerst.", ephemeral=True
                )
                return
            embed = discord.Embed(title="✏️ Rollenvergabe Module", color=discord.Color.blurple())
            for m in result.data:
                display = m.get("display_name") or m.get("role_name", "?")
                emoji   = m.get("emoji", "🎭")
                role    = interaction.guild.get_role(int(m["role_id"]))
                count   = _member_count(interaction.guild, role) if role else 0
                desc    = (m.get("role_desc") or "*keine Beschreibung*")[:120]
                embed.add_field(
                    name=f"{emoji} ID `{m['id']}` — {display}",
                    value=f"Rolle: {role.mention if role else '❓'} | Mitglieder: {count}\n{desc}",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RollenCog(bot))