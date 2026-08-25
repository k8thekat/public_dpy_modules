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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, Optional, TypedDict, Union

import discord
from asqlite import Connection
from discord import Message, User, app_commands
from discord.app_commands import Choice
from discord.ext import commands

from kuma_kuma import Kuma_Kuma
from utils import KumaCog as Cog, KumaContext as Context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Row

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


# Column -> what :meth:`Preferences.enabled` answers with when there is no row yet or the read
# failed, mirrored from the `DEFAULT` clauses above. This is also the allowlist `set_user_settings`
# validates against, so a new preference is declared in exactly two places: the schema and here.
#
# .. note::
#     `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a column added
#     here is also added to a live table by :meth:`Preferences.migrate` on the next load.
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


# Cog-declared preferences live beside `user_settings` rather than inside it. One declared by a cog is
# that cog's to name and to type; a column per declaration would put every extension's vocabulary into
# core's schema and leave it there once the extension was gone.
USER_PREFERENCES_SETUP_SQL: str = """
CREATE TABLE IF NOT EXISTS user_preferences (
    userid INTEGER NOT NULL,
    pref_key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (userid, pref_key))
"""

# Discord's ceiling on a select option's label and description. A declaration longer than this is cut
# rather than refused; a panel that will not render is a worse answer than a summary that stops short.
SELECT_TEXT_SIZE: int = 100


def setting_label(key: str) -> str:
    """Turns a column name into the name the panel shows (`hint_style_block` -> `Hint Style Block`)."""
    return key.replace("_", " ").title()


class PreferenceChoice(NamedTuple):
    """One selectable value of a cog-declared preference.

    Attributes
    ----------
    value: :class:`str`
        What is stored, and what the owning cog compares against.
    label: :class:`str`
        The name shown on the select.
    summary: :class:`str`
        The line beneath the name, saying what picking it does.

    """

    value: str
    label: str
    summary: str = ""


@dataclass(frozen=True)
class Preference:
    """One setting a cog offers, declared in that cog's `__preferences__`.

    The switches on `user_settings` are core's own and are declared in :data:`USER_SETTING_DEFAULTS`.
    This is the other kind: a setting that belongs to one cog, has more than two states, and should
    arrive and leave with the extension that owns it::

        class ClaudeCog(Cog):
            __preferences__ = (
                Preference(
                    key="claude.verbosity",
                    label="Session verbosity",
                    default="default",
                    choices=(PreferenceChoice(value="silent", label="Silent"), ...),
                ),
            )

    Attributes
    ----------
    key: :class:`str`
        Stable identifier, stored in the database. Namespace it by cog (`claude.verbosity`); changing
        it puts everyone back on the default.
    label: :class:`str`
        Short name for the `/preferences` panel.
    summary: :class:`str`
        The line beneath the name.
    default: :class:`str`
        What :meth:`Preferences.value` answers with when nothing is stored.
    choices: :class:`tuple[PreferenceChoice, ...]`
        The values on offer, in the order the select lists them.

    """

    key: str
    label: str
    summary: str = field(default="")
    default: str = field(default="")
    choices: tuple[PreferenceChoice, ...] = field(default=())

    def choice(self, value: str) -> Optional[PreferenceChoice]:
        """Returns the choice `value` names, or `None` when it is not one this preference offers."""
        return next((entry for entry in self.choices if entry.value == value), None)


class PreferenceButton(discord.ui.Button):
    """A button that hands its press to the panel that built it, via `action`.

    The panel is built imperatively — a row per column the database hands back — so there is no fixed
    layout to declare with `@discord.ui.button`, and a plain Button has a no-op callback.
    """

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
        # `self.view` is set by discord.py when the item is added, at any nesting depth.
        view: Optional[PreferencesPanel] = self.view  # type: ignore[assignment]
        if view is None:
            return
        await view._dispatch(interaction=interaction, action=self.action)  # noqa: SLF001 - the panel owns this button.


class PreferenceSelect(discord.ui.Select):
    """The picker for one cog-declared preference, handing its choice to the panel that built it.

    .. note::
        A select cannot be a `Section` accessory — the API takes only a button or a thumbnail there —
        so a choice preference is a text display with this on the row beneath it, rather than the one
        line a switch gets.

    """

    def __init__(self, *, preference: Preference, current: str) -> None:
        super().__init__(
            placeholder=preference.label[:SELECT_TEXT_SIZE],
            options=[
                discord.SelectOption(
                    label=choice.label[:SELECT_TEXT_SIZE],
                    value=choice.value,
                    description=choice.summary[:SELECT_TEXT_SIZE] or None,
                    # What marks the stored value on screen; Discord shows the defaulted option in
                    # place of the placeholder, so the panel needs no separate "currently" line.
                    default=choice.value == current,
                )
                for choice in preference.choices
            ],
        )
        self.preference: Preference = preference

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the choice to the owning panel."""
        view: Optional[PreferencesPanel] = self.view  # type: ignore[assignment]
        if view is None:
            return
        action: str = f"choose:{self.preference.key}:{self.values[0]}"
        await view._dispatch(interaction=interaction, action=action)  # noqa: SLF001 - the panel owns this select.


class PreferencesPanel(discord.ui.LayoutView):
    """A Discord user's own preferences, a `Section` per column with its on/off button beside it.

    The rows come from the row the database handed back rather than a list kept here, so a column
    added to `user_settings` shows up on its own; `SETTING_SUMMARIES` only decides whether it gets a
    line of explanation under its name.

    Not persistent, like `HintsPanel` and unlike `SessionPanel` in `claude.py` — `/preferences` is
    cheap to re-run.

    .. warning::
        A Components V2 message cannot carry `content` or `embeds`.

    """

    # The `Preferences` and `PreferencesPanel` annotations are quoted throughout because the cog is
    # defined below this and the file has no `from __future__ import annotations` — nor can it take
    # one, as discord.py resolves a hybrid command's annotations against the module at runtime and a
    # `Choice` hidden in a type-checking block would not be there to resolve. A body annotation never
    # evaluates, so those are left bare.
    @classmethod
    async def build(cls, *, cog: "Preferences", user: Union[User, discord.Member], settings: UserSettings) -> "PreferencesPanel":
        """Reads the cog-declared values a panel needs, then builds it.

        A constructor cannot await and those values live in their own table; this is what keeps every
        call site from having to know that.

        Parameters
        ----------
        cog: :class:`Preferences`
            The owning cog, for the reads and for whatever a press calls back into.
        user: :class:`Union[User, discord.Member]`
            Whose preferences are shown, and the only person the panel answers.
        settings: :class:`UserSettings`
            The switch row, already read by whatever is opening the panel.

        Returns
        -------
        :class:`PreferencesPanel`
            The built panel.

        """
        return cls(cog=cog, user=user, settings=settings, values=await cog.declared_values(user=user))

    def __init__(
        self,
        *,
        cog: "Preferences",
        user: Union[User, discord.Member],
        settings: UserSettings,
        values: Optional[dict[str, str]] = None,
    ) -> None:
        # The panel dies with the message that carries it: every reply is sent with
        # `delete_after=self.message_timeout`, so taking the timeout from the same place means the
        # buttons never sit dead on a message still on screen, nor outlive one that is gone.
        super().__init__(timeout=cog.message_timeout)
        self.cog: Preferences = cog
        self.user: Union[User, discord.Member] = user
        self.settings: UserSettings = settings
        # `settings_excluded_keys` drops the bookkeeping columns (`id`, `userid`); they are stored on
        # a person but they are not one of their preferences.
        self.options: list[str] = [key for key in settings.keys() if key not in Cog.settings_excluded_keys]  # noqa: SIM118 - It thinks it's a dict; when it's a sqlite3.Row Tuple object.
        # Read off the loaded cogs rather than held, so a preference arrives with its extension and
        # leaves with it. Empty when :meth:`build` was bypassed, which only the switches then show.
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

        # The cog-declared half, under the same heading — they are all one person's settings and
        # splitting them by where they happen to be stored would be an implementation detail on screen.
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

        # Outside the container: Reset acts *on* the panel rather than being a setting of it, and the
        # container's border is what makes that read.
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
            label="On" if on else "Off",
            emoji="✔️" if on else "✖️",
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
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
                # The key came off our own row, but it travels through a custom ID to get back here,
                # so `set_user_settings` validating it against the column list still earns its keep.
                updated: Optional[UserSettings] = await self.cog.set_user_settings(
                    user=self.user,
                    setting=key,
                    value=not bool(self.settings[key]),  # type: ignore - see above.
                )
            elif action.startswith("choose:"):
                # A choice is stored in `user_preferences`, so the switch row is untouched; it is
                # passed straight back so the rebuilt panel still shows the switches beside it.
                chosen, _, value = action.removeprefix("choose:").partition(":")
                updated = self.settings if await self.cog.set_value(user=self.user, key=chosen, value=value) else None
            elif action == "reset":
                updated = await self.cog.reset_user_settings(user=self.user)
            else:
                return
        except (ConnectionError, ValueError):
            updated = None

        if updated is None:
            # The panel on screen still shows what is stored, so leave it be and say so alongside.
            await interaction.response.send_message(
                content=f"We encountered an error saving that. {self.cog.emoji_table.kuma_crying}",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(view=await PreferencesPanel.build(cog=self.cog, user=self.user, settings=updated))


class Preferences(Cog):
    """A Discord user's own settings, as opposed to a guild's.

    The split from `moderator`'s `settings` is not a matter of taste. Discord applies
    `default_permissions` per *top level* command and it cannot vary by subcommand, so guild settings
    (admin, guild only) and user settings (open to everyone, usable in DMs) cannot live under one
    command no matter how they are nested.

    The rule that keeps them apart when reading: `settings` is about a place, `preferences` is about
    a person.
    """

    # Allowed column names for UPDATE — guards against SQL injection via the setting parameter.
    # Taken from `USER_SETTING_DEFAULTS` so the two can never disagree about what a setting is.
    _USER_SETTING_COLUMNS: frozenset[str] = frozenset(USER_SETTING_DEFAULTS)

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(USER_SETTINGS_SETUP_SQL)
            await conn.execute(USER_PREFERENCES_SETUP_SQL)
            await self.migrate(conn=conn)

    @staticmethod
    async def migrate(*, conn: Connection) -> None:
        """Adds any preference declared in `USER_SETTING_DEFAULTS` that the live table is missing.

        `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already exists, so a column
        added to the schema above would never reach a database that predates it — every read of it
        would raise and take `/preferences` down with it. Reconciling here means adding a preference
        is still a one line change.

        Parameters
        ----------
        conn: :class:`Connection`
            The connection to migrate on; taken rather than acquired so this runs inside the
            caller's transaction.

        """
        rows: list[Row] = await conn.fetchall("""PRAGMA table_info(user_settings)""")
        existing: set[str] = {row["name"] for row in rows}

        for column, default in USER_SETTING_DEFAULTS.items():
            if column in existing:
                continue
            # The column name is one of our own keys, never user input; the value is an int literal
            # we built from a bool. SQLite cannot parameterise DDL, so this has to be interpolated.
            await conn.execute(f"""ALTER TABLE user_settings ADD COLUMN {column} INT NOT NULL DEFAULT {int(default)}""")
            LOGGER.info("<%s.%s> | Added the missing preference column. | Column: %s", __class__.__name__, "migrate", column)

    async def get_user_settings(self, user: User | discord.Member) -> UserSettings | None:
        """Get the preferences belonging to a Discord user, creating them on first look.

        Parameters
        ----------
        user: :class:`User | discord.Member`
            The Discord user object.

        Returns
        -------
        :class:`UserSettings | None`
            The user's preferences.

        Raises
        ------
        :exc:`ConnectionError`
            Raises a connection error if unable to connect to the Database for any reason.

        """
        try:
            async with self.bot.pool.acquire() as conn:
                res: UserSettings | None = await conn.fetchone("""SELECT * FROM user_settings WHERE userid = ?""", user.id)  # type: ignore - I know the dataset because of above.
                if res is None:
                    return await self.set_user_settings(user=user, default=True)
                return res
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
        user: User | discord.Member,
        setting: str | None = None,
        value: bool = False,
        default: bool = False,
    ) -> UserSettings | None:
        """Set or update the preferences of the provided Discord user.

        Parameters
        ----------
        user: :class:`User | discord.Member`
            The Discord user object.
        setting: :class:`str`, optional
            The column name in the user_settings table to update (e.g. ``"hints_enabled"``).
            Must be one of :attr:`_USER_SETTING_COLUMNS`. Required when ``default`` is False.
        value: :class:`bool`, optional
            The value to write to ``setting``, by default False.
        default: :class:`bool`, optional
            When True, inserts a new row with default values for the user instead of updating an
            existing one. Use this for the first time we see someone.

        Returns
        -------
        :class:`UserSettings | None`
            The Discord user's preferences.

        Raises
        ------
        :exc:`ValueError`
            If ``default`` is False and ``setting`` is None or not a valid column name.
        :exc:`ConnectionError`
            If we are unable to connect to the Database.

        """
        if not default and (setting is None or setting not in self._USER_SETTING_COLUMNS):
            msg = f"setting must be one of {self._USER_SETTING_COLUMNS!r}, got {setting!r}."
            raise ValueError(msg)

        try:
            async with self.bot.pool.acquire() as conn:
                if default:
                    # `DO NOTHING` rather than a bare INSERT. Two commands racing on someone's very
                    # first use would otherwise have the loser raise on the UNIQUE constraint.
                    data: UserSettings | None = await conn.fetchone(
                        """INSERT INTO user_settings(userid) VALUES(?)
                           ON CONFLICT (userid) DO NOTHING RETURNING *""",
                        user.id,
                    )  # pyright: ignore[reportAssignmentType]
                    if data is None:
                        data = await conn.fetchone("""SELECT * FROM user_settings WHERE userid = ?""", user.id)  # pyright: ignore[reportAssignmentType]
                else:
                    data = await conn.fetchone(
                        f"""UPDATE user_settings SET {setting} = ? WHERE userid = ? RETURNING *""",  # noqa: S608 - column name validated above
                        value,
                        user.id,
                    )  # pyright: ignore[reportAssignmentType]
                return data
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

    async def reset_user_settings(self, user: User | discord.Member) -> UserSettings | None:
        """Drop the Discord user's rows, then let :meth:`get_user_settings` re-create the switches.

        Deleting rather than writing each column back by hand means a column added to `user_settings`
        later comes back at whatever default it declares, with nothing to keep in step here. The
        cog-declared preferences go the same way and for the same reason: an absent row *is* the
        default, so there is nothing to write back.

        Parameters
        ----------
        user: :class:`User | discord.Member`
            The Discord user object.

        Returns
        -------
        :class:`UserSettings | None`
            The Discord user's preferences, back at their defaults.

        Raises
        ------
        :exc:`ConnectionError`
            If we are unable to connect to the Database.

        """
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM user_settings WHERE userid = ?""", user.id)
            await conn.execute("""DELETE FROM user_preferences WHERE userid = ?""", user.id)
        return await self.get_user_settings(user=user)

    async def enabled(self, user: User | discord.Member, setting: str) -> bool:
        """Returns one preference as a bool, falling back to the column default when anything fails.

        This is what other cogs call. A preference lookup must never be the reason another cog's
        command fails, so a database error answers with the default rather than raising.

        Parameters
        ----------
        user: :class:`User | discord.Member`
            The Discord user object.
        setting: :class:`str`
            The column name to read.

        Returns
        -------
        :class:`bool`
            The stored value, or the column's default when there is no row or the read failed.

        """
        # From the table rather than a hardcoded comparison. The old `setting == "hints_enabled"`
        # answered False for every other preference, so anything defaulting to on would have read
        # as off the moment the database hiccuped.
        default: bool = USER_SETTING_DEFAULTS.get(setting, False)
        try:
            res: UserSettings | None = await self.get_user_settings(user=user)
        except ConnectionError:
            return default
        if res is None:
            return default
        return bool(res[setting])  # type: ignore - the caller passes a column name.

    def registry(self) -> "Iterator[Preference]":
        """Yields every preference declared by a currently loaded cog.

        Walked from the loaded cogs on each call, as `HintsCog.registry` is, so a preference arrives
        with its extension and leaves with it and there is nothing to keep in step.
        """
        for cog in self.bot.cogs.values():
            for preference in getattr(cog, "__preferences__", ()):
                if isinstance(preference, Preference):
                    yield preference

    def find(self, key: str) -> Optional[Preference]:
        """Returns the preference registered under `key`, or `None` when no loaded cog declares it."""
        return next((entry for entry in self.registry() if entry.key == key), None)

    async def declared_values(self, *, user: discord.abc.Snowflake) -> dict[str, str]:
        """Returns every declared preference's current value for a user, defaults filled in.

        Parameters
        ----------
        user: :class:`discord.abc.Snowflake`
            Anything carrying the Discord ID. Wider than :meth:`enabled` takes on purpose — a cog
            reading a preference for a session it owns has the ID and not always the user object.

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

        # Only keys a loaded cog still declares. A row left behind by an unloaded extension stays in
        # the table — it is that extension's, not ours to drop — but nothing here can render it.
        values.update({row["pref_key"]: str(row["value"]) for row in rows if row["pref_key"] in values})
        return values

    async def value(self, *, user: discord.abc.Snowflake, key: str) -> str:
        """Reads one cog-declared preference, answering its declared default when nothing is stored.

        What other cogs call, and it never raises for the same reason :meth:`enabled` does not: a
        preference lookup must not be why another cog's command fails.

        Parameters
        ----------
        user: :class:`discord.abc.Snowflake`
            Anything carrying the Discord ID.
        key: :class:`str`
            The :attr:`Preference.key` to read.

        Returns
        -------
        :class:`str`
            The stored value, or the declared default when there is none, the read failed, or the
            declaration no longer offers what was stored.

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
        # A cog that has changed its choices since this was written should not be handed back a value
        # it no longer has a path for; the default is the one value it must always be able to take.
        if declared is not None and declared.choices and declared.choice(stored) is None:
            return default
        return stored

    async def set_value(self, *, user: discord.abc.Snowflake, key: str, value: str) -> bool:
        """Stores one cog-declared preference, refusing a value the declaration does not offer.

        Validated against the registry for the reason :meth:`set_user_settings` validates against the
        column list: both arrive through a component's custom ID, which is not ours by the time it
        comes back.

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

    @commands.hybrid_group(name="preferences", invoke_without_command=True)
    async def preferences(self, context: Context) -> Message:
        """See your own settings. These are yours, not a server's."""
        res: UserSettings | None = await self.get_user_settings(user=context.author)
        if res is not None:
            return await context.send(
                view=await PreferencesPanel.build(cog=self, user=context.author, settings=res),
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        return await context.send(
            content=f"We encountered an error reading your preferences. {self.emoji_table.kuma_crying}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @preferences.command(name="view")
    async def view_preference(self, context: Context) -> Message:
        """See your own settings. These are yours, not a server's."""
        return await self.preferences(context)

    @preferences.command(name="set")
    @app_commands.choices(option=Cog.settings_choices(UserSettings))
    async def set_preference(self, context: Context, option: Choice[str], value: bool) -> Message:
        """Change one of your settings."""
        updated: UserSettings | None = await self.set_user_settings(user=context.author, setting=option.value, value=value)
        if updated is not None:
            return await context.send(
                view=await PreferencesPanel.build(cog=self, user=context.author, settings=updated),
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        return await context.send(
            content=f"We encountered an error updating the database. {self.emoji_table.kuma_crying}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @preferences.command(name="reset")
    async def reset_preferences(self, context: Context) -> Message:
        """Put all of your settings back to their defaults."""
        res: UserSettings | None = await self.reset_user_settings(user=context.author)
        if res is not None:
            return await context.send(
                view=await PreferencesPanel.build(cog=self, user=context.author, settings=res),
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        return await context.send(
            content=f"Reset your preferences. {self.emoji_table.kuma_star_eye}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(Preferences(bot=bot))
