"""Copyright (C) 2021-2026 Katelynn Cadwallader.

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

__author__ = "k8thekat"
__license__ = "GNU"
__version__ = "3.0.0"

import logging
import sqlite3
import time
from configparser import ConfigParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Optional

import discord
from a_sonarr_radarr import (
    DiskSpace,
    Health,
    MonitorType,
    QualityProfile,
    QueueRecord,
    RadarrAPI,
    RootFolder,
    Series,
    SeriesStatus,
    SignalRAction,
    SignalRMessage,
    SonarrAPI,
    SonarrError,
    SonarrValidationError,
    SystemStatus,
    to_size,
)
from discord import app_commands

from utils import KumaCog as Cog, PanelAccess, UnicodeTable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from sqlite3 import Row

    from kuma_kuma import Kuma_Kuma

LOGGER = logging.getLogger(__name__)

PANEL_TIMEOUT: float = 300.0

# Five rows keeps a page inside both budgets at once: the 40 component cap (each row is a Section, a
# TextDisplay and a Thumbnail) and the 4000 characters discord.py will not check for us.
LIBRARY_PER_PAGE: int = 5

# An overview runs to several paragraphs on TVDB/TMDB, and a panel that is mostly synopsis buries the
# numbers underneath it.
OVERVIEW_LIMIT: int = 300
QUEUE_LIMIT: int = 8
PROGRESS_WIDTH: int = 12


# region --- Sonarr constants ---

# Sonarr's own colour language: green is producing, grey has finished, gold has not started yet.
SERIES_STATUS_ACCENTS: dict[str, discord.Colour] = {
    SeriesStatus.continuing.value: discord.Colour.from_str("#4CAF50"),
    SeriesStatus.ended.value: discord.Colour.from_str("#607D8B"),
    SeriesStatus.upcoming.value: discord.Colour.from_str("#FFB300"),
    SeriesStatus.deleted.value: discord.Colour.from_str("#B71C1C"),
}

# The monitor choices worth offering; the full enum has thirteen and most are answers to questions
# nobody asks from Discord.
MONITOR_CHOICES: tuple[tuple[MonitorType, str, str], ...] = (
    (MonitorType.all, "All episodes", "Every episode, aired or not."),
    (MonitorType.future, "Future episodes", "Only episodes that have not aired yet."),
    (MonitorType.missing, "Missing episodes", "Aired episodes with no file."),
    (MonitorType.first_season, "First season", "Pilot season only, to try it out."),
    (MonitorType.latest_season, "Latest season", "Just the current season."),
    (MonitorType.none, "Nothing", "Add it, but do not download anything."),
)
# endregion

# region --- Radarr constants ---

# Radarr's movie statuses: green is released, gold is in theaters, blue is announced.
MOVIE_STATUS_ACCENTS: dict[str, discord.Colour] = {
    "released": discord.Colour.from_str("#4CAF50"),
    "inCinemas": discord.Colour.from_str("#FFB300"),
    "announced": discord.Colour.from_str("#2196F3"),
    "deleted": discord.Colour.from_str("#B71C1C"),
    "tba": discord.Colour.from_str("#9E9E9E"),
}

# Radarr's `minimumAvailability` choices for when a movie is eligible for grabbing.
AVAILABILITY_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("released", "Released", "After physical or digital release."),
    ("inCinemas", "In Cinemas", "When it reaches theaters."),
    ("announced", "Announced", "As soon as it is announced."),
)

# Title-casing `inCinemas` gives `Incinemas`, so a lookup table is the only clean path.
MOVIE_STATUS_DISPLAY: dict[str, str] = {
    "released": "Released",
    "inCinemas": "In Cinemas",
    "announced": "Announced",
    "deleted": "Deleted",
    "tba": "TBA",
}
# endregion

# Shared accent for a lookup result that is not yet added to either library.
UNADDED_ACCENT: discord.Colour = discord.Colour.from_str("#5865F2")

# Notification panel accent; separate from status accents.
NOTIFICATION_ACCENT: discord.Colour = discord.Colour.from_str("#00BCD4")

NOTIFICATION_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS arr_notifications (
    service TEXT PRIMARY KEY NOT NULL,
    channel_id INTEGER NOT NULL,
    media_added INTEGER NOT NULL DEFAULT 1,
    media_removed INTEGER NOT NULL DEFAULT 0,
    file_imported INTEGER NOT NULL DEFAULT 1,
    grab INTEGER NOT NULL DEFAULT 0,
    upgrade INTEGER NOT NULL DEFAULT 0,
    health_warning INTEGER NOT NULL DEFAULT 0)
"""

# Columns added after the initial schema; each is tried via ALTER TABLE and silently skipped if it
# already exists, so a restart on an older database picks them up without losing the channel setting.
_MIGRATION_COLUMNS: tuple[str, ...] = (
    "media_added INTEGER NOT NULL DEFAULT 1",
    "media_removed INTEGER NOT NULL DEFAULT 0",
    "file_imported INTEGER NOT NULL DEFAULT 1",
    "grab INTEGER NOT NULL DEFAULT 0",
    "upgrade INTEGER NOT NULL DEFAULT 0",
    "health_warning INTEGER NOT NULL DEFAULT 0",
)


# TODO: Move to KumaCog.
async def _owner_only(interaction: discord.Interaction) -> bool:
    """Restrict a command to the bot owner; writes to disk and starts downloads."""
    bot: Kuma_Kuma = interaction.client  # type: ignore[assignment]
    allowed: bool = interaction.user.id == bot.owner_user_id
    if allowed is False:
        LOGGER.info(
            "<%s.%s> | Refused | User: %s | Command: %s",
            "_owner_only",
            "check",
            interaction.user.id,
            interaction.command.qualified_name if interaction.command else "unknown",
        )
    return allowed


# region --- Settings ---


class ArrSettings(NamedTuple):
    """One ``[SONARR]`` or ``[RADARR]`` section from ``local.ini``."""

    url: str
    api_key: str
    url_base: str


class NotificationConfig(NamedTuple):
    """Persisted notification preferences for one service."""

    channel_id: int
    media_added: bool = True
    media_removed: bool = False
    file_imported: bool = True
    grab: bool = False
    upgrade: bool = False
    health_warning: bool = False


def load_arr_settings(section: str) -> Optional[ArrSettings]:
    """Read credentials for one *arr service out of ``local.ini``.

    Read here rather than through :class:`KumaConfig`, so that an instance nobody has configured yet
    disables one cog instead of stopping the bot from starting.

    Parameters
    ----------
    section : :class:`str`
        The INI section name, e.g. ``'SONARR'`` or ``'RADARR'``.

    Returns
    -------
    :class:`Optional[ArrSettings]`
        The parsed credentials, or ``None`` when the section or a key is missing.

    """
    path: Path = Path(__file__).parent.parent.joinpath("local.ini")
    if path.is_file() is False:
        return None

    settings = ConfigParser()
    settings.read(filenames=path.as_posix())
    url: Optional[str] = settings.get(section=section, option="url", fallback=None)
    api_key: Optional[str] = settings.get(section=section, option="api_key", fallback=None)
    if not url or not api_key:
        return None
    url_base: str = settings.get(section=section, option="url_base", fallback="")
    return ArrSettings(url=url, api_key=api_key, url_base=url_base)


# endregion


# region --- Shared helpers ---


def truncate(text: Optional[str], limit: int = OVERVIEW_LIMIT) -> str:
    """Returns `text` cut to `limit`, broken on a word rather than mid-syllable."""
    if not text:
        return "-# No overview."
    body: str = " ".join(text.split())
    if len(body) <= limit:
        return body
    return f"{body[:limit].rsplit(' ', 1)[0]}…"


# TODO: Moved to the Panel.
def progress_bar(percent: float, width: int = PROGRESS_WIDTH) -> str:
    """Returns a filled bar for a percentage.

    A bar reads faster than a number in a list of ten, and unlike a code fence it can sit beside bold
    text and emoji.
    """
    filled: int = round((max(0.0, min(100.0, percent)) / 100) * width)
    return f"{UnicodeTable.black_rectangle * filled}{UnicodeTable.white_rectangle * (width - filled)}"


def accent_for_series(series: Series) -> discord.Colour:
    """Returns the container accent that matches a series' status."""
    if series.in_library is False:
        return UNADDED_ACCENT
    return SERIES_STATUS_ACCENTS.get(str(series.status), discord.Colour.blurple())


def accent_for_movie(movie: Series) -> discord.Colour:
    """Returns the container accent that matches a movie's status."""
    if movie.in_library is False:
        return UNADDED_ACCENT
    return MOVIE_STATUS_ACCENTS.get(str(movie.status), discord.Colour.blurple())


def movie_status_label(movie: Series) -> str:
    """Returns the display name for a Radarr movie's status."""
    return MOVIE_STATUS_DISPLAY.get(str(movie.status), str(movie.status).title())


def movie_web_url(movie: Series, base_url: str) -> Optional[str]:
    """Returns the movie's page in the Radarr web UI."""
    return f"{base_url.rstrip('/')}/movie/{movie.title_slug}" if movie.title_slug else None


def movie_tmdb_url(movie: Series) -> Optional[str]:
    """Returns the TMDB page for a Radarr movie."""
    tmdb_id: int = movie._raw.get("tmdbId", 0)  # noqa: SLF001
    return f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id else None


def movie_studio(movie: Series) -> Optional[str]:
    """Returns the studio name from a Radarr movie payload."""
    return movie._raw.get("studio") or movie.network  # noqa: SLF001


# endregion


# region --- Shared UI components ---


class ArrButton(discord.ui.Button):
    """A button that calls its :attr:`on_press` handler when clicked.

    Panels are built from live data, so ``@discord.ui.button`` does not apply here.
    """

    def __init__(
        self,
        *,
        on_press: Callable[[discord.Interaction], Awaitable[None]],
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji, disabled=disabled)
        self.on_press: Callable[[discord.Interaction], Awaitable[None]] = on_press

    async def callback(self, interaction: discord.Interaction) -> None:
        """Route to the :attr:`on_press` handler."""
        await self.on_press(interaction)


class ArrSelect(discord.ui.Select):
    """A select that calls its :attr:`on_select` handler with the chosen value.

    .. warning::
        Must be placed inside a :class:`discord.ui.ActionRow`; a select added directly to a
        ``Container`` serialises fine but Discord responds with a 400.

    """

    def __init__(
        self,
        *,
        on_select: Callable[[discord.Interaction, str], Awaitable[None]],
        placeholder: str,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(
            placeholder=placeholder, options=options or [discord.SelectOption(label="Nothing to pick")], min_values=1, max_values=1
        )
        self.on_select: Callable[[discord.Interaction, str], Awaitable[None]] = on_select

    async def callback(self, interaction: discord.Interaction) -> None:
        """Route the chosen value to the :attr:`on_select` handler."""
        await self.on_select(interaction, self.values[0])


class SettledPanel(discord.ui.LayoutView):
    """Static outcome panel with no interactive components."""

    def __init__(self, *, note: str) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour.from_str("#607D8B"))
        container.add_item(discord.ui.TextDisplay(note))
        self.add_item(container)


