import os
import discord


def has_admin_rights(interaction: discord.Interaction) -> bool:
    """Check if user is admin or MBL."""
    if interaction.user.guild_permissions.administrator:
        return True
    if str(interaction.user.id) == str(os.getenv("MBL")):
        return True
    return False


async def has_staff_role(member: discord.Member, role_ids: list[str]) -> bool:
    """Check if member has any of the given role IDs."""
    user_role_ids = [str(r.id) for r in member.roles]
    return any(rid in role_ids for rid in user_role_ids)