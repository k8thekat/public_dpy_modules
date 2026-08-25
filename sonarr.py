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

import logging
import time
from configparser import ConfigParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Optional, Union

import discord

# TEMPORARY: `async_sonarr` moved out to ~/gitHub/async_sonarr and is not on PyPI yet, so this
# resolves through an editable install rather than a first party directory:
#   pip install -e ~/gitHub/async_sonarr --no-deps
# The import itself does not change once it is published.
from async_sonarr import (
    CoverType,
    DiskSpace,
    Health,
    MonitorType,
    QualityProfile,
    QueueRecord,
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

from utils import KumaCog as Cog

if TYPE_CHECKING:
    from kuma_kuma import Kuma_Kuma

LOGGER = logging.getLogger()

PANEL_TIMEOUT: float = 300.0

# Five rows keeps a page inside both budgets at once: the 40 component cap (each row is a Section, a
# TextDisplay and a Thumbnail) and the 4000 characters discord.py will not check for us.
LIBRARY_PER_PAGE: int = 5

# An overview runs to several paragraphs on TVDB, and a panel that is mostly synopsis buries the
# numbers underneath it.
OVERVIEW_LIMIT: int = 300
QUEUE_LIMIT: int = 8
PROGRESS_WIDTH: int = 12

# Sonarr's own colour language: green is producing, grey has finished, gold has not started yet.
STATUS_ACCENTS: dict[str, discord.Colour] = {
    SeriesStatus.continuing.value: discord.Colour.from_str("#4CAF50"),
    SeriesStatus.ended.value: discord.Colour.from_str("#607D8B"),
    SeriesStatus.upcoming.value: discord.Colour.from_str("#FFB300"),
    SeriesStatus.deleted.value: discord.Colour.from_str("#B71C1C"),
}
UNADDED_ACCENT: discord.Colour = discord.Colour.from_str("#5865F2")

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


class SonarrSettings(NamedTuple):
    """The `[SONARR]` section of `local.ini`."""

    url: str
    api_key: str
    url_base: str


def load_settings() -> Optional[SonarrSettings]:
    """Read the Sonarr credentials out of `local.ini`.

    Read here rather than through :class:`KumaConfig`, so that an instance nobody has configured yet
    disables this one cog instead of stopping the bot from starting.

    Returns
    -------
    :class:`Optional[SonarrSettings]`
        The settings, or `None` when the section or the key is missing.

    """
    path: Path = Path(__file__).parent.parent.joinpath("local.ini")
    if not path.is_file():
        return None

    settings = ConfigParser()
    settings.read(filenames=path.as_posix())
    url: Optional[str] = settings.get(section="SONARR", option="url", fallback=None)
    api_key: Optional[str] = settings.get(section="SONARR", option="api_key", fallback=None)
    if not url or not api_key:
        return None
    return SonarrSettings(url=url, api_key=api_key, url_base=settings.get(section="SONARR", option="url_base", fallback=""))


def accent_for(series: Series) -> discord.Colour:
    """Returns the container accent that matches a series' status."""
    if not series.in_library:
        return UNADDED_ACCENT
    return STATUS_ACCENTS.get(str(series.status), discord.Colour.blurple())


def truncate(text: Optional[str], limit: int = OVERVIEW_LIMIT) -> str:
    """Returns `text` cut to `limit`, broken on a word rather than mid-syllable."""
    if not text:
        return "-# No overview."
    body: str = " ".join(text.split())
    if len(body) <= limit:
        return body
    return f"{body[:limit].rsplit(' ', 1)[0]}…"


def progress_bar(percent: float, width: int = PROGRESS_WIDTH) -> str:
    """Returns a filled bar for a percentage.

    A bar reads faster than a number in a list of ten, and unlike a code fence it can sit beside bold
    text and emoji.
    """
    filled: int = round((max(0.0, min(100.0, percent)) / 100) * width)
    return f"{'▰' * filled}{'▱' * (width - filled)}"


class SonarrButton(discord.ui.Button):
    """A button that hands its press back to the panel that built it, via `action`.

    Every panel here is built imperatively from live data, so there is no static layout to declare with
    `@discord.ui.button`.
    """

    def __init__(
        self,
        *,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji, disabled=disabled)
        self.action: str = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the press to the owning panel."""
        panel: Optional[Any] = self.view
        if panel is not None:
            await panel.dispatch(interaction=interaction, action=self.action)


class SonarrSelect(discord.ui.Select):
    """A select that hands its choice back to the panel that built it, via `action`.

    .. warning::
        Always added inside a :class:`discord.ui.ActionRow`. discord.py will serialise a select placed
        straight onto a `Container`, and Discord answers that with a 400 at send time.

    """

    def __init__(self, *, action: str, placeholder: str, options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder=placeholder, options=options or [discord.SelectOption(label="Nothing to pick")], min_values=1, max_values=1
        )
        self.action: str = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the choice to the owning panel."""
        panel: Optional[Any] = self.view
        if panel is not None:
            await panel.dispatch(interaction=interaction, action=self.action, value=self.values[0])


class SonarrPanel(discord.ui.LayoutView):
    """Shared plumbing for every panel in this cog.

    .. warning::
        A Components V2 message cannot carry `content` or `embeds`, so anything a reader needs has to
        be a `TextDisplay` inside the layout.

    """

    def __init__(self, *, cog: SonarrCog, user_id: int) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog: SonarrCog = cog
        self.user_id: int = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone but the person the panel was opened for."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                content=f"That panel isn't yours! {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    async def dispatch(self, interaction: discord.Interaction, action: str, value: Optional[str] = None) -> None:
        """Handle one press or choice; overridden by every panel."""
        raise NotImplementedError

    def art(self, series: Series) -> Optional[discord.ui.MediaGallery]:
        """Returns the wide backdrop for a series, when TVDB has one.

        Only `remoteUrl` art is usable — the sibling path on the Sonarr host needs the API key, and
        Discord fetches these itself with no way to attach one.
        """
        backdrop: Optional[str] = series.fanart or series.banner
        if backdrop is None:
            return None
        return discord.ui.MediaGallery(discord.MediaGalleryItem(media=backdrop, description=f"{series.title} artwork"))

    def accessory(self, series: Series, *, action: str = "") -> discord.ui.Item:
        """Returns a section accessory: the poster when there is one, otherwise a button that does something.

        .. note::
            A `Section` accessory may only be a button or a thumbnail; discord.py enforces neither, so
            the fallback stays inside that pair deliberately.

        """
        poster: Optional[str] = series.poster
        if poster is not None:
            return discord.ui.Thumbnail(media=poster, description=f"{series.title} poster")
        return SonarrButton(action=action or "noop", label="Details", disabled=not action)

    def links(self, series: Series) -> str:
        """Returns the external id line that goes under a series."""
        parts: list[str] = []
        web: Optional[str] = series.web_url(self.cog.base_url)
        if web is not None:
            parts.append(f"[Sonarr]({web})")
        if series.tvdb_url is not None:
            parts.append(f"[TVDB]({series.tvdb_url})")
        if series.imdb_url is not None:
            parts.append(f"[IMDb]({series.imdb_url})")
        return f"-# {f' {self.cog.unicode.middle_dot} '.join(parts)}" if parts else ""


class SeriesPanel(SonarrPanel):
    """One series in full: its artwork, its numbers, and the actions that apply to it."""

    def __init__(self, *, cog: SonarrCog, user_id: int, series: Series, note: Optional[str] = None) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.series: Series = series

        container = discord.ui.Container(accent_colour=accent_for(series))
        backdrop: Optional[discord.ui.MediaGallery] = self.art(series=series)
        if backdrop is not None:
            container.add_item(backdrop)

        container.add_item(discord.ui.TextDisplay(f"## {series.display_title}\n-# {self.headline()}"))
        container.add_item(discord.ui.Section(truncate(series.overview), accessory=self.accessory(series=series)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.details()))

        links: str = self.links(series=series)
        if links:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(links))

        if note is not None:
            container.add_item(discord.ui.TextDisplay(f"-# {note}"))

        container.add_item(self.actions())
        self.add_item(container)

    def headline(self) -> str:
        """Returns the status line that sits under the title."""
        series: Series = self.series
        dot: str = self.cog.unicode.middle_dot
        parts: list[str] = [str(series.status).title() or "Unknown"]
        if series.network:
            parts.append(series.network)
        if series.certification:
            parts.append(series.certification)
        if series.runtime:
            parts.append(f"{series.runtime}m")
        if not series.in_library:
            parts.append("**not in your library**")
        elif not series.monitored:
            parts.append("unmonitored")
        return f" {dot} ".join(parts)

    def details(self) -> str:
        """Returns the bullet block of numbers.

        A bullet list rather than a table: Discord renders no tables at all, and a code fence would
        cost the bold and the timestamp.
        """
        series: Series = self.series
        dot: str = self.cog.unicode.middle_dot
        lines: list[str] = []

        if series.in_library:
            lines.append(
                f"- **Episodes** — {progress_bar(series.percent_of_episodes)} "
                f"{series.episode_file_count}/{series.episode_count} ({series.percent_of_episodes:.0f}%)",
            )
            lines.append(f"- **On disk** — {series.size_display} across {series.season_count} seasons")
            if series.missing_episode_count:
                lines.append(f"- **Missing** — {series.missing_episode_count} episodes {self.cog.emoji_table.kuma_hmm}")
        else:
            lines.append(f"- **Seasons** — {series.season_count}")

        if series.next_airing is not None:
            lines.append(f"- **Next episode** — {self.cog.to_discord_timestamp(time=series.next_airing, style='R')}")
        elif series.previous_airing is not None:
            lines.append(f"- **Last aired** — {self.cog.to_discord_timestamp(time=series.previous_airing, style='R')}")

        if series.rating is not None:
            lines.append(f"- **Rating** — {series.rating:.1f} {self.cog.unicode.star} ({series.rating_votes:,} votes)")
        if series.genres:
            lines.append(f"- **Genres** — {f' {dot} '.join(series.genres[:5])}")
        return "\n".join(lines)

    def actions(self) -> discord.ui.ActionRow:
        """Returns the row of things that can be done to this series."""
        row = discord.ui.ActionRow()
        if self.series.in_library:
            row.add_item(SonarrButton(action="search", label="Search", emoji="🔍"))
            row.add_item(SonarrButton(action="refresh", label="Refresh", emoji="🔄"))
            row.add_item(SonarrButton(action="remove", label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger))
        web: Optional[str] = self.series.web_url(self.cog.base_url)
        if web is not None:
            # A link button fires no interaction, so it needs no handler.
            row.add_item(discord.ui.Button(label="Open in Sonarr", style=discord.ButtonStyle.link, url=web))
        return row

    async def dispatch(self, interaction: discord.Interaction, action: str, value: Optional[str] = None) -> None:  # noqa: ARG002 # signature is the base's
        """Runs a command against the series, or hands over to the remove confirmation."""
        if action == "remove":
            await interaction.response.edit_message(view=RemovePanel(cog=self.cog, user_id=self.user_id, series=self.series))
            return

        note: str
        try:
            if action == "search":
                await self.cog.sonarr.search_series(series_id=self.series.id)
                note = f"Search queued {self.cog.emoji_table.kuma_tea}"
            elif action == "refresh":
                await self.cog.sonarr.refresh_series(series_id=self.series.id)
                note = f"Refresh queued {self.cog.emoji_table.kuma_tea}"
            else:
                return
        except SonarrError as error:
            await self.cog.report(interaction=interaction, error=error)
            return

        fresh: Series = await self.cog.sonarr.get_series(series_id=self.series.id) or self.series
        await interaction.response.edit_message(view=SeriesPanel(cog=self.cog, user_id=self.user_id, series=fresh, note=note))


