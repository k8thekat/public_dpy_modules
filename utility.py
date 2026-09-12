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

import ast
import asyncio
import datetime
import inspect
import io
import json
import logging
import os
import platform
import random
import re
import unicodedata
from pathlib import Path
from re import Match
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NamedTuple, Optional, Self, TypedDict, Union, Unpack

import aiofiles
import discord
import psutil
from discord import app_commands
from discord.ext import commands
from git import Repo

from kuma_kuma import LOG_TAIL_MAX_BYTES, Kuma_Kuma
from utils import CodeFormat, KumaCog as Cog, KumaEmbed, KumaView, code_block, colourise_log, parse_levels

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiohttp import ClientResponse

    from kuma_kuma import Kuma_Kuma
    from utils import KumaContext as Context
    from utils._types import EmbedParams, GitHubIssueSubmissionResponse, ViewParams, ViewParamsPartial


ErrorAliases = (discord.errors.HTTPException, discord.errors.NotFound, TypeError, ValueError, discord.errors.DiscordException)
BOT_NAME = "Kuma Kuma"
LOGGER = logging.getLogger()
CUSTOM_EMOJI_PATTERN: re.Pattern[str] = re.compile(r"<a?:(\w+):(\d+)>")

# Matches emoji and sticker CDN URLs from both hosts Discord uses.
# Groups: (asset_type: "emojis"|"stickers"), (id), (extension), (query_string)
CDN_ASSET_PATTERN: re.Pattern[str] = re.compile(
    r"https?://(?:cdn\.discordapp\.com|media\.discordapp\.net)/(emojis|stickers)/(\d+)\.(png|gif|json|webp)(\?\S*)?",
)


class CDNAsset(NamedTuple):
    """A single emoji or sticker parsed from a Discord CDN URL.

    Attributes
    ----------
    kind : :class:`Literal["emoji", "sticker"]`
        Whether this asset is an emoji or a sticker.
    id : :class:`int`
        The snowflake ID of the asset.
    animated : :class:`bool`
        Best-guess animation state. ``True`` when the query string contains
        ``animated=true``, the extension is ``.gif``, or the extension is
        ambiguous (``.webp`` without a query param) - the caller should probe
        with :meth:`KumaCog.resolve_cdn_emoji` to confirm.

    """

    kind: Literal["emoji", "sticker"]
    id: int
    animated: bool


def parse_cdn_assets(content: str) -> list[CDNAsset]:
    """Extract emoji and sticker references from Discord CDN URLs in text.

    Scans ``content`` for ``cdn.discordapp.com`` and ``media.discordapp.net`` links
    pointing at ``/emojis/{id}`` or ``/stickers/{id}``. Each unique ID is returned
    once, in the order it first appears.

    Parameters
    ----------
    content : :class:`str`
        The message text to scan.

    Returns
    -------
    :class:`list[CDNAsset]`
        Parsed assets, deduplicated by ID and ordered by first occurrence.

    """
    assets: list[CDNAsset] = []
    seen: set[int] = set()
    for match in CDN_ASSET_PATTERN.finditer(content):
        asset_type: str = match.group(1)
        asset_id: int = int(match.group(2))
        extension: str = match.group(3)
        query: str = match.group(4) or ""
        if asset_id in seen:
            continue
        seen.add(asset_id)

        kind: Literal["emoji", "sticker"] = "emoji" if asset_type == "emojis" else "sticker"
        # Query param is authoritative; .gif is a certain yes; .webp is ambiguous so
        # default to True - resolve_cdn_emoji will probe and fall back on a 415.
        animated: bool = "animated=true" in query or extension != "png"
        assets.append(CDNAsset(kind=kind, id=asset_id, animated=animated))

    return assets


def get_latest_commits(url: str, repo: Repo, branch: str, max_count: int = 5) -> str:
    """Retrieves a Github Repo's latest commits.

    Parameters
    ----------
    url: : :class:`str`
        The base url of the github repository.
    repo : :class:`git.Repo`
        The git repository to pull commits from.
    branch : :class:`str`
        The branch to pull commits from.
    max_count : :class:`int`, optional
        The max number of github commit's to collect, by default 5.

    Returns
    -------
    :class:`str`
        An elongated string of GitHub commit information separated by new lines.

    """
    reply = ""
    # url = "https://github.com/k8thekat/Kuma_Kuma"
    # repo: Repo = Repo(Path(__file__).parent.as_posix())
    commits = repo.iter_commits(branch, max_count=max_count)
    for i in commits:
        assert i.author.name  # noqa: S101
        commit_link = f"[{i.hexsha[:4]}]({url + f'/commit/{i.hexsha}'})"
        i.authored_datetime.strftime("%Y/%-m/%-d")
        reply += f"({commit_link}) **{i.author.name}** | *{discord.utils.format_dt(i.authored_datetime, 'd')}* | (+`{i.stats.total['insertions']}` -`{i.stats.total['deletions']}`)\n"  # noqa: E501
    return reply


async def count_lines(path: str, filetype: str = ".py", skip_venv: bool = True) -> int:
    """Count lines of Code."""
    lines = 0
    for i in os.scandir(path=path):
        if i.is_file():
            if i.path.endswith(filetype):
                if skip_venv and re.search(pattern=r"(\\|/)?venv(\\|/)", string=i.path):
                    continue
                lines += len((await (await aiofiles.open(file=i.path)).read()).split(sep="\n"))
        elif i.is_dir():
            lines += await count_lines(path=i.path, filetype=filetype)
    return lines


async def count_others(path: str, filetype: str = ".py", file_contains: str = "def", skip_venv: bool = True) -> int:
    """Counts the files in directory or functions."""
    line_count = 0
    for i in os.scandir(path=path):
        if i.is_file():
            if i.path.endswith(filetype):
                if skip_venv and re.search(pattern=r"(\\|/)?venv(\\|/)", string=i.path):
                    continue
                line_count += len(
                    [line for line in (await (await aiofiles.open(file=i.path)).read()).split(sep="\n") if file_contains in line]
                )
        elif i.is_dir():
            line_count += await count_others(path=i.path, filetype=filetype, file_contains=file_contains)
    return line_count


