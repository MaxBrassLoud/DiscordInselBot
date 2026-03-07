import discord
from bot.core.settings import upsert_settings
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("spieleabend.views")


class SetupSpielabendView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.ping_role = None
        self.channel = None
        self.delete_roles = []

        ping_role_select = discord.ui.RoleSelect(
            placeholder="Wähle die Rolle die gepingt werden soll",
            min_values=1, max_values=1,
        )
        ping_role_select.callback = self.ping_role_callback
        self.add_item(ping_role_select)

        channel_select = discord.ui.ChannelSelect(
            placeholder="Wähle den Kanal für Spieleabende",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        channel_select.callback = self.channel_callback
        self.add_item(channel_select)

        delete_role_select = discord.ui.RoleSelect(
            placeholder="Wähle Rollen die Spieleabende löschen dürfen",
            min_values=1, max_values=10,
        )
        delete_role_select.callback = self.delete_roles_callback
        self.add_item(delete_role_select)

        self.save_button = discord.ui.Button(
            label="Speichern", style=discord.ButtonStyle.success,
            emoji="💾", disabled=True
        )
        self.save_button.callback = self.save_callback
        self.add_item(self.save_button)

    async def ping_role_callback(self, interaction: discord.Interaction):
        self.ping_role = interaction.data['values'][0]
        await self.update_status(interaction)

    async def channel_callback(self, interaction: discord.Interaction):
        self.channel = interaction.data['values'][0]
        await self.update_status(interaction)

    async def delete_roles_callback(self, interaction: discord.Interaction):
        self.delete_roles = interaction.data['values']
        await self.update_status(interaction)

    async def update_status(self, interaction: discord.Interaction):
        if self.ping_role and self.channel and self.delete_roles:
            self.save_button.disabled = False
        embed = interaction.message.embeds[0]
        embed.clear_fields()
        embed.add_field(name="🔔 Ping Rolle",    value=f"<@&{self.ping_role}>" if self.ping_role else "*Nicht ausgewählt*", inline=False)
        embed.add_field(name="📢 Kanal",         value=f"<#{self.channel}>" if self.channel else "*Nicht ausgewählt*", inline=False)
        embed.add_field(name="🗑️ Lösch-Rollen", value=" ".join([f"<@&{rid}>" for rid in self.delete_roles]) if self.delete_roles else "*Nicht ausgewählt*", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            await upsert_settings(str(self.guild_id), {
                "ping_role_id":    str(self.ping_role),
                "channel_id":      str(self.channel),
                "delete_role_ids": ",".join([str(rid) for rid in self.delete_roles])
            })
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = "✅ Setup erfolgreich gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: {str(e)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler: {str(e)}", ephemeral=True)


class SpielabendView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Dabei",      style=discord.ButtonStyle.success,   custom_id="spieleabend_dabei",      emoji="✅")
    async def dabei_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "dabei")

    @discord.ui.button(label="Vielleicht", style=discord.ButtonStyle.primary,   custom_id="spieleabend_vielleicht", emoji="❓")
    async def vielleicht_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "vielleicht")

    @discord.ui.button(label="Keine Zeit", style=discord.ButtonStyle.danger,    custom_id="spieleabend_keine_zeit", emoji="❌")
    async def keine_zeit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "keine_zeit")

    async def handle_response(self, interaction: discord.Interaction, status: str):
        user_id    = str(interaction.user.id)
        message_id = str(interaction.message.id)
        try:
            supabase = get_supabase()
            result = supabase.table("game_nights").select("*").eq("message_id", message_id).execute()
            if not result.data:
                await interaction.response.send_message("❌ Spieleabend nicht gefunden!", ephemeral=True)
                return
            gn = result.data[0]
            dabei      = [uid for uid in gn.get('dabei',      []) if uid != user_id]
            vielleicht = [uid for uid in gn.get('vielleicht', []) if uid != user_id]
            keine_zeit = [uid for uid in gn.get('keine_zeit', []) if uid != user_id]
            if status == "dabei":        dabei.append(user_id)
            elif status == "vielleicht": vielleicht.append(user_id)
            elif status == "keine_zeit": keine_zeit.append(user_id)
            supabase.table("game_nights").update({
                "dabei": dabei, "vielleicht": vielleicht, "keine_zeit": keine_zeit
            }).eq("message_id", message_id).execute()
            embed = interaction.message.embeds[0]
            dabei_text      = " ".join([f"<@{uid}>" for uid in dabei])      or "*Niemand*"
            vielleicht_text = " ".join([f"<@{uid}>" for uid in vielleicht]) or "*Niemand*"
            keine_zeit_text = " ".join([
                interaction.guild.get_member(int(uid)).display_name
                for uid in keine_zeit if interaction.guild.get_member(int(uid))
            ]) or "*Niemand*"
            for i, field in enumerate(embed.fields):
                if field.name == "✅ Dabei":
                    embed.set_field_at(i, name="✅ Dabei",      value=dabei_text,      inline=False)
                elif field.name == "❓ Vielleicht":
                    embed.set_field_at(i, name="❓ Vielleicht", value=vielleicht_text, inline=False)
                elif field.name == "❌ Keine Zeit":
                    embed.set_field_at(i, name="❌ Keine Zeit", value=keine_zeit_text, inline=False)
            await interaction.message.edit(embed=embed)
            await interaction.response.send_message("✅ Status aktualisiert!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {str(e)}", ephemeral=True)