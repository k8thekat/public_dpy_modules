"""Copyright (C) 2021-2025 Katelynn Cadwallader.

This file is part of Kuma Kuma Bear, a Discord Bot.

Kuma Kuma Bear is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3, or (at your option)
any later version.

Kuma Kuma Bear is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
License for more details.

You should have received a copy of the GNU General Public License
along with Kuma Kuma Bear; see the file COPYING.  If not, write to the Free
Software Foundation, 51 Franklin Street - Fifth Floor, Boston, MA
02110-1301, USA.

"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import KumaCog as Cog

if TYPE_CHECKING:
    from sqlite3 import Row

    from kuma_kuma import Kuma_Kuma

LOGGER = logging.getLogger()

# How long a member is considered "new" after joining the guild.
NEW_MEMBER_THRESHOLD = datetime.timedelta(days=7)

AUTOMOD_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS automod_rules (
    id INTEGER PRIMARY KEY NOT NULL,
    guildid INTEGER NOT NULL,
    ruleid INTEGER NOT NULL UNIQUE)
"""


class AutoMod(Cog):
    """Houses AutoModeration commands and interactions.

    ### Mention Spam Guard:
    - Mention spam protection using Discord's native AutoMod with custom escalation.

    Creates a native `mention_spam` AutoMod rule that blocks offending messages at the gateway.
    When that rule fires, the `on_auto_moderation_action` listener escalates the action
    based on member tenure; new members are banned, established members are kicked.
    """

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        # If a member has ANY of these roles, they are treated as "new" regardless of join date.
        self.new_member_role_ids: set[int] = set()
        # In-memory cache of the rule IDs we created, backed by the ``automod_rules`` table.
        # The listener only escalates for these; the user can rename a rule on creation, so we
        # track by ID rather than name. Populated from the database in ``cog_load``.
        self.tracked_rule_ids: set[int] = set()

    async def cog_load(self) -> None:
        """Ensures the ``automod_rules`` table exists and warms the tracked-rule cache."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute(AUTOMOD_SETUP_SQL)
            rows: list[Row] = await conn.fetchall("""SELECT ruleid FROM automod_rules""")
        self.tracked_rule_ids = {row["ruleid"] for row in rows}
        LOGGER.info(
            "<%s.%s> | Loaded %s tracked AutoMod rule(s).",
            __class__.__name__,
            "cog_load",
            len(self.tracked_rule_ids),
        )

    async def _track_rule(self, guild_id: int, rule_id: int) -> None:
        """Persists a created AutoMod rule ID and adds it to the in-memory cache."""
        try:
            async with self.bot.pool.acquire() as conn:
                await conn.execute(
                    """INSERT OR IGNORE INTO automod_rules(guildid, ruleid) VALUES(?, ?)""",
                    guild_id,
                    rule_id,
                )
        except sqlite3.DatabaseError:
            LOGGER.exception(
                "<%s.%s> | Failed to persist AutoMod rule %s for guild %s.",
                __class__.__name__,
                "_track_rule",
                rule_id,
                guild_id,
            )
        self.tracked_rule_ids.add(rule_id)

    async def _untrack_rule(self, rule_id: int) -> None:
        """Removes a persisted AutoMod rule ID and evicts it from the in-memory cache."""
        try:
            async with self.bot.pool.acquire() as conn:
                await conn.execute("""DELETE FROM automod_rules WHERE ruleid = ?""", rule_id)
        except sqlite3.DatabaseError:
            LOGGER.exception(
                "<%s.%s> | Failed to remove AutoMod rule %s from the database.",
                __class__.__name__,
                "_untrack_rule",
                rule_id,
            )
        self.tracked_rule_ids.discard(rule_id)

    def _is_new_member(self, member: discord.Member) -> bool:
        """Returns True if the member is considered new/unverified."""
        if any(role.id in self.new_member_role_ids for role in member.roles):
            return True
        if member.joined_at is not None:
            return discord.utils.utcnow() - member.joined_at < NEW_MEMBER_THRESHOLD
        return False

    @commands.Cog.listener("on_auto_moderation_action")
    async def on_mention_spam(self, execution: discord.AutoModAction) -> None:
        """Escalates native mention_spam rule actions based on member tenure."""
        if execution.rule_trigger_type is not discord.AutoModRuleTriggerType.mention_spam:
            return

        # Only escalate for rules we created; the user may have renamed them, so match by ID.
        if execution.rule_id not in self.tracked_rule_ids:
            return

        guild: discord.Guild = execution.guild
        member: discord.Member | None = execution.member or guild.get_member(execution.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(execution.user_id)
            except discord.NotFound:
                return

        if member.guild_permissions.administrator or member.id in self.bot.owner_ids:
            return

        content_preview: str = (
            (execution.content[:100] + "...") if execution.content and len(execution.content) > 100 else (execution.content or "N/A")
        )

        if self._is_new_member(member):
            try:
                await member.ban(reason=f"Mention spam from new member (AutoMod escalation) | Content: {content_preview}")
                LOGGER.info(
                    "<%s.%s> | Banned new member %s (%s) in guild %s for mention spam.",
                    __class__.__name__,
                    "on_mention_spam",
                    member,
                    member.id,
                    guild.id,
                )
            except discord.Forbidden:
                LOGGER.warning(
                    "<%s.%s> | Missing permissions to ban %s (%s) in guild %s.",
                    __class__.__name__,
                    "on_mention_spam",
                    member,
                    member.id,
                    guild.id,
                )

    mention_spam = app_commands.Group(
        name="mention_spam",
        description="Manage the mention spam AutoMod rule.",
        default_permissions=discord.Permissions(administrator=True),
    )

    @mention_spam.command(name="create", description="Create a Discord AutoMod mention-spam rule for this server.")
    @app_commands.describe(
        mention_limit="Max role/user mentions allowed per message before triggering.",
        alert_channel="Channel to send alert messages to.",
    )
    async def create_mention_rule(
        self,
        interaction: discord.Interaction,
        rule_name: str,
        mention_limit: app_commands.Range[int, 1, 50] = 5,
        alert_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Creates a native Discord AutoMod rule that blocks messages exceeding the mention limit.

        The rule only uses `block_message` — escalation is handled by the
        `on_auto_moderation_action` listener based on member tenure.
        """
        if interaction.guild is None:
            await interaction.response.send_message(
                content=f"This command must be used in a server. {self.emoji_table.kuma_hmm}",
                ephemeral=True,
            )
            return

        trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.mention_spam,
            mention_limit=mention_limit,
            mention_raid_protection=True,
        )

        actions: list[discord.AutoModRuleAction] = [
            discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message),
        ]
        if alert_channel is not None:
            actions.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.send_alert_message,
                    channel_id=alert_channel.id,
                ),
            )

        try:
            rule: discord.AutoModRule = await interaction.guild.create_automod_rule(
                name=rule_name,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=f"Created by {interaction.user} via /mention_spam create",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                content=f"I don't have permission to create **AutoMod** rules. {self.emoji_table.kuma_crying}",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                content=f"Failed to create the rule. {self.emoji_table.kuma_sad}\n> {e}",
                ephemeral=True,
            )
            return

        # Track the rule so the listener knows this is one of ours when it fires.
        await self._track_rule(guild_id=interaction.guild.id, rule_id=rule.id)

        await interaction.response.send_message(
            content=(
                f"Created AutoMod rule **{rule.name}** (ID: `{rule.id}`). {self.emoji_table.kuma_star_eye}\n"
                f"> Mention limit: **{mention_limit}** per message\n"
                f"> Raid protection: **enabled**\n"
                f"> Escalation: new members are **banned**, established members are **kicked**"
            ),
            ephemeral=True,
        )

    async def autocomplete_automod_rules(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Populates the rule parameter with all AutoMod rules on the server."""
        if interaction.guild is None:
            return []
        try:
            rules: list[discord.AutoModRule] = await interaction.guild.fetch_automod_rules()
        except (discord.Forbidden, discord.HTTPException):
            return []
        return [
            app_commands.Choice(name=f"{rule.name} ({rule.trigger.type.name})"[:100], value=str(rule.id))
            for rule in rules
            if current.lower() in rule.name.lower() or current in str(rule.id)
        ][:25]

    @mention_spam.command(name="delete", description="Delete an AutoMod rule from this server.")
    @app_commands.describe(rule="The AutoMod rule to delete.")
    @app_commands.autocomplete(rule=autocomplete_automod_rules)
    async def delete_mention_rule(self, interaction: discord.Interaction, rule: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                content=f"This command must be used in a server. {self.emoji_table.kuma_hmm}",
                ephemeral=True,
            )
            return

        try:
            rules: list[discord.AutoModRule] = await interaction.guild.fetch_automod_rules()
        except discord.Forbidden:
            await interaction.response.send_message(
                content=f"I don't have permission to view AutoMod rules. {self.emoji_table.kuma_crying}",
                ephemeral=True,
            )
            return

        target: discord.AutoModRule | None = next((r for r in rules if str(r.id) == rule), None)
        if target is None:
            await interaction.response.send_message(
                content=f"Couldn't find that AutoMod rule. {self.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return

        try:
            await target.delete(reason=f"Deleted by {interaction.user} via /mention_spam delete")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.response.send_message(
                content=f"Failed to delete **{target.name}**. {self.emoji_table.kuma_sad}\n> {e}",
                ephemeral=True,
            )
            return

        await self._untrack_rule(rule_id=target.id)

        await interaction.response.send_message(
            content=f"Deleted AutoMod rule **{target.name}**. {self.emoji_table.kuma_chuckle}",
            ephemeral=True,
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(AutoMod(bot=bot))
