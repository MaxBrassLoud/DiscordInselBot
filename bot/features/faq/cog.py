import discord
from discord.ext import commands
from discord import app_commands

FAQ = {
    "Wie werde ich Mitglied?": "Gar Ned",
    "Wo baut ihr": "Nirgends",
    "Warum kann ich nicht beitreten(mitgliederstopp)": "Weils so ist",
    "Wann ist das nächste Event ": "Wenn es die Ankündigung dazu gibt...",
    "Wie du zu uns findest": "Gar Ned",
    "Wo kann man eine eigene Insel bauen": "Bau Einfach",
    "wie wird im Dorf gebaut(Stil und ohne Absprache außer es betrifft jemanden, wir dürfen umbauen etc.)": "What the hell"
}


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
        answer = FAQ.get(question)
        if answer is None:
            await interaction.response.send_message(
                "❓ Diese Frage ist nicht in der FAQ enthalten.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📖 FAQ Antwort",
            description=f"**{question}**\n{answer}",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)


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