class YoinkEmbed(KumaEmbed):
    """Embed displaying a single yoinkable asset (emoji or sticker).

    Exactly one of ``emoji`` or ``sticker`` must be provided; passing neither raises :class:`ValueError`.

    Parameters
    ----------
    cog : :class:`KumaCog`
        The parent Cog, passed through to :class:`KumaEmbed`.
    emoji : :class:`Optional[discord.PartialEmoji | discord.Emoji]`
        The emoji to display. Sets the title, image, ID, and animated fields.
    sticker : :class:`Optional[Union[discord.StickerItem, discord.Sticker]]`
        The sticker to display. Sets the title, image, ID, and description fields.
    image_data : :class:`Optional[bytes]`
        Pre-fetched image bytes from a CDN probe. When present the copy paths
        skip a second ``emoji.read()`` call.
    **kwargs : :class:`Unpack[EmbedParams]`
        Any additional keyword arguments forwarded to :class:`KumaEmbed`.

    """

    emoji: Optional[discord.PartialEmoji | discord.Emoji] = None
    sticker: Optional[Union[discord.StickerItem, discord.Sticker]] = None
    image_data: Optional[bytes] = None

    def __init__(
        self,
        cog: Cog,
        *,
        emoji: Optional[discord.PartialEmoji | discord.Emoji] = None,
        sticker: Optional[Union[discord.StickerItem, discord.Sticker]] = None,
        image_data: Optional[bytes] = None,
        **kwargs: Unpack[EmbedParams],
    ) -> None:

        self.emoji = emoji
        self.sticker = sticker
        self.image_data = image_data

        if kwargs.get("color") is None:
            kwargs["color"] = discord.Color.green()

        if emoji is None and sticker is None:
            err = "You must provide either an Emoji or a Sticker."
            raise ValueError(err)

        if emoji is not None:
            kwargs.setdefault("title", f"**Yoink** -> `:{emoji.name}:`")

            super().__init__(cog=cog, **kwargs)

            self.set_image(url=str(emoji.url))
            self.add_field(name="ID:", value=str(emoji.id))
            self.add_field(name="Animated:", value=str(emoji.animated), inline=True)

        elif sticker is not None:
            kwargs.setdefault("title", f"**Yoink** -> `{sticker.name}`")

            super().__init__(cog=cog, **kwargs)

            self.set_image(url=str(sticker.url))
            self.add_field(name="ID:", value=str(sticker.id))
            if isinstance(sticker, discord.GuildSticker) and sticker.description:
                self.add_field(name="Description:", value=sticker.description, inline=True)

        self.thumbnail_icon = None


