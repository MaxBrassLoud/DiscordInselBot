import discord
from discord.ext import commands
from discord import app_commands
import random

FAQ = {
    "Wie werde ich Mitglied?": "Aktuell nehmen wir leider keine neuen Mitglieder mehr auf. \nWir werden im https://discord.com/channels/1253751493513969735/1416403634471702609 bescheid geben, sobald wir wieder neue Mitglieder aufnehmen.\nSchaut also regelmäßig dort vorbei, um das nicht zu verpassen.\n \nSolltest du einen Freund haben, der bereits ein Mitglied bei uns ist, dann kannst du ein https://discord.com/channels/1253751493513969735/1479931271042957382 und die Situation schildern.\nDarauf hin kannst du auf Zustimmung von unserem Server Team eine https://discord.com/channels/1253751493513969735/1394713441863860336 erstellen und dort ausführlich die Fragen beantowrten.",
    "Wo baut ihr": "In einem Ozean",
    "Warum kann ich nicht beitreten": "Aktuell nehmen wir keine neuen Mitglieder mehr auf. \n \nGründe dafür sind, das zuletzt zu viele unserem Clan beitreten wollten, sodass wir die Mengen an Spielern nicht mehr kontrollieren konnten und wir eine Communtiy sind in der sich viele Freundschaften wiederfinden. Durch zu viele Neuzugänge ist dies leider teilweise verloren gegangen.",
    "Wann ist das nächste Event ": "Wenn es die Ankündigung dazu gibt...",
    "Wie du zu uns findest": "Durch Magie",
    "Wo kann man eine eigene Insel bauen": "Bau Einfach",
    "Wie wird im Dorf gebaut": "What the hell"
}

random_1 = 0
random_2 = 499
async def faq_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    matches = [q for q in FAQ if current_lower in q.lower()]
    return [app_commands.Choice(name=q, value=q) for q in matches[:25]]


# -------------------------------------------------------------------
# Kontextmenü-Callback (außerhalb der Cog-Klasse)
# -------------------------------------------------------------------
async def faq_context_menu_callback(interaction: discord.Interaction, message: discord.Message):
    """Wird aufgerufen, wenn jemand Rechtsklick → Apps → 'Mit FAQ antworten' wählt."""
    options = [
        discord.SelectOption(label=q[:100], value=q)   # label max 100 Zeichen
        for q in FAQ.keys()
    ]
    select = FAQSelect(options, message)
    view = discord.ui.View(timeout=60)
    view.add_item(select)
    await interaction.response.send_message(
        "Wähle eine FAQ-Frage aus:", view=view, ephemeral=True
    )


class FAQSelect(discord.ui.Select):
    def __init__(self, options, target_message: discord.Message):
        super().__init__(placeholder="FAQ-Frage auswählen", options=options)
        self.target_message = target_message

    async def callback(self, interaction: discord.Interaction):
        question = self.values[0]
        answer = FAQ.get(question)
        if answer is None:
            await interaction.response.send_message(
                "Fehler: Frage nicht gefunden.", ephemeral=True
            )
            return

        if question == "Warum kann ich nicht beitreten":
            if random.randint(random_1, random_2) == 67:
                answer = "Weils so ist..."
            else:
                pass

        embed = discord.Embed(
            title="📖 FAQ Antwort",
            description=f"**{question}**\n{answer}",
            color=discord.Color.blue()
        )

        try:
            await self.target_message.reply(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Fehlende Berechtigung, um auf diese Nachricht zu antworten.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅ Antwort wurde gesendet.", ephemeral=True
        )


# -------------------------------------------------------------------
# Cog-Klasse (enthält nur noch den Slash-Befehl)
# -------------------------------------------------------------------
class FAQCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    faq = app_commands.Group(
        name="faq",
        description="Schnell häufig gestellte Fragen beantworten",
    )

    @faq.command(name="answer")
    @app_commands.describe(question="Wähle eine der Fragen aus")
    @app_commands.autocomplete(question=faq_autocomplete)
    async def faq_answer(self, interaction: discord.Interaction, question: str):
        try:
            await interaction.response.defer(thinking=True, ephemeral=False)
        except discord.NotFound:
            return

        answer = FAQ.get(question)
        if answer is None:
            await interaction.followup.send(
                "❓ Diese Frage ist nicht in der FAQ enthalten.", ephemeral=True
            )
            return

        if question == "Warum kann ich nicht beitreten":
            if random.randint(random_1, random_2) == 67:
                answer = "Weils so ist..."
            else:
                pass

        embed = discord.Embed(
            title="📖 FAQ Antwort",
            description=f"**{question}**\n{answer}",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=False)


# -------------------------------------------------------------------
# Setup: Cog laden + Kontextmenü registrieren
# -------------------------------------------------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(FAQCog(bot))

    # Nachrichten-Kontextmenü hinzufügen
    menu = app_commands.ContextMenu(
        name="Mit FAQ antworten",
        callback=faq_context_menu_callback,
    )
    bot.tree.add_command(menu)
