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
    """Öffentliche Rollenvergabe-Nachricht (Embed)."""
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
        lines.append(f"{emoji} = {role.mention}")
        if desc:
            for dl in desc.splitlines():
                if dl.strip():
                    lines.append(f"   └ *{dl.strip()}*")
        lines.append("")
    embed.description += "\n\n" + "\n".join(lines).rstrip()
    return embed


def _build_editor_embed(guild: discord.Guild,
                        modules: list[dict],
                        channel_id: str | None,
                        dirty: bool = False) -> discord.Embed:
    """Editor-Embed mit nummerierter Modul-Liste."""
    title = "🛠️ Rollenvergabe bearbeiten" + (" • *ungespeichert*" if dirty else "")
    embed = discord.Embed(title=title, color=discord.Color.orange() if dirty else discord.Color.blurple())

    if modules:
        for i, mod in enumerate(modules, 1):
            role = guild.get_role(int(mod["role_id"]))
            role_txt = role.mention if role else "❓ gelöscht"
            emoji = mod.get("emoji", "🎭")
            desc  = (mod.get("role_desc") or "*keine Beschreibung*")[:100]
            embed.add_field(
                name=f"{i}. {emoji} {mod.get('display_name', '?')} → {role_txt}",
                value=desc,
                inline=False,
            )
    else:
        embed.add_field(name="Module", value="*Keine Module – füge eins hinzu.*", inline=False)

    embed.add_field(
        name="📢 Zielkanal",
        value=f"<#{channel_id}>" if channel_id else "*nicht gesetzt*",
        inline=False,
    )
    embed.set_footer(text="Änderungen erst mit 💾 Speichern wirksam.")
    return embed


async def _apply_to_public_message(bot: discord.Client, guild: discord.Guild,
                                   modules: list[dict], channel_id: str):
    """Schreibt Module + Kanal in die DB und aktualisiert die öffentliche Nachricht."""
    supabase = get_supabase()

    # 1) Alte role_modules-Zeilen der Gilde löschen und neu schreiben
    supabase.table("role_modules").delete().eq("guild_id", str(guild.id)).execute()

    saved = []
    for mod in modules:
        role = guild.get_role(int(mod["role_id"]))
        if not role:
            continue
        row = {
            "guild_id":     str(guild.id),
            "role_id":      str(role.id),
            "role_name":    mod.get("role_name", role.name),
            "display_name": mod.get("display_name", role.name),
            "role_desc":    mod.get("role_desc", ""),
            "emoji":        mod.get("emoji", "🎭"),
            "channel_id":   str(channel_id),
        }
        supabase.table("role_modules").insert(row).execute()
        saved.append(row)

    # 2) Bestehende öffentliche Nachricht suchen
    rm = supabase.table("role_message").select("*").eq(
        "guild_id", str(guild.id)
    ).execute()

    old_msg = None
    old_channel = None
    if rm.data:
        old_channel = guild.get_channel(int(rm.data[0]["channel_id"]))
        if old_channel:
            try:
                old_msg = await old_channel.fetch_message(int(rm.data[0]["message_id"]))
            except (discord.NotFound, discord.Forbidden):
                old_msg = None

    target_channel = guild.get_channel(int(channel_id))
    if not target_channel:
        raise RuntimeError("Zielkanal nicht gefunden.")

    embed = _build_roles_embed(guild, saved)

    # 3) Wenn Kanal gewechselt: alte Nachricht löschen, neue senden
    if old_msg and old_channel and old_channel.id != target_channel.id:
        try:
            await old_msg.delete()
        except Exception:
            pass
        msg = await target_channel.send(embed=embed)
        # Reactions setzen
        for mod in saved:
            try:
                await msg.add_reaction(mod["emoji"])
            except discord.HTTPException as e:
                logger.warning(f"Emoji {mod['emoji']} konnte nicht gesetzt werden: {e}")

    elif old_msg:
        # 4) Gleicher Kanal: Nachricht editieren
        try:
            await old_msg.edit(embed=embed)
        except Exception as e:
            logger.warning(f"Edit fehlgeschlagen: {e}")

        # Reactions abgleichen
        wanted   = {m["emoji"] for m in saved}
        existing = {str(r.emoji) for r in old_msg.reactions}
        for reaction in list(old_msg.reactions):
            if str(reaction.emoji) not in wanted:
                try:
                    await old_msg.clear_reaction(reaction.emoji)
                except discord.HTTPException:
                    pass
        for emoji in wanted - existing:
            try:
                await old_msg.add_reaction(emoji)
            except discord.HTTPException as e:
                logger.warning(f"Emoji {emoji} konnte nicht gesetzt werden: {e}")
        msg = old_msg

    else:
        # 5) Keine Nachricht vorhanden → neue senden
        msg = await target_channel.send(embed=embed)
        for mod in saved:
            try:
                await msg.add_reaction(mod["emoji"])
            except discord.HTTPException as e:
                logger.warning(f"Emoji {mod['emoji']} konnte nicht gesetzt werden: {e}")

    # 6) role_message-Eintrag upserten
    supabase.table("role_message").upsert({
        "guild_id":   str(guild.id),
        "channel_id": str(target_channel.id),
        "message_id": str(msg.id),
    }).execute()

    return msg


# ──────────────────────────────────────────────────────────────
#  Modal: Modul hinzufügen (mit vorausgefüllten Feldern beim Edit)
# ──────────────────────────────────────────────────────────────
class ModuleModal(discord.ui.Modal):
    role_name = discord.ui.TextInput(label="Anzeigename", required=True, max_length=100)
    role_emoji = discord.ui.TextInput(label="Emoji (Reaction)", required=True, max_length=60)
    role_desc = discord.ui.TextInput(
        label="Beschreibung", required=True,
        style=discord.TextStyle.paragraph, max_length=300,
    )

    def __init__(self, editor: "RoleEditorView", index: int | None = None):
        """
        index=None → neues Modul
        index=int  → bestehendes Modul bearbeiten
        """
        super().__init__(title="Modul hinzufügen" if index is None else "Modul bearbeiten")
        self.editor = editor
        self.index  = index

        if index is not None:
            mod = editor.modules[index]
            self.role_name.default  = mod.get("display_name", "")
            self.role_emoji.default = mod.get("emoji", "🎭")
            self.role_desc.default  = mod.get("role_desc", "") or ""

    async def on_submit(self, interaction: discord.Interaction):
        name  = self.role_name.value.strip()
        emoji = self.role_emoji.value.strip()
        desc  = self.role_desc.value.strip()

        # Duplikat-Check
        for i, mod in enumerate(self.editor.modules):
            if self.index is not None and i == self.index:
                continue
            if (mod.get("display_name") or "").lower() == name.lower():
                await interaction.response.send_message(
                    "❌ Ein anderes Modul hat diesen Namen schon.", ephemeral=True
                )
                return
            if mod.get("emoji") == emoji:
                await interaction.response.send_message(
                    "❌ Ein anderes Modul nutzt dieses Emoji schon.", ephemeral=True
                )
                return

        if self.index is None:
            # Neu → Rolle auswählen
            view = RolePickerView(
                display_name=name, role_desc=desc, emoji=emoji,
                editor=self.editor,
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🎭 Rolle auswählen",
                    description=(
                        f"Modul **{name}** wird angelegt.\n"
                        f"Emoji: {emoji}\n"
                        f"Beschreibung: *{desc}*\n\n"
                        f"Wähle nun die **Discord-Rolle**."
                    ),
                    color=discord.Color.blurple(),
                ),
                view=view, ephemeral=True,
            )
        else:
            # Bestehend → Werte übernehmen, Rolle bleibt
            self.editor.modules[self.index]["display_name"] = name
            self.editor.modules[self.index]["emoji"]        = emoji
            self.editor.modules[self.index]["role_desc"]    = desc
            self.editor.mark_dirty()
            await interaction.response.edit_message(
                embed=self.editor.build_embed(), view=self.editor
            )


