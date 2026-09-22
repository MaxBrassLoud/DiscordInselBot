from __future__ import annotations

from typing import Any, Optional

import discord

from .case import ImageEvidence, RaidCase
from .config import RAID_ACTIONS


_STYLE_MAP = {
    "approve": discord.ButtonStyle.success,
    "warn_release": discord.ButtonStyle.primary,
    "keep_timeout": discord.ButtonStyle.secondary,
    "ban": discord.ButtonStyle.danger,
}


# ================== Report View ==================
class RaidReportView(discord.ui.View):
    def __init__(
        self,
        cog: Any,
        case: Optional[RaidCase] = None,
        disabled: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.case = case

        for action, meta in RAID_ACTIONS.items():
            button = discord.ui.Button(
                label=meta["label"],
                style=_STYLE_MAP[action],
                custom_id=f"raid_report:{action}",
                disabled=disabled,
            )
            button.callback = self._make_callback(action)
            self.add_item(button)

    def _make_callback(self, action: str):
        async def callback(interaction: discord.Interaction):
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Nur auf Servern nutzbar.", ephemeral=True
                )
                return

            # Moderator-Check: Admin, Moderator-Rolle oder MBL
            is_mod = await self.cog.is_moderator(
                interaction.user, interaction.guild
            )
            if not is_mod:
                required_perm = RAID_ACTIONS[action]["required_perm"]
                if not getattr(
                    interaction.user.guild_permissions, required_perm, False
                ):
                    await interaction.response.send_message(
                        "Keine Berechtigung.", ephemeral=True
                    )
                    return

            case = await self.cog.resolve_case(interaction, self.case)
            if not case:
                await interaction.response.send_message(
                    "Fall konnte nicht mehr gelesen werden.", ephemeral=True
                )
                return

            user_label = f"<@{case.user_id}>"
            meta = RAID_ACTIONS[action]
            await interaction.response.send_message(
                f"Bestaetigen: **{meta['label']}** fuer {user_label}?",
                view=RaidConfirmView(self.cog, action, case, interaction.message),
                ephemeral=True,
            )

        return callback


# ================== Confirm View ==================
class RaidConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: Any,
        action: str,
        case: RaidCase,
        report_message: Optional[discord.Message],
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.action = action
        self.case = case
        self.report_message = report_message

        confirm = discord.ui.Button(
            label=RAID_ACTIONS[action]["confirm_label"],
            style=_STYLE_MAP[action],
            custom_id=f"raid_confirm:{action}",
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(
            label="Abbrechen",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid_cancel:{action}",
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction):
        await self.cog.execute_report_action(
            interaction, self.action, self.case, self.report_message
        )

    async def _cancel(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Abgebrochen.", view=self)


# ================== Forbidden Image Review ==================
class ForbiddenReviewView(discord.ui.View):
    """
    Zeigt dem Moderator nacheinander jedes Bild, welches im Fall vorkam.
    Fuer jedes Bild kann "Hinzufuegen" oder "Nicht hinzufuegen" geklickt werden.
    """

    def __init__(
        self,
        cog: Any,
        case_id: str,
        images: list[ImageEvidence],
        index: int = 0,
    ):
        super().__init__(timeout=900)
        self.cog = cog
        self.case_id = case_id
        self.images = images
        self.index = index
        self.added_count = 0
        self.skipped_count = 0
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.clear_items()
        if self.index < len(self.images):
            add_btn = discord.ui.Button(
                label="Hinzufuegen",
                style=discord.ButtonStyle.danger,
                custom_id="forbidden_review:add",
            )
            add_btn.callback = self._add
            self.add_item(add_btn)

            skip_btn = discord.ui.Button(
                label="Nicht hinzufuegen",
                style=discord.ButtonStyle.secondary,
                custom_id="forbidden_review:skip",
            )
            skip_btn.callback = self._skip
            self.add_item(skip_btn)

    def build_embed(self) -> discord.Embed:
        if self.index >= len(self.images):
            embed = discord.Embed(
                title="Review abgeschlossen",
                description=(
                    f"Alle **{len(self.images)}** Bilder wurden bearbeitet.\n\n"
                    f"Hinzugefuegt: **{self.added_count}**\n"
                    f"Uebersprungen: **{self.skipped_count}**"
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"Fall-ID: {self.case_id}")
            return embed

        img = self.images[self.index]
        embed = discord.Embed(
            title=f"Verbotene Bilder | Bild {self.index + 1}/{len(self.images)}",
            description=(
                f"**Datei:** `{img.filename}`\n"
                f"**SHA1:** `{img.sha1}`\n"
                f"**Phash:** `{img.phash}`\n\n"
                "Soll dieses Bild zur Liste der verbotenen Bilder hinzugefuegt werden?"
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_image(url=img.url)
        embed.set_footer(text=f"Fall-ID: {self.case_id}")
        return embed

    async def _advance(self, interaction: discord.Interaction, added: bool):
        img = self.images[self.index]
        if added:
            ok = await self.cog.forbidden_store.add(img.sha1, img.phash)
            if ok:
                self.added_count += 1
                await self.cog.db.log_moderation_action(
                    guild_id=str(interaction.guild_id or 0),
                    action="raid_forbidden_image_add",
                    target_id=img.sha1,
                    target_name=img.filename,
                    moderator_id=str(interaction.user.id),
                    moderator_name=str(interaction.user),
                    reason="Bild zur Verbotsliste hinzugefuegt",
                )
            else:
                # schon vorhanden -> als uebersprungen zaehlen
                self.skipped_count += 1
        else:
            self.skipped_count += 1

        self.index += 1
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    async def _add(self, interaction: discord.Interaction):
        await self._advance(interaction, True)

    async def _skip(self, interaction: discord.Interaction):
        await self._advance(interaction, False)