# endregion


# region --- Base cog ---


class ArrCog(Cog):
    """Base cog for Sonarr and Radarr.

    Holds the API client, connection lifecycle, error reporting, and shared command bodies.  Subclasses
    set service-specific class variables and carry the decorated command wrappers; discord.py binds
    ``@group.command`` at class definition time, so the decorators must live on the concrete cog.

    """

    # Subclass configuration.

    service_name: ClassVar[str]
    """``'Sonarr'`` or ``'Radarr'``, for labels and messages."""

    ini_section: ClassVar[str]
    """The INI heading that holds this service's credentials."""

    external_db: ClassVar[str]
    """``'TVDB'`` for Sonarr, ``'TMDB'`` for Radarr."""

    client_cls: ClassVar[type[SonarrAPI]]
    """The API class to construct."""

    detail_cls: ClassVar[type[DetailPanel]]
    add_cls: ClassVar[type[AddPanel]]
    status_cls: ClassVar[type[StatusPanel]]

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        self.settings: Optional[ArrSettings] = load_arr_settings(self.ini_section)
        self._client: Optional[SonarrAPI] = None
        self.profiles: list[QualityProfile] = []
        self.folders: list[RootFolder] = []
        self._notifications: Optional[NotificationConfig] = None

    # Properties.

    @property
    def api(self) -> SonarrAPI:
        """Returns the active API client.

        Raises
        ------
        RuntimeError
            The cog loaded without credentials; every command guards on :attr:`configured` first.

        """
        if self._client is None:
            msg: str = f"The {self.service_name} client is not configured."
            raise RuntimeError(msg)
        return self._client

    @property
    def configured(self) -> bool:
        """Whether ``local.ini`` had a usable section for this service."""
        return self._client is not None

    @property
    def base_url(self) -> str:
        """Returns the instance root, for the link buttons."""
        return self._client.base_url if self._client is not None else ""

    @property
    def default_profile_id(self) -> int:
        """Returns the quality profile an add starts on."""
        return self.profiles[0].id if len(self.profiles) > 0 else 1

    @property
    def default_folder_path(self) -> str:
        """Returns the root folder an add starts on."""
        return self.folders[0].path if len(self.folders) > 0 else ""

    # Lifecycle.

    async def cog_load(self) -> None:
        """Connect, warm caches, attach the event listener, and restore the notification channel.

        An unreachable instance at startup logs a warning and returns; the bot outlives the downtime
        and the listener reconnects on its own.
        """
        # Notification table + cached config.
        async with self.bot.pool.acquire() as conn:
            await conn.execute(NOTIFICATION_SETUP_SQL)
            # Migrate older schemas that lack the toggle columns.
            for col_def in _MIGRATION_COLUMNS:
                col_name: str = col_def.split()[0]
                try:
                    await conn.execute(f"ALTER TABLE arr_notifications ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass  # column already exists
                else:
                    LOGGER.info("<%s.%s> | Migrated column | %s", __class__.__name__, "cog_load", col_name)

            row: Optional[Row] = await conn.fetchone(
                """SELECT channel_id, media_added, media_removed, file_imported, grab, upgrade, health_warning
                   FROM arr_notifications WHERE service = ?""",
                self.ini_section,
            )
        if row is not None:
            self._notifications = NotificationConfig(
                channel_id=row["channel_id"],
                media_added=bool(row["media_added"]),
                media_removed=bool(row["media_removed"]),
                file_imported=bool(row["file_imported"]),
                grab=bool(row["grab"]),
                upgrade=bool(row["upgrade"]),
                health_warning=bool(row["health_warning"]),
            )

        # API client.
        if self.settings is None:
            LOGGER.warning(
                "<%s.%s> | No [%s] section in local.ini; commands will refuse.",
                __class__.__name__,
                "cog_load",
                self.ini_section,
            )
            return

        client: SonarrAPI = self.client_cls(
            base_url=self.settings.url,
            api_key=self.settings.api_key,
            session=self.bot.session,
            url_base=self.settings.url_base,
        )
        # Always store the client so commands work once the instance comes back up.
        self._client = client
        try:
            status: SystemStatus = await client.connect()
            self.profiles = await client.quality_profiles()
            self.folders = await client.root_folders()
            await client.library()
            await client.listen(callback=self._on_hub_event)
        except SonarrError as e:
            LOGGER.warning(
                "<%s.%s> | %s unreachable at load | Reason: %s",
                __class__.__name__,
                "cog_load",
                self.service_name,
                e.error_reason,
            )
            return

        LOGGER.info(
            "<%s.%s> | Ready | Version: %s | Profiles: %s",
            __class__.__name__,
            "cog_load",
            status.version,
            len(self.profiles),
        )

    async def cog_unload(self) -> None:
        """Stop the listener; a reload otherwise leaves a websocket writing into a dead cog."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # Error handling.

    async def report(self, interaction: discord.Interaction, error: SonarrError, *, deferred: bool = False) -> None:
        """Send a Kuma-styled ephemeral reply for a wrapper error."""
        emoji_table = self.emoji_table
        name: str = self.service_name
        tri: str = self.unicode.right_triangle_arrow
        if isinstance(error, SonarrValidationError):
            note: str = f"{name} refused that {tri} {error.summary} {emoji_table.kuma_pout}"
        elif error.status_code == 0:
            # The SignalR websocket is a live heartbeat; if it is connected the service is running but
            # the REST API is overloaded (big library serialise, queue churn, disk I/O).  Distinguish
            # that from a genuinely unreachable instance so the user knows to retry, not to panic.
            if self.configured and self.api.listening:
                note = (
                    f"{name} is running but too busy to answer right now, try again in a moment. "
                    f"{emoji_table.kuma_tea}\n-# {error.error_reason}"
                )
            else:
                note = f"I couldn't reach {name}. {emoji_table.kuma_sad}\n-# {error.error_reason}"
        else:
            note = f"{name} answered `{error.status_code}` {tri} {error.error_reason} {emoji_table.kuma_sad}"

        LOGGER.warning(
            "<%s.%s> | Reported | Status: %s | Reason: %s",
            __class__.__name__,
            "report",
            error.status_code,
            error.error_reason,
        )
        if deferred is True or interaction.response.is_done() is True:
            await interaction.followup.send(content=note, ephemeral=True)
        else:
            await interaction.response.send_message(content=note, ephemeral=True)

    async def guard(self, interaction: discord.Interaction) -> bool:
        """Check whether the cog is configured; sends an ephemeral hint when it is not."""
        if self.configured is True:
            return True
        await interaction.response.send_message(
            content=(
                f"{self.service_name} isn't set up yet. {self.emoji_table.kuma_shrug}\n"
                f"-# Add a `[{self.ini_section}]` section to `local.ini` with `url` and `api_key`, then reload this cog."
            ),
            ephemeral=True,
        )
        return False

    async def build_status(self, user_id: int) -> StatusPanel:
        """Fetch all status data and return the built :class:`StatusPanel`."""
        return self.status_cls(
            cog=self,
            user_id=user_id,
            status=await self.api.system_status(),
            queue=await self.api.queue(),
            warnings=await self.api.health(),
            mounts=await self.api.disk_space(),
            library=await self.api.library(),
        )

    # Shared helpers.

    async def autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002 # discord.py's callback signature
        """Suggest library items by title; served from the cache so it fires on every keystroke."""
        if self.configured is False:
            return []
        try:
            matches: list[Series] = await self.api.find(term=current)
        except SonarrError:
            return []
        return [app_commands.Choice(name=entry.display_title[:100], value=str(entry.id)) for entry in matches[:25]]

    async def resolve(self, interaction: discord.Interaction, term: str) -> Optional[Series]:
        """Resolve an autocomplete value or typed title to a media item.

        Users can submit free text instead of picking a choice, so a title search backs up the id lookup.
        """
        if term.isdigit():
            found: Optional[Series] = await self.api.get_series(series_id=int(term))
            if found is not None:
                return found
        matches: list[Series] = await self.api.find(term=term, limit=1)
        if len(matches) > 0:
            return matches[0]
        await interaction.response.send_message(
            content=f"I couldn't find **{term}** in the library. {self.emoji_table.kuma_sad}",
            ephemeral=True,
        )
        return None

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Handle :class:`~discord.app_commands.CheckFailure` silently; re-raise everything else."""
        if isinstance(error, app_commands.CheckFailure):
            note: str = f"That command isn't available right now. {self.emoji_table.kuma_shrug}"
            if interaction.response.is_done() is True:
                await interaction.followup.send(content=note, ephemeral=True)
            else:
                await interaction.response.send_message(content=note, ephemeral=True)
            return
        raise error

    # Notifications.

    async def _on_hub_event(self, name: SignalRMessage, action: SignalRAction, resource: dict[str, Any]) -> None:
        """SignalR subscriber callback; sends a notification when media is added or imported.

        The library applies cache updates before calling subscribers, so by the time this fires
        the media is already in ``_series_cache`` and a lookup is a dict read.
        """
        cfg: Optional[NotificationConfig] = self._notifications
        LOGGER.debug(
            "<%s.%s> | Hub event | Name: %s | Action: %s | Channel: %s",
            __class__.__name__,
            "_on_hub_event",
            name,
            action,
            cfg.channel_id if cfg is not None else None,
        )
        if cfg is None:
            return

        event: Optional[str] = None
        media: Optional[Series] = None

        # Media added to the library.
        if action is SignalRAction.created and name in {SignalRMessage.series, SignalRMessage.movie}:
            if cfg.media_added is False:
                return
            media = Series(data=resource)  # type: ignore[arg-type] # hub sends a full SeriesPayload
            noun: str = "Series" if name is SignalRMessage.series else "Movie"
            event = f"{noun} added to {self.service_name} {self.emoji_table.kuma_happy}"

        # Media removed from the library.
        elif action is SignalRAction.deleted and name in {SignalRMessage.series, SignalRMessage.movie}:
            if cfg.media_removed is False:
                return
            media = Series(data=resource)  # type: ignore[arg-type]
            noun = "Series" if name is SignalRMessage.series else "Movie"
            event = f"{noun} removed from {self.service_name} {self.emoji_table.kuma_hmm}"

        # File imported (episode or movie file landed on disk).
        elif action is SignalRAction.created and name in {SignalRMessage.episode_file, SignalRMessage.movie_file}:
            if cfg.file_imported is False:
                return
            # The resource is the file payload; look up the parent from the cache.
            parent_key: str = "seriesId" if name is SignalRMessage.episode_file else "movieId"
            parent_id: int = resource.get(parent_key, 0)
            if parent_id:
                media = self.api._series_cache.get(parent_id)  # noqa: SLF001 # _series_cache is the library's escape hatch
            if media is None:
                return
            noun = "Episode" if name is SignalRMessage.episode_file else "Movie"
            event = f"{noun} file imported {self.emoji_table.kuma_tea}"

        # File upgraded (better quality replaced the existing file).
        elif action is SignalRAction.updated and name in {SignalRMessage.episode_file, SignalRMessage.movie_file}:
            if cfg.upgrade is False:
                return
            parent_key = "seriesId" if name is SignalRMessage.episode_file else "movieId"
            parent_id = resource.get(parent_key, 0)
            if parent_id:
                media = self.api._series_cache.get(parent_id)  # noqa: SLF001
            if media is None:
                return
            noun = "Episode" if name is SignalRMessage.episode_file else "Movie"
            event = f"{noun} file upgraded {self.emoji_table.kuma_star_eye}"

        # Grab (download started).
        elif action is SignalRAction.created and name is SignalRMessage.queue:
            if cfg.grab is False:
                return
            # Queue resources carry a title but no full series payload; send a plain text notification.
            title: str = resource.get("title", "Unknown")
            try:
                channel: Optional[Any] = self.bot.get_channel(cfg.channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(cfg.channel_id)
                await channel.send(
                    content=(f"{self.emoji_table.kuma_peak} **{self.service_name} Grab** {self.unicode.right_triangle_arrow} {title}"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                LOGGER.exception(
                    "<%s.%s> | Failed to send grab notification | Channel: %s",
                    __class__.__name__,
                    "_on_hub_event",
                    cfg.channel_id,
                )
            return

        # Health warning.
        elif name is SignalRMessage.health and action is SignalRAction.created:
            if cfg.health_warning is False:
                return
            source: str = resource.get("source", "Unknown")
            message: str = resource.get("message", "No details.")
            is_error: bool = resource.get("type", "").lower() == "error"
            marker: str = self.emoji_table.kuma_shock if is_error is True else self.emoji_table.kuma_hmm
            # Health events have no media; send a plain text notification.
            try:
                channel: Optional[Any] = self.bot.get_channel(cfg.channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(cfg.channel_id)
                await channel.send(
                    content=f"{marker} **{self.service_name} Health** {self.unicode.right_triangle_arrow} **{source}**\n-# {message[:300]}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                LOGGER.exception(
                    "<%s.%s> | Failed to send health notification | Channel: %s",
                    __class__.__name__,
                    "_on_hub_event",
                    cfg.channel_id,
                )
            return

        if event is None or media is None:
            return

        try:
            channel = self.bot.get_channel(cfg.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(cfg.channel_id)
            await channel.send(view=NotificationPanel(cog=self, media=media, event=event))
        except Exception:
            LOGGER.exception(
                "<%s.%s> | Failed to send notification | Channel: %s | Title: %s",
                __class__.__name__,
                "_on_hub_event",
                cfg.channel_id,
                media.title,
            )

    async def _cmd_notifications(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel]) -> None:
        """Open the notification settings panel, optionally setting the channel first."""
        if not await self.guard(interaction=interaction):
            return

        # A channel argument sets or moves the target before opening the panel.
        if channel is not None:
            # Preserve existing toggles when moving the channel; fall back to NamedTuple defaults for a fresh config.
            prev: Optional[NotificationConfig] = self._notifications
            await self._save_notifications(
                NotificationConfig(
                    channel_id=channel.id,
                    media_added=prev.media_added if prev is not None else True,
                    media_removed=prev.media_removed if prev is not None else False,
                    file_imported=prev.file_imported if prev is not None else True,
                    grab=prev.grab if prev is not None else False,
                    upgrade=prev.upgrade if prev is not None else False,
                    health_warning=prev.health_warning if prev is not None else False,
                ),
            )

        await interaction.response.send_message(
            view=NotificationSettingsPanel(cog=self, user_id=interaction.user.id),
            ephemeral=True,
        )

    async def _save_notifications(self, config: Optional[NotificationConfig]) -> None:
        """Persist notification config and update the cached state."""
        self._notifications = config
        try:
            async with self.bot.pool.acquire() as conn:
                if config is None:
                    await conn.execute(
                        """DELETE FROM arr_notifications WHERE service = ?""",
                        self.ini_section,
                    )
                else:
                    await conn.execute(
                        """INSERT OR REPLACE INTO arr_notifications
                           (service, channel_id, media_added, media_removed, file_imported, grab, upgrade, health_warning)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                        self.ini_section,
                        config.channel_id,
                        int(config.media_added),
                        int(config.media_removed),
                        int(config.file_imported),
                        int(config.grab),
                        int(config.upgrade),
                        int(config.health_warning),
                    )
        except sqlite3.DatabaseError:
            LOGGER.exception(
                "<%s.%s> | Failed to persist notification config.",
                __class__.__name__,
                "_save_notifications",
            )

    # Command bodies.
    # discord.py binds `@group.command` at class definition time, so the decorators must live on the
    # concrete cog.  These private methods hold the logic that every command shares.

    async def _cmd_list(
        self,
        interaction: discord.Interaction,
        filter_by: Optional[str] = None,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=ephemeral)
        try:
            entries: list[Series] = await self.api.find(term=filter_by, limit=500) if filter_by else await self.api.library()
        except SonarrError as e:
            await self.report(interaction=interaction, error=e, deferred=True)
            return
        owner_id: int = access.owner_id(user=interaction.user, bot=self.bot)
        await interaction.followup.send(view=ListingPanel(cog=self, user_id=owner_id, entries=entries), ephemeral=ephemeral)

    async def _cmd_info(
        self,
        interaction: discord.Interaction,
        term: str,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        if not await self.guard(interaction=interaction):
            return
        try:
            found: Optional[Series] = await self.resolve(interaction=interaction, term=term)
        except SonarrError as e:
            await self.report(interaction=interaction, error=e)
            return
        if found is None:
            return
        owner_id: int = access.owner_id(user=interaction.user, bot=self.bot)
        await interaction.response.send_message(
            view=self.detail_cls(cog=self, user_id=owner_id, media=found),
            ephemeral=ephemeral,
        )

    async def _cmd_search(
        self,
        interaction: discord.Interaction,
        term: str,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=ephemeral)
        try:
            results: list[Series] = await self.api.lookup(term=term)
        except SonarrError as e:
            await self.report(interaction=interaction, error=e, deferred=True)
            return

        if len(results) == 0:
            await interaction.followup.send(
                content=f"{self.external_db} has nothing for **{term}**. {self.emoji_table.kuma_sad}",
                ephemeral=ephemeral,
            )
            return

        owner_id: int = access.owner_id(user=interaction.user, bot=self.bot)
        await interaction.followup.send(
            view=SearchPanel(cog=self, user_id=owner_id, term=term, results=results),
            ephemeral=ephemeral,
        )

    async def _cmd_add(self, interaction: discord.Interaction, term: str) -> None:
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            results: list[Series] = await self.api.lookup(term=term)
        except SonarrError as e:
            await self.report(interaction=interaction, error=e, deferred=True)
            return

        if len(results) == 0:
            await interaction.followup.send(
                content=f"{self.external_db} has nothing for **{term}**. {self.emoji_table.kuma_sad}",
                ephemeral=True,
            )
            return

        # A single result is the common case for an id term; skip a click and open it chosen.
        chosen: Optional[Series] = results[0] if len(results) == 1 else None
        await interaction.followup.send(
            view=self.add_cls(cog=self, user_id=interaction.user.id, term=term, results=results, chosen=chosen),
            ephemeral=True,
        )

    async def _cmd_remove(self, interaction: discord.Interaction, term: str) -> None:
        if not await self.guard(interaction=interaction):
            return
        try:
            found: Optional[Series] = await self.resolve(interaction=interaction, term=term)
        except SonarrError as e:
            await self.report(interaction=interaction, error=e)
            return
        if found is None:
            return
        await interaction.response.send_message(
            view=RemovePanel(cog=self, user_id=interaction.user.id, media=found),
            ephemeral=True,
        )

    async def _cmd_status(
        self,
        interaction: discord.Interaction,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=ephemeral)
        try:
            panel: StatusPanel = await self.build_status(user_id=access.owner_id(user=interaction.user, bot=self.bot))
        except SonarrError as e:
            await self.report(interaction=interaction, error=e, deferred=True)
            return
        await interaction.followup.send(view=panel, ephemeral=ephemeral)


# endregion


# region --- Panels ---


class ArrPanel(discord.ui.LayoutView):
    """Base panel for every Sonarr and Radarr view in this cog.

    Stores the cog and user id, gates interactions to the owner, and provides helpers that branch
    on the service type so each concrete panel reads the same regardless of which *arr it drives.

    .. warning::
        A Components V2 message cannot carry ``content`` or ``embeds``, so anything a reader needs
        has to be a ``TextDisplay`` inside the layout.

    """

    def __init__(self, *, cog: ArrCog, user_id: int) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog: ArrCog = cog
        self.user_id: int = user_id

    # Properties.

    @property
    def is_sonarr(self) -> bool:
        """Whether this panel is driving a Sonarr instance."""
        return isinstance(self.cog, SonarrCog)

    @property
    def service_name(self) -> str:
        """``'Sonarr'`` or ``'Radarr'``, for labels and messages."""
        return self.cog.service_name

    @property
    def api(self) -> SonarrAPI:
        """The active API client from the cog."""
        return self.cog.api

    @property
    def external_name(self) -> str:
        """``'TVDB'`` for Sonarr, ``'TMDB'`` for Radarr."""
        return self.cog.external_db

    # Interaction.

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone but the person the panel was opened for."""
        # A panel owned by the bot is public; anyone may interact.
        if self.cog.bot.user is not None and self.user_id == self.cog.bot.user.id:
            return True
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                content=f"That panel isn't yours! {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    # Media display helpers.

    def accent(self, media: Series) -> discord.Colour:
        """Returns the container accent for a media item."""
        if self.is_sonarr is True:
            return accent_for_series(media)
        return accent_for_movie(media)

    def status_label(self, media: Series) -> str:
        """Returns the human-readable status string for a media item."""
        if self.is_sonarr is True:
            return str(media.status).title() or "Unknown"
        return movie_status_label(media) or "Unknown"

    def web_url(self, media: Series) -> Optional[str]:
        """Returns the URL to this item's page inside the *arr web UI."""
        if self.is_sonarr is True:
            return media.web_url(self.cog.base_url)
        return movie_web_url(media, self.cog.base_url)

    def external_url(self, media: Series) -> Optional[str]:
        """Returns the TVDB or TMDB page for this item."""
        if self.is_sonarr is True:
            return media.tvdb_url
        return movie_tmdb_url(media)

    def studio_or_network(self, media: Series) -> Optional[str]:
        """Returns the network (Sonarr) or studio (Radarr) for a media item."""
        if self.is_sonarr is True:
            return media.network
        return movie_studio(media)

    # Layout helpers.

    def art(self, media: Series) -> Optional[discord.ui.MediaGallery]:
        """Returns the wide backdrop when one exists.

        Only ``remoteUrl`` art is usable; the local path on the *arr host needs the API key
        and Discord fetches these itself with no way to attach one.
        """
        backdrop: Optional[str] = media.fanart or media.banner
        if backdrop is None:
            return None
        return discord.ui.MediaGallery(discord.MediaGalleryItem(media=backdrop, description=f"{media.title} artwork"))

    def poster(self, media: Series) -> discord.ui.Item:
        """Returns the poster thumbnail for a section accessory, or a disabled button fallback.

        .. note::
            ``Section`` accessory only accepts a button or thumbnail; the fallback stays inside
            that pair deliberately.

        """
        poster_url: Optional[str] = media.poster
        if poster_url is not None:
            return discord.ui.Thumbnail(media=poster_url, description=f"{media.title} poster")
        return discord.ui.Button(label="Details", disabled=True)

    def links(self, media: Series) -> str:
        """Returns the external-id link line for a media item."""
        dot: str = self.cog.unicode.middle_dot
        parts: list[str] = []
        web: Optional[str] = self.web_url(media)
        if web is not None:
            parts.append(f"[{self.service_name}]({web})")
        ext: Optional[str] = self.external_url(media)
        if ext is not None:
            parts.append(f"[{self.external_name}]({ext})")
        if media.imdb_url is not None:
            parts.append(f"[IMDb]({media.imdb_url})")
        return f"-# {f' {dot} '.join(parts)}" if len(parts) > 0 else ""

    # API helpers.

    async def search_media(self, media_id: int) -> None:
        """Queue a search for a media item on the *arr instance."""
        api: SonarrAPI = self.cog.api
        if isinstance(api, RadarrAPI):
            await api.search_movie(movie_id=media_id)
        else:
            await api.search_series(series_id=media_id)

    async def refresh_media(self, media_id: int) -> None:
        """Refresh metadata and rescan the folder for a media item."""
        api: SonarrAPI = self.cog.api
        if isinstance(api, RadarrAPI):
            await api.refresh_movie(movie_id=media_id)
        else:
            await api.refresh_series(series_id=media_id)

    # Transition helpers.

    def _build_detail(self, media: Series, *, note: Optional[str] = None) -> DetailPanel:
        """Construct the right :class:`DetailPanel` subclass for this panel's service."""
        return self.cog.detail_cls(cog=self.cog, user_id=self.user_id, media=media, note=note)


# region --- Detail panels ---


class DetailPanel(ArrPanel):
    """Full detail view for a single media item.

    Layout is shared; :meth:`details` is overridden per service for episode vs. file stats.
    """

    def __init__(
        self,
        *,
        cog: ArrCog,
        user_id: int,
        media: Series,
        note: Optional[str] = None,
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.media: Series = media

        container = discord.ui.Container(accent_colour=self.accent(media))
        backdrop: Optional[discord.ui.MediaGallery] = self.art(media)
        if backdrop is not None:
            container.add_item(backdrop)

        container.add_item(discord.ui.TextDisplay(f"## {media.display_title}\n-# {self.headline()}"))
        container.add_item(discord.ui.Section(truncate(media.overview), accessory=self.poster(media)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.details()))

        link_line: str = self.links(media)
        if link_line:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(link_line))

        if note is not None:
            container.add_item(discord.ui.TextDisplay(f"-# {note}"))

        container.add_item(self.actions())
        self.add_item(container)

    def headline(self) -> str:
        """Returns the subtitle line under the title."""
        media: Series = self.media
        dot: str = self.cog.unicode.middle_dot
        parts: list[str] = [self.status_label(media)]
        source: Optional[str] = self.studio_or_network(media)
        if source:
            parts.append(source)
        if media.certification:
            parts.append(media.certification)
        if media.runtime:
            parts.append(f"{media.runtime}m")
        if media.in_library is False:
            parts.append("**not in your library**")
        elif media.monitored is False:
            parts.append("unmonitored")
        return f" {dot} ".join(parts)

    def details(self) -> str:
        """Returns the stats block. Override per service."""
        raise NotImplementedError

    def actions(self) -> discord.ui.ActionRow:
        """Returns the action row for this item."""
        row = discord.ui.ActionRow()
        if self.media.in_library is True:
            row.add_item(ArrButton(on_press=self.queue_search, label="Search", emoji="🔍"))
            row.add_item(ArrButton(on_press=self.queue_refresh, label="Refresh", emoji="🔄"))
            row.add_item(ArrButton(on_press=self.open_remove, label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger))
        else:
            row.add_item(ArrButton(on_press=self.open_add, label="Add", emoji="➕", style=discord.ButtonStyle.success))
        web: Optional[str] = self.web_url(self.media)
        if web is not None:
            row.add_item(discord.ui.Button(label=f"Open in {self.service_name}", style=discord.ButtonStyle.link, url=web))
        return row

    # Button handlers.

    async def queue_search(self, interaction: discord.Interaction) -> None:
        """Queue a search and refresh the panel."""
        try:
            await self.search_media(media_id=self.media.id)
            note: str = f"Search queued {self.cog.emoji_table.kuma_tea}"
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e)
            return
        fresh: Series = await self.api.get_series(series_id=self.media.id) or self.media
        await interaction.response.edit_message(view=self._build_detail(media=fresh, note=note))

    async def queue_refresh(self, interaction: discord.Interaction) -> None:
        """Queue a metadata refresh and rebuild the panel."""
        try:
            await self.refresh_media(media_id=self.media.id)
            note: str = f"Refresh queued {self.cog.emoji_table.kuma_tea}"
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e)
            return
        fresh: Series = await self.api.get_series(series_id=self.media.id) or self.media
        await interaction.response.edit_message(view=self._build_detail(media=fresh, note=note))

    async def open_remove(self, interaction: discord.Interaction) -> None:
        """Open the :class:`RemovePanel` for this item."""
        await interaction.response.edit_message(view=RemovePanel(cog=self.cog, user_id=self.user_id, media=self.media))

    async def open_add(self, interaction: discord.Interaction) -> None:
        """Open the :class:`AddPanel` with this item pre-selected."""
        add_view = self.cog.add_cls(
            cog=self.cog,
            user_id=self.user_id,
            term=self.media.display_title,
            results=[self.media],
            chosen=self.media,
        )
        await interaction.response.edit_message(view=add_view)


class SeriesDetailPanel(DetailPanel):
    """Sonarr detail panel with episode progress, seasons, and airing schedule."""

    def details(self) -> str:
        """Returns the episode stats block."""
        media: Series = self.media
        dot: str = self.cog.unicode.middle_dot
        tri: str = self.cog.unicode.right_triangle_arrow
        lines: list[str] = []

        if media.in_library is True:
            lines.append(
                f"- **Episodes** {tri} {progress_bar(media.percent_of_episodes)} "
                f"{media.episode_file_count}/{media.episode_count} ({media.percent_of_episodes:.0f}%)",
            )
            lines.append(f"- **On disk** {tri} {media.size_display} across {media.season_count} seasons")
            if media.missing_episode_count:
                lines.append(f"- **Missing** {tri} {media.missing_episode_count} episodes {self.cog.emoji_table.kuma_hmm}")
        else:
            lines.append(f"- **Seasons** {tri} {media.season_count}")

        if media.next_airing is not None:
            lines.append(f"- **Next episode** {tri} {self.cog.to_discord_timestamp(time=media.next_airing, style='R')}")
        elif media.previous_airing is not None:
            lines.append(f"- **Last aired** {tri} {self.cog.to_discord_timestamp(time=media.previous_airing, style='R')}")

        if media.rating is not None:
            lines.append(f"- **Rating** {tri} {media.rating:.1f} {self.cog.unicode.star} ({media.rating_votes:,} votes)")
        if len(media.genres) > 0:
            lines.append(f"- **Genres** {tri} {f' {dot} '.join(media.genres[:5])}")
        return "\n".join(lines)


class MovieDetailPanel(DetailPanel):
    """Radarr detail panel with file status, release date, and added date."""

    def details(self) -> str:
        """Returns the movie stats block."""
        media: Series = self.media
        dot: str = self.cog.unicode.middle_dot
        tri: str = self.cog.unicode.right_triangle_arrow
        lines: list[str] = []

        if media.in_library is True:
            if media.has_file is True:
                lines.append(f"- **File** {tri} {self.cog.emoji_table.kuma_happy} {media.size_display}")
            else:
                lines.append(f"- **File** {tri} {self.cog.emoji_table.kuma_hmm} Not downloaded")
        if media.added is not None and media.in_library is True:
            lines.append(f"- **Added** {tri} {self.cog.to_discord_timestamp(time=media.added, style='R')}")
        if media.first_aired is not None:
            lines.append(f"- **Released** {tri} {self.cog.to_discord_timestamp(time=media.first_aired, style='D')}")
        if media.rating is not None:
            lines.append(f"- **Rating** {tri} {media.rating:.1f} {self.cog.unicode.star} ({media.rating_votes:,} votes)")
        if len(media.genres) > 0:
            lines.append(f"- **Genres** {tri} {f' {dot} '.join(media.genres[:5])}")
        return "\n".join(lines)


# endregion

# region --- Listing panels ---


class ListingPanel(ArrPanel):
    """Paginated library listing with poster thumbnails per row."""

    def __init__(self, *, cog: ArrCog, user_id: int, entries: list[Series], page: int = 0) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.entries: list[Series] = entries
        self.pages: int = max(1, -(-len(entries) // LIBRARY_PER_PAGE))
        self.page: int = max(0, min(page, self.pages - 1))

        start: int = self.page * LIBRARY_PER_PAGE
        self.window: list[Series] = entries[start : start + LIBRARY_PER_PAGE]

        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_peak} {self.service_name} Library"))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        if len(self.window) == 0:
            container.add_item(discord.ui.TextDisplay("-# Nothing in the library yet."))
        for media in self.window:
            container.add_item(discord.ui.Section(self.row(media), accessory=self.poster(media)))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {self.summary()}"))

        if len(self.window) > 0:
            picker = discord.ui.ActionRow()
            picker.add_item(
                ArrSelect(
                    on_select=self.open_item,
                    placeholder=f"Open a {'series' if self.is_sonarr is True else 'movie'}…",
                    options=[
                        discord.SelectOption(
                            label=media.display_title[:100],
                            value=str(media.id),
                            description=f"{self.status_label(media)} {self.cog.unicode.middle_dot} {media.size_display}"[:100],
                        )
                        for media in self.window
                    ],
                ),
            )
            container.add_item(picker)

        self.add_item(container)
        # Outside the container: these act *on* the panel rather than being part of the listing,
        # and the container's border is what makes that read.
        self.add_item(self.navigation())

    def row(self, media: Series) -> str:
        """Returns the two-line summary beside a media item's poster."""
        dot: str = self.cog.unicode.middle_dot
        state: str = "" if media.monitored is True else " (unmonitored)"
        if self.is_sonarr is True:
            detail: str = (
                f"{progress_bar(media.percent_of_episodes, width=8)} {media.episode_file_count}/{media.episode_count} "
                f"{dot} {media.size_display} {dot} {self.status_label(media)}{state}"
            )
        else:
            file_marker: str = self.cog.emoji_table.kuma_happy if media.has_file is True else self.cog.emoji_table.kuma_hmm
            detail = f"{file_marker} {media.size_display} {dot} {self.status_label(media)}{state}"
        return f"**{media.display_title}**\n-# {detail}"

    def summary(self) -> str:
        """Returns the totals line under the listing."""
        dot: str = self.cog.unicode.middle_dot
        total_size: int = sum(entry.size_on_disk for entry in self.entries)
        page: str = f" {dot} page {self.page + 1} of {self.pages}" if self.pages > 1 else ""
        if self.is_sonarr is True:
            missing: int = sum(entry.missing_episode_count for entry in self.entries)
            return f"{len(self.entries)} series {dot} {to_size(total_size)} {dot} {missing} episodes missing{page}"
        missing = sum(1 for entry in self.entries if entry.monitored is True and entry.has_file is False)
        return f"{len(self.entries)} movies {dot} {to_size(total_size)} {dot} {missing} missing{page}"

    def navigation(self) -> discord.ui.ActionRow:
        """Returns the pagination and reload row."""
        row = discord.ui.ActionRow()
        if self.pages > 1:
            row.add_item(ArrButton(on_press=self.prev_page, label="Prev", disabled=self.page == 0))
            row.add_item(ArrButton(on_press=self.next_page, label="Next", disabled=self.page >= self.pages - 1))
        row.add_item(ArrButton(on_press=self.reload, label="Reload", emoji="🔄"))
        return row

    # Button / select handlers.

    async def open_item(self, interaction: discord.Interaction, value: str) -> None:
        """Open the detail panel for the selected library item."""
        media: Optional[Series] = await self.api.get_series(series_id=int(value))
        if media is None:
            await interaction.response.send_message(
                content=f"{self.service_name} no longer has that one. {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(view=self._build_detail(media=media))

    async def prev_page(self, interaction: discord.Interaction) -> None:
        """Navigate to the previous page."""
        await interaction.response.edit_message(
            view=ListingPanel(cog=self.cog, user_id=self.user_id, entries=self.entries, page=self.page - 1),
        )

    async def next_page(self, interaction: discord.Interaction) -> None:
        """Navigate to the next page."""
        await interaction.response.edit_message(
            view=ListingPanel(cog=self.cog, user_id=self.user_id, entries=self.entries, page=self.page + 1),
        )

    async def reload(self, interaction: discord.Interaction) -> None:
        """Re-fetch the library and rebuild the panel."""
        try:
            entries: list[Series] = await self.api.library(force=True)
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e)
            return
        await interaction.response.edit_message(
            view=ListingPanel(cog=self.cog, user_id=self.user_id, entries=entries, page=self.page),
        )


# endregion

# region --- Add panels ---


class AddPanel(ArrPanel):
    """Add-to-library panel with search results and settings selects.

    Two states in one view: nothing chosen yet, or a result selected with profile/folder/setting
    selects visible. Picking a different result rebuilds the panel.

    .. note::
        Subclasses set their service-specific setting (``monitor`` or ``availability``) **before**
        calling ``super().__init__``, because the layout calls :meth:`setting_row`.

    """

    def __init__(
        self,
        *,
        cog: ArrCog,
        user_id: int,
        term: str,
        results: list[Series],
        chosen: Optional[Series] = None,
        profile_id: Optional[int] = None,
        folder_path: Optional[str] = None,
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.term: str = term
        self.results: list[Series] = results
        self.chosen: Optional[Series] = chosen
        self.profile_id: int = profile_id if profile_id is not None else cog.default_profile_id
        self.folder_path: str = folder_path if folder_path is not None else cog.default_folder_path

        accent_colour: discord.Colour = self.accent(chosen) if chosen is not None else UNADDED_ACCENT
        container = discord.ui.Container(accent_colour=accent_colour)

        if chosen is not None:
            backdrop: Optional[discord.ui.MediaGallery] = self.art(chosen)
            if backdrop is not None:
                container.add_item(backdrop)
            container.add_item(discord.ui.TextDisplay(f"## {chosen.display_title}\n-# {self.headline(chosen)}"))
            container.add_item(discord.ui.Section(truncate(chosen.overview), accessory=self.poster(chosen)))
            link_line: str = self.links(chosen)
            if link_line:
                container.add_item(discord.ui.TextDisplay(link_line))
        else:
            noun: str = "series" if self.is_sonarr is True else "movie"
            container.add_item(
                discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_peak} Add a {noun}\n-# {len(results)} results for `{term}`")
            )

        container.add_item(discord.ui.Separator())
        container.add_item(self.results_row())

        if chosen is not None and chosen.in_library is False:
            container.add_item(self.profile_row())
            container.add_item(self.folder_row())
            container.add_item(self.setting_row())

        container.add_item(self.actions())
        self.add_item(container)

    def headline(self, media: Series) -> str:
        """Returns the subtitle line for a chosen result."""
        dot: str = self.cog.unicode.middle_dot
        parts: list[str] = [self.status_label(media)]
        source: Optional[str] = self.studio_or_network(media)
        if source:
            parts.append(source)
        if self.is_sonarr is True:
            parts.append(f"{media.season_count} seasons")
        elif media.year:
            parts.append(str(media.year))
        if media.rating is not None:
            parts.append(f"{media.rating:.1f} {self.cog.unicode.star}")
        if media.in_library is True:
            parts.append(f"**already in your library** {self.cog.emoji_table.kuma_hmm}")
        return f" {dot} ".join(parts)

    def results_row(self) -> discord.ui.ActionRow:
        """Returns the search results select row."""
        row = discord.ui.ActionRow()
        if self.is_sonarr is True:
            options: list[discord.SelectOption] = [
                discord.SelectOption(
                    label=media.display_title[:100],
                    value=str(index),
                    description=("Already added" if media.in_library is True else f"TVDB {media.tvdb_id}")[:100],
                    default=self.chosen is not None and media.tvdb_id == self.chosen.tvdb_id,
                )
                for index, media in enumerate(self.results)
            ]
        else:
            tmdb_key: str = "tmdbId"
            options = [
                discord.SelectOption(
                    label=media.display_title[:100],
                    value=str(index),
                    description=("Already added" if media.in_library is True else f"TMDB {media._raw.get(tmdb_key, 'N/A')}")[:100],  # noqa: SLF001
                    default=self.chosen is not None and media._raw.get(tmdb_key, 0) == self.chosen._raw.get(tmdb_key, -1),  # noqa: SLF001
                )
                for index, media in enumerate(self.results)
            ]
        row.add_item(ArrSelect(on_select=self.choose_result, placeholder="Pick a result…", options=options))
        return row

    def profile_row(self) -> discord.ui.ActionRow:
        """Returns the quality profile select."""
        row = discord.ui.ActionRow()
        row.add_item(
            ArrSelect(
                on_select=self.set_profile,
                placeholder="Quality profile…",
                options=[
                    discord.SelectOption(label=profile.name[:100], value=str(profile.id), default=profile.id == self.profile_id)
                    for profile in self.cog.profiles[:25]
                ],
            ),
        )
        return row

    def folder_row(self) -> discord.ui.ActionRow:
        """Returns the root folder select."""
        row = discord.ui.ActionRow()
        row.add_item(
            ArrSelect(
                on_select=self.set_folder,
                placeholder="Root folder…",
                options=[
                    discord.SelectOption(
                        label=folder.path[:100],
                        value=folder.path,
                        description=f"{folder.free_space_display} free"[:100],
                        default=folder.path == self.folder_path,
                    )
                    for folder in self.cog.folders[:25]
                ],
            ),
        )
        return row

    def setting_row(self) -> discord.ui.ActionRow:
        """Returns the service-specific setting select. Override per subclass."""
        raise NotImplementedError

    def actions(self) -> discord.ui.ActionRow:
        """Returns the add/cancel action row."""
        row = discord.ui.ActionRow()
        addable: bool = self.chosen is not None and self.chosen.in_library is False
        row.add_item(
            ArrButton(on_press=self.perform_add, label="Add", emoji="➕", style=discord.ButtonStyle.success, disabled=addable is False)
        )
        if self.chosen is not None and self.chosen.in_library is True:
            row.add_item(ArrButton(on_press=self.open_chosen, label="Open it", emoji="🔍"))
        row.add_item(ArrButton(on_press=self.cancel, label="Cancel"))
        return row

    def _rebuild(self, *, chosen: Optional[Series], profile_id: int, folder_path: str) -> AddPanel:
        """Rebuild with updated values. Subclass adds its own setting kwarg."""
        raise NotImplementedError

    async def perform_add(self, interaction: discord.Interaction) -> None:
        """Add the chosen result to the library. Override per subclass."""
        raise NotImplementedError

    # Button / select handlers.

    async def cancel(self, interaction: discord.Interaction) -> None:
        """Dismiss the add panel without adding."""
        self.stop()
        await interaction.response.edit_message(view=SettledPanel(note=f"Nothing added. {self.cog.emoji_table.kuma_shrug}"))

    async def open_chosen(self, interaction: discord.Interaction) -> None:
        """Open the detail panel for the chosen item already in the library."""
        if self.chosen is None:
            return
        media: Optional[Series] = await self.api.get_series(series_id=self.chosen.id)
        if media is not None:
            await interaction.response.edit_message(view=self._build_detail(media=media))

    async def choose_result(self, interaction: discord.Interaction, value: str) -> None:
        """Apply the selected search result and rebuild."""
        chosen: Series = self.results[int(value)]
        await interaction.response.edit_message(
            view=self._rebuild(chosen=chosen, profile_id=self.profile_id, folder_path=self.folder_path),
        )

    async def set_profile(self, interaction: discord.Interaction, value: str) -> None:
        """Apply the selected quality profile and rebuild."""
        await interaction.response.edit_message(
            view=self._rebuild(chosen=self.chosen, profile_id=int(value), folder_path=self.folder_path),
        )

    async def set_folder(self, interaction: discord.Interaction, value: str) -> None:
        """Apply the selected root folder and rebuild."""
        await interaction.response.edit_message(
            view=self._rebuild(chosen=self.chosen, profile_id=self.profile_id, folder_path=value),
        )


class SeriesAddPanel(AddPanel):
    """Sonarr add panel with :attr:`monitor` type selection."""

    def __init__(self, *, monitor: MonitorType = MonitorType.all, **kwargs: Any) -> None:
        self.monitor: MonitorType = monitor
        super().__init__(**kwargs)

    def setting_row(self) -> discord.ui.ActionRow:
        """Returns the monitoring select."""
        row = discord.ui.ActionRow()
        row.add_item(
            ArrSelect(
                on_select=self.set_monitor,
                placeholder="What to monitor…",
                options=[
                    discord.SelectOption(label=label, value=monitor.value, description=description, default=monitor is self.monitor)
                    for monitor, label, description in MONITOR_CHOICES
                ],
            ),
        )
        return row

    async def set_monitor(self, interaction: discord.Interaction, value: str) -> None:
        """Apply the chosen monitor type and rebuild."""
        self.monitor = MonitorType(value)
        await interaction.response.edit_message(
            view=self._rebuild(chosen=self.chosen, profile_id=self.profile_id, folder_path=self.folder_path),
        )

    def _rebuild(self, *, chosen: Optional[Series], profile_id: int, folder_path: str) -> SeriesAddPanel:
        return SeriesAddPanel(
            cog=self.cog,
            user_id=self.user_id,
            term=self.term,
            results=self.results,
            chosen=chosen,
            profile_id=profile_id,
            folder_path=folder_path,
            monitor=self.monitor,
        )

    async def perform_add(self, interaction: discord.Interaction) -> None:
        """Add the chosen series to Sonarr."""
        if self.chosen is None:
            return
        await interaction.response.defer()
        try:
            added: Series = await self.cog.api.add_series(
                self.chosen,
                quality_profile_id=self.profile_id,
                root_folder_path=self.folder_path,
                monitor=self.monitor,
                search_for_missing=self.monitor is not MonitorType.none,
            )
        except SonarrValidationError as e:
            await interaction.followup.send(
                content=f"Sonarr said no {self.cog.unicode.right_triangle_arrow} {e.summary} {self.cog.emoji_table.kuma_pout}",
                ephemeral=True,
            )
            return
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e, deferred=True)
            return

        self.stop()
        note: str = (
            f"Added to `{self.folder_path}` {self.cog.unicode.middle_dot} "
            f"monitoring **{self.monitor.value}** {self.cog.emoji_table.kuma_happy}"
        )
        LOGGER.info("<%s.%s> | Added | Title: %s | Id: %s", __class__.__name__, "perform_add", added.title, added.id)
        await interaction.edit_original_response(view=self._build_detail(media=added, note=note))


class MovieAddPanel(AddPanel):
    """Radarr add panel with :attr:`availability` selection."""

    def __init__(self, *, availability: str = "released", **kwargs: Any) -> None:
        self.availability: str = availability
        super().__init__(**kwargs)

    def setting_row(self) -> discord.ui.ActionRow:
        """Returns the minimum availability select."""
        row = discord.ui.ActionRow()
        row.add_item(
            ArrSelect(
                on_select=self.set_availability,
                placeholder="Minimum availability…",
                options=[
                    discord.SelectOption(label=label, value=avail_value, description=description, default=avail_value == self.availability)
                    for avail_value, label, description in AVAILABILITY_CHOICES
                ],
            ),
        )
        return row

    async def set_availability(self, interaction: discord.Interaction, value: str) -> None:
        """Apply the chosen availability and rebuild."""
        self.availability = value
        await interaction.response.edit_message(
            view=self._rebuild(chosen=self.chosen, profile_id=self.profile_id, folder_path=self.folder_path),
        )

    def _rebuild(self, *, chosen: Optional[Series], profile_id: int, folder_path: str) -> MovieAddPanel:
        return MovieAddPanel(
            cog=self.cog,
            user_id=self.user_id,
            term=self.term,
            results=self.results,
            chosen=chosen,
            profile_id=profile_id,
            folder_path=folder_path,
            availability=self.availability,
        )

    async def perform_add(self, interaction: discord.Interaction) -> None:
        """Add the chosen movie to Radarr."""
        if self.chosen is None:
            return
        api: SonarrAPI = self.cog.api
        assert isinstance(api, RadarrAPI)  # noqa: S101 # type-narrowing; MovieAddPanel is Radarr-only
        await interaction.response.defer()
        try:
            added: Series = await api.add_movie(
                self.chosen,
                quality_profile_id=self.profile_id,
                root_folder_path=self.folder_path,
                minimum_availability=self.availability,
                search_for_movie=True,
            )
        except SonarrValidationError as e:
            await interaction.followup.send(
                content=f"Radarr said no {self.cog.unicode.right_triangle_arrow} {e.summary} {self.cog.emoji_table.kuma_pout}",
                ephemeral=True,
            )
            return
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e, deferred=True)
            return

        self.stop()
        availability_label: str = MOVIE_STATUS_DISPLAY.get(self.availability, self.availability)
        note: str = (
            f"Added to `{self.folder_path}` {self.cog.unicode.middle_dot} "
            f"availability **{availability_label}** {self.cog.emoji_table.kuma_happy}"
        )
        LOGGER.info("<%s.%s> | Added | Title: %s | Id: %s", __class__.__name__, "perform_add", added.title, added.id)
        await interaction.edit_original_response(view=self._build_detail(media=added, note=note))


# endregion

# region --- Remove panel ---


class RemovePanel(ArrPanel):
    """Removal confirmation panel with a delete-files toggle."""

    def __init__(
        self,
        *,
        cog: ArrCog,
        user_id: int,
        media: Series,
        delete_files: bool = False,
        expires_at: Optional[float] = None,
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.media: Series = media
        self.delete_files: bool = delete_files
        # A destructive confirmation states a deadline, so it has to hold one. `View.timeout`
        # measures inactivity and restarts on every press, so toggling the files switch would
        # extend it forever; this is carried across a re-render instead.
        self.expires_at: float = expires_at if expires_at is not None else time.time() + PANEL_TIMEOUT

        container = discord.ui.Container(accent_colour=discord.Colour.from_str("#B71C1C"))
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_hmm} Remove {media.display_title}?"))
        container.add_item(discord.ui.Section(self.consequences(), accessory=self.poster(media)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# This expires <t:{int(self.expires_at)}:R>"))
        container.add_item(self.actions())
        self.add_item(container)

    @property
    def expired(self) -> bool:
        """Whether the confirmation has passed its deadline."""
        return time.time() >= self.expires_at

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects expired or wrong-user interactions."""
        if not await super().interaction_check(interaction):
            return False
        if self.expired is True:
            self.stop()
            await interaction.response.edit_message(
                view=SettledPanel(note=f"That removal expired without an answer. {self.cog.emoji_table.kuma_shrug}"),
            )
            return False
        return True

    def consequences(self) -> str:
        """Returns the description of what removal will do."""
        media: Series = self.media
        lines: list[str] = [f"-# {media.path}" if media.path else "-# No path on disk."]
        if self.delete_files is True:
            if self.is_sonarr is True:
                lines.append(
                    f"**{media.size_display}** across {media.episode_file_count} files "
                    f"**will be deleted.** {self.cog.emoji_table.kuma_shock}"
                )
            else:
                lines.append(f"**{media.size_display}** **will be deleted.** {self.cog.emoji_table.kuma_shock}")
        else:
            lines.append(f"The entry is removed; **{media.size_display}** of files stay on disk.")
        return "\n".join(lines)

    def actions(self) -> discord.ui.ActionRow:
        """Returns the action row with toggle, confirm, and cancel."""
        row = discord.ui.ActionRow()
        row.add_item(
            ArrButton(
                on_press=self.toggle_files,
                label="Delete files" if self.delete_files is True else "Keep files",
                emoji=self.cog.emoji_table.kuma_shock if self.delete_files is True else self.cog.emoji_table.kuma_happy,
                style=discord.ButtonStyle.danger if self.delete_files is True else discord.ButtonStyle.secondary,
            ),
        )
        row.add_item(ArrButton(on_press=self.confirm_remove, label="Remove", style=discord.ButtonStyle.danger))
        row.add_item(ArrButton(on_press=self.cancel, label="Cancel", style=discord.ButtonStyle.success))
        return row

    # Button handlers.

    async def toggle_files(self, interaction: discord.Interaction) -> None:
        """Flip the delete-files switch and rebuild."""
        await interaction.response.edit_message(
            view=RemovePanel(
                cog=self.cog,
                user_id=self.user_id,
                media=self.media,
                delete_files=not self.delete_files,
                expires_at=self.expires_at,
            ),
        )

    async def confirm_remove(self, interaction: discord.Interaction) -> None:
        """Delete the media item and show the outcome."""
        await interaction.response.defer()
        try:
            # `delete_series` works for movies too; the library resource routes by id.
            await self.api.delete_series(series_id=self.media.id, delete_files=self.delete_files)
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e, deferred=True)
            return

        self.stop()
        dot: str = self.cog.unicode.middle_dot
        fate: str = "and its files were deleted" if self.delete_files is True else "the files were left on disk"
        LOGGER.info(
            "<%s.%s> | Removed | Title: %s | Files deleted: %s",
            __class__.__name__,
            "confirm_remove",
            self.media.title,
            self.delete_files,
        )
        await interaction.edit_original_response(
            view=SettledPanel(note=f"Removed **{self.media.display_title}** {dot} {fate}. {self.cog.emoji_table.kuma_happy}"),
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        """Cancel removal and return to the detail panel."""
        self.stop()
        await interaction.response.edit_message(
            view=self._build_detail(media=self.media, note=f"Kept. {self.cog.emoji_table.kuma_happy}"),
        )


# endregion

# region --- Status panels ---


class StatusPanel(ArrPanel):
    """Instance status panel with queue, health, disk space, and version.

    :meth:`library_block` is overridden per service for episode vs. movie counting.
    """

    def __init__(
        self,
        *,
        cog: ArrCog,
        user_id: int,
        status: SystemStatus,
        queue: list[QueueRecord],
        warnings: list[Health],
        mounts: list[DiskSpace],
        library: list[Series],
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.status: SystemStatus = status

        # Red when something needs a person, amber when a download is stuck, green otherwise.
        errors: bool = any(warning.is_error for warning in warnings)
        stalled: bool = any(record.stalled for record in queue)
        accent_colour: discord.Colour = (
            discord.Colour.from_str("#B71C1C")
            if errors is True
            else discord.Colour.from_str("#FFB300")
            if stalled is True or len(warnings) > 0
            else discord.Colour.from_str("#4CAF50")
        )

        container = discord.ui.Container(accent_colour=accent_colour)
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_tea} {status.instance_name}\n-# {self.headline()}"))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(discord.ui.TextDisplay(self.queue_block(queue=queue)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.library_block(library=library, mounts=mounts)))

        if len(warnings) > 0:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(self.health_block(warnings=warnings)))

        row = discord.ui.ActionRow()
        row.add_item(ArrButton(on_press=self.reload, label="Refresh", emoji="🔄"))
        row.add_item(discord.ui.Button(label=f"Open in {self.service_name}", style=discord.ButtonStyle.link, url=cog.base_url))
        container.add_item(row)
        self.add_item(container)

    def headline(self) -> str:
        """Returns the version and event-listener status line."""
        dot: str = self.cog.unicode.middle_dot
        listening: str = "live" if self.cog.api.listening is True else "polling"
        parts: list[str] = [f"v{self.status.version}", self.status.branch]
        if self.status.is_docker is True:
            parts.append("docker")
        parts.append(f"events {listening}")
        if self.status.start_time is not None:
            parts.append(f"up since {self.cog.to_discord_timestamp(time=self.status.start_time, style='R')}")
        return f" {dot} ".join(parts)

    def queue_block(self, queue: list[QueueRecord]) -> str:
        """Returns the active download queue block."""
        if len(queue) == 0:
            return f"### Queue\n-# Nothing downloading. {self.cog.emoji_table.kuma_shrug}"

        dot: str = self.cog.unicode.middle_dot
        lines: list[str] = [f"### Queue {dot} {len(queue)} items"]
        for record in queue[:QUEUE_LIMIT]:
            marker: str = f" {self.cog.emoji_table.kuma_sad}" if record.stalled is True else ""
            eta: str = ""
            if record.estimated_completion_time is not None and record.stalled is False:
                eta = f" {dot} {self.cog.to_discord_timestamp(time=record.estimated_completion_time, style='R')}"
            lines.append(f"- **{record.title[:70]}**{marker}")
            lines.append(f"  -# {progress_bar(record.progress, width=10)} {record.progress:.0f}% {dot} {record.size_display}{eta}")
        if len(queue) > QUEUE_LIMIT:
            lines.append(f"-# …and {len(queue) - QUEUE_LIMIT} more.")
        return "\n".join(lines)

    def library_block(self, library: list[Series], mounts: list[DiskSpace]) -> str:
        """Returns the library totals and disk space. Override per service."""
        raise NotImplementedError

    def health_block(self, warnings: list[Health]) -> str:
        """Returns the health warnings block."""
        emoji_table = self.cog.emoji_table
        tri: str = self.cog.unicode.right_triangle_arrow
        lines: list[str] = [f"### Health {emoji_table.kuma_hmm}"]
        for warning in warnings[:5]:
            marker: str = emoji_table.kuma_shock if warning.is_error is True else emoji_table.kuma_hmm
            lines.append(f"- {marker} **{warning.source}** {tri} {warning.message[:150]}")
        return "\n".join(lines)

    # Button handler.

    async def reload(self, interaction: discord.Interaction) -> None:
        """Re-fetch status data and rebuild the panel."""
        await interaction.response.defer()
        try:
            panel: StatusPanel = await self.cog.build_status(user_id=self.user_id)
        except SonarrError as e:
            await self.cog.report(interaction=interaction, error=e, deferred=True)
            return
        await interaction.edit_original_response(view=panel)


class SeriesStatusPanel(StatusPanel):
    """Sonarr status panel with episode and continuing series counts."""

    def library_block(self, library: list[Series], mounts: list[DiskSpace]) -> str:
        """Returns episode-based library totals and disk space."""
        dot: str = self.cog.unicode.middle_dot
        tri: str = self.cog.unicode.right_triangle_arrow
        missing: int = sum(series.missing_episode_count for series in library)
        continuing: int = sum(1 for series in library if series.continuing is True)
        lines: list[str] = [
            "### Library",
            f"- **Series** {tri} {len(library)} ({continuing} continuing)",
            f"- **Episodes** {tri} {sum(series.episode_file_count for series in library):,} on disk {dot} {missing:,} missing",
            f"- **Size** {tri} {to_size(sum(series.size_on_disk for series in library))}",
        ]
        lines.extend(
            f"- **{mount.path}** {tri} {mount.free_space_display} free of {mount.total_space_display} ({mount.used_percent:.0f}% used)"
            for mount in mounts[:3]
        )
        return "\n".join(lines)


class MovieStatusPanel(StatusPanel):
    """Radarr status panel with movie file counts and monitored gaps."""

    def library_block(self, library: list[Series], mounts: list[DiskSpace]) -> str:
        """Returns movie-based library totals and disk space."""
        tri: str = self.cog.unicode.right_triangle_arrow
        on_disk: int = sum(1 for movie in library if movie.has_file is True)
        missing: int = sum(1 for movie in library if movie.monitored is True and movie.has_file is False)
        lines: list[str] = [
            "### Library",
            f"- **Movies** {tri} {len(library)} ({on_disk} on disk)",
            f"- **Missing** {tri} {missing:,} monitored without a file",
            f"- **Size** {tri} {to_size(sum(movie.size_on_disk for movie in library))}",
        ]
        lines.extend(
            f"- **{mount.path}** {tri} {mount.free_space_display} free of {mount.total_space_display} ({mount.used_percent:.0f}% used)"
            for mount in mounts[:3]
        )
        return "\n".join(lines)


# endregion

# region --- Search panel ---


class SearchPanel(ArrPanel):
    """Paginated search results panel."""

    def __init__(
        self,
        *,
        cog: ArrCog,
        user_id: int,
        term: str,
        results: list[Series],
        page: int = 0,
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.term: str = term
        self.results: list[Series] = results
        self.pages: int = max(1, -(-len(results) // LIBRARY_PER_PAGE))
        self.page: int = max(0, min(page, self.pages - 1))

        start: int = self.page * LIBRARY_PER_PAGE
        self.window: list[Series] = results[start : start + LIBRARY_PER_PAGE]

        container = discord.ui.Container(accent_colour=UNADDED_ACCENT)
        container.add_item(
            discord.ui.TextDisplay(
                f"## {cog.emoji_table.kuma_peak} Search results\n-# {len(results)} results for `{term}` {self.page_label()}"
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        if len(self.window) == 0:
            container.add_item(discord.ui.TextDisplay(f"-# No results. {cog.emoji_table.kuma_shrug}"))
        for media in self.window:
            container.add_item(discord.ui.Section(self.row(media), accessory=self.poster(media)))

        if len(self.window) > 0:
            picker = discord.ui.ActionRow()
            picker.add_item(
                ArrSelect(
                    on_select=self.open_result,
                    placeholder="Open a result…",
                    options=[
                        discord.SelectOption(
                            label=media.display_title[:100],
                            value=str(index + start),
                            description=self.option_detail(media)[:100],
                        )
                        for index, media in enumerate(self.window)
                    ],
                ),
            )
            container.add_item(picker)

        self.add_item(container)
        self.add_item(self.navigation())

    def page_label(self) -> str:
        """Returns the page indicator for multi-page results."""
        dot: str = self.cog.unicode.middle_dot
        return f"{dot} page {self.page + 1} of {self.pages}" if self.pages > 1 else ""

    def row(self, media: Series) -> str:
        """Returns the two-line summary beside a result's poster."""
        dot: str = self.cog.unicode.middle_dot
        parts: list[str] = [self.status_label(media)]
        if self.is_sonarr is True:
            if media.network:
                parts.append(media.network)
            parts.append(f"{media.season_count} seasons")
        else:
            studio: Optional[str] = movie_studio(media)
            if studio:
                parts.append(studio)
            if media.year:
                parts.append(str(media.year))
        if media.in_library is True:
            parts.append("in library")
        detail: str = f" {dot} ".join(parts)
        return f"**{media.display_title}**\n-# {detail}"

    def option_detail(self, media: Series) -> str:
        """Returns the description line for a select option."""
        if media.in_library is True:
            return "Already in library"
        if self.is_sonarr is True:
            return f"TVDB {media.tvdb_id}" if media.tvdb_id else self.status_label(media)
        tmdb_id: int = media._raw.get("tmdbId", 0)  # noqa: SLF001
        return f"TMDB {tmdb_id}" if tmdb_id else self.status_label(media)

    def navigation(self) -> discord.ui.ActionRow:
        """Returns the pagination row."""
        row = discord.ui.ActionRow()
        if self.pages > 1:
            row.add_item(ArrButton(on_press=self.prev_page, label="Prev", disabled=self.page == 0))
            row.add_item(ArrButton(on_press=self.next_page, label="Next", disabled=self.page >= self.pages - 1))
        row.add_item(ArrButton(on_press=self.close, label="Close"))
        return row

    # Button / select handlers.

    async def close(self, interaction: discord.Interaction) -> None:
        """Dismiss the search panel."""
        self.stop()
        await interaction.response.edit_message(view=SettledPanel(note=f"Search closed. {self.cog.emoji_table.kuma_shrug}"))

    async def open_result(self, interaction: discord.Interaction, value: str) -> None:
        """Open the detail panel for a selected search result."""
        media: Series = self.results[int(value)]
        # If already in the library, fetch the full record for accurate stats.
        if media.in_library is True:
            fresh: Optional[Series] = await self.api.get_series(series_id=media.id)
            if fresh is not None:
                media = fresh
        await interaction.response.edit_message(view=self._build_detail(media=media))

    async def prev_page(self, interaction: discord.Interaction) -> None:
        """Navigate to the previous page."""
        await interaction.response.edit_message(
            view=SearchPanel(cog=self.cog, user_id=self.user_id, term=self.term, results=self.results, page=self.page - 1),
        )

    async def next_page(self, interaction: discord.Interaction) -> None:
        """Navigate to the next page."""
        await interaction.response.edit_message(
            view=SearchPanel(cog=self.cog, user_id=self.user_id, term=self.term, results=self.results, page=self.page + 1),
        )


# endregion

# region --- Notification panel ---


class NotificationPanel(discord.ui.LayoutView):
    """Non-interactive announcement sent to a channel when media is added or imported.

    No ``timeout`` needed; no interactive components means nothing for the framework to collect.
    """

    def __init__(
        self,
        *,
        cog: ArrCog,
        media: Series,
        event: str,
    ) -> None:
        super().__init__(timeout=None)
        accent: discord.Colour = (
            (accent_for_series(media) if isinstance(cog, SonarrCog) else accent_for_movie(media))
            if media.in_library is True
            else NOTIFICATION_ACCENT
        )

        container = discord.ui.Container(accent_colour=accent)

        # Artwork.
        backdrop: Optional[str] = media.fanart or media.banner
        if backdrop is not None:
            container.add_item(
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=backdrop, description=f"{media.title} artwork")),
            )

        # Header.
        container.add_item(discord.ui.TextDisplay(f"## {media.display_title}\n-# {event}"))

        # Poster + overview.
        poster: Optional[str] = media.poster
        if poster is not None:
            accessory: discord.ui.Item = discord.ui.Thumbnail(media=poster, description=f"{media.title} poster")
        else:
            accessory = discord.ui.TextDisplay("-# Poster unavailable.")
        container.add_item(discord.ui.Section(truncate(media.overview), accessory=accessory))

        # Link button.
        if isinstance(cog, SonarrCog):
            web: Optional[str] = media.web_url(cog.base_url)
            label: str = "Sonarr"
        else:
            web = movie_web_url(media, cog.base_url)
            label = "Radarr"
        if web is not None:
            row = discord.ui.ActionRow()
            row.add_item(discord.ui.Button(label=f"Open in {label}", style=discord.ButtonStyle.link, url=web))
            container.add_item(row)

        self.add_item(container)


# Event toggle descriptions shown in the settings panel.
_EVENT_TOGGLES: tuple[tuple[str, str, str], ...] = (
    ("media_added", "Media added", "Series or movie appears in the library."),
    ("media_removed", "Media removed", "Series or movie is removed from the library."),
    ("file_imported", "File imported", "Episode or movie file lands on disk."),
    ("grab", "Grab", "A download is picked up from the indexer."),
    ("upgrade", "Upgrade", "A higher quality file replaces the existing one."),
    ("health_warning", "Health warnings", "Instance reports an error or warning."),
)


class NotificationSettingsPanel(ArrPanel):
    """Toggle panel for choosing which SignalR events post to the notification channel."""

    def __init__(self, *, cog: ArrCog, user_id: int) -> None:
        super().__init__(cog=cog, user_id=user_id)
        cfg: Optional[NotificationConfig] = cog._notifications  # noqa: SLF001

        container = discord.ui.Container(accent_colour=NOTIFICATION_ACCENT)
        container.add_item(
            discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_tea} {cog.service_name} Notifications"),
        )

        # Channel status.
        if cfg is not None:
            channel_obj: Optional[Any] = cog.bot.get_channel(cfg.channel_id)
            channel_label: str = channel_obj.mention if channel_obj is not None else f"`{cfg.channel_id}`"
            container.add_item(discord.ui.TextDisplay(f"-# Posting to {channel_label}"))
        else:
            cmd_name: str = "sonarr" if isinstance(cog, SonarrCog) else "radarr"
            container.add_item(discord.ui.TextDisplay(f"-# No channel set. Use `/{cmd_name} notifications #channel` to pick one."))

        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        # Event toggles; each section has its own on/off accessory button.
        for field, label, description in _EVENT_TOGGLES:
            enabled: bool = getattr(cfg, field, False) if cfg is not None else False
            container.add_item(
                discord.ui.Section(
                    f"**{label}**\n-# {description}",
                    accessory=self._toggle_button(field=field, enabled=enabled, disabled=cfg is None),
                ),
            )

        container.add_item(discord.ui.Separator())

        # Disable all row.
        disable_row = discord.ui.ActionRow()
        disable_row.add_item(
            ArrButton(
                on_press=self.disable_all,
                label="Disable notifications",
                style=discord.ButtonStyle.danger,
                disabled=cfg is None,
            ),
        )
        container.add_item(disable_row)

        self.add_item(container)

    def _toggle_button(self, *, field: str, enabled: bool, disabled: bool) -> ArrButton:
        """Returns the on/off accessory for one event toggle."""
        return ArrButton(
            on_press=self._make_toggle(field),
            label="On" if enabled is True else "Off",
            emoji="✔️" if enabled is True else "✖️",
            style=discord.ButtonStyle.success if enabled is True else discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    def _make_toggle(self, field: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        """Returns a handler that flips one toggle and rebuilds the panel."""

        async def toggle(interaction: discord.Interaction) -> None:
            cfg: Optional[NotificationConfig] = self.cog._notifications  # noqa: SLF001
            if cfg is None:
                return
            current: bool = getattr(cfg, field)
            updated: NotificationConfig = cfg._replace(**{field: not current})
            await self.cog._save_notifications(config=updated)  # noqa: SLF001
            await interaction.response.edit_message(
                view=NotificationSettingsPanel(cog=self.cog, user_id=self.user_id),
            )

        return toggle

    async def disable_all(self, interaction: discord.Interaction) -> None:
        """Clear the notification config entirely."""
        await self.cog._save_notifications(config=None)  # noqa: SLF001
        await interaction.response.edit_message(
            view=NotificationSettingsPanel(cog=self.cog, user_id=self.user_id),
        )


# endregion
# endregion


# region --- Sonarr cog ---


class SonarrCog(ArrCog, name="Sonarr"):
    """Sonarr cog. Reads are served from the :class:`SonarrAPI` cache kept current by SignalR."""

    service_name: ClassVar[str] = "Sonarr"
    ini_section: ClassVar[str] = "SONARR"
    external_db: ClassVar[str] = "TVDB"
    client_cls: ClassVar[type[SonarrAPI]] = SonarrAPI
    detail_cls: ClassVar[type[DetailPanel]] = SeriesDetailPanel
    add_cls: ClassVar[type[AddPanel]] = SeriesAddPanel
    status_cls: ClassVar[type[StatusPanel]] = SeriesStatusPanel

    sonarr_group = app_commands.Group(name="sonarr", description="Manage the Sonarr library.", guild_only=False)

    @sonarr_group.command(name="list", description="Show the Sonarr library.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        filter_by="Only show series whose title contains this.",
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    async def sonarr_list(
        self,
        interaction: discord.Interaction,
        filter_by: Optional[str] = None,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Opens the library panel."""
        await self._cmd_list(interaction, filter_by, ephemeral, access)

    @sonarr_group.command(name="info", description="Everything Sonarr knows about one series.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        series="Start typing a title from your library.",
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    @app_commands.autocomplete(series=ArrCog.autocomplete)
    async def sonarr_info(
        self,
        interaction: discord.Interaction,
        series: str,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Opens the detail panel for one series."""
        await self._cmd_info(interaction, series, ephemeral, access)

    @sonarr_group.command(name="search", description="Search TVDB and browse results.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        term="A title, or an id term such as tvdb:121361.",
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    async def sonarr_search(
        self,
        interaction: discord.Interaction,
        term: str,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Looks the term up and opens the paginated search panel."""
        await self._cmd_search(interaction, term, ephemeral, access)

    @sonarr_group.command(name="add", description="Search TVDB and add a series to Sonarr.")
    @app_commands.check(_owner_only)
    @app_commands.describe(term="A title, or an id term such as tvdb:121361.")
    async def sonarr_add(self, interaction: discord.Interaction, term: str) -> None:
        """Looks the term up and opens the add panel."""
        await self._cmd_add(interaction, term)

    @sonarr_group.command(name="remove", description="Remove a series from Sonarr.")
    @app_commands.check(_owner_only)
    @app_commands.describe(series="Start typing a title from your library.")
    @app_commands.autocomplete(series=ArrCog.autocomplete)
    async def sonarr_remove(self, interaction: discord.Interaction, series: str) -> None:
        """Opens the removal confirmation."""
        await self._cmd_remove(interaction, series)

    @sonarr_group.command(name="status", description="What Sonarr is downloading, and how it is doing.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    async def sonarr_status(
        self,
        interaction: discord.Interaction,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Opens the instance status panel."""
        await self._cmd_status(interaction, ephemeral, access)

    @sonarr_group.command(name="notifications", description="Configure Sonarr notification events and channel.")
    @app_commands.check(_owner_only)
    @app_commands.describe(channel="Set or move the notification channel. Omit to open the settings panel.")
    async def sonarr_notifications(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        """Open the notification settings panel."""
        await self._cmd_notifications(interaction, channel)


# endregion


# region --- Radarr cog ---


class RadarrCog(ArrCog, name="Radarr"):
    """Radarr cog. Reads are served from the :class:`RadarrAPI` cache kept current by SignalR."""

    service_name: ClassVar[str] = "Radarr"
    ini_section: ClassVar[str] = "RADARR"
    external_db: ClassVar[str] = "TMDB"
    client_cls: ClassVar[type[SonarrAPI]] = RadarrAPI
    detail_cls: ClassVar[type[DetailPanel]] = MovieDetailPanel
    add_cls: ClassVar[type[AddPanel]] = MovieAddPanel
    status_cls: ClassVar[type[StatusPanel]] = MovieStatusPanel

    radarr_group = app_commands.Group(name="radarr", description="Manage the Radarr library.", guild_only=False)

    @radarr_group.command(name="list", description="Show the Radarr library.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        filter_by="Only show movies whose title contains this.",
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    async def radarr_list(
        self,
        interaction: discord.Interaction,
        filter_by: Optional[str] = None,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Opens the library panel."""
        await self._cmd_list(interaction, filter_by, ephemeral, access)

    @radarr_group.command(name="info", description="Everything Radarr knows about one movie.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        movie="Start typing a title from your library.",
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    @app_commands.autocomplete(movie=ArrCog.autocomplete)
    async def radarr_info(
        self,
        interaction: discord.Interaction,
        movie: str,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Opens the detail panel for one movie."""
        await self._cmd_info(interaction, movie, ephemeral, access)

    @radarr_group.command(name="search", description="Search TMDB and browse results.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        term="A title, or an id term such as tmdb:550.",
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    async def radarr_search(
        self,
        interaction: discord.Interaction,
        term: str,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Looks the term up and opens the paginated search panel."""
        await self._cmd_search(interaction, term, ephemeral, access)

    @radarr_group.command(name="add", description="Search TMDB and add a movie to Radarr.")
    @app_commands.check(_owner_only)
    @app_commands.describe(term="A title, or an id term such as tmdb:550.")
    async def radarr_add(self, interaction: discord.Interaction, term: str) -> None:
        """Looks the term up and opens the add panel."""
        await self._cmd_add(interaction, term)

    @radarr_group.command(name="remove", description="Remove a movie from Radarr.")
    @app_commands.check(_owner_only)
    @app_commands.describe(movie="Start typing a title from your library.")
    @app_commands.autocomplete(movie=ArrCog.autocomplete)
    async def radarr_remove(self, interaction: discord.Interaction, movie: str) -> None:
        """Opens the removal confirmation."""
        await self._cmd_remove(interaction, movie)

    @radarr_group.command(name="status", description="What Radarr is downloading, and how it is doing.")
    @app_commands.check(_owner_only)
    @app_commands.describe(
        ephemeral="Hide the response so only you can see it (default True).",
        access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).",
    )
    async def radarr_status(
        self,
        interaction: discord.Interaction,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> None:
        """Opens the instance status panel."""
        await self._cmd_status(interaction, ephemeral, access)

    @radarr_group.command(name="notifications", description="Configure Radarr notification events and channel.")
    @app_commands.check(_owner_only)
    @app_commands.describe(channel="Set or move the notification channel. Omit to open the settings panel.")
    async def radarr_notifications(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        """Open the notification settings panel."""
        await self._cmd_notifications(interaction, channel)


# endregion


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(SonarrCog(bot=bot))
    await bot.add_cog(RadarrCog(bot=bot))