# ──────────────────────────────────────────────────────────────
#  Role-Picker (beim Hinzufügen)
# ──────────────────────────────────────────────────────────────
class RolePickerView(discord.ui.View):
    def __init__(self, display_name: str, role_desc: str, emoji: str, editor: "RoleEditorView"):
        super().__init__(timeout=120)
        self.display_name = display_name
        self.role_desc    = role_desc
        self.emoji        = emoji
        self.editor       = editor

        role_sel = discord.ui.RoleSelect(placeholder="Wähle die Discord-Rolle...", min_values=1, max_values=1)
        role_sel.callback = self.role_selected
        self.add_item(role_sel)

    async def role_selected(self, interaction: discord.Interaction):
        role_id  = interaction.data["values"][0]
        role_obj = interaction.guild.get_role(int(role_id))
        role_name = role_obj.name if role_obj else "?"

        for mod in self.editor.modules:
            if mod["role_id"] == role_id:
                await interaction.response.send_message(
                    "❌ Diese Rolle ist bereits zugewiesen.", ephemeral=True
                )
                return

        if len(self.editor.modules) >= MAX_ROLE_MODULES:
            await interaction.response.send_message(
                f"❌ Maximal {MAX_ROLE_MODULES} Module.", ephemeral=True
            )
            return

        self.editor.modules.append({
            "display_name": self.display_name,
            "role_desc":    self.role_desc,
            "emoji":        self.emoji,
            "role_id":      role_id,
            "role_name":    role_name,
        })
        self.editor.mark_dirty()

        # Editor-Nachricht aktualisieren
        try:
            await self.editor._original_interaction.edit_original_response(
                embed=self.editor.build_embed(), view=self.editor
            )
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ Modul {self.emoji} **{self.display_name}** hinzugefügt.", ephemeral=True
        )


# ──────────────────────────────────────────────────────────────
#  Select: Modul zum Bearbeiten / Löschen auswählen
# ──────────────────────────────────────────────────────────────
class ModuleNumberSelect(discord.ui.Select):
    def __init__(self, editor: "RoleEditorView", mode: str):
        self.editor = editor
        self.mode   = mode  # "edit" oder "delete"

        options = []
        for i, mod in enumerate(editor.modules[:25]):
            options.append(discord.SelectOption(
                label=f"{i+1}. {mod.get('display_name','?')[:80]}",
                value=str(i),
                emoji=mod.get("emoji", "🎭") if len(mod.get("emoji", "")) <= 4 else None,
                description=(mod.get("role_desc") or "")[:90] or None,
            ))
        if not options:
            options = [discord.SelectOption(label="Keine Module", value="-1")]

        super().__init__(
            placeholder="✏️ Modul zum Bearbeiten wählen" if mode == "edit"
                        else "🗑️ Modul zum Löschen wählen",
            min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "-1":
            await interaction.response.send_message("Keine Module vorhanden.", ephemeral=True)
            return

        idx = int(self.values[0])

        if self.mode == "edit":
            await interaction.response.send_modal(ModuleModal(self.editor, index=idx))
        else:
            removed = self.editor.modules.pop(idx)
            self.editor.mark_dirty()
            await interaction.response.edit_message(
                embed=self.editor.build_embed(), view=self.editor
            )


# ──────────────────────────────────────────────────────────────
#  Editor-View
# ──────────────────────────────────────────────────────────────
class RoleEditorView(discord.ui.View):
    def __init__(self, guild: discord.Guild, bot: discord.Client,
                 modules: list[dict], channel_id: str | None):
        super().__init__(timeout=600)
        self.guild        = guild
        self.bot          = bot
        self.modules      = modules          # in-memory; erst bei Save in DB
        self.channel_id   = channel_id
        self.dirty        = False
        self._original_interaction: discord.Interaction | None = None
        self._rebuild()

    # ── Helpers ──
    def mark_dirty(self):
        self.dirty = True
        self._rebuild()

    def build_embed(self) -> discord.Embed:
        return _build_editor_embed(self.guild, self.modules, self.channel_id, self.dirty)

    # ── UI-Aufbau ──
    def _rebuild(self):
        self.clear_items()

        # Reihe 1: Hinzufügen / Bearbeiten / Löschen
        add_btn = discord.ui.Button(
            label="➕ Modul hinzufügen", style=discord.ButtonStyle.success,
            disabled=len(self.modules) >= MAX_ROLE_MODULES,
        )
        async def add_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(ModuleModal(self, index=None))
        add_btn.callback = add_cb
        self.add_item(add_btn)

        if self.modules:
            edit_sel = ModuleNumberSelect(self, mode="edit")
            self.add_item(edit_sel)

            del_sel = ModuleNumberSelect(self, mode="delete")
            self.add_item(del_sel)

        # Reihe 2: Kanal ändern
        ch_sel = discord.ui.ChannelSelect(
            placeholder="📢 Zielkanal ändern",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        async def ch_cb(interaction: discord.Interaction):
            self.channel_id = interaction.data["values"][0]
            self.mark_dirty()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        ch_sel.callback = ch_cb
        self.add_item(ch_sel)

        # Reihe 3: Speichern / Abbrechen
        save_btn = discord.ui.Button(
            label="💾 Speichern", style=discord.ButtonStyle.primary,
            disabled=not (self.modules and self.channel_id),
        )
        save_btn.callback = self._save
        self.add_item(save_btn)

        cancel_btn = discord.ui.Button(
            label="❌ Abbrechen", style=discord.ButtonStyle.danger,
        )
        cancel_btn.callback = self._cancel
        self.add_item(cancel_btn)

    # ── Speichern ──
    async def _save(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        try:
            await _apply_to_public_message(
                self.bot, self.guild, self.modules, self.channel_id
            )
            self.dirty = False
            self._rebuild()
            await interaction.edit_original_response(
                embed=self.build_embed(), view=self
            )
            await interaction.followup.send(
                "✅ Gespeichert und öffentliche Nachricht aktualisiert!", ephemeral=True
            )
        except Exception as e:
            logger.exception("save")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    # ── Abbrechen ──
    async def _cancel(self, interaction: discord.Interaction):
        self.dirty = False
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Abgebrochen",
                description="Es wurden keine Änderungen gespeichert.",
                color=discord.Color.red(),
            ),
            view=None,
        )


# ──────────────────────────────────────────────────────────────
#  Cog
# ──────────────────────────────────────────────────────────────
class RollenCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Reaction added ──
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

    # ── Reaction removed ──
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

    # ── on_ready: Sync ──
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

                # Embed aktualisieren
                try:
                    await msg.edit(embed=_build_roles_embed(guild, mods))
                except Exception as e:
                    logger.warning(f"Embed-Update fehlgeschlagen: {e}")

                # Bot-Reactions synchronisieren
                wanted   = {m.get("emoji", "🎭") for m in mods}
                existing = {str(r.emoji) for r in msg.reactions}

                for reaction in list(msg.reactions):
                    if str(reaction.emoji) not in wanted:
                        try:
                            await msg.clear_reaction(reaction.emoji)
                        except discord.HTTPException:
                            pass

                for emoji in wanted - existing:
                    try:
                        await msg.add_reaction(emoji)
                    except discord.HTTPException as e:
                        logger.warning(f"Konnte Emoji {emoji} nicht setzen: {e}")

                # Verpasste Reaktionen aufarbeiten
                await self._sync_reactions(guild, msg, mods)

            logger.info("✅ Rollenvergabe-Nachrichten synchronisiert")
        except Exception as e:
            logger.error(f"[on_ready] Fehler: {e}")

    async def _sync_reactions(self, guild: discord.Guild, msg: discord.Message, mods: list[dict]):
        """Gleicht Reactions mit tatsächlichen Rollen ab (verpasste Reaktionen)."""
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

            for uid in reacted_users.get(emoji, set()):
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

    @rollen.command(name="setup", description="Erstellt die Rollenvergabe (nur wenn noch keine existiert)")
    async def setup_rollen(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        supabase = get_supabase()
        rm = supabase.table("role_message").select("*").eq(
            "guild_id", str(interaction.guild_id)
        ).execute()
        if rm.data:
            await interaction.response.send_message(
                "ℹ️ Es existiert schon eine Rollenvergabe. Nutze `/rollen bearbeiten`.",
                ephemeral=True,
            )
            return

        # Direkt in Editor springen
        view = RoleEditorView(interaction.guild, self.bot, modules=[], channel_id=None)
        view._original_interaction = interaction
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    @rollen.command(name="bearbeiten", description="Öffnet den Editor für die Rollenvergabe-Nachricht")
    async def rollen_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        supabase = get_supabase()

        # bestehende Module + Kanal laden
        mods = supabase.table("role_modules").select("*").eq(
            "guild_id", str(interaction.guild_id)
        ).execute().data or []

        rm = supabase.table("role_message").select("*").eq(
            "guild_id", str(interaction.guild_id)
        ).execute()
        channel_id = rm.data[0]["channel_id"] if rm.data else None

        # Module in Editor-Format bringen
        modules = [
            {
                "display_name": m.get("display_name") or m.get("role_name", "?"),
                "role_desc":    m.get("role_desc", ""),
                "emoji":        m.get("emoji", "🎭"),
                "role_id":      str(m["role_id"]),
                "role_name":    m.get("role_name", "?"),
            }
            for m in mods
        ]

        view = RoleEditorView(interaction.guild, self.bot, modules=modules, channel_id=channel_id)
        view._original_interaction = interaction
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RollenCog(bot))