class YoinkGuildSelect(discord.ui.Select["YoinkView"]):
    def __init__(
        self,
        *,
        emoji: Optional[discord.PartialEmoji | discord.Emoji] = None,
        sticker: Optional[Union[discord.Sticker, discord.StandardSticker, discord.GuildSticker]] = None,
        image_data: Optional[bytes] = None,
        placeholder: str,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(placeholder=placeholder, options=options)
        self.emoji = emoji
        self.sticker = sticker
        self.image_data = image_data

    async def callback(self, interaction: discord.Interaction) -> None:
        if len(self.values) > 0 and self.view is not None:
            to_guild = self.view.cog.bot.get_guild(int(self.values[0]))
            if to_guild is None:
                await interaction.response.send_message(
                    content=f"Failed to find the guild. {self.view.cog.emoji_table.kuma_hmm}",
                    ephemeral=True,
                )
                return
            if self.sticker is not None:
                try:
                    s_emoji: str = "" if not isinstance(self.sticker, discord.GuildSticker) else self.sticker.emoji
                    sticker = await to_guild.create_sticker(
                        name=self.sticker.name,
                        description=self.sticker.description,
                        emoji=s_emoji,
                        file=await self.sticker.to_file(),
                        reason="Yoinked",
                    )
                    await interaction.response.send_message(content=f"Successfully copied the sticker. -> {sticker}", ephemeral=True)
                except ErrorAliases as e:
                    LOGGER.exception(
                        "<%s.%s> | Exception occurred when trying to create a sticker.",
                        __class__.__name__,
                        "copy_sticker",
                        exc_info=e,
                    )
                    self.view.remove_item(self)
                    await interaction.response.send_message(
                        content=f"We encountered an error creating the sticker. {self.view.cog.emoji_table.kuma_crying}\n> {e}",
                        ephemeral=True,
                    )
                    return

            elif self.emoji is not None:
                try:
                    image: bytes = self.image_data or await self.emoji.read()
                    emoji = await to_guild.create_custom_emoji(name=self.emoji.name, image=image, reason="Yoinked")
                    await interaction.response.send_message(content=f"Successfully copied the emoji. -> {emoji}", ephemeral=True)
                except ErrorAliases as e:
                    LOGGER.exception(
                        "<%s.%s> | Exception occurred when trying to create an emoji.",
                        __class__.__name__,
                        "copy_emoji",
                        exc_info=e,
                    )
                    self.view.remove_item(self)
                    await interaction.response.send_message(
                        content=f"We encountered an error creating the emoji. {self.view.cog.emoji_table.kuma_crying}\n> {e}",
                        ephemeral=True,
                    )
                    return
        return


class YoinkView(KumaView):
    embeds: Sequence[YoinkEmbed]

    def __init__(self, **kwargs: Unpack[ViewParams]) -> None:
        super().__init__(**kwargs)
        self.options: list[discord.SelectOption] = self.guild_options()
        self.components.extend([self.copy_to_guild, self.to_app_emoji])

    def guild_options(self) -> list[discord.SelectOption]:
        """Returns a `discord.SelectOption` array of Guilds the bot has emoji/sticker management of."""
        return [
            discord.SelectOption(label=guild.name, value=str(object=guild.id))
            for guild in self.cog.bot.guilds
            if guild.me.guild_permissions.manage_emojis_and_stickers
        ]

    def reset_view(self) -> Self:
        """Restore the initial button layout and navigate back to the first embed."""
        super().reset_view()
        self.reset_callback.disabled = True
        if self.embeds is not None and len(self.embeds) > 1:
            self.previous_callback.disabled = True
            self.next_callback.disabled = False
        return self

    @discord.ui.button(label="Copy to Guild", style=discord.ButtonStyle.green, disabled=False, row=1)
    async def copy_to_guild(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        await interaction.response.defer()
        item.disabled = True
        self.reset_callback.disabled = False

        embed: YoinkEmbed = self.embeds[self.indx]
        if embed.sticker is not None:
            # StickerItem is a lightweight stub; fetch the full object. Sticker subclasses are already complete.
            full_sticker = await embed.sticker.fetch() if isinstance(embed.sticker, discord.StickerItem) else embed.sticker
            self.add_item(
                item=YoinkGuildSelect(
                    sticker=full_sticker,
                    placeholder="Which Guild...?",
                    options=self.options,
                ),
            )
        elif embed.emoji is not None:
            self.add_item(
                item=YoinkGuildSelect(
                    emoji=embed.emoji,
                    image_data=embed.image_data,
                    placeholder="Which Guild...?",
                    options=self.options,
                ),
            )
        else:
            # Deferred already, so this has to be a followup rather than a response.
            await interaction.followup.send(
                content=f"Oops, looks like our Embed didn't have what we needed.. {self.cog.emoji_table.kuma_pout}",
                ephemeral=True,
            )
            return

        await interaction.edit_original_response(view=self)

    @discord.ui.button(label="To App Emoji", style=discord.ButtonStyle.blurple, disabled=False, row=1)
    async def to_app_emoji(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        await interaction.response.defer(ephemeral=True)
        embed = self.embeds[self.indx]

        try:
            if embed.emoji is not None:
                image: bytes = embed.image_data or await embed.emoji.read()
                name: str = re.sub(r"[^\w]", "_", embed.emoji.name or "yoinked")[:32]
                app_emoji = await self.cog.bot.create_application_emoji(name=name, image=image)
                await interaction.followup.send(content=f"Created your application emoji~ {name}\n{app_emoji}.", ephemeral=True)
                return

            if embed.sticker is not None:
                res = await self.cog.get_request(url=str(embed.sticker.url))
                if res is None:
                    await interaction.followup.send(content="Failed to fetch sticker image.", ephemeral=True)
                    return
                name = re.sub(r"[^\w]", "_", embed.sticker.name)[:32]
                app_emoji = await self.cog.bot.create_application_emoji(name=name, image=res)
                await interaction.followup.send(content=f"Created application emoji {app_emoji}.", ephemeral=True)

        except ErrorAliases as e:
            LOGGER.exception("<%s.%s> | Exception creating application emoji.", __class__.__name__, "to_app_emoji", exc_info=e)
            await interaction.followup.send(
                content=f"Failed to create application emoji... {self.cog.emoji_table.kuma_sad} \n{e}",
                ephemeral=True,
            )


class GithubIssueSubmissionModal(discord.ui.Modal):
    """Opens a GitHub issue from a Discord message, in one modal.

    The repository and the submission type used to be a :class:`discord.ui.View` of two selects whose
    callbacks opened this modal once both had been answered. That two step existed only because a
    modal could not hold a select; :class:`discord.ui.Label` (discord.py 2.6) can wrap one, so the
    whole flow is a single dialog and there is no half answered state to carry between components.

    .. warning::
        A modal takes at most five children and every :class:`discord.ui.Label` counts as one, so
        this is **full**. A sixth field means dropping one of these or folding it into the body.

    """

    bot: Kuma_Kuma
    issue_msg: discord.Message
    repos: ClassVar[list[str]] = ["AMPAPI_Python", "Kuma_Kuma", "public_dpy_modules", "GatekeeperV2", "ImageSorter"]
    submission_types: ClassVar[list[str]] = ["Issue", "Feature"]
    # A text input takes at most 4000 characters, so a long message has to be cut to fit the default.
    body_size: ClassVar[int] = 4000

    def __init__(
        self,
        bot: Kuma_Kuma,
        issue_msg: discord.Message,
        title: str = "Create a Github Issue for Kuma Kuma.",
    ) -> None:
        self.issue_msg = issue_msg
        self.bot = bot
        super().__init__(title=title)

        self.repo = discord.ui.Select(
            placeholder="Please select a Repository...",
            options=[discord.SelectOption(label=entry.replace("_", " "), value=entry) for entry in self.repos],
        )
        self.submission_type = discord.ui.Select(
            placeholder="Type of Issue to submit...",
            options=[discord.SelectOption(label=entry, value=entry) for entry in self.submission_types],
        )
        self.issue_title = discord.ui.TextInput(placeholder="A short summary of the issue.", required=True)
        self.issue_body = discord.ui.TextInput(
            default=self.issue_msg.content[: self.body_size],
            style=discord.TextStyle.long,
            required=True,
        )

        # Pre-filled rather than appended silently, so the link can be read before it is submitted
        # and cleared when the issue has outgrown the message that started it.
        self.source = discord.ui.TextInput(default=self.issue_msg.jump_url, required=False)

        self.add_item(item=discord.ui.Label(text="Repository", description="Which repo the issue is opened against.", component=self.repo))
        self.add_item(item=discord.ui.Label(text="Type", description="An issue or a feature request.", component=self.submission_type))
        self.add_item(item=discord.ui.Label(text="Issue Title", component=self.issue_title))
        self.add_item(
            item=discord.ui.Label(
                text="Issue Body", description="Pre-filled with the message; edit it however you like.", component=self.issue_body
            ),
        )
        self.add_item(
            item=discord.ui.Label(
                text="Source", description="Jump link back to the Discord message. Clear it to leave it out.", component=self.source
            ),
        )

    def build_body(self) -> str:
        """Assembles the issue body from the body field and the source link.

        Kept out of :meth:`on_submit` so the assembly can be read and tested without an interaction.
        This is GitHub flavoured markdown, not Discord's, so a `---` rule renders here - the opposite
        of everywhere else in this repo.

        Returns
        -------
        :class:`str`
            The issue body to POST.

        """
        if not self.source.value:
            return self.issue_body.value
        return f"{self.issue_body.value}\n\n---\n*Submitted from [Discord]({self.source.value}).*"

    async def on_submit(self, interaction: discord.Interaction) -> discord.InteractionCallbackResponse:
        # A required select always answers with exactly one value, but reading `[0]` blind would turn
        # any surprise into an IndexError inside `on_error` rather than a message anyone can act on.
        if not self.repo.values or not self.submission_type.values:
            return await interaction.response.send_message(
                content=f"I didn't catch the repository or the type. {self.bot.emoji_table.kuma_hmm}",
                ephemeral=True,
            )

        repo: str = self.repo.values[0]
        url: str = f"https://api.github.com/repos/{self.bot.config.github_owner}/{repo}/issues"
        headers: dict[str, str] = {
            "Authorization": "token " + self.bot.config.github_token,
            "Accept": "application/vnd.github.raw+json",
        }
        # TODO: See about adding file attachments from the message to the Github issue.
        modified_title: str = f"[{self.submission_type.values[0]}] {self.issue_title.value} | submitted via Discord"
        data: dict[str, Union[str, list]] = {
            "title": modified_title,
            "body": self.build_body(),
            # `assignees`, not `assigness` - GitHub drops unknown fields without complaining, so
            # every issue opened this way came out unassigned. Taken from the configured repo owner
            # rather than a hardcoded name, since that is who the token belongs to.
            "assignees": [self.bot.config.github_owner],
        }

        # `async with`, so a failed call releases the connection instead of holding it open.
        async with self.bot.session.post(url=url, data=json.dumps(data), headers=headers) as res:
            if res.status == 201:
                resp: GitHubIssueSubmissionResponse = await res.json()
                LOGGER.info(
                    "<%s.%s> | Opened a GitHub issue. | Repo: %s | Number: %s | By: %s",
                    __class__.__name__,
                    "on_submit",
                    repo,
                    resp.get("number", "UNK"),
                    interaction.user.name,
                )
                return await interaction.response.send_message(embed=GithubIssueSubmissionEmbed(gh_response=resp, user=interaction.user))

            LOGGER.error(
                "<%s.%s> | Failed to open a GitHub issue. | Repo: %s | Status: %s | By: %s",
                __class__.__name__,
                "on_submit",
                repo,
                res.status,
                interaction.user.name,
            )
            return await interaction.response.send_message(
                content=f"Failed to create an issue. {self.bot.emoji_table.kuma_sad} | Status: {res.status}\n"
                f"> You can try manually [here](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#create-an-issue).",
                ephemeral=True,
            )


class GithubIssueSubmissionEmbed(discord.Embed):
    gh_response: GitHubIssueSubmissionResponse
    user: Union[discord.Member, discord.User]

    def __init__(
        self,
        gh_response: GitHubIssueSubmissionResponse,
        user: Union[discord.Member, discord.User],
        colour: discord.Color = discord.Color.og_blurple(),  # noqa: B008
        title: str = "__GitHub Issue Submission__",
        timestamp: datetime.datetime = discord.utils.utcnow(),  # noqa: B008
    ) -> None:
        self.gh_response = gh_response
        self.user = user
        super().__init__(
            colour=colour,
            title=title,
            url=self.gh_response.get("html_url", None),
            description=self.gh_response.get("title", None),
            timestamp=timestamp,
        )
        self.add_field(name="**Issue Number:**", value=self.gh_response.get("number", "UNK"), inline=False)
        # field values are limited to `1024` chars.
        self.add_field(name="**Issue Body:**", value=self.gh_response.get("body", "UNK")[:1024])
        self.set_footer(text=f"Issue submitted by {user.display_name}")


class URLref(TypedDict):
    """Used for URL linking."""

    aliases: list[str]
    urls: list[str]


#: Safe character budget for log text inside a single CV2 page, leaving room
#: for heading, footer, code fence wrapper and breathing room.
LOG_PAGE_BUDGET: int = 3800


def _paginate_log_entries(entries: list[str], *, budget: int = LOG_PAGE_BUDGET) -> list[str]:
    """Group coloured log entries into code-block pages fitting the CV2 character budget.

    Parameters
    ----------
    entries : :class:`list[str]`
        Coloured log entries, one string per record.
    budget : :class:`int`, optional
        Maximum characters of log text per page, by default :attr:`LOG_PAGE_BUDGET`.

    Returns
    -------
    :class:`list[str]`
        Code-blocked pages ready for a :class:`discord.ui.TextDisplay`.

    """
    # The code_block wrapper adds ~12 chars for the fence markers.
    page_budget: int = budget - 12

    pages: list[str] = []
    current: list[str] = []
    current_length: int = 0

    for entry in entries:
        cost: int = len(entry) + 1
        if current and current_length + cost > page_budget:
            pages.append(code_block("\n".join(current), CodeFormat.ANSI))
            current = []
            current_length = 0
        current.append(entry)
        current_length += cost

    if current:
        pages.append(code_block("\n".join(current), CodeFormat.ANSI))

    return pages or [code_block("(empty)", CodeFormat.ANSI)]


class LogPageButton(discord.ui.Button["LogPanel"]):
    """Steps a :class:`LogPanel` one page in either direction."""

    def __init__(self, *, step: int, label: str, emoji: str) -> None:
        super().__init__(style=discord.ButtonStyle.blurple, label=label, emoji=emoji)
        self.step: int = step

    async def callback(self, interaction: discord.Interaction) -> None:
        """Turn the panel; a dead view means the message outlived its handler."""
        view: Optional[LogPanel] = self.view
        if view is None:
            return
        await view.turn(interaction=interaction, step=self.step)


class LogFileButton(discord.ui.Button["LogPanel"]):
    """Sends the full log file as an ephemeral attachment."""

    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Get File", emoji="\U0001f4c4")

    async def callback(self, interaction: discord.Interaction) -> None:
        """Read the log file and send it as an ephemeral attachment with context."""
        view: Optional[LogPanel] = self.view
        if view is None:
            return

        log_path: Path = view.log_path
        if not log_path.exists():
            await interaction.response.send_message(content="Log file no longer exists.", ephemeral=True)
            return

        try:
            log_file = discord.File(
                fp=io.BytesIO(initial_bytes=log_path.read_text().encode(encoding="utf-8")),
                filename=f"kuma_log_{datetime.datetime.now(tz=datetime.UTC).strftime('%Y-%m-%d')}.txt",
            )
        except OSError:
            await interaction.response.send_message(content="Could not read the log file.", ephemeral=True)
            return

        await interaction.response.send_message(
            content=f"Full log - `{log_path.name}` · captured <t:{int(datetime.datetime.now(tz=datetime.UTC).timestamp())}:f>",
            file=log_file,
            ephemeral=True,
        )


class LogPanel(discord.ui.LayoutView):
    """Paginated log viewer as a Components V2 panel.

    Parameters
    ----------
    cog : :class:`KumaCog`
        The parent cog.
    owner_id : :class:`int`
        Who may press the buttons.
    pages : :class:`Sequence[str]`
        Pre-formatted code-block pages of log content.
    log_path : :class:`Path`
        Path to the log file for the Get File button.
    filter_label : :class:`Optional[str]`, optional
        The active level filter shown in the heading, by default ``None``.
    index : :class:`int`, optional
        Which page to render, by default ``0``.
    timeout : :class:`Optional[float]`, optional
        View timeout, by default ``120.0``.

    """

    def __init__(
        self,
        *,
        cog: Cog,
        owner_id: int,
        pages: Sequence[str],
        log_path: Path,
        filter_label: Optional[str] = None,
        index: int = 0,
        timeout: Optional[float] = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog: Cog = cog
        self.owner_id: int = owner_id
        self.pages: Sequence[str] = pages
        self.log_path: Path = log_path
        self.filter_label: Optional[str] = filter_label
        self.index: int = index
        self.length: int = len(pages)

        heading: str = "## Log Viewer"
        if filter_label is not None:
            heading += f" · `{filter_label}`"

        container = discord.ui.Container(accent_colour=discord.Colour.og_blurple())
        container.add_item(discord.ui.TextDisplay(heading))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(pages[index]))

        if self.length > 1:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(self._page_footer()))

        self.add_item(container)

        nav_row: discord.ui.ActionRow[Self] = discord.ui.ActionRow()
        if self.length > 1:
            nav_row.add_item(LogPageButton(step=-1, label="Previous", emoji="\U00002b05"))
            nav_row.add_item(LogPageButton(step=1, label="Next", emoji="\U000027a1"))
        nav_row.add_item(LogFileButton())
        self.add_item(nav_row)

    def _page_footer(self) -> str:
        """The page counter line."""
        return f"-# Page {self.index + 1}/{self.length}"

    def rebuild(self, index: int) -> LogPanel:
        """Return the same panel showing ``index`` instead."""
        return LogPanel(
            cog=self.cog,
            owner_id=self.owner_id,
            pages=self.pages,
            log_path=self.log_path,
            filter_label=self.filter_label,
            index=index,
            timeout=self.timeout,
        )

    async def turn(self, *, interaction: discord.Interaction, step: int) -> None:
        """Redraw the panel on a neighbouring page, wrapping at both ends."""
        panel: LogPanel = self.rebuild((self.index + step) % self.length)
        await interaction.response.edit_message(view=panel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone but the owner."""
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(content="That panel isn't yours.", ephemeral=True)
        return False


# ---------------------------------------------------------------------------
#  fnsearch helpers
# ---------------------------------------------------------------------------

_FN_SEARCH_SKIP: set[str] = {".venv", "venv", "__pycache__", "node_modules", ".git", ".mypy_cache", ".ruff_cache", "dist", "cache"}
_FN_SEARCH_MAX_RESULTS: int = 15


class _FnMatch(NamedTuple):
    """A single function or method matched by :func:`_search_repo`.

    Attributes
    ----------
    file : :class:`str`
        Repo-relative file path.
    line : :class:`int`
        Start line (1-indexed, includes decorators).
    end_line : :class:`int`
        End line (1-indexed, inclusive).
    qualified_name : :class:`str`
        ``ClassName.method`` or bare ``function_name``.
    source : :class:`str`
        The full extracted source text.

    """

    file: str
    line: int
    end_line: int
    qualified_name: str
    source: str


def _walk_functions(
    node: ast.AST,
    class_name: Optional[str] = None,
) -> list[tuple[Union[ast.FunctionDef, ast.AsyncFunctionDef], Optional[str]]]:
    """Yield ``(func_node, enclosing_class_name)`` for every definition in *node*.

    Recurses into classes to capture methods and into functions to capture
    closures, carrying the innermost enclosing class name forward.

    """
    results: list[tuple[Union[ast.FunctionDef, ast.AsyncFunctionDef], Optional[str]]] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            results.extend(_walk_functions(child, class_name=child.name))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            results.append((child, class_name))
            # Nested functions keep the enclosing class context
            results.extend(_walk_functions(child, class_name=class_name))
    return results


def _search_repo(root: Path, name: str) -> list[_FnMatch]:
    """Parse every ``.py`` file under *root* and collect functions matching *name*.

    Parameters
    ----------
    root : :class:`~pathlib.Path`
        The repository root to search.
    name : :class:`str`
        Function name to match.  Dotted form ``Class.method`` restricts
        results to that class.  Matching is case-insensitive; exact hits
        sort before substring hits.

    Returns
    -------
    list[:class:`_FnMatch`]
        Matched functions, exact-first then alphabetical, capped at
        :data:`_FN_SEARCH_MAX_RESULTS`.

    """
    matches: list[_FnMatch] = []
    name_lower: str = name.lower()

    # Support Class.method queries
    class_filter: Optional[str] = None
    search_name: str = name_lower
    if "." in name:
        class_filter, search_name = name.rsplit(".", maxsplit=1)
        class_filter = class_filter.lower()

    for py_file in sorted(root.rglob("*.py")):
        parts: tuple[str, ...] = py_file.relative_to(root).parts
        if any(p.startswith(".") or p in _FN_SEARCH_SKIP for p in parts):
            continue

        try:
            source: str = py_file.read_text(encoding="utf-8")
            tree: ast.Module = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        source_lines: list[str] = source.splitlines(keepends=True)

        for func_node, enclosing_class in _walk_functions(tree):
            fn_name_lower: str = func_node.name.lower()

            # Class filter; skip if the enclosing class doesn't match
            if class_filter is not None and (enclosing_class is None or enclosing_class.lower() != class_filter):
                continue

            if fn_name_lower != search_name and search_name not in fn_name_lower:
                continue

            # Pull decorators into the extracted source
            start_line: int = func_node.lineno
            if func_node.decorator_list:
                start_line = func_node.decorator_list[0].lineno
            end_line: int = func_node.end_lineno or func_node.lineno

            fn_source: str = "".join(source_lines[start_line - 1 : end_line]).rstrip()
            rel_path: str = str(py_file.relative_to(root))
            qualified: str = f"{enclosing_class}.{func_node.name}" if enclosing_class else func_node.name

            matches.append(_FnMatch(
                file=rel_path,
                line=start_line,
                end_line=end_line,
                qualified_name=qualified,
                source=fn_source,
            ))

    # Exact matches first, then partials; alphabetical within each group
    matches.sort(key=lambda m: (search_name != m.qualified_name.rsplit(".", maxsplit=1)[-1].lower(), m.file, m.line))
    return matches[:_FN_SEARCH_MAX_RESULTS]


class Utility(Cog):
    """A class to house useful commands about the bot and its code."""

    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"
    lookup: ClassVar[dict[str, URLref]] = {
        "gatekeeper": {
            "aliases": ["gk", "gkwiki"],
            "urls": [
                "https://github.com/k8thekat/GatekeeperV2",
                "https://github.com/k8thekat/GatekeeperV2/wiki",
                "https://github.com/k8thekat/GatekeeperV2/wiki/Commands",
                "https://github.com/k8thekat/GatekeeperV2/wiki/Permissions",
                "https://github.com/k8thekat/GatekeeperV2/wiki/Server-Banners",
                "https://github.com/k8thekat/GatekeeperV2/wiki/Auto-Whitelisting",
            ],
        },
        "cubecoders": {
            "aliases": ["cubecoders", "cc", "amp"],
            "urls": ["https://discord.gg/cubecoders", "https://cubecoders.com/"],
        },
        "ampapipython": {
            "aliases": ["ampapi", "cc-api", "api"],
            "urls": ["https://github.com/k8thekat/AMPAPI_Python"],
        },
        "discord": {
            "aliases": ["dpy", "d.py", "dpydocs", "dpy_docs"],
            "urls": ["https://discordpy.readthedocs.io/en/stable/", "https://discord.gg/dpy"],
        },
    }

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        self.yoink_menu = app_commands.ContextMenu(name="Yoink!", callback=self.yoink)
        self.gh_issue = app_commands.ContextMenu(name="Create GH issue", callback=self.create_github_issue)
        self.bot.tree.add_command(self.yoink_menu)
        self.bot.tree.add_command(self.gh_issue)

    # async def cog_load(self) -> None:
    #     global BOT_NAME
    #     BOT_NAME = self.bot.user.name

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.yoink_menu.name, type=self.yoink_menu.type)
        self.bot.tree.remove_command(self.gh_issue.name, type=self.gh_issue.type)

    @commands.command(help="Shows info about the bot", aliases=["botinfo", "info", "bi"])
    async def about(self, ctx: Context) -> None:
        """Tells you information about the bot itself."""
        await ctx.defer()
        # assert self.bot.user
        information: discord.AppInfo = await self.bot.application_info()
        embed = KumaEmbed(
            cog=self,
            color=discord.Color.og_blurple(),
            title="__Kuma Kuma Bear__",
            description="https://github.com/k8thekat/Kuma_Kuma",
        )
        # embed = discord.Embed(
        #     color=discord.Color.og_blurple(),
        #     title="__Kuma Kuma Bear__",
        #     description="https://github.com/k8thekat/Kuma_Kuma",
        # )

        embed.set_author(
            name=f"Made by {information.owner.name}",
            icon_url=information.owner.display_avatar.url,
        )
        memory_usage = psutil.Process().memory_full_info().uss / 1024**2
        cpu_usage: float = psutil.cpu_percent()
        load_avg: tuple = psutil.getloadavg()

        # embed.add_field(name="Process", value=f"{memory_usage:.2f} MBs \n{cpu_usage:.2f}% CPU")
        embed.add_field(
            inline=False,
            name="__Bot Stats__",
            value=f"""**Uptime:** {self.bot.uptime}
            **Memory:** {memory_usage:.2f} MB
            **CPU:** {cpu_usage:.2f}%
            **Load Avg:** 1m: `{load_avg[0]:.2f}%` | 5m: `{load_avg[1]:.2f}%` | 15m: `{load_avg[2]:.2f}%`
            **Threads:** {psutil.Process().num_threads()}
            **Latency:** {self.bot.latency * 1000:.2f}ms""",
        )

        # `len([self.bot.get_all_members()])` was measuring a one element list wrapped around the
        # generator, so this always read 1. Counted two ways because they answer different questions:
        # `bot.users` is unique people, and summing `member_count` is seats across every guild.
        unique_users: int = len(self.bot.users)
        total_members: int = sum(guild.member_count or 0 for guild in self.bot.guilds)

        # An `Intents` object reprs as all ~20 flags, most of them False, which is a wall of noise in
        # an embed. The three privileged ones are the only ones that have to be granted in the
        # developer portal, so they are the useful thing to report - with the raw bitfield alongside,
        # which is what the portal and `Intents._from_value()` actually speak.
        privileged: dict[str, bool] = {
            "members": self.bot.intents.members,
            "message_content": self.bot.intents.message_content,
            "presences": self.bot.intents.presences,
        }
        granted: str = ", ".join(f"`{name}`" for name, enabled in privileged.items() if enabled) or "`none`"

        embed.add_field(
            inline=False,
            name="__Discord__",
            value=f"""**Guilds:** {len(self.bot.guilds)}
            **Users:** {unique_users:,} unique | {total_members:,} members
            **Privileged Intents:** {granted}
            **Intents Value:** `{self.bot.intents.value}`""",
        )

        try:
            embed.add_field(
                name="__Code Stats__",
                value=f"**Lines:** {await count_lines(path='./', filetype='.py'):,}"
                f"\n**Functions:** {await count_others(path='./', filetype='.py', file_contains='def '):,}"
                f"\n**Classes**: {await count_others(path='./', filetype='.py', file_contains='class '):,}",
            )
        except (FileNotFoundError, UnicodeDecodeError):
            pass
        embed.add_field(
            name="__Latest Kuma Kuma Commits__:",
            value=get_latest_commits(
                url="https://github.com/k8thekat/Kuma_Kuma",
                repo=Repo(Path(__file__).parent.parent),
                branch="main",
                max_count=5,
            ),
            inline=False,
        )
        embed.add_field(
            name="__Latest Extension Commits__:",
            value=get_latest_commits(
                url="https://github.com/k8thekat/public_dpy_modules",
                repo=Repo(Path(__file__).parent),
                branch="main",
                max_count=5,
            ),
            inline=False,
        )

        embed.timestamp = discord.utils.utcnow()
        thumbnail = discord.File(Path("./resources/kuma_kuma_emojis/kuma_kuma_bear_sticker2.jpg"), filename="thumbnail.png")
        embed.set_thumbnail(url="attachment://thumbnail.png")
        embed.set_footer(
            text=f"Made with discord.py v{discord.__version__}, Running {platform.python_implementation()} v{platform.python_version()}",
            icon_url="https://i.imgur.com/5BFecvA.png",
        )
        banner = discord.File(Path("./resources/kuma_kuma_emojis/kuma_kuma_bear_banner.jpg"), filename="banner.png")
        embed.set_image(url="attachment://banner.png")
        await ctx.reply(embed=embed, files=[banner, thumbnail], delete_after=self.message_timeout)

    @commands.command(name="charinfo")
    async def charinfo(self, context: Context, *, characters: str = "") -> Union[discord.Message, None]:
        """Shows you information about a number of characters.

        Only up to 25 characters at a time.
        """
        if characters.startswith("<") and characters.endswith(">"):
            return await context.send(content=f"Char: {characters} | `{characters}`")

        def to_string(c: str) -> str:
            digit: str = f"{ord(c):x}"
            name: str = unicodedata.name(c, "Name not found.")
            return f"`\\U{digit:>08}`: {name} - `{c}` \N{EM DASH} {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>"

        msg: str = "\n".join(map(to_string, characters))
        if len(msg) > 2000:
            await context.reply(
                content=f"Output too long to display.. {self.emoji_table.kuma_head_clench}",
                delete_after=self.message_timeout,
            )
            return await context.send(content=f"{msg[:1995]} ....")
        return await context.send(content=msg, delete_after=self.message_timeout)

    @commands.command(name="ping")
    async def ping(self, context: Context) -> discord.Message:
        """Pong..."""
        return await context.send(
            content=f"Pong `{round(number=self.bot.latency * 1000)}ms` {self.emoji_table.kuma_heart}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @commands.command(name="get_webhooks", help="Displays a channels webhooks by `Name` and `ID`", aliases=["getwh", "gwh"])
    @commands.guild_only()
    @commands.has_permissions(manage_webhooks=True)
    async def get_webhooks(
        self,
        context: Context,
        channel: Union[discord.VoiceChannel, discord.TextChannel, discord.StageChannel, discord.ForumChannel, None],
    ) -> discord.Message:
        assert isinstance(context.channel, (discord.VoiceChannel, discord.TextChannel, discord.StageChannel, discord.ForumChannel))  # noqa: S101

        channel = channel or context.channel
        channel_webhooks: str = "\n".join([f"**{webhook.name}** | ID: `{webhook.id}`" for webhook in await channel.webhooks()])
        return await context.reply(content=f"> {channel.mention} Webhooks \n{channel_webhooks}", delete_after=self.message_timeout)

    # TODO(@k8thekat): Make a Choice using `lookup.keys() for *var*.`
    @commands.command(name="link", help="Access to useful URLs via lookup parameters")
    async def url_linking(self, context: Context, var: str = "") -> discord.Message:
        var = var.lower()
        if var == "?" or var == "":
            return await context.reply(content="*Possible Lookups:*\n" + " | ".join(list(self.lookup)))
        for key in self.lookup:
            if var in key or var in self.lookup[key]["aliases"]:
                return await context.reply(
                    suppress_embeds=True,
                    content=f"Is this right *Kuma*? {self.emoji_table.kuma_peak}:\n- " + "\n- ".join(list(self.lookup[key]["urls"])),
                )
        return await context.reply(
            content=f"I was unable to understand your request.. {self.emoji_table.kuma_head_clench}",
        )

    @commands.command(name="source")
    async def source(self, context: Context, *, command: Union[str, None]) -> Union[discord.Message, None]:
        """Displays full source code or for a specific command.

        To display the source code of a subcommand you can separate it by
        periods, e.g. tag.create for the create subcommand of the tag command
        or by spaces.
        """
        source_url = "https://github.com/k8thekat/Kuma_Kuma"
        branch = "main"
        if command is None:
            return await context.reply(source_url)

        if command == "help":
            src = type(self.bot.help_command)
            module = src.__module__
            filename = inspect.getsourcefile(src)

        else:
            obj = self.bot.get_command(command.replace(".", " "))
            if obj is None:
                return await context.reply(content=f"Could not find that command. {self.emoji_table.kuma_hmm}")

            # since we found the command we're looking for, presumably anyway, let's
            # try to access the code itself
            src = obj.callback.__code__
            module = obj.callback.__module__
            filename = src.co_filename
            code_class = obj._cog  # noqa: SLF001

            # Handles my separate repo URLs. (Could store this as part of the cog class?)
            # This requires you do define `repo_url` per script for files in a different parent directory than your bot.py
            if code_class is not None and hasattr(obj._cog, "repo_url"):  # noqa: SLF001
                source_url = obj._cog.repo_url  # pyright: ignore[reportAttributeAccessIssue]  # noqa: SLF001

        lines, firstlineno = inspect.getsourcelines(src)
        if not module.startswith("discord"):
            # not a built-in command
            if filename is None:
                return await context.reply(content=f"I couldn't find what you were looking for... {self.emoji_table.kuma_shrug}")

            # Given Kuma Kumas' submodules are in an extensions folder; this fixes the source link pathing.
            location: str = os.path.relpath(filename).replace("\\", "/").replace("extensions", "")

        else:
            location = module.replace(".", "/") + ".py"
            branch = "main"

        final_url: str = f"<{source_url}/blob/{branch}/{location}#L{firstlineno}-L{firstlineno + len(lines) - 1}>"
        await context.reply(content=final_url)
        return None

    @commands.command(name="fnsearch", aliases=["fns"])
    async def fnsearch(self, context: Context, name: str, *, repo: Optional[str] = None) -> Optional[discord.Message]:
        """Search a project repo for a function by name and display its full source.

        Uses the ``ast`` module to locate functions and methods, including
        decorators.  Supports dotted queries like ``Utility.ping`` to narrow
        by enclosing class.  Partial (substring) matches are included when no
        exact match exists.

        Falls back to a file attachment when the combined output exceeds
        Discord's character limit.

        Parameters
        ----------
        context : :class:`~utils.KumaContext`
            The invocation context.
        name : :class:`str`
            The function or method name to search for.  Use ``Class.method``
            to restrict to a specific class.
        repo : :class:`str`, optional
            Absolute path to the repo root, by default the bot's own repo.

        Examples
        --------
        - ``.fns ping`` --- find every function named ``ping``
        - ``.fns Utility.ping`` --- only the ``ping`` method on ``Utility``
        - ``.fns setup`` --- find all ``setup()`` functions across the repo
        - ``.fns purge /home/kat/gitHub/other_project`` --- search a different repo

        """
        root: Path = Path(repo) if repo else Path(__file__).resolve().parent.parent
        if not root.is_dir():
            return await context.reply(
                content=f"Directory not found: `{root}` {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )

        LOGGER.info("<%s.fnsearch> | Searching | name: %s, root: %s", __class__.__name__, name, root)

        # Offload synchronous file I/O and AST parsing to a thread
        matches: list[_FnMatch] = await asyncio.to_thread(_search_repo, root, name)

        if not matches:
            return await context.reply(
                content=f"No functions matching **{name}** found. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )

        # Build output; one block per match with a file:line header
        blocks: list[str] = []
        for m in matches:
            header: str = f"# {m.file}:{m.line} --- {m.qualified_name}"
            blocks.append(f"{header}\n{m.source}")

        combined: str = "\n\n".join(blocks)
        match_count: int = len(matches)
        label: str = f"**{match_count}** match{'es' if match_count != 1 else ''}"

        # If it fits in a message, send inline as a code block
        formatted: str = code_block(combined, "py")
        if len(formatted) <= 1950:
            return await context.reply(content=f"{label} for **{name}**:\n{formatted}")

        # Too long; send as a .py file attachment with a summary line
        filename: str = f"{name.replace('.', '_')}_results.py"
        file: discord.File = discord.File(fp=io.BytesIO(combined.encode()), filename=filename)
        summary: str = ", ".join(f"`{m.qualified_name}` ({m.file}:{m.line})" for m in matches)
        return await context.reply(content=f"{label} for **{name}**: {summary}", file=file)

    @app_commands.checks.has_permissions(manage_emojis_and_stickers=True)
    async def yoink(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(ephemeral=True)

        embeds: list[YoinkEmbed] = []
        seen_ids: set[int] = set()

        embeds.extend(YoinkEmbed(cog=self, sticker=sticker) for sticker in message.stickers)

        for reaction in message.reactions:
            emoji = reaction.emoji
            if not isinstance(emoji, str) and emoji.id is not None and emoji.id not in seen_ids:
                seen_ids.add(emoji.id)
                embeds.append(YoinkEmbed(cog=self, emoji=emoji))

        for match in CUSTOM_EMOJI_PATTERN.finditer(message.content):
            parsed: discord.PartialEmoji = discord.PartialEmoji.from_str(match.group(0))
            if parsed.id is None or parsed.id in seen_ids:
                continue
            # `with_state` wires the emoji to the bot's HTTP client so `.read()` works.
            partial: discord.PartialEmoji = discord.PartialEmoji.with_state(
                self.bot.connection_state,
                name=parsed.name or "_",
                animated=parsed.animated,
                id=parsed.id,
            )
            seen_ids.add(parsed.id)
            embeds.append(YoinkEmbed(cog=self, emoji=partial))

        # Pick up bare CDN URLs (emoji/sticker links pasted without markup).
        for asset in parse_cdn_assets(message.content):
            if asset.id in seen_ids:
                continue
            seen_ids.add(asset.id)
            if asset.kind == "emoji":
                # Probe the CDN to confirm animation; caches the image bytes on success.
                partial, image_data = await self.resolve_cdn_emoji(
                    name="cdn_emoji",
                    emoji_id=asset.id,
                    animated=asset.animated,
                )
                embeds.append(YoinkEmbed(cog=self, emoji=partial, image_data=image_data))
            else:
                # Stickers require an API call to build a proper object.
                try:
                    sticker = await self.bot.fetch_sticker(asset.id)
                    embeds.append(YoinkEmbed(cog=self, sticker=sticker))
                except discord.NotFound:
                    LOGGER.warning("<%s.%s> | CDN sticker not found | ID: %s", __class__.__name__, "yoink", asset.id)

        if not embeds:
            await interaction.followup.send(
                content=f"No yoinkable emojis or stickers found. {self.emoji_table.kuma_pout}",
                ephemeral=True,
            )
            return

        # embed.set_footer(text=f"{i + 1}/{len(embeds)} | Kuma Kuma Bear")

        embed = embeds[0]
        view = YoinkView(owner=interaction.user, cog=self, embeds=embeds, dispatched_by=None)
        await interaction.followup.send(embed=embed, files=embed.attachments, view=view)

    async def create_github_issue(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Create a github issue via a Discord Message."""
        if interaction.user.id not in self.bot.owner_ids and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                content=f"Kuma Kuma Bear says Creating GitHub Issues is only allowed for __Trusted Users__. {self.emoji_table.kuma_pout}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            return

        # Straight to the modal. The repository and type are fields inside it now, so the message
        # that used to ask for them first has nothing left to say.
        await interaction.response.send_modal(GithubIssueSubmissionModal(bot=self.bot, issue_msg=message))

    @commands.command(name="logs", help="Show the tail of the current log file. eg. `logs 25 ERROR WARNING`")
    @commands.is_owner()
    async def get_log_file(
        self,
        context: Context,
        entries: int = 15,
        *,
        levels: Optional[str] = None,
    ) -> None:
        """Show the tail of the current log file in a paginated viewer.

        Parameters
        ----------
        context : :class:`Context`
            The invoking command context.
        entries : :class:`int`, optional
            How many of the most recent records to show, by default 15.
        levels : :class:`Optional[str]`, optional
            Only show these levels, by default ``None`` (all). Space or comma
            separated, e.g. ``ERROR WARNING``.

        """
        try:
            wanted: Optional[frozenset[str]] = parse_levels(levels)
        except ValueError:
            await context.send(
                content=f"Unknown log level. {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )
            return

        try:
            raw_entries: list[str] = self.bot.loghandler._tail_entries(  # noqa: SLF001
                max(entries, 1),
                levels=wanted,
                max_bytes=LOG_TAIL_MAX_BYTES,
            )
        except (FileNotFoundError, OSError):
            await context.send(
                content=f"Could not read the log file. {self.emoji_table.kuma_sad}",
                delete_after=self.message_timeout,
            )
            return

        if not raw_entries:
            scope: str = f" matching `{levels}`" if levels else ""
            await context.send(
                content=f"No log entries{scope}. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )
            return

        # Colourise and paginate into code blocks that fit the CV2 budget.
        pages: list[str] = _paginate_log_entries([colourise_log(entry) for entry in raw_entries])

        panel: LogPanel = LogPanel(
            cog=self,
            owner_id=context.author.id,
            pages=pages,
            log_path=self.bot.loghandler.cur_log,
            filter_label=levels,
        )
        await context.send(view=panel, delete_after=self.message_timeout)

    @commands.command(name="app_emojis", help="Displays a list of all application emojis.")
    async def app_emojis(self, context: Context, *, query: Optional[str], codefmt: bool = False) -> None:
        """Displays a list of all application emojis."""
        emojis = await self.bot.fetch_application_emojis()
        self.bot._app_emojis = sorted(emojis, key=lambda x: x.name)  # noqa: SLF001

        content = "__**Application Emojis:**__\n"
        if query is not None:
            content = ""

        for indx, emoji in enumerate(self.bot._app_emojis):  # noqa: SLF001
            temp = f"- {emoji} | Inline: `<:{emoji.name}:{emoji.id}>`"
            if codefmt:
                temp = f'`{emoji.name} = "<:{emoji.name}:{emoji.id}>"`'

            if query is not None:
                if query.lower() in emoji.name.lower():
                    content = f"Found matching emoji {self.emoji_table.kuma_happy}:\n{temp}"
                    # await context.send(content=f"Found matching emoji {self.emoji_table.kuma_happy}:\n{temp}", reference=context.message)
                    break

                continue

            if indx > len(emojis) - 1:
                break

            if len(content + temp) > 1950:
                await context.send(content=content, reference=context.message)
                content = temp + "\n"
            else:
                content += temp + "\n"

        # content will always be > 0 if query is None.
        if len(content):
            await context.send(content=content, reference=context.message)
        else:
            await context.send(content=f"Could not find matching emoji {self.emoji_table.kuma_pout}", reference=context.message)
            return

    @commands.command(name="reload_app_emojis", help="Reloads the application emojis from Discord.")
    async def reload_app_emojis(self, context: Context) -> discord.Message:
        """Reloads the application emojis from Discord."""
        self.bot._app_emojis = await self.bot.fetch_application_emojis()  # noqa: SLF001
        return await context.send(content=f"Reloaded application emojis {self.emoji_table.kuma_happy}", delete_after=self.message_timeout)

    @commands.command(name="coin_flip", help="Flip a coin")
    async def coin_flip(self, context: Context) -> None:
        seed = datetime.datetime.now(datetime.UTC).timestamp()
        random.seed(seed)
        val = random.randint(0, 1)  # noqa: S311
        await context.send(content=f"{'Heads' if val == 0 else 'Tails'} {self.emoji_table.kuma_wow}")


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(Utility(bot=bot))
