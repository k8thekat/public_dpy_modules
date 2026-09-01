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

import logging
from typing import TYPE_CHECKING, NamedTuple, Optional, TypedDict, Union

import discord
from discord import User, app_commands
from discord.app_commands import Choice  # noqa: TC002 - discord.py resolves command annotations at runtime.

from utils import KumaCog as Cog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Row

    from asqlite import Connection

    from kuma_kuma import Kuma_Kuma

LOGGER = logging.getLogger()

# `userid` is UNIQUE rather than the primary key so the table keeps the same shape as `moderator`,
# which lets `RETURNING *` hand back a row the TypedDict already describes.
USER_SETTINGS_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS user_settings (
    id INTEGER PRIMARY KEY NOT NULL,
    userid INTEGER NOT NULL UNIQUE,
    hints_enabled INT NOT NULL DEFAULT 1,
    hint_style_block INT NOT NULL DEFAULT 0,
    auto_mystbin INT NOT NULL DEFAULT 1,
    thread_rename INT NOT NULL DEFAULT 1)
"""


class UserSettings(TypedDict):
    id: int
    userid: int
    hints_enabled: bool
    hint_style_block: bool
    auto_mystbin: bool
    thread_rename: bool


# What `enabled()` falls back to; also the allowlist `set_user_settings` validates against.
USER_SETTING_DEFAULTS: dict[str, bool] = {
    "hints_enabled": True,
    "hint_style_block": False,
    "auto_mystbin": True,
    "thread_rename": True,
}

# What each column means, for the panel. A column with no entry here still shows, it just shows
# without the explanation, so adding one to the table can never break this.
SETTING_SUMMARIES: dict[str, str] = {
    "hints_enabled": "Whether I show you hints at all.",
    "hint_style_block": "Show hints in the roomier two line style instead of the compact one.",
    "auto_mystbin": "Move your long code blocks to Mystbin. Only where the server has it switched on too.",
    "thread_rename": "Let me mark your threads `[LOCKED]` or `[CLOSED]` in their title when they are.",
}


# Cog-declared preferences; one row per (user, key) rather than a column per preference.
USER_PREFERENCES_SETUP_SQL: str = """
CREATE TABLE IF NOT EXISTS user_preferences (
    userid INTEGER NOT NULL,
    pref_key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (userid, pref_key))
"""

# Discord's ceiling on a select option's label and description; longer values are truncated.
SELECT_TEXT_SIZE: int = 100


def setting_label(key: str) -> str:
    """Turns a column name into the name the panel shows (``hint_style_block`` -> ``Hint Style Block``)."""
    return key.replace("_", " ").title()


class PreferenceChoice(NamedTuple):
    """One selectable value of a cog-declared preference.

    Attributes
    ----------
    value: :class:`str`
        What is stored and what the owning cog compares against.
    label: :class:`str`
        The name shown on the select.
    summary: :class:`str`
        The line beneath the name.

    """

    value: str
    label: str
    summary: str = ""


class Preference(NamedTuple):
    """One setting a cog offers, declared in ``__preferences__``.

    Belongs to one cog, has more than two states, and arrives with its extension.

    Attributes
    ----------
    key: :class:`str`
        Stable identifier stored in the database; namespace by cog (``claude.verbosity``).
    label: :class:`str`
        Short name for the ``/preferences`` panel.
    summary: :class:`str`
        Line beneath the name.
    default: :class:`str`
        What :meth:`Preferences.value` answers when nothing is stored.
    choices: :class:`tuple[PreferenceChoice, ...]`
        The values on offer, in select list order.

    """

    key: str
    label: str
    summary: str = ""
    default: str = ""
    choices: tuple[PreferenceChoice, ...] = ()

    def choice(self, value: str) -> Optional[PreferenceChoice]:
        """Returns the choice matching ``value``, or ``None``."""
        return next((entry for entry in self.choices if entry.value == value), None)


class PreferenceButton(discord.ui.Button["PreferencesPanel"]):
    """A button that hands its press to the panel via ``action``."""

    def __init__(
        self,
        *,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji)
        self.action: str = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the press to the owning panel."""
        view: Optional[PreferencesPanel] = self.view
        if view is None:
            return
        await view._dispatch(interaction=interaction, action=self.action)  # noqa: SLF001 - the panel owns this button.


class PreferenceSelect(discord.ui.Select["PreferencesPanel"]):
    """The picker for one cog-declared preference, handing its choice to the panel.

    .. note::
        A select cannot be a ``Section`` accessory, so a choice shows as text with this beneath it.

    """

    def __init__(self, *, preference: Preference, current: str) -> None:
        super().__init__(
            placeholder=preference.label[:SELECT_TEXT_SIZE],
            options=[
                discord.SelectOption(
                    label=choice.label[:SELECT_TEXT_SIZE],
                    value=choice.value,
                    description=choice.summary[:SELECT_TEXT_SIZE] or None,
                    # Discord shows the defaulted option in place of the placeholder.
                    default=choice.value == current,
                )
                for choice in preference.choices
            ],
        )
        self.preference: Preference = preference

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the choice to the owning panel."""
        view: Optional[PreferencesPanel] = self.view
        if view is None:
            return
        action: str = f"choose:{self.preference.key}:{self.values[0]}"
        await view._dispatch(interaction=interaction, action=action)  # noqa: SLF001 - the panel owns this select.


class PreferencesPanel(discord.ui.LayoutView):
    """A user's preferences panel — a ``Section`` per column with its toggle beside it.

    Not persistent; ``/preferences`` is cheap to re-run.

    .. warning::
        A Components V2 message cannot carry ``content`` or ``embeds``.

    """

    @classmethod
    async def build(cls, *, cog: Preferences, user: Union[User, discord.Member], settings: UserSettings) -> PreferencesPanel:
        """Reads cog-declared values, then builds the panel.

        Parameters
        ----------
        cog: :class:`Preferences`
            The owning cog.
        user: :class:`Union[User, discord.Member]`
            Whose preferences are shown.
        settings: :class:`UserSettings`
            The switch row, already read.

        Returns
        -------
        :class:`PreferencesPanel`
            The built panel.

        """
        return cls(cog=cog, user=user, settings=settings, values=await cog.declared_values(user=user))

    def __init__(
        self,
        *,
        cog: Preferences,
        user: Union[User, discord.Member],
        settings: UserSettings,
        values: Optional[dict[str, str]] = None,
    ) -> None:
        # Panel timeout matches `delete_after`; buttons die with the message.
        super().__init__(timeout=cog.message_timeout)
        self.cog: Preferences = cog
        self.user: Union[User, discord.Member] = user
        self.settings: UserSettings = settings
        self.options: list[str] = [key for key in settings.keys() if key not in Cog.settings_excluded_keys]  # noqa: SIM118 - sqlite3.Row, not a dict.
        # Read off loaded cogs; a preference arrives with its extension and leaves with it.
        self.preferences: list[Preference] = list(cog.registry())
        self.values: dict[str, str] = values if values is not None else {}

        container: discord.ui.Container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(
            discord.ui.Section(
                f"## {cog.emoji_table.kuma_peak} {user.display_name} Preferences\n-# These are yours, not a server's.",
                accessory=discord.ui.Thumbnail(media=user.display_avatar.url),
            ),
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        for key in self.options:
            summary: str = SETTING_SUMMARIES.get(key, "")
            container.add_item(
                discord.ui.Section(
                    f"**{setting_label(key)}**" + (f"\n-# {summary}" if summary else ""),
                    accessory=self._toggle(key=key, on=bool(settings[key])),  # type: ignore - the key came off the row itself.
                ),
            )

        # Cog-declared preferences, under the same heading.
        for preference in self.preferences:
            body: str = f"**{preference.label}**" + (f"\n-# {preference.summary}" if preference.summary else "")
            container.add_item(discord.ui.TextDisplay(body))
            container.add_item(
                discord.ui.ActionRow().add_item(
                    PreferenceSelect(preference=preference, current=self.values.get(preference.key, preference.default)),
                ),
            )

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {self._summary()}"))
        self.add_item(container)

        # Outside the container — Reset acts on the panel, not a setting in it.
        self.add_item(
            discord.ui.ActionRow().add_item(
                PreferenceButton(action="reset", label="Reset", emoji="🔄", style=discord.ButtonStyle.danger),
            ),
        )

    def _summary(self) -> str:
        """Returns the counts line under the list."""
        on: int = sum(1 for key in self.options if bool(self.settings[key]))  # type: ignore - the keys came off the row itself.
        parts: list[str] = [f"{len(self.options)} switches · {on} on, {len(self.options) - on} off"]
        if self.preferences:
            parts.append(f"{len(self.preferences)} choice{'s' if len(self.preferences) > 1 else ''}")
        return " · ".join(parts)

    def _toggle(self, *, key: str, on: bool) -> PreferenceButton:
        """Returns the on/off accessory for one setting."""
        return PreferenceButton(
            action=f"toggle:{key}",
            label="On" if on is True else "Off",
            emoji="✔️" if on is True else "✖️",
            style=discord.ButtonStyle.success if on is True else discord.ButtonStyle.secondary,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone but the person the panel was opened for."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                content=f"Those preferences aren't yours! {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    async def _dispatch(self, *, interaction: discord.Interaction, action: str) -> None:
        """Applies a press and re-renders the panel in place."""
        try:
            if action.startswith("toggle:"):
                key: str = action.removeprefix("toggle:")
                # The key travels through a custom ID; `set_user_settings` still validates it.
                updated: Optional[UserSettings] = await self.cog.set_user_settings(
                    user=self.user,
                    setting=key,
                    value=not bool(self.settings[key]),  # type: ignore - the key came off the row.
                )
            elif action.startswith("choose:"):
                # Choice goes to `user_preferences`; pass the switch row back unchanged.
                chosen, _, value = action.removeprefix("choose:").partition(":")
                updated = self.settings if await self.cog.set_value(user=self.user, key=chosen, value=value) else None
            elif action == "reset":
                updated = await self.cog.reset_user_settings(user=self.user)
            else:
                return
        except (ConnectionError, ValueError):
            updated = None

        if updated is None:
            await interaction.response.send_message(
                content=f"We encountered an error saving that. {self.cog.emoji_table.kuma_crying}",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(view=await PreferencesPanel.build(cog=self.cog, user=self.user, settings=updated))


class Preferences(Cog):
    """A Discord user's own settings, as opposed to a guild's.

    ``settings`` is about a place, ``preferences`` is about a person.

    """

    # Allowed column names for UPDATE — guards against SQL injection via the setting parameter.
    _USER_SETTING_COLUMNS: frozenset[str] = frozenset(USER_SETTING_DEFAULTS)

    preferences = app_commands.Group(
        name="preferences",
        description="See your own settings. These are yours, not a server's.",
        guild_only=True,
    )

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(USER_SETTINGS_SETUP_SQL)
            await conn.execute(USER_PREFERENCES_SETUP_SQL)
            # await self.migrate(conn=conn)

    @staticmethod
    async def migrate(*, conn: Connection) -> None:
        """Adds any column declared in ``USER_SETTING_DEFAULTS`` that the live table is missing.

        Parameters
        ----------
        conn: :class:`Connection`
            The connection to migrate on; taken so this runs inside the caller's transaction.

        """
        rows: list[Row] = await conn.fetchall("""PRAGMA table_info(user_settings)""")
        existing: set[str] = {row["name"] for row in rows}

        for column, default in USER_SETTING_DEFAULTS.items():
            if column in existing:
                continue
            # The column name is one of our own keys, never user input; SQLite cannot parameterise DDL.
            await conn.execute(f"""ALTER TABLE user_settings ADD COLUMN {column} INT NOT NULL DEFAULT {int(default)}""")
            LOGGER.info("<%s.%s> | Added the missing preference column. | Column: %s", __class__.__name__, "migrate", column)

    async def get_user_settings(self, user: Union[User, discord.Member]) -> Optional[UserSettings]:
        """Get the preferences belonging to a Discord user, creating them on first look.

        Parameters
        ----------
        user: :class:`Union[User, discord.Member]`
            The Discord user object.

        Returns
        -------
        :class:`Optional[UserSettings]`
            The user's preferences.

        Raises
        ------
        :exc:`ConnectionError`
            Unable to connect to the database.

        """
        try:
            async with self.bot.pool.acquire() as conn:
                result: Optional[UserSettings] = await conn.fetchone("""SELECT * FROM user_settings WHERE userid = ?""", user.id)  # type: ignore - I know the dataset because of above.
                if result is None:
                    return await self.set_user_settings(user=user, default=True)
                return result
        except Exception as e:
            LOGGER.exception(
                "<%s.%s> | We encountered an error connecting to the database. | UserID: %s",
                __class__.__name__,
                "get_user_settings",
                user.id,
                exc_info=e,
            )
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None

    async def set_user_settings(
        self,
        user: Union[User, discord.Member],
        setting: Optional[str] = None,
        value: bool = False,
        default: bool = False,
    ) -> Optional[UserSettings]:
        """Set or update the preferences of the provided Discord user.

        Parameters
        ----------
        user: :class:`Union[User, discord.Member]`
            The Discord user object.
        setting: :class:`Optional[str]`, optional
            Column name to update; must be in :attr:`_USER_SETTING_COLUMNS`, by default None.
        value: :class:`bool`, optional
            The value to write, by default False.
        default: :class:`bool`, optional
            Insert a default row instead of updating, by default False.

        Returns
        -------
        :class:`Optional[UserSettings]`
            The user's preferences.

        Raises
        ------
        :exc:`ValueError`
            ``setting`` is not a valid column name.
        :exc:`ConnectionError`
            Unable to connect to the database.

        """
        if default is False and (setting is None or setting not in self._USER_SETTING_COLUMNS):
            msg = f"setting must be one of {self._USER_SETTING_COLUMNS!r}, got {setting!r}."
            raise ValueError(msg)

        try:
            async with self.bot.pool.acquire() as conn:
                if default is True:
                    # `DO NOTHING` rather than a bare INSERT; two commands racing on someone's
                    # first use would otherwise have the loser raise on the UNIQUE constraint.
                    result: Optional[UserSettings] = await conn.fetchone(
                        """INSERT INTO user_settings(userid) VALUES(?)
                           ON CONFLICT (userid) DO NOTHING RETURNING *""",
                        user.id,
                    )  # pyright: ignore[reportAssignmentType]
                    if result is None:
                        result = await conn.fetchone("""SELECT * FROM user_settings WHERE userid = ?""", user.id)  # pyright: ignore[reportAssignmentType]
                else:
                    result = await conn.fetchone(
                        f"""UPDATE user_settings SET {setting} = ? WHERE userid = ? RETURNING *""",  # noqa: S608 - column name validated above
                        value,
                        user.id,
                    )  # pyright: ignore[reportAssignmentType]
                return result
        except Exception as e:
            LOGGER.exception(
                "<%s.%s> | We encountered an error connecting to the database. | UserID: %s",
                __class__.__name__,
                "set_user_settings",
                user.id,
                exc_info=e,
            )
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None

    async def reset_user_settings(self, user: Union[User, discord.Member]) -> Optional[UserSettings]:
        """Drop the Discord user's rows, then re-create the switches at their defaults.

        Parameters
        ----------
        user: :class:`Union[User, discord.Member]`
            The Discord user object.

        Returns
        -------
        :class:`Optional[UserSettings]`
            The user's preferences, back at their defaults.

        Raises
        ------
        :exc:`ConnectionError`
            Unable to connect to the database.

        """
        try:
            async with self.bot.pool.acquire() as conn:
                await conn.execute("""DELETE FROM user_settings WHERE userid = ?""", user.id)
                await conn.execute("""DELETE FROM user_preferences WHERE userid = ?""", user.id)
        except Exception as e:
            LOGGER.exception(
                "<%s.%s> | We encountered an error connecting to the database. | UserID: %s",
                __class__.__name__,
                "reset_user_settings",
                user.id,
                exc_info=e,
            )
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None
        return await self.get_user_settings(user=user)

    async def enabled(self, user: Union[User, discord.Member], setting: str) -> bool:
        """Returns one preference as a bool, falling back to the column default when anything fails.

        Parameters
        ----------
        user: :class:`Union[User, discord.Member]`
            The Discord user object.
        setting: :class:`str`
            The column name to read.

        Returns
        -------
        :class:`bool`
            The stored value, or the column's default when there is no row or the read failed.

        """
        default: bool = USER_SETTING_DEFAULTS.get(setting, False)
        try:
            result: Optional[UserSettings] = await self.get_user_settings(user=user)
        except ConnectionError:
            return default
        if result is None:
            return default
        return bool(result[setting])  # type: ignore - the caller passes a column name.

    def registry(self) -> Iterator[Preference]:
        """Yields every preference declared by a currently loaded cog."""
        for cog in self.bot.cogs.values():
            for preference in getattr(cog, "__preferences__", ()):
                if isinstance(preference, Preference):
                    yield preference

    def find(self, key: str) -> Optional[Preference]:
        """Returns the preference registered under ``key``, or ``None``."""
        return next((entry for entry in self.registry() if entry.key == key), None)

    async def declared_values(self, *, user: discord.abc.Snowflake) -> dict[str, str]:
        """Returns every declared preference's current value for a user, defaults filled in.

        Parameters
        ----------
        user: :class:`discord.abc.Snowflake`
            Anything carrying the Discord ID.

        Returns
        -------
        :class:`dict[str, str]`
            Key to value, one entry per preference a loaded cog declares.

        """
        values: dict[str, str] = {preference.key: preference.default for preference in self.registry()}
        try:
            async with self.bot.pool.acquire() as conn:
                rows: list[Row] = await conn.fetchall("""SELECT pref_key, value FROM user_preferences WHERE userid = ?""", user.id)
        except Exception as e:  # noqa: BLE001 - a preference read must never be why a panel fails to open.
            LOGGER.warning("<%s.%s> | Could not read the stored preferences. | Error: %s", __class__.__name__, "declared_values", e)
            return values

        # Only keys a loaded cog still declares; unloaded extension rows stay in the table.
        values.update({row["pref_key"]: str(row["value"]) for row in rows if row["pref_key"] in values})
        return values

    async def value(self, *, user: discord.abc.Snowflake, key: str) -> str:
        """Reads one cog-declared preference, answering its declared default when nothing is stored.

        Parameters
        ----------
        user: :class:`discord.abc.Snowflake`
            Anything carrying the Discord ID.
        key: :class:`str`
            The :attr:`Preference.key` to read.

        Returns
        -------
        :class:`str`
            The stored value, or the declared default.

        """
        declared: Optional[Preference] = self.find(key)
        default: str = declared.default if declared is not None else ""
        try:
            async with self.bot.pool.acquire() as conn:
                row: Optional[Row] = await conn.fetchone(
                    """SELECT value FROM user_preferences WHERE userid = ? AND pref_key = ?""",
                    user.id,
                    key,
                )
        except Exception as e:  # noqa: BLE001 - a preference read must never be why another cog's command fails.
            LOGGER.warning("<%s.%s> | Could not read the preference. | Key: %s | Error: %s", __class__.__name__, "value", key, e)
            return default

        if row is None:
            return default
        stored: str = str(row["value"])
        # A cog that changed its choices should not get back a value it no longer offers.
        if declared is not None and declared.choices and declared.choice(stored) is None:
            return default
        return stored

    async def set_value(self, *, user: discord.abc.Snowflake, key: str, value: str) -> bool:
        """Stores one cog-declared preference, refusing a value the declaration does not offer.

        Parameters
        ----------
        user: :class:`discord.abc.Snowflake`
            Anything carrying the Discord ID.
        key: :class:`str`
            The :attr:`Preference.key` to write.
        value: :class:`str`
            One of the declared :attr:`Preference.choices`.

        Returns
        -------
        :class:`bool`
            Whether it was stored.

        """
        declared: Optional[Preference] = self.find(key)
        if declared is None or (declared.choices and declared.choice(value) is None):
            LOGGER.warning(
                "<%s.%s> | Refused a value no loaded cog declares. | Key: %s | Value: %s",
                __class__.__name__,
                "set_value",
                key,
                value,
            )
            return False

        try:
            async with self.bot.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_preferences (userid, pref_key, value) VALUES (?, ?, ?)
                       ON CONFLICT (userid, pref_key) DO UPDATE SET value = excluded.value""",
                    user.id,
                    key,
                    value,
                )
        except Exception as e:
            LOGGER.exception("<%s.%s> | Could not store the preference. | Key: %s", __class__.__name__, "set_value", key, exc_info=e)
            return False
        return True

    @preferences.command(name="view")
    async def view_preference(self, interaction: discord.Interaction) -> None:
        """See your own settings. These are yours, not a server's."""
        result: Optional[UserSettings] = await self.get_user_settings(user=interaction.user)
        if result is not None:
            await interaction.response.send_message(
                view=await PreferencesPanel.build(cog=self, user=interaction.user, settings=result),
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            return

        await interaction.response.send_message(
            content=f"We encountered an error reading your preferences. {self.emoji_table.kuma_crying}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @preferences.command(name="set")
    @app_commands.choices(option=Cog.settings_choices(UserSettings))
    async def set_preference(self, interaction: discord.Interaction, option: Choice[str], value: bool) -> None:
        """Change one of your settings."""
        updated: Optional[UserSettings] = await self.set_user_settings(user=interaction.user, setting=option.value, value=value)
        if updated is not None:
            await interaction.response.send_message(
                view=await PreferencesPanel.build(cog=self, user=interaction.user, settings=updated),
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            return

        await interaction.response.send_message(
            content=f"We encountered an error updating the database. {self.emoji_table.kuma_crying}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @preferences.command(name="reset")
    async def reset_preferences(self, interaction: discord.Interaction) -> None:
        """Put all of your settings back to their defaults."""
        result: Optional[UserSettings] = await self.reset_user_settings(user=interaction.user)
        if result is not None:
            await interaction.response.send_message(
                view=await PreferencesPanel.build(cog=self, user=interaction.user, settings=result),
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            return

        await interaction.response.send_message(
            content=f"Could not reset your preferences. {self.emoji_table.kuma_crying}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(Preferences(bot=bot))