class LibraryPanel(SonarrPanel):
    """The library, a page at a time, each row carrying its own poster."""

    def __init__(self, *, cog: SonarrCog, user_id: int, entries: list[Series], page: int = 0) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.entries: list[Series] = entries
        self.pages: int = max(1, -(-len(entries) // LIBRARY_PER_PAGE))
        self.page: int = max(0, min(page, self.pages - 1))

        start: int = self.page * LIBRARY_PER_PAGE
        self.window: list[Series] = entries[start : start + LIBRARY_PER_PAGE]

        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_peak} Sonarr Library"))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        if not self.window:
            container.add_item(discord.ui.TextDisplay("-# Nothing in the library yet."))
        for series in self.window:
            container.add_item(discord.ui.Section(self.row(series=series), accessory=self.accessory(series=series)))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {self.summary()}"))

        if self.window:
            picker = discord.ui.ActionRow()
            picker.add_item(
                SonarrSelect(
                    action="open",
                    placeholder="Open a series…",
                    options=[
                        discord.SelectOption(
                            label=series.display_title[:100],
                            value=str(series.id),
                            description=f"{str(series.status).title()} {self.cog.unicode.middle_dot} {series.size_display}"[:100],
                        )
                        for series in self.window
                    ],
                ),
            )
            container.add_item(picker)

        self.add_item(container)
        # Outside the container: these act *on* the panel rather than being part of the listing, and the
        # container's border is what makes that read.
        self.add_item(self.navigation())

    def row(self, series: Series) -> str:
        """Returns the two lines shown beside a series' poster."""
        dot: str = self.cog.unicode.middle_dot
        state: str = "" if series.monitored else " (unmonitored)"
        detail: str = (
            f"{progress_bar(series.percent_of_episodes, width=8)} {series.episode_file_count}/{series.episode_count} "
            f"{dot} {series.size_display} {dot} {str(series.status).title()}{state}"
        )
        return f"**{series.display_title}**\n-# {detail}"

    def summary(self) -> str:
        """Returns the counts line under the listing."""
        dot: str = self.cog.unicode.middle_dot
        total_size: int = sum(series.size_on_disk for series in self.entries)
        missing: int = sum(series.missing_episode_count for series in self.entries)
        page: str = f" {dot} page {self.page + 1} of {self.pages}" if self.pages > 1 else ""
        return f"{len(self.entries)} series {dot} {to_size(total_size)} {dot} {missing} episodes missing{page}"

    def navigation(self) -> discord.ui.ActionRow:
        """Returns the paging and refresh row."""
        row = discord.ui.ActionRow()
        if self.pages > 1:
            row.add_item(SonarrButton(action="prev", label="Prev", disabled=self.page == 0))
            row.add_item(SonarrButton(action="next", label="Next", disabled=self.page >= self.pages - 1))
        row.add_item(SonarrButton(action="reload", label="Reload", emoji="🔄"))
        return row

    async def dispatch(self, interaction: discord.Interaction, action: str, value: Optional[str] = None) -> None:  # signature is the base's
        """Pages the listing, reloads it, or opens one series."""
        if action == "open" and value is not None:
            series: Optional[Series] = await self.cog.sonarr.get_series(series_id=int(value))
            if series is None:
                await interaction.response.send_message(
                    content=f"Sonarr no longer has that one. {self.cog.emoji_table.kuma_shrug}",
                    ephemeral=True,
                )
                return
            await interaction.response.edit_message(view=SeriesPanel(cog=self.cog, user_id=self.user_id, series=series))
            return

        page: int = self.page
        entries: list[Series] = self.entries
        if action == "prev":
            page -= 1
        elif action == "next":
            page += 1
        elif action == "reload":
            try:
                entries = await self.cog.sonarr.library(force=True)
            except SonarrError as error:
                await self.cog.report(interaction=interaction, error=error)
                return
        else:
            return

        await interaction.response.edit_message(view=LibraryPanel(cog=self.cog, user_id=self.user_id, entries=entries, page=page))


class AddPanel(SonarrPanel):
    """Search results, then the one being added with its artwork and its settings.

    The panel has two states rather than two views: nothing is chosen yet, or something is, and picking
    a different result simply rebuilds it.
    """

    def __init__(
        self,
        *,
        cog: SonarrCog,
        user_id: int,
        term: str,
        results: list[Series],
        chosen: Optional[Series] = None,
        profile_id: Optional[int] = None,
        folder_path: Optional[str] = None,
        monitor: MonitorType = MonitorType.all,
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.term: str = term
        self.results: list[Series] = results
        self.chosen: Optional[Series] = chosen
        self.monitor: MonitorType = monitor
        self.profile_id: int = profile_id if profile_id is not None else cog.default_profile_id
        self.folder_path: str = folder_path if folder_path is not None else cog.default_folder_path

        accent: discord.Colour = accent_for(chosen) if chosen is not None else UNADDED_ACCENT
        container = discord.ui.Container(accent_colour=accent)

        if chosen is not None:
            backdrop: Optional[discord.ui.MediaGallery] = self.art(series=chosen)
            if backdrop is not None:
                container.add_item(backdrop)
            container.add_item(discord.ui.TextDisplay(f"## {chosen.display_title}\n-# {self.headline(series=chosen)}"))
            container.add_item(discord.ui.Section(truncate(chosen.overview), accessory=self.accessory(series=chosen)))
            links: str = self.links(series=chosen)
            if links:
                container.add_item(discord.ui.TextDisplay(links))
        else:
            container.add_item(
                discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_peak} Add a series\n-# {len(results)} results for `{term}`")
            )

        container.add_item(discord.ui.Separator())
        container.add_item(self.results_row())

        if chosen is not None and not chosen.in_library:
            container.add_item(self.profile_row())
            container.add_item(self.folder_row())
            container.add_item(self.monitor_row())

        container.add_item(self.actions())
        self.add_item(container)

    def headline(self, series: Series) -> str:
        """Returns the status line under a chosen result."""
        dot: str = self.cog.unicode.middle_dot
        parts: list[str] = [str(series.status).title() or "Unknown"]
        if series.network:
            parts.append(series.network)
        parts.append(f"{series.season_count} seasons")
        if series.rating is not None:
            parts.append(f"{series.rating:.1f} {self.cog.unicode.star}")
        if series.in_library:
            parts.append(f"**already in your library** {self.cog.emoji_table.kuma_hmm}")
        return f" {dot} ".join(parts)

    def results_row(self) -> discord.ui.ActionRow:
        """Returns the row holding the results select."""
        row = discord.ui.ActionRow()
        row.add_item(
            SonarrSelect(
                action="choose",
                placeholder="Pick a result…",
                options=[
                    discord.SelectOption(
                        label=series.display_title[:100],
                        value=str(index),
                        description=("Already added" if series.in_library else f"TVDB {series.tvdb_id}")[:100],
                        default=self.chosen is not None and series.tvdb_id == self.chosen.tvdb_id,
                    )
                    for index, series in enumerate(self.results)
                ],
            ),
        )
        return row

    def profile_row(self) -> discord.ui.ActionRow:
        """Returns the quality profile select."""
        row = discord.ui.ActionRow()
        row.add_item(
            SonarrSelect(
                action="profile",
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
            SonarrSelect(
                action="folder",
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

    def monitor_row(self) -> discord.ui.ActionRow:
        """Returns the monitoring select."""
        row = discord.ui.ActionRow()
        row.add_item(
            SonarrSelect(
                action="monitor",
                placeholder="What to monitor…",
                options=[
                    discord.SelectOption(label=label, value=monitor.value, description=description, default=monitor is self.monitor)
                    for monitor, label, description in MONITOR_CHOICES
                ],
            ),
        )
        return row

    def actions(self) -> discord.ui.ActionRow:
        """Returns the confirm and cancel row."""
        row = discord.ui.ActionRow()
        addable: bool = self.chosen is not None and not self.chosen.in_library
        row.add_item(SonarrButton(action="add", label="Add", emoji="➕", style=discord.ButtonStyle.success, disabled=not addable))
        if self.chosen is not None and self.chosen.in_library:
            row.add_item(SonarrButton(action="open", label="Open it", emoji="🔍"))
        row.add_item(SonarrButton(action="cancel", label="Cancel"))
        return row

    async def dispatch(self, interaction: discord.Interaction, action: str, value: Optional[str] = None) -> None:  # signature is the base's
        """Applies a choice and re-renders, or performs the add."""
        if action == "cancel":
            self.stop()
            await interaction.response.edit_message(view=SettledPanel(note=f"Nothing added. {self.cog.emoji_table.kuma_shrug}"))
            return

        if action == "open" and self.chosen is not None:
            series: Optional[Series] = await self.cog.sonarr.get_series(series_id=self.chosen.id)
            if series is not None:
                await interaction.response.edit_message(view=SeriesPanel(cog=self.cog, user_id=self.user_id, series=series))
            return

        if action == "add":
            await self.perform_add(interaction=interaction)
            return

        chosen: Optional[Series] = self.chosen
        profile_id: int = self.profile_id
        folder_path: str = self.folder_path
        monitor: MonitorType = self.monitor

        if action == "choose" and value is not None:
            chosen = self.results[int(value)]
        elif action == "profile" and value is not None:
            profile_id = int(value)
        elif action == "folder" and value is not None:
            folder_path = value
        elif action == "monitor" and value is not None:
            monitor = MonitorType(value)
        else:
            return

        await interaction.response.edit_message(
            view=AddPanel(
                cog=self.cog,
                user_id=self.user_id,
                term=self.term,
                results=self.results,
                chosen=chosen,
                profile_id=profile_id,
                folder_path=folder_path,
                monitor=monitor,
            ),
        )

    async def perform_add(self, interaction: discord.Interaction) -> None:
        """POST the chosen result, then show what was created."""
        if self.chosen is None:
            return
        # Sonarr writes to disk and may start grabbing immediately, so the response is deferred rather
        # than risking the interaction expiring mid-add.
        await interaction.response.defer()
        try:
            added: Series = await self.cog.sonarr.add_series(
                self.chosen,
                quality_profile_id=self.profile_id,
                root_folder_path=self.folder_path,
                monitor=self.monitor,
                search_for_missing=self.monitor is not MonitorType.none,
            )
        except SonarrValidationError as error:
            await interaction.followup.send(content=f"Sonarr said no — {error.summary} {self.cog.emoji_table.kuma_pout}", ephemeral=True)
            return
        except SonarrError as error:
            await self.cog.report(interaction=interaction, error=error, deferred=True)
            return

        self.stop()
        note: str = (
            f"Added to `{self.folder_path}` {self.cog.unicode.middle_dot} "
            f"monitoring **{self.monitor.value}** {self.cog.emoji_table.kuma_happy}"
        )
        LOGGER.info("<%s.%s> | Added | Title: %s | Id: %s", __class__.__name__, "perform_add", added.title, added.id)
        await interaction.edit_original_response(view=SeriesPanel(cog=self.cog, user_id=self.user_id, series=added, note=note))


class RemovePanel(SonarrPanel):
    """The confirmation in front of a delete, with the files toggle it needs."""

    def __init__(
        self,
        *,
        cog: SonarrCog,
        user_id: int,
        series: Series,
        delete_files: bool = False,
        expires_at: Optional[float] = None,
    ) -> None:
        super().__init__(cog=cog, user_id=user_id)
        self.series: Series = series
        self.delete_files: bool = delete_files
        # A destructive confirmation states a deadline, so it has to hold one. `View.timeout` measures
        # inactivity and restarts on every press, so toggling the files switch would extend it forever;
        # this is carried across a re-render instead.
        self.expires_at: float = expires_at if expires_at is not None else time.time() + PANEL_TIMEOUT

        container = discord.ui.Container(accent_colour=discord.Colour.from_str("#B71C1C"))
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_hmm} Remove {series.display_title}?"))
        container.add_item(discord.ui.Section(self.consequences(), accessory=self.accessory(series=series)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# This expires <t:{int(self.expires_at)}:R>"))
        container.add_item(self.actions())
        self.add_item(container)

    @property
    def expired(self) -> bool:
        """Whether the confirmation has outlived its stated deadline."""
        return time.time() >= self.expires_at

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects a press that arrives after the stated deadline, as well as the wrong person."""
        if not await super().interaction_check(interaction):
            return False
        if self.expired:
            self.stop()
            await interaction.response.edit_message(
                view=SettledPanel(note=f"That removal expired without an answer. {self.cog.emoji_table.kuma_shrug}"),
            )
            return False
        return True

    def consequences(self) -> str:
        """Returns the plain statement of what pressing Remove will do."""
        series: Series = self.series
        lines: list[str] = [f"-# {series.path}" if series.path else "-# No path on disk."]
        if self.delete_files:
            lines.append(
                f"**{series.size_display}** across {series.episode_file_count} files **will be deleted.** {self.cog.emoji_table.kuma_shock}"
            )
        else:
            lines.append(f"The entry is removed; **{series.size_display}** of files stay on disk.")
        return "\n".join(lines)

    def actions(self) -> discord.ui.ActionRow:
        """Returns the toggle, the confirm and the cancel."""
        row = discord.ui.ActionRow()
        row.add_item(
            SonarrButton(
                action="toggle",
                label="Delete files" if self.delete_files else "Keep files",
                emoji="🗑️" if self.delete_files else "📁",
                style=discord.ButtonStyle.danger if self.delete_files else discord.ButtonStyle.secondary,
            ),
        )
        row.add_item(SonarrButton(action="confirm", label="Remove", style=discord.ButtonStyle.danger))
        row.add_item(SonarrButton(action="cancel", label="Cancel", style=discord.ButtonStyle.success))
        return row

    async def dispatch(self, interaction: discord.Interaction, action: str, value: Optional[str] = None) -> None:  # noqa: ARG002 # signature is the base's
        """Toggles the files switch, performs the removal, or backs out."""
        if action == "toggle":
            await interaction.response.edit_message(
                view=RemovePanel(
                    cog=self.cog,
                    user_id=self.user_id,
                    series=self.series,
                    delete_files=not self.delete_files,
                    expires_at=self.expires_at,
                ),
            )
            return

        if action == "cancel":
            self.stop()
            await interaction.response.edit_message(
                view=SeriesPanel(cog=self.cog, user_id=self.user_id, series=self.series, note=f"Kept. {self.cog.emoji_table.kuma_happy}"),
            )
            return

        if action != "confirm":
            return

        await interaction.response.defer()
        try:
            await self.cog.sonarr.delete_series(series_id=self.series.id, delete_files=self.delete_files)
        except SonarrError as error:
            await self.cog.report(interaction=interaction, error=error, deferred=True)
            return

        self.stop()
        fate: str = "and its files were deleted" if self.delete_files else "the files were left on disk"
        LOGGER.info(
            "<%s.%s> | Removed | Title: %s | Files deleted: %s",
            __class__.__name__,
            "dispatch",
            self.series.title,
            self.delete_files,
        )
        await interaction.edit_original_response(
            view=SettledPanel(note=f"Removed **{self.series.display_title}** — {fate}. {self.cog.emoji_table.kuma_happy}"),
        )


class StatusPanel(SonarrPanel):
    """What the instance is doing: its queue, its health, its disks and its own version."""

    def __init__(
        self,
        *,
        cog: SonarrCog,
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
        accent: discord.Colour = (
            discord.Colour.from_str("#B71C1C")
            if errors
            else discord.Colour.from_str("#FFB300")
            if stalled or warnings
            else discord.Colour.from_str("#4CAF50")
        )

        container = discord.ui.Container(accent_colour=accent)
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_tea} {status.instance_name}\n-# {self.headline()}"))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(discord.ui.TextDisplay(self.queue_block(queue=queue)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.library_block(library=library, mounts=mounts)))

        if warnings:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(self.health_block(warnings=warnings)))

        row = discord.ui.ActionRow()
        row.add_item(SonarrButton(action="reload", label="Refresh", emoji="🔄"))
        row.add_item(discord.ui.Button(label="Open in Sonarr", style=discord.ButtonStyle.link, url=cog.base_url))
        container.add_item(row)
        self.add_item(container)

    def headline(self) -> str:
        """Returns the version line, and whether the event listener is attached."""
        dot: str = self.cog.unicode.middle_dot
        listening: str = "live" if self.cog.sonarr.listening else "polling"
        parts: list[str] = [f"v{self.status.version}", self.status.branch]
        if self.status.is_docker:
            parts.append("docker")
        parts.append(f"events {listening}")
        if self.status.start_time is not None:
            parts.append(f"up since {self.cog.to_discord_timestamp(time=self.status.start_time, style='R')}")
        return f" {dot} ".join(parts)

    def queue_block(self, queue: list[QueueRecord]) -> str:
        """Returns the active downloads, which is the part of a status anyone actually wants."""
        if not queue:
            return f"### Queue\n-# Nothing downloading. {self.cog.emoji_table.kuma_shrug}"

        dot: str = self.cog.unicode.middle_dot
        lines: list[str] = [f"### Queue {dot} {len(queue)} items"]
        for record in queue[:QUEUE_LIMIT]:
            marker: str = f" {self.cog.emoji_table.kuma_sad}" if record.stalled else ""
            eta: str = ""
            if record.estimated_completion_time is not None and not record.stalled:
                eta = f" {dot} {self.cog.to_discord_timestamp(time=record.estimated_completion_time, style='R')}"
            lines.append(f"- **{record.title[:70]}**{marker}")
            lines.append(f"  -# {progress_bar(record.progress, width=10)} {record.progress:.0f}% {dot} {record.size_display}{eta}")
        if len(queue) > QUEUE_LIMIT:
            lines.append(f"-# …and {len(queue) - QUEUE_LIMIT} more.")
        return "\n".join(lines)

    def library_block(self, library: list[Series], mounts: list[DiskSpace]) -> str:
        """Returns the library totals and the free space under them."""
        dot: str = self.cog.unicode.middle_dot
        missing: int = sum(series.missing_episode_count for series in library)
        continuing: int = sum(1 for series in library if series.continuing)
        lines: list[str] = [
            "### Library",
            f"- **Series** — {len(library)} ({continuing} continuing)",
            f"- **Episodes** — {sum(series.episode_file_count for series in library):,} on disk {dot} {missing:,} missing",
            f"- **Size** — {to_size(sum(series.size_on_disk for series in library))}",
        ]
        lines.extend(
            f"- **{mount.path}** — {mount.free_space_display} free of {mount.total_space_display} ({mount.used_percent:.0f}% used)"
            for mount in mounts[:3]
        )
        return "\n".join(lines)

    def health_block(self, warnings: list[Health]) -> str:
        """Returns Sonarr's own health warnings."""
        emoji_table = self.cog.emoji_table
        lines: list[str] = [f"### Health {emoji_table.kuma_hmm}"]
        for warning in warnings[:5]:
            marker: str = emoji_table.kuma_shock if warning.is_error else emoji_table.kuma_hmm
            lines.append(f"- {marker} **{warning.source}** — {warning.message[:150]}")
        return "\n".join(lines)

    async def dispatch(self, interaction: discord.Interaction, action: str, value: Optional[str] = None) -> None:  # noqa: ARG002 # signature is the base's
        """Rebuilds the panel from a fresh read."""
        if action != "reload":
            return
        await interaction.response.defer()
        try:
            panel: StatusPanel = await self.cog.build_status(user_id=self.user_id)
        except SonarrError as error:
            await self.cog.report(interaction=interaction, error=error, deferred=True)
            return
        await interaction.edit_original_response(view=panel)


class SettledPanel(discord.ui.LayoutView):
    """What a finished flow leaves behind: the outcome, and no buttons to press again."""

    def __init__(self, *, note: str) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour.from_str("#607D8B"))
        container.add_item(discord.ui.TextDisplay(note))
        self.add_item(container)


class SonarrCog(Cog, name="Sonarr"):
    """Drive a Sonarr instance from Discord: add, remove, inspect and watch.

    Reads are served from :class:`SonarrAPI`'s cache, which the SignalR hub keeps current, so a command
    is usually answered without touching the network at all.
    """

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        self.settings: Optional[SonarrSettings] = load_settings()
        self._sonarr: Optional[SonarrAPI] = None
        self.profiles: list[QualityProfile] = []
        self.folders: list[RootFolder] = []

    @property
    def sonarr(self) -> SonarrAPI:
        """Returns the client.

        Raises
        ------
        RuntimeError
            The cog loaded without credentials; every command guards on :attr:`configured` first.

        """
        if self._sonarr is None:
            msg = "The Sonarr client is not configured."
            raise RuntimeError(msg)
        return self._sonarr

    @property
    def configured(self) -> bool:
        """Whether `local.ini` had a usable `[SONARR]` section."""
        return self._sonarr is not None

    @property
    def base_url(self) -> str:
        """Returns the instance root, for the link buttons."""
        return self._sonarr.base_url if self._sonarr is not None else ""

    @property
    def default_profile_id(self) -> int:
        """Returns the quality profile an add starts on."""
        return self.profiles[0].id if self.profiles else 1

    @property
    def default_folder_path(self) -> str:
        """Returns the root folder an add starts on."""
        return self.folders[0].path if self.folders else ""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Gate every command in this cog to k8thekat.

        Registers as a check on each app command in the cog, so there is one gate rather than a
        decorator per command. `owner_user_id` rather than `owner_ids`, because the latter grows at
        runtime through the `trusted` command and these commands write to disk and start downloads.
        """
        allowed: bool = interaction.user.id == self.bot.owner_user_id
        if not allowed:
            LOGGER.info(
                "<%s.%s> | Refused | User: %s | Command: %s",
                __class__.__name__,
                "interaction_check",
                interaction.user.id,
                interaction.command.qualified_name if interaction.command else "unknown",
            )
        return allowed

    async def cog_load(self) -> None:
        """Connect, warm the caches, and attach the event listener.

        A Sonarr that is down at start-up must not stop the cog loading — the bot outlives it, and the
        listener reconnects on its own once it comes back.
        """
        if self.settings is None:
            LOGGER.warning("<%s.%s> | No [SONARR] section in local.ini; commands will refuse.", __class__.__name__, "cog_load")
            return

        client = SonarrAPI(
            base_url=self.settings.url,
            api_key=self.settings.api_key,
            session=self.bot.session,
            url_base=self.settings.url_base,
        )
        try:
            status: SystemStatus = await client.connect()
            self.profiles = await client.quality_profiles()
            self.folders = await client.root_folders()
            await client.library()
            await client.listen()
        except SonarrError as error:
            LOGGER.warning("<%s.%s> | Sonarr unreachable at load | Reason: %s", __class__.__name__, "cog_load", error.error_reason)
            self._sonarr = client
            return

        self._sonarr = client
        LOGGER.info("<%s.%s> | Ready | Version: %s | Profiles: %s", __class__.__name__, "cog_load", status.version, len(self.profiles))

    async def cog_unload(self) -> None:
        """Stop the listener; a reload otherwise leaves a websocket writing into a dead cog."""
        if self._sonarr is not None:
            await self._sonarr.close()
            self._sonarr = None

    async def report(self, interaction: discord.Interaction, error: SonarrError, *, deferred: bool = False) -> None:
        """Turn a wrapper error into one Kuma-styled ephemeral reply."""
        emoji_table = self.emoji_table
        if isinstance(error, SonarrValidationError):
            note: str = f"Sonarr refused that — {error.summary} {emoji_table.kuma_pout}"
        elif error.status_code == 0:
            note = f"I couldn't reach Sonarr. {emoji_table.kuma_sad}\n-# {error.error_reason}"
        else:
            note = f"Sonarr answered `{error.status_code}` — {error.error_reason} {emoji_table.kuma_sad}"

        LOGGER.warning("<%s.%s> | Reported | Status: %s | Reason: %s", __class__.__name__, "report", error.status_code, error.error_reason)
        if deferred or interaction.response.is_done():
            await interaction.followup.send(content=note, ephemeral=True)
        else:
            await interaction.response.send_message(content=note, ephemeral=True)

    async def guard(self, interaction: discord.Interaction) -> bool:
        """Returns whether the cog can serve a command, telling the caller when it cannot."""
        if self.configured:
            return True
        await interaction.response.send_message(
            content=(
                f"Sonarr isn't set up yet. {self.emoji_table.kuma_shrug}\n"
                "-# Add a `[SONARR]` section to `local.ini` with `url` and `api_key`, then reload this cog."
            ),
            ephemeral=True,
        )
        return False

    async def build_status(self, user_id: int) -> StatusPanel:
        """Read everything the status panel shows and build it."""
        return StatusPanel(
            cog=self,
            user_id=user_id,
            status=await self.sonarr.system_status(),
            queue=await self.sonarr.queue(),
            warnings=await self.sonarr.health(),
            mounts=await self.sonarr.disk_space(),
            library=await self.sonarr.library(),
        )

    async def series_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002 # discord.py's callback signature
        """Suggest library series by title.

        Served entirely from the cache, which is what makes it fast enough to fire on every keystroke.
        """
        if not self.configured:
            return []
        try:
            matches: list[Series] = await self.sonarr.find(term=current)
        except SonarrError:
            return []
        return [app_commands.Choice(name=series.display_title[:100], value=str(series.id)) for series in matches[:25]]

    async def resolve(self, interaction: discord.Interaction, series: str) -> Optional[Series]:
        """Turn an autocomplete value, or a typed title, into a series.

        Nothing stops a caller submitting free text instead of picking a choice, so a title search backs
        the id lookup up.
        """
        if series.isdigit():
            found: Optional[Series] = await self.sonarr.get_series(series_id=int(series))
            if found is not None:
                return found
        matches: list[Series] = await self.sonarr.find(term=series, limit=1)
        if matches:
            return matches[0]
        await interaction.response.send_message(
            content=f"I couldn't find **{series}** in the library. {self.emoji_table.kuma_sad}",
            ephemeral=True,
        )
        return None

    sonarr_group = app_commands.Group(
        name="sonarr",
        description="Manage the Sonarr library.",
        guild_only=False,
    )

    @sonarr_group.command(name="list", description="Show the Sonarr library.")
    @app_commands.describe(filter_by="Only show series whose title contains this.")
    async def sonarr_list(self, interaction: discord.Interaction, filter_by: Optional[str] = None) -> None:
        """Opens the library panel."""
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            entries: list[Series] = await self.sonarr.find(term=filter_by, limit=500) if filter_by else await self.sonarr.library()
        except SonarrError as error:
            await self.report(interaction=interaction, error=error, deferred=True)
            return
        await interaction.followup.send(view=LibraryPanel(cog=self, user_id=interaction.user.id, entries=entries), ephemeral=True)

    @sonarr_group.command(name="info", description="Everything Sonarr knows about one series.")
    @app_commands.describe(series="Start typing a title from your library.")
    @app_commands.autocomplete(series=series_autocomplete)
    async def sonarr_info(self, interaction: discord.Interaction, series: str) -> None:
        """Opens the detail panel for one series."""
        if not await self.guard(interaction=interaction):
            return
        try:
            found: Optional[Series] = await self.resolve(interaction=interaction, series=series)
        except SonarrError as error:
            await self.report(interaction=interaction, error=error)
            return
        if found is None:
            return
        await interaction.response.send_message(view=SeriesPanel(cog=self, user_id=interaction.user.id, series=found), ephemeral=True)

    @sonarr_group.command(name="add", description="Search TVDB and add a series to Sonarr.")
    @app_commands.describe(term="A title, or an id term such as tvdb:121361.")
    async def sonarr_add(self, interaction: discord.Interaction, term: str) -> None:
        """Looks the term up and opens the add panel."""
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            results: list[Series] = await self.sonarr.lookup(term=term)
        except SonarrError as error:
            await self.report(interaction=interaction, error=error, deferred=True)
            return

        if not results:
            await interaction.followup.send(content=f"TVDB has nothing for **{term}**. {self.emoji_table.kuma_sad}", ephemeral=True)
            return

        # A single result is the common case for an id term, so skip a click and open it chosen.
        chosen: Optional[Series] = results[0] if len(results) == 1 else None
        await interaction.followup.send(
            view=AddPanel(cog=self, user_id=interaction.user.id, term=term, results=results, chosen=chosen),
            ephemeral=True,
        )

    @sonarr_group.command(name="remove", description="Remove a series from Sonarr.")
    @app_commands.describe(series="Start typing a title from your library.")
    @app_commands.autocomplete(series=series_autocomplete)
    async def sonarr_remove(self, interaction: discord.Interaction, series: str) -> None:
        """Opens the removal confirmation."""
        if not await self.guard(interaction=interaction):
            return
        try:
            found: Optional[Series] = await self.resolve(interaction=interaction, series=series)
        except SonarrError as error:
            await self.report(interaction=interaction, error=error)
            return
        if found is None:
            return
        await interaction.response.send_message(view=RemovePanel(cog=self, user_id=interaction.user.id, series=found), ephemeral=True)

    @sonarr_group.command(name="status", description="What Sonarr is downloading, and how it is doing.")
    async def sonarr_status(self, interaction: discord.Interaction) -> None:
        """Opens the instance status panel."""
        if not await self.guard(interaction=interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            panel: StatusPanel = await self.build_status(user_id=interaction.user.id)
        except SonarrError as error:
            await self.report(interaction=interaction, error=error, deferred=True)
            return
        await interaction.followup.send(view=panel, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Answer a failed check quietly rather than letting it surface as an unknown error."""
        if isinstance(error, app_commands.CheckFailure):
            note: str = f"These are k8thekat's alone for now. {self.emoji_table.kuma_shrug}"
            if interaction.response.is_done():
                await interaction.followup.send(content=note, ephemeral=True)
            else:
                await interaction.response.send_message(content=note, ephemeral=True)
            return
        LOGGER.exception(
            "<%s.%s> | Command failed | Command: %s", __class__.__name__, "cog_app_command_error", interaction.command, exc_info=error
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(SonarrCog(bot=bot))
