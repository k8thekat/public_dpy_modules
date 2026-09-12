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

import asyncio
import hashlib
import html
import io
import json
import logging
import re
import struct
import sys
import time
from configparser import ConfigParser
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Optional, Self, TypedDict, Union, Unpack

import asyncpraw
import asyncprawcore
import asyncprawcore.exceptions
import discord
import tzlocal
from discord import Color, app_commands
from discord.ext import commands, tasks
from fake_useragent import UserAgent
from PIL import Image, ImageFilter
from PIL._util import DeferredError  # No public API to check for a deferred/broken image.
from PIL.Image import Resampling

from utils import KumaCog as Cog, KumaContainer, KumaLayoutView, PanelAccess

# Optional compiled helper for byte-level edge comparisons; the cog degrades to hash-only
# duplicate detection when it is not installed.
try:
    import xy_binfind
except ImportError:
    xy_binfind = None

# Optional perceptual-hash helper; robust to JPEG re-encoding/quality differences from the
# Reddit CDN that SHA-256 (byte-exact) misses. Falls back to hash + edge dedupe when absent.
try:
    import imagehash as _imagehash
except ImportError:
    _imagehash = None

if TYPE_CHECKING:
    from collections.abc import Sequence
    from sqlite3 import Row

    from aiohttp import ClientResponse
    from asyncpraw.models import Subreddit
    from asyncpraw.reddit import Submission

    from kuma_kuma import Kuma_Kuma
    from utils import ContainerParams, KumaContext as Context, LayoutViewParams
    from utils.ui import V

LOGGER: logging.Logger = logging.getLogger(__name__)

REDDIT_BASE_URL = "https://www.reddit.com"
WEBHOOK_CACHE_TTL: int = 180  # Seconds to cache guild webhook fetches for autocomplete.
# The reaction that queues an image for comparison, on the crawler's own posts only.
# An embed title caps at 256; a `##` heading has no cap, but a paragraph is not a heading.
TITLE_LIMIT: int = 256
# Characters of page body a `RedditPagePanel` caller may spend, leaving the heading and footer room
# inside Discord's 4000 character Components V2 budget.
PAGE_LIMIT: int = 3500
# Extensions Discord's CDN will serve a media gallery item from; see `media_filename`.
IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
# Stable `custom_id` for the crawler gallery buttons; the trailing digits are the page to render.
# The buttons carry their own destination so a page turn needs no in-memory view, which is what
# survives a restart. See `on_gallery_page`.
GALLERY_PAGE_PREFIX: str = "RS::PAGE::"
GALLERY_PAGE_REGEX: re.Pattern[str] = re.compile(r"RS::PAGE::(?P<POSITION>\d+)")
# How long a sent gallery stays pageable before `prune_galleries` drops its rows.
GALLERY_MAX_AGE_DAYS: int = 30

SUBREDDIT_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS subreddit (
    id INTEGER PRIMARY KEY NOT NULL,
    name TEXT COLLATE NOCASE NOT NULL UNIQUE,
    webhook_id INTEGER,
    FOREIGN KEY (webhook_id) references webhook(id)
)"""

WEBHOOK_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS webhook (
    id INTEGER PRIMARY KEY NOT NULL,
    name TEXT COLLATE NOCASE NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE
)"""

METRICS_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS crawler_metrics (
    id INTEGER PRIMARY KEY NOT NULL,
    subreddit TEXT NOT NULL,
    run_at REAL NOT NULL,
    posts_seen INTEGER DEFAULT 0,
    images_found INTEGER DEFAULT 0,
    duplicates_skipped INTEGER DEFAULT 0,
    webhooks_sent INTEGER DEFAULT 0
)"""


GALLERY_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS gallery_page (
    id INTEGER PRIMARY KEY NOT NULL,
    message_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    subreddit TEXT NOT NULL,
    title TEXT NOT NULL,
    permalink TEXT NOT NULL,
    created REAL NOT NULL,
    media_url TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    first_post_url TEXT,
    previous_post_url TEXT,
    UNIQUE (message_id, position)
)"""


class Webhook(TypedDict):
    name: str
    url: str
    id: int


class GalleryPageRow(TypedDict):
    """One page of a sent crawler gallery; enough to rebuild its :class:`RedditPostContainer`."""

    message_id: int
    position: int
    subreddit: str
    title: str
    permalink: str
    created: float
    media_url: str
    width: Optional[int]
    height: Optional[int]
    first_post_url: Optional[str]
    previous_post_url: Optional[str]


class JSONTyped(TypedDict):
    url_list: list[str]
    last_check: float
    hash_list: list[str]
    phash_list: NotRequired[list[str]]
    last_message_jump: NotRequired[dict[str, str]]


class ImageInfo:
    """Resolution and edge-comparison result for a downloaded image."""

    __slots__ = ("edge_res", "height", "width")

    def __init__(self, width: int = 0, height: int = 0, edge_res: bool = True) -> None:
        self.width: int = width
        self.height: int = height
        self.edge_res: bool = edge_res


class SubRedditTable(TypedDict):
    subreddit: str
    webhook_url: str
    webhook_name: str


class ImageComparison:
    """Compares two PIL Images edge maps to determine if they are "similar".

    Attributes
    ----------
    match_percent: :class:`int`
        This is the percentage base match value, results must be this or higher. Defaults to 90%.
    line_detect: :class:`int`
        This is the 0-255 value we use to determine if the pixel is a "line". Defaults to 128.
    sample_percent: :class:`int`
        This is the % of edge cords to use for comparison. Defaults to 10%.
    sample_dimensions: :class:`tuple[int, int]`
        This is the default resolution to scale all images down to (or up). Defaults to (500, 500).

    """

    def __init__(self) -> None:
        self._match_percent: int = 90
        self._line_detect: int = 128
        self._sample_percent: int = 10
        self._sample_dimensions: tuple[int, int] = (500, 500)
        self._etime: float = 0.0
        self._p_match: int = 0

    @property
    def results(self) -> str:
        """Get the recent results from `compare()` showing the time taken and the percentage of a match.

        Returns
        -------
        :class:`str`
            Results of the most recent compare.

        """
        return f"Time taken {self._etime:.2f} seconds, with a {self._p_match}% match."

    @property
    def match_percent(self) -> int:
        """The percentage base match value, results must be this or higher. Defaults to 90%."""
        return self._match_percent

    @property
    def line_detect(self) -> int:
        """The 0-255 value we use to determine if the pixel is a "line". Defaults to 128."""
        return self._line_detect

    @property
    def sample_percent(self) -> int:
        """The % of edge cords to use for comparison. Defaults to 10%."""
        return self._sample_percent

    @property
    def sample_dimensions(self) -> tuple[int, int]:
        """The default resolution to scale all images down to (or up). Defaults to (500, 500)."""
        return self._sample_dimensions

    def set_match_percent(self, percent: int = 90) -> None:
        """Sets the percentage required of match's to be considered a duplicate.

        Parameters
        ----------
        percent: :class:`int`, optional
            0-100 Percent value, by default 90.

        Raises
        ------
        ValueError
            Value out of bounds.

        """
        if percent > 100 or percent < 0:
            msg = "You must provide a value no greater than 100 and no less than 0."
            raise ValueError(msg)
        self._match_percent = percent

    def set_line_detect(self, line_value: int = 128) -> None:
        """Sets the value to consider a "pixel" value to be considered an edge/line.

        Parameters
        ----------
        line_value: :class:`int`, optional
            0-255 Pixel value, by default 128.

        Raises
        ------
        ValueError
            Value out of bounds.

        """
        if line_value > 255 or line_value < 0:
            msg = "You must provide a value no greater than 255 and no less than 0."
            raise ValueError(msg)
        self._line_detect = line_value

    def set_sample_percent(self, percent: int = 10) -> None:
        """Sets the percentage of Edge (X,Y) cords to use when comparing images. Images will have 10000+/- edges found.

        eg. `(10000 * .01) = 100` points checked.

        Parameters
        ----------
        percent: :class:`int`, optional
            0-100 Percent value, by default 10.

        Raises
        ------
        ValueError
            Value out of bounds.

        """
        if percent > 100 or percent < 0:
            msg = "You must provide a value no greater than 100 and no less than 0."
            raise ValueError(msg)
        self._sample_percent = percent

    def set_sample_resolution(self, dimensions: tuple[int, int] = (500, 500)) -> None:
        """Set the image dimensions to scale down images for like comparisons and pixel edge detection.

        .. note::
            A lower resolution speeds the process, and a fixed dimension value keeps all images
            lined up when doing array comparisons.

        Parameters
        ----------
        dimensions: :class:`tuple[int, int]`, optional
            The dimensions to scale images to, by default (500, 500).

        Raises
        ------
        ValueError
            Value out of bounds.

        """
        for value in dimensions:
            if value < 0:
                msg = "You must provide a value greater than 0."
                raise ValueError(msg)
        self._sample_dimensions = dimensions

    def _convert(self, image: Image.Image) -> Image.Image:
        """Converts the image to Grayscale `("L")` mode.

        Parameters
        ----------
        image: :class:`Image.Image`
            PIL Image.

        Returns
        -------
        :class:`Image.Image`
            PIL Image.

        """
        if image.mode == "L":
            return image
        if image.mode == "P" and "transparency" in image.info:
            image = image.convert("RGBA")
        return image.convert("L")

    def _filter(self, image: Image.Image, img_filter: type[ImageFilter.Filter] = ImageFilter.FIND_EDGES) -> Image.Image:
        """Applies the filter provided to the image and returns the results.

        Parameters
        ----------
        image: :class:`Image.Image`
            PIL Image.
        img_filter: :class:`ImageFilter.Filter`, optional
            PIL Image Filter, by default ImageFilter.FIND_EDGES.

        Returns
        -------
        :class:`Image.Image`
            Filtered PIL Image.

        """
        return image.filter(filter=img_filter)

    def _image_resize(
        self,
        source: Image.Image,
        comparison: Optional[Image.Image] = None,
        sampling: Resampling = Resampling.BICUBIC,
        scale_percent: int = 50,
        image_size: Optional[tuple[int, int]] = (500, 500),
    ) -> tuple[Image.Image, Optional[Image.Image]]:
        """Resizes the source image and resizes the comparison image to the same resolution as the source.

        `**THIS MUST BE BEFORE _filter or it will saturate the white.**`

        This can be run solo; to resize the `source` parameter.

        Parameters
        ----------
        source: :class:`Image.Image`
            PIL Image.
        comparison: :class:`Optional[Image.Image]`, optional
            PIL Image, the image to scale down, by default `None`.
        sampling: :class:`Resampling`, optional
            PIL Resampling, by default Resampling.BICUBIC.
        scale_percent: :class:`int`, optional
            The percentage to resize the image when `image_size` is `None`, by default 50.
        image_size: :class:`Optional[tuple[int, int]]`, optional
            The dimensions to scale the image down (or up) to, set to `None` to use source image dimensions, by default (500, 500).

        Returns
        -------
        :class:`tuple[Image.Image, Optional[Image.Image]]`
            Resized PIL Images.

        """
        if image_size is None:
            dimensions: tuple[int, int] = (
                int(source.height * (scale_percent / 100)),
                int(source.width * (scale_percent / 100)),
            )
        else:
            dimensions = image_size

        source = source.resize(size=dimensions, resample=sampling)
        if comparison is not None:
            comparison = comparison.resize(size=dimensions, resample=sampling)
            return source, comparison
        return source, None

    def _edge_detect(self, image: Image.Image) -> Optional[list[tuple[int, int]]]:
        """Retrieves all our pixel data of the Image.

        Then iterates from 0,0 looking for a pixel value above or equal to our `line_detect` value.
        When a pixel value high enough has been found it is added to our array.

        Parameters
        ----------
        image: :class:`Image.Image`
            PIL Image.

        Returns
        -------
        :class:`Optional[list[tuple[int, int]]]`
            List of (X,Y) cords.

        Raises
        ------
        RuntimeError
            We ran into an error handling getdata().
        ValueError
            We failed to get any data from the image.

        """
        edges: list[tuple[int, int]] = []

        pixels = image.getdata()
        if isinstance(pixels, DeferredError):
            msg = f"We ran into an error handling the image. | {pixels.ex}"
            raise RuntimeError(msg)  # noqa: TRY004
        if pixels is None:
            msg = "We failed to get any data from the image."
            raise ValueError(msg)
        for x in range(len(pixels)):
            if pixels[x] >= self._line_detect:
                edges.append((int(x % image.width), int(x / image.width)))  # noqa: PERF401

        return edges

    def _pixel_comparison(self, image: Image.Image, cords: tuple[int, int]) -> bool:
        """Uses (X,Y) cords to check a pixel if its above or equal to our `line_detect` value.

        Parameters
        ----------
        image: :class:`Image.Image`
            PIL Image.
        cords: :class:`tuple[int, int]`
            X,Y coordinates.

        Returns
        -------
        :class:`bool`
            `True` if the pixel value is higher than our `line_detect` value else `False`.

        Raises
        ------
        ValueError
            Coordinate value out of bounds.

        """
        if cords[0] >= image.width or cords[0] < 0:
            msg = f"You provided a X value that is out of bounds. Value: {cords[0]} - Limit: {image.width}"
            raise ValueError(msg)
        if cords[1] >= image.height or cords[1] < 0:
            msg = f"You provided a Y value that is out of bounds. Value: {cords[1]} - Limit: {image.height}"
            raise ValueError(msg)
        res = image.getpixel(cords)
        return isinstance(res, int) and res >= self._line_detect

    def _pixel_nearmatch(self, image: Image.Image, cords: tuple[int, int], distance: int = 3) -> bool:
        """Will search a radius around (X,Y) cords.

        Based upon the provided distance value looking for a pixel value above our `line_detect` value.

        Parameters
        ----------
        image: :class:`Image.Image`
            PIL Image.
        cords: :class:`tuple[int, int]`
            X,Y coordinates.
        distance: :class:`int`, optional
            Radius from (X,Y), by default 3.

        Returns
        -------
        :class:`bool`
            `True` if the pixel value is higher than our `line_detect` value else `False`.

        """
        for y in range(-distance, distance + 1):
            res_y: int = cords[1] + y
            if res_y >= image.height or res_y < 0:
                continue

            for x in range(-distance, distance + 1):
                res_x: int = cords[0] + x
                if res_x >= image.width or res_x < 0:
                    continue

                res = image.getpixel((res_x, res_y))
                if isinstance(res, int) and res >= self._line_detect:
                    return True

        return False

    def compare(self, source: Image.Image, comparison: Image.Image, resize_dimensions: Optional[tuple[int, int]] = (500, 500)) -> bool:
        """Automates the edge detection of our source image against our comparison image to see if the images are "similar".

        Parameters
        ----------
        source: :class:`Image.Image`
            PIL Image.
        comparison: :class:`Image.Image`
            PIL Image.
        resize_dimensions: :class:`Optional[tuple[int, int]]`, optional
            The dimensions to scale the image down (or up) to, set to `None` to use source image dimensions, by default (500, 500).

        Returns
        -------
        :class:`bool`
            `True` if the resulting image has enough matches over our `match_percent`.

        """
        results_array: list[bool] = []
        stime: float = time.time()

        # We need to convert both images to GrayScale and run PIL Find Edges filter.
        source = self._convert(image=source)
        comparison = self._convert(image=comparison)

        # We need to make our source and comparison image match resolutions.
        # We also scale them down to help processing speed.
        res_source, res_comparison = self._image_resize(source=source, comparison=comparison, image_size=resize_dimensions)
        if res_comparison is not None:
            source = self._filter(image=res_source)
            comparison = self._filter(image=res_comparison)

        # We find all our edges, append any matches above our pixel threshold; otherwise we attempt to do a near match search.
        # After we have looked at both options; we append our bool result into our array and decide if the matches are above the threshold.
        edges: Optional[list[tuple[int, int]]] = self._edge_detect(image=source)
        if edges is None or len(edges) == 0:
            return False

        step: int = int(len(edges) / (len(edges) * (self._sample_percent / 100)))
        for pixel in range(0, len(edges), step):
            res: bool = self._pixel_comparison(image=comparison, cords=edges[pixel])
            if res is False:
                res = self._pixel_nearmatch(image=comparison, cords=edges[pixel])
            results_array.append(res)

        self._p_match = int((sum(results_array) / len(results_array)) * 100)
        self._etime = time.time() - stime
        return self._p_match >= self._match_percent


# TODO: Improve the filtering; still making mistakes.
def link_label(text: str) -> str:
    """Escape a masked link's label so brackets inside it do not close the link early.

    Reddit titles are full of `[OC]` and `[Serious]` tags; an unescaped `]` ends the label and the
    remainder of the title arrives as literal text with a bare URL trailing it.

    Parameters
    ----------
    text: :class:`str`
        The title, or any other string being used as the label of a `[label](url)`.

    Returns
    -------
    :class:`str`
        The same string with `[` and `]` backslash escaped.

    """
    return text.replace("[", "\\[").replace("]", "\\]")


def media_filename(img_url: str) -> str:
    """Name an in-line attachment after the extension its URL claims.

    A :class:`discord.MediaGalleryItem` is served by Discord's CDN off the attachment's name, so the
    extensionless `image` the embed path used renders as a broken tile rather than a photo.

    Parameters
    ----------
    img_url: :class:`str`
        The source URL of the image.

    Returns
    -------
    :class:`str`
        A filename of the form `image.<ext>`, falling back to `.png` when the URL claims nothing.

    """
    suffix: str = Path(img_url.split(sep="?", maxsplit=1)[0]).suffix.lower()
    return f"image{suffix}" if suffix in IMAGE_SUFFIXES else "image.png"


class RedditTextPanel(KumaContainer):
    """Pre-rendered text pages in a Components V2 container.

    The Components V2 counterpart of the `KumaEmbed` + :class:`KumaView` pairing used for the
    listings and the crawler metrics.

    .. warning::
        Discord's 4000 character budget is **not** enforced by discord.py, and `content_length()`
        counts `TextDisplay` content only. Pages are built by the caller, so the caller owns the
        budget; :attr:`PAGE_LIMIT` is what the cog's own callers slice to.

    """

    def __init__(self, *, title: str, body: str) -> None:
        """Build the panel.

        Parameters
        ----------
        title: :class:`str`
            The heading, repeated on every page.
        body: :class:`str`
            The body of the Container.

        """
        super().__init__(include_footer=True)
        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        self.add_separator(True)  # noqa: FBT003
        self.add_item(discord.ui.TextDisplay(body))


class RedditPostContainer(KumaContainer):
    """Everything from a Reddit Post, turned into a re-usable :class:`RedditPostContainer` object that supports pagination."""

    __slots__ = ("created", "first_post_url", "img_info", "media", "permalink", "previous_post_url", "sub", "title")
    default_color: Color = discord.Color.orange()
    """Default to :class:`discord.Color.orange()`."""

    view: KumaLayoutView

    def __init__(
        self,
        sub: str,
        title: str,
        permalink: str,
        created: datetime,
        media: str,
        img_info: Optional[ImageInfo] = None,
        first_post_url: Optional[str] = None,
        previous_post_url: Optional[str] = None,
        *children: discord.ui.Item[V],
        **kwargs: Unpack[ContainerParams],
    ) -> None:
        self.sub: str = sub
        self.title: str = title
        self.permalink: str = permalink
        self.media: str = media
        self.created: datetime = created
        self.img_info: Optional[ImageInfo] = img_info
        self.first_post_url: Optional[str] = first_post_url
        self.previous_post_url: Optional[str] = previous_post_url

        if kwargs.get("accent_color") is None and kwargs.get("accent_colour") is None:
            kwargs["accent_color"] = self.default_color

        super().__init__(*children, include_footer=True, **kwargs)

    async def _kuma_populate(self) -> None:
        """Build the post body once mounted; the base :meth:`~KumaContainer._kuma_prepare` adds the footer."""
        self.add_item(discord.ui.TextDisplay(f"## [{link_label(self.title[:TITLE_LIMIT])}]({self.permalink})"))
        self.add_separator(True)  # noqa: FBT003
        self.add_item(self._details())
        links: Optional[discord.ui.TextDisplay] = self._links()

        if links is not None:
            self.add_item(links)

        self.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(self.media)))

    def _details(self) -> discord.ui.TextDisplay:
        """The subtext line under the title - where it came from, how old it is, how big it is."""
        assert self.view.cog  # noqa: S101 | We know
        parts: list[str] = [
            f"{self.view.cog.unicode.right_hook_arrow} [/r/{self.sub}]({REDDIT_BASE_URL}/r/{self.sub}/new/)",
            f"<t:{int(self.created.timestamp())}:R>",
        ]

        if self.img_info is not None and self.img_info.width and self.img_info.height:
            parts.append(f"{self.img_info.width}x{self.img_info.height}")

        return discord.ui.TextDisplay(f"-# {f' {self.view.cog.unicode.middle_dot} '.join(parts)}")

    def _links(self) -> Optional[discord.ui.TextDisplay]:
        """The jump links back through the day's posts; ``None`` when the crawler has sent none yet.

        Returns
        -------
        :class:`Optional[discord.ui.TextDisplay]`
            The links line, or ``None`` when there are no links; a :class:`discord.ui.TextDisplay`
            cannot carry empty content.

        """
        links: list[str] = []
        if self.first_post_url is not None:
            links.append(f"[First of the day]({self.first_post_url})")
        if self.previous_post_url is not None:
            links.append(f"[Previous post]({self.previous_post_url})")
        if not links:
            return None
        return discord.ui.TextDisplay(f" {self._middle_dot} ".join(links))

    def _paginator_footer(self) -> Self:
        """Label gallery pagination as *Image*."""
        count: str = f"{self.view_pos + 1}/{self.view.c_length}"
        return self.add_item(discord.ui.TextDisplay(content=f"-# Image {count} {self._middle_dot} Kuma Kuma Bear"))

    @classmethod
    def from_row(cls, row: GalleryPageRow) -> RedditPostContainer:
        """Rebuild a page from its stored `gallery_page` row.

        Parameters
        ----------
        row: :class:`GalleryPageRow`
            The stored page, as returned by :meth:`RedditImageCrawler.get_gallery_pages`.

        Returns
        -------
        :class:`RedditPostContainer`
            The rebuilt page.

        """
        img_info: Optional[ImageInfo] = None
        if row["width"] and row["height"]:
            # Stored dimensions are display only; edge_res is a dedupe result that is not persisted.
            img_info = ImageInfo(width=row["width"], height=row["height"], edge_res=False)

        return cls(
            sub=row["subreddit"],
            title=row["title"],
            permalink=row["permalink"],
            created=datetime.fromtimestamp(row["created"], tz=UTC),
            media=row["media_url"],
            img_info=img_info,
            first_post_url=row["first_post_url"],
            previous_post_url=row["previous_post_url"],
        )

    @classmethod
    def from_submission(
        cls,
        *,
        sub: str,
        submission: Submission,
        media: str,
        img_info: Optional[ImageInfo] = None,
        first_post_url: Optional[str] = None,
        previous_post_url: Optional[str] = None,
    ) -> RedditPostContainer:
        """Build the render data from an asyncpraw Submission.

        Parameters
        ----------
        sub: :class:`str`
            The subreddit name the submission belongs to, without the leading `/r/`.
        submission: :class:`Submission`
            The asyncpraw Submission; provides the title, permalink and creation timestamp.
        media: :class:`str`
            The image to show, as a remote URL.
        img_info: :class:`Optional[ImageInfo]`, optional
            The resolution information of the image, by default `None`.
        first_post_url: :class:`Optional[str]`, optional
            A jump link to the first post sent today for this subreddit, by default `None`.
        previous_post_url: :class:`Optional[str]`, optional
            A jump link to the previous Discord message sent for this subreddit, by default `None`.

        Returns
        -------
        :class:`RedditPostContainer`
            The populated render data.

        """
        return cls(
            sub=sub,
            title=submission.title,
            permalink=f"{REDDIT_BASE_URL}{submission.permalink}",
            created=submission.created_datetime,
            media=media,
            img_info=img_info,
            first_post_url=first_post_url,
            previous_post_url=previous_post_url,
        )


class RedditGalleryView(KumaLayoutView):
    """A crawler gallery whose page turns outlive the bot process.

    Pages are rebuilt from the `gallery_page` table by :meth:`RedditImageCrawler.on_gallery_page`
    rather than from the view held in memory, so a restart does not break an old post.

    .. note::
        The navigation buttons are deliberately inert :class:`discord.ui.Button` rather than
        :class:`NavButton`; each carries the page it renders in its `custom_id` and the cog's
        listener does the work. A live :class:`NavButton` callback would double handle the click
        alongside that listener.

    """

    async def page_turn(self, interaction: discord.Interaction, step: int) -> None:  # noqa: ARG002
        """Not used; :meth:`RedditImageCrawler.on_gallery_page` owns page turns for this view.

        The inherited implementation drives :class:`NavButton` state this view does not build, so it
        is stubbed out rather than left to raise on the attributes it expects.

        Parameters
        ----------
        interaction: :class:`discord.Interaction`
            The component interaction; unused.
        step: :class:`int`
            The offset that would have been applied; unused.

        """
        LOGGER.warning("<%s.%s> | Page turns are handled by the cog listener.", type(self).__name__, "page_turn")

    def navigation_row(self) -> discord.ui.ActionRow[KumaLayoutView]:
        """Build the paging row; each button names the page it renders in its `custom_id`.

        Returns
        -------
        :class:`discord.ui.ActionRow`
            The row holding the paging buttons.

        """
        return discord.ui.ActionRow(
            discord.ui.Button(
                style=discord.ButtonStyle.blurple,
                label="Previous",
                emoji="\U00002b05",
                disabled=self.indx == 0,
                custom_id=f"{GALLERY_PAGE_PREFIX}{max(self.indx - 1, 0)}",
            ),
            discord.ui.Button(
                style=discord.ButtonStyle.blurple,
                label="Next",
                emoji="\U000027a1",
                disabled=self.indx == self.c_length - 1,
                custom_id=f"{GALLERY_PAGE_PREFIX}{min(self.indx + 1, self.c_length - 1)}",
            ),
        )


class RedditImageCrawler(Cog):
    """Reddit Subreddit Image Crawler.

    Periodically crawls configured subreddits for new image submissions,
    filters out duplicates via sha256 hashing and (optionally) pixel edge comparison,
    then delivers each post as a :class:`RedditPost` through its mapped Discord webhook.
    """

    _reddit: asyncpraw.Reddit

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        self.file_dir: Path = Path(__file__).parent

        self.interrupt_loop: bool = False
        self.delay_loop: float = 1

        # Used to keep track of edits to the DB to prevent un-needed DB lookups on submissions.
        self.recent_edit: bool = False

        # Image hash and URL storage.
        self.json: Path = self.file_dir.joinpath("reddit.json")
        self.url_list: set[str] = set()  # Recently sent URLs.
        self.hash_list: set[str] = set()  # Recently hashed images.
        self.phash_list: set[str] = set()  # Perceptual hashes for encoding-agnostic dedupe.
        self.url_prefixes: tuple[str, ...] = ("http://", "https://")

        # Edge Detection comparison.
        self.array_bin: Path = self.file_dir.joinpath("reddit_array.bin")
        self.pixel_cords_array: list[bytes] = []
        self.image_comp: ImageComparison = ImageComparison()

        # This is how many posts in each subreddit the script will look back.
        # By default the subreddit script looks at subreddits in `NEW` listing order.
        self.submission_limit: int = 30
        self.subreddits: list[SubRedditTable] = []
        self.webhooks: list[Webhook] = []

        self.last_check: datetime = datetime.now(tz=UTC)

        # Tracks the first post sent per subreddit each day for the embed jump links.
        self.daily_first_post: dict[str, tuple[date, str]] = {}
        # Tracks the last Discord message sent per subreddit for the embed jump links.
        self.last_message_jump: dict[str, str] = {}

        # This forces the timezone to change based upon your OS for better readability in logs.
        # This script uses `UTC` for functionality purposes.
        self.system_tz: tzinfo = tzlocal.get_localzone()

        # Purely used to fill out the user_agent parameter of PRAW.
        self.sys_os: str = sys.platform.title()
        # Used on our aiohttp session; some image hosts reject the default aiohttp agent.
        self.user_agent: str = UserAgent().chrome

        # Default value; change in `reddit_cog.ini`.
        self.user_name: str = "Reddit Crawler"

        # Guild webhook fetches cached per guild id for autocomplete; see WEBHOOK_CACHE_TTL.
        self._guild_webhook_cache: dict[int, tuple[float, list[discord.Webhook]]] = {}

    def crawler_webhook_ids(self) -> set[int]:
        """The Discord IDs of the webhooks the crawler posts through.

        The database stores a webhook's URL and its own row ID, neither of which is the snowflake a
        message carries in `Message.webhook_id`. The ID is the second to last path segment of the
        URL, so it is read back out of there rather than stored twice.

        Returns
        -------
        :class:`set[int]`
            Every webhook ID the crawler knows about; empty when none are configured.

        """
        ids: set[int] = set()
        for webhook in self.webhooks:
            segments: list[str] = webhook["url"].rstrip("/").split("/")
            if len(segments) >= 2 and segments[-2].isdigit():
                ids.add(int(segments[-2]))
        return ids

    async def cog_load(self) -> None:
        """Creates the Sqlite tables if not present.

        Gets settings from `reddit_cog.ini`.

        Creates `reddit.json` if not present and gets our URL and Hash lists.

        Creates our subreddit list and starts the scrape loop.
        """
        async with self.bot.pool.acquire() as conn:
            await conn.execute(SUBREDDIT_SETUP_SQL)
            await conn.execute(WEBHOOK_SETUP_SQL)
            await conn.execute(METRICS_SETUP_SQL)
            await conn.execute(GALLERY_SETUP_SQL)

        # self._sessions = aiohttp.ClientSession(headers={"User-Agent": self.user_agent})

        # Grab our PRAW settings.
        await self._ini_load()
        # Grab our hash/url DB.
        try:
            self.last_check = self.json_load()
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            LOGGER.warning("<%s.%s> | Failed to load %s, using current time. | %s", __class__.__name__, "cog_load", self.json, e)
            self.last_check = datetime.now(tz=UTC)

        self.subreddits = await self._get_all_subreddits()

        if xy_binfind is None:
            LOGGER.warning(
                "<%s.%s> | `xy_binfind` is not installed; edge comparison is disabled, using sha256 hashing only.",
                __class__.__name__,
                "cog_load",
            )
        else:
            # Load our array data.
            await self.read_array()

        if self.check_loop.is_running() is False:
            LOGGER.info(
                "<%s.%s> | Starting our Crawler Loop | Interval: %s minutes", __class__.__name__, "cog_load", self.check_loop.minutes
            )
            self.check_loop.start()

    async def cog_unload(self) -> None:
        """Saves our URL and hash lists.

        Stops our scrape loop if running and closes any open connections.
        """
        self.json_save()
        if xy_binfind is not None:
            await self.save_array()
        await self._reddit.close()
        if self.check_loop.is_running() is True:
            self.check_loop.cancel()

    async def _ini_load(self) -> None:
        """Gets the Reddit login information and additional settings from `reddit_cog.ini`.

        Raises
        ------
        FileNotFoundError
            If `local.ini` does not exist in the extensions directory.

        """
        _setting_file: Path = self.file_dir.parent.joinpath("local.ini")
        if _setting_file.is_file() is False:
            msg = f"Failed to load `{_setting_file}`; the file does not exist."
            raise FileNotFoundError(msg)

        settings = ConfigParser(converters={"list": lambda setting: [value.strip() for value in setting.split(",")]})
        settings.read(_setting_file.as_posix())
        # PRAW SETTINGS
        _reddit_secret: str = settings.get("REDDIT", "secret")
        _reddit_client_id: str = settings.get("REDDIT", "client_id")
        _reddit_username: str = settings.get("REDDIT", "username")
        _reddit_password: str = settings.get("REDDIT", "password")

        # CONFIG
        # _temp_name: str = settings.get("CONFIG", "username")
        # if len(_temp_name):
        #     self.user_name = _temp_name

        self._reddit = asyncpraw.Reddit(
            client_id=_reddit_client_id,
            client_secret=_reddit_secret,
            password=_reddit_password,
            user_agent=f"{self.sys_os}:kuma_kuma.reddit_is:v2 (by /u/{_reddit_username})",
            username=_reddit_username,
        )

    def json_load(self) -> datetime:
        """Loads our last_check, url_list, hash_list and phash_list from `reddit.json`.

        Otherwise creates the file and sets our initial last_check time.

        Returns
        -------
        :class:`datetime`
            The last check value from the json file, otherwise the current time.

        """
        last_check: datetime = datetime.now(tz=UTC)

        if self.json.is_file() is False:
            self.json_save()
            return last_check

        with self.json.open() as jfile:
            data: JSONTyped = json.load(jfile)

        if "last_check" in data:
            last_check = datetime.fromtimestamp(data["last_check"], tz=UTC)

        if "url_list" in data:
            self.url_list = set(data["url_list"])

        if "hash_list" in data:
            self.hash_list = set(data["hash_list"])

        if "phash_list" in data:
            self.phash_list = set(data["phash_list"])

        if "last_message_jump" in data:
            self.last_message_jump = dict(data["last_message_jump"])

        return last_check

    def json_save(self) -> None:
        """Saves our last_check, url_list, hash_list and phash_list to `reddit.json`.

        We generate an upper limit of the lists based upon the number of subreddits times the submission search limit;
        this allows for configuration changes without having to re-scale the limit of the lists.
        """
        limiter: int = (len(self.subreddits) * self.submission_limit) * 3

        # Sets don't support slicing; pop arbitrary entries when over the limit.
        while len(self.url_list) > limiter:
            self.url_list.pop()

        while len(self.hash_list) > limiter:
            self.hash_list.pop()

        while len(self.phash_list) > limiter:
            self.phash_list.pop()

        data: JSONTyped = {
            "last_check": self.last_check.timestamp(),
            "url_list": list(self.url_list),
            "hash_list": list(self.hash_list),
            "phash_list": list(self.phash_list),
            "last_message_jump": self.last_message_jump,
        }
        with self.json.open("w") as jfile:
            json.dump(data, jfile)

    async def autocomplete_subreddit(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002
        """Autocomplete for the subreddit name from the database."""
        return [
            app_commands.Choice(name=subreddit["subreddit"], value=subreddit["subreddit"])
            for subreddit in self.subreddits
            if current.lower() in subreddit["subreddit"].lower()
        ][:25]

    async def autocomplete_webhook(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002
        """Autocomplete for the webhook name from the database."""
        return [
            app_commands.Choice(name=webhook["name"], value=webhook["name"])
            for webhook in self.webhooks
            if current.lower() in webhook["name"].lower()
        ][:25]

    async def autocomplete_webhook_with_guild(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Autocomplete offering both database webhooks (by name) and the server's webhooks (by id, labeled with their channel)."""
        choices: list[app_commands.Choice[str]] = []
        seen_urls: set[str] = set()
        for webhook in self.webhooks:
            seen_urls.add(webhook["url"])
            if current.lower() in webhook["name"].lower():
                choices.append(app_commands.Choice(name=webhook["name"], value=webhook["name"]))

        if interaction.guild is not None and isinstance(interaction.user, discord.Member):
            for guild_webhook in await self.get_guild_webhooks(guild=interaction.guild, member=interaction.user):
                if guild_webhook.url in seen_urls:
                    continue
                label: str = f"{guild_webhook.name} (#{guild_webhook.channel})" if guild_webhook.channel else str(guild_webhook.name)
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=str(guild_webhook.id)))
        return choices[:25]

    @tasks.loop(minutes=5, reconnect=True)
    async def check_loop(self) -> None:
        """Crawl subreddits for new images and refresh autocomplete data."""
        # Helps keep our Autocomplete up to date. Beats calling it every time the slash command runs.
        if self.recent_edit is True:
            self.subreddits = await self._get_all_subreddits()
            self.webhooks = await self._get_all_webhooks()
            self.recent_edit = False

        if len(self.subreddits) == 0:
            LOGGER.warning("<%s.%s> | No Subreddits found...", __class__.__name__, "check_loop")
            return

        try:
            count: int = await self.subreddit_media_handler(last_check=self.last_check)

        except Exception as e:
            count = 0
            LOGGER.exception("<%s.%s> | Exception in the media handler.", __class__.__name__, "check_loop", exc_info=e)

        self.last_check = datetime.now(tz=UTC)
        self.json_save()
        if xy_binfind is not None:
            await self.save_array()

        pruned: int = await self.prune_galleries()
        if pruned:
            LOGGER.debug("<%s.%s> | Pruned %s stale gallery page(s).", __class__.__name__, "check_loop", pruned)

        if count >= 1:
            LOGGER.debug("<%s.%s> | Finished sending %s %s.", __class__.__name__, "check_loop", count, "Images" if count > 1 else "Image")
        else:
            LOGGER.debug("<%s.%s> | No new Images to send.", __class__.__name__, "check_loop")

    @check_loop.before_loop
    async def before_check_loop(self) -> None:
        """Wait for the bot to be ready before starting the crawler."""
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_interaction")
    async def on_gallery_page(self, interaction: discord.Interaction) -> None:
        """Turn a crawler gallery to the page named by the pressed button's `custom_id`.

        The pages are read back from the database rather than a view held in memory, so an old post
        still turns after a restart.

        .. note::
            Anyone who can see the post can turn it, and the turn edits the shared channel message
            for everyone; there is no per-viewer page state. This listener bypasses
            :meth:`KumaLayoutView.interaction_check`, so any gate on who may page belongs here.

        Parameters
        ----------
        interaction: :class:`discord.Interaction`
            The component interaction; ignored unless its `custom_id` matches :attr:`GALLERY_PAGE_REGEX`.

        """
        if interaction.type is not discord.InteractionType.component or interaction.message is None:
            return

        custom_id: str = str((interaction.data or {}).get("custom_id", ""))
        match: Optional[re.Match[str]] = GALLERY_PAGE_REGEX.fullmatch(custom_id)
        if match is None:
            return

        rows: list[GalleryPageRow] = await self.get_gallery_pages(message_id=interaction.message.id)
        if not rows:
            await interaction.response.send_message(
                content=f"That gallery is too old for me to page through anymore. {self.emoji_table.kuma_tear}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            return

        containers: list[RedditPostContainer] = [RedditPostContainer.from_row(row) for row in rows]
        view: RedditGalleryView = await RedditGalleryView(cog=self, owner=self.bot.user, timeout=None).add_containers(
            containers,
            position=int(match.group("POSITION")),
        )
        await interaction.response.edit_message(view=view)

    async def _get_subreddit(self, name: str) -> Optional[Row]:
        """Get a Row from the Subreddit Table.

        Parameters
        ----------
        name: :class:`str`
            Name of the Subreddit.

        Returns
        -------
        :class:`Optional[Row]`
            Row['id', 'name', 'webhook_id'], otherwise `None`.

        """
        async with self.bot.pool.acquire() as conn:
            return await conn.fetchone("""SELECT id, name, webhook_id FROM subreddit WHERE name = ?""", name)

    async def _add_subreddit(self, name: str) -> Optional[Row]:
        """Add a Row to the Subreddit Table.

        Parameters
        ----------
        name: :class:`str`
            Name of the Subreddit.

        Returns
        -------
        :class:`Optional[Row]`
            Row['id', 'name', 'webhook_id'], otherwise `None` if the Subreddit already exists.

        """
        res: Optional[Row] = await self._get_subreddit(name=name)
        if res is not None:
            return None
        async with self.bot.pool.acquire() as conn:
            return await conn.fetchone("""INSERT INTO subreddit(name) VALUES(?) ON CONFLICT(name) DO NOTHING RETURNING *""", name)

    async def _del_subreddit(self, name: str) -> Optional[int]:
        """Delete a Row from the Subreddit Table.

        Parameters
        ----------
        name: :class:`str`
            Name of the Subreddit.

        Returns
        -------
        :class:`Optional[int]`
            Row count, otherwise `None` if the Subreddit does not exist.

        """
        res: Optional[Row] = await self._get_subreddit(name=name)
        if res is None:
            return None
        async with self.bot.pool.acquire() as conn:
            cur = await conn.execute("""DELETE FROM subreddit WHERE name = ?""", name)
            return cur.get_cursor().rowcount

    async def _get_all_subreddit_names(self) -> list[str]:
        """Gets all Subreddit names from the Subreddit Table.

        Returns
        -------
        :class:`list[str]`
            A list of Subreddit names; empty if no entries exist.

        """
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall("""SELECT name FROM subreddit""")
            return [entry["name"] for entry in res]

    async def _get_all_subreddits(self) -> list[SubRedditTable]:
        """Gets all Row entries of the Subreddit Table with their mapped webhook info.

        Returns
        -------
        :class:`list[SubRedditTable]`
            An empty list if no entries are in the Subreddit table.

        """
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall(
                """SELECT s.name AS subreddit, COALESCE(w.url, '') AS webhook_url, COALESCE(w.name, '') AS webhook_name
                FROM subreddit s LEFT JOIN webhook w ON s.webhook_id = w.id""",
            )
        return [SubRedditTable(subreddit=row["subreddit"], webhook_url=row["webhook_url"], webhook_name=row["webhook_name"]) for row in res]

    async def _update_subreddit(self, name: str, webhook: Union[int, str]) -> Optional[Row]:
        """Update a Subreddit Row `webhook_id` value.

        Parameters
        ----------
        name: :class:`str`
            Name of the Subreddit.
        webhook: :class:`Union[int, str]`
            Webhook name, url, ID or the string "none" to unset.

        Returns
        -------
        :class:`Optional[Row]`
            Row['id', 'name', 'webhook_id'], otherwise `None`.

        """
        res: Optional[Row] = await self._get_subreddit(name=name)
        if res is None:
            return None

        webhook_id: Optional[int] = None
        if not (isinstance(webhook, str) and webhook.lower() == "none"):
            wh_res: Optional[Row] = await self._get_webhook(arg=webhook)
            if wh_res is None:
                return None
            webhook_id = wh_res["id"]

        async with self.bot.pool.acquire() as conn:
            return await conn.fetchone("""UPDATE subreddit SET webhook_id = ? WHERE name = ? RETURNING *""", webhook_id, name)

    async def _get_webhook(self, arg: Union[str, int, None]) -> Optional[Row]:
        """Lookup a Row in the Webhook Table.

        Parameters
        ----------
        arg: :class:`Union[str, int, None]`
            Supports webhook name, id or url queries.

        Returns
        -------
        :class:`Optional[Row]`
            Row['name', 'id', 'url'], otherwise `None`.

        """
        if arg is None:
            return None
        async with self.bot.pool.acquire() as conn:
            if isinstance(arg, int):
                return await conn.fetchone("""SELECT name, id, url FROM webhook WHERE id = ?""", arg)
            if arg.startswith("http"):
                return await conn.fetchone("""SELECT name, id, url FROM webhook WHERE url = ?""", arg)
            return await conn.fetchone("""SELECT name, id, url FROM webhook WHERE name = ?""", arg)

    async def _add_webhook(self, name: str, url: str) -> Optional[Row]:
        """Add a Row to the Webhook Table.

        Parameters
        ----------
        name: :class:`str`
            A string to represent the Webhook URL in the table.
        url: :class:`str`
            Discord webhook URL.

        Returns
        -------
        :class:`Optional[Row]`
            Row['id', 'name', 'url'], otherwise `None` if the Webhook already exists.

        """
        res: Optional[Row] = await self._get_webhook(arg=url)
        if res is not None:
            return None
        async with self.bot.pool.acquire() as conn:
            return await conn.fetchone("""INSERT INTO webhook(name, url) VALUES(?, ?) ON CONFLICT(url) DO NOTHING RETURNING *""", name, url)

    async def _del_webhook(self, arg: Union[int, str]) -> Optional[int]:
        """Delete a Row matching the arg from the Webhook Table.

        Converts string numbers into `ints` if they are digits and
        unsets the `webhook_id` of any Subreddit Rows using it.

        Parameters
        ----------
        arg: :class:`Union[int, str]`
            Supports Webhook ID, Name or URL.

        Returns
        -------
        :class:`Optional[int]`
            Row count, otherwise `None` if the Webhook does not exist.

        """
        if isinstance(arg, str) and arg.isdigit():
            arg = int(arg)
        res: Optional[Row] = await self._get_webhook(arg=arg)
        if res is None:
            return None
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""UPDATE subreddit SET webhook_id = ? WHERE webhook_id = ?""", None, res["id"])
            cur = await conn.execute("""DELETE FROM webhook WHERE id = ?""", res["id"])
            return cur.get_cursor().rowcount

    async def _get_all_webhooks(self) -> list[Webhook]:
        """Gets all Webhook Table Rows.

        Returns
        -------
        :class:`list[Webhook]`
            Structured as `[{"name": str, "url": str, "id": int}]`; empty if no entries exist.

        """
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall("""SELECT name, id, url FROM webhook""")
            return [Webhook(name=entry["name"], url=entry["url"], id=entry["id"]) for entry in res]

    async def _add_gallery_pages(self, message_id: int, containers: Sequence[RedditPostContainer]) -> None:
        """Store a sent gallery's pages so they can be rebuilt after a restart.

        Parameters
        ----------
        message_id: :class:`int`
            The Discord message the gallery was sent as.
        containers: :class:`Sequence[RedditPostContainer]`
            The gallery pages, in display order.

        """
        rows: list[tuple[object, ...]] = [
            (
                message_id,
                position,
                container.sub,
                container.title,
                container.permalink,
                container.created.timestamp(),
                container.media,
                None if container.img_info is None else container.img_info.width,
                None if container.img_info is None else container.img_info.height,
                container.first_post_url,
                container.previous_post_url,
            )
            for position, container in enumerate(containers)
        ]
        async with self.bot.pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO gallery_page
                (message_id, position, subreddit, title, permalink, created, media_url, width, height, first_post_url, previous_post_url)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(message_id, position) DO NOTHING""",
                rows,
            )

    async def get_gallery_pages(self, message_id: int) -> list[GalleryPageRow]:
        """Gets a sent gallery's stored pages in display order.

        Parameters
        ----------
        message_id: :class:`int`
            The Discord message the gallery was sent as.

        Returns
        -------
        :class:`list[GalleryPageRow]`
            The pages; empty when the gallery is unknown or has been pruned.

        """
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall(
                """SELECT message_id, position, subreddit, title, permalink, created, media_url, width, height,
                first_post_url, previous_post_url FROM gallery_page WHERE message_id = ? ORDER BY position""",
                message_id,
            )
        return [
            GalleryPageRow(
                message_id=row["message_id"],
                position=row["position"],
                subreddit=row["subreddit"],
                title=row["title"],
                permalink=row["permalink"],
                created=row["created"],
                media_url=row["media_url"],
                width=row["width"],
                height=row["height"],
                first_post_url=row["first_post_url"],
                previous_post_url=row["previous_post_url"],
            )
            for row in res
        ]

    async def prune_galleries(self) -> int:
        """Drop stored pages for galleries older than :attr:`GALLERY_MAX_AGE_DAYS`.

        A snowflake sorts by creation time, so the cutoff is compared against `message_id`
        directly rather than storing a second timestamp column.

        Returns
        -------
        :class:`int`
            The number of rows removed.

        """
        cutoff: datetime = datetime.now(tz=UTC) - timedelta(days=GALLERY_MAX_AGE_DAYS)
        async with self.bot.pool.acquire() as conn:
            cur = await conn.execute("""DELETE FROM gallery_page WHERE message_id < ?""", discord.utils.time_snowflake(cutoff))
            return cur.get_cursor().rowcount

    async def get_guild_webhooks(self, *, guild: discord.Guild, member: discord.Member) -> list[discord.Webhook]:
        """Gets the Guilds usable webhooks, limited to what the member can see and manage.

        Only incoming webhooks with tokens are returned; the member must be able to view the
        webhook's channel and have `manage_webhooks` permission in it. Results are cached per
        guild for :attr:`WEBHOOK_CACHE_TTL` seconds to keep autocomplete responsive.

        Parameters
        ----------
        guild: :class:`discord.Guild`
            The guild to fetch webhooks from.
        member: :class:`discord.Member`
            The member to permission-check each webhook's channel against.

        Returns
        -------
        :class:`list[discord.Webhook]`
            The webhooks the member is allowed to use; empty if the bot lacks `manage_webhooks`.

        """
        cached: Optional[tuple[float, list[discord.Webhook]]] = self._guild_webhook_cache.get(guild.id)
        if cached is not None and (time.time() - cached[0]) < WEBHOOK_CACHE_TTL:
            webhooks: list[discord.Webhook] = cached[1]
        else:
            try:
                webhooks = await guild.webhooks()
            except (discord.Forbidden, discord.HTTPException) as e:
                LOGGER.warning(
                    "<%s.%s> | Failed to fetch webhooks for guild %s. | %s",
                    __class__.__name__,
                    "get_guild_webhooks",
                    guild.id,
                    e,
                )
                return []
            self._guild_webhook_cache[guild.id] = (time.time(), webhooks)

        usable: list[discord.Webhook] = []
        for webhook in webhooks:
            if webhook.type is not discord.WebhookType.incoming or webhook.token is None:
                continue
            channel = webhook.channel or guild.get_channel(webhook.channel_id or 0)
            if channel is None:
                continue
            perms: discord.Permissions = channel.permissions_for(member)
            if perms.view_channel and perms.manage_webhooks:
                usable.append(webhook)
        return usable

    async def process_subreddit_submissions(
        self,
        sub: str,
        order_type: Literal["New", "Hot", "Top"],
        count: Optional[int] = None,
        last_check: Optional[datetime] = None,
    ) -> list[tuple[Submission, str]]:
        """Gets a Subreddits Submissions in the provided listing order and returns any image urls found.

        Parameters
        ----------
        sub: :class:`str`
            The subreddit to check. Do not include the `/r/`. eg `NoStupidQuestions`.
        order_type: :class:`str`
            The subreddit listing order; either `New`, `Hot` or `Top`.
        count: :class:`Optional[int]`, optional
            The number of submissions to look back through, by default `submission_limit` (30).
        last_check: :class:`Optional[datetime]`, optional
            Ignore submissions created before this time, by default `None`.

        Returns
        -------
        :class:`list[tuple[Submission, str]]`
            A list of Submission and image url pairs.

        """
        img_url_to_send: list[tuple[Submission, str]] = []

        if count is None:
            count = self.submission_limit

        try:
            # If by some miracle a subreddit is removed or
            # no longer accessible for any reason this handles the redirect with useful information.

            cur_subreddit: Subreddit = await self._reddit.subreddit(display_name=sub, fetch=True)
            # limit - controls how far back to go (true limit is 100 entries).
            if order_type.lower() == "new":
                res = cur_subreddit.new(limit=count)
            elif order_type.lower() == "hot":
                res = cur_subreddit.hot(limit=count)
            elif order_type.lower() == "top":
                res = cur_subreddit.top(limit=count)
            else:
                return img_url_to_send

            # asyncpraw listings are lazy - the actual HTTP request fires here during iteration
            # (via _next_batch), not when .new()/.hot()/.top() was called above, so a network
            # timeout surfaces inside this loop and it must stay in the try.
            async for submission in res:
                post_time: datetime = submission.created_datetime
                if last_check is not None and post_time < last_check:
                    continue

                LOGGER.debug(
                    "<%s.%s> | Checking subreddit %s submission title: %s | url: %s | post_time: %s | last_check: %s",
                    __class__.__name__,
                    "process_subreddit_submissions",
                    sub,
                    submission.title,
                    submission.url,
                    post_time.astimezone(self.system_tz).ctime(),
                    last_check.astimezone(self.system_tz).ctime() if last_check is not None else "None",
                )
                if hasattr(submission, "url") and submission.url.lower().find("gallery") != -1:
                    LOGGER.debug(
                        "<%s.%s> | %s -> Found a gallery url, getting the image urls.",
                        __class__.__name__,
                        "process_subreddit_submissions",
                        submission.title,
                    )
                    img_url_to_send.extend([(submission, entry) for entry in await self.convert_gallery_submissions(submission=submission)])

                # Usually submissions with multiple images will be using this `attr`.
                elif hasattr(submission, "media_metadata"):
                    LOGGER.debug(
                        "<%s.%s> | %s -> Found a media_metadata attribute.",
                        __class__.__name__,
                        "process_subreddit_submissions",
                        submission.title,
                    )
                    img_url_to_send.extend([(submission, entry) for entry in await self.get_media_metadata_urls(submission=submission)])

                elif hasattr(submission, "url_overridden_by_dest"):
                    LOGGER.debug(
                        "<%s.%s> | %s -> Has attribute url_overridden_by_dest, getting the url.",
                        __class__.__name__,
                        "process_subreddit_submissions",
                        submission.title,
                    )
                    if submission.url_overridden_by_dest.startswith(self.url_prefixes):
                        img_url_to_send.append((submission, submission.url_overridden_by_dest))

        except asyncprawcore.exceptions.Redirect:
            LOGGER.warning(
                "<%s.%s> | Failed to fetch /r/%s | Type: %s ",
                __class__.__name__,
                "process_subreddit_submissions",
                sub,
                count,
            )
            return img_url_to_send

        except asyncprawcore.exceptions.ServerError as e:
            # Reddit-side 5xx (500/502/503) raised
            LOGGER.warning(
                "<%s.%s> | Reddit returned a <ServerError> while paging /r/%s, returning %s partial result(s). | Error: %s",
                __class__.__name__,
                "process_subreddit_submissions",
                sub,
                len(img_url_to_send),
                e,
            )
            return img_url_to_send

        except asyncprawcore.exceptions.RequestException as e:
            # Transient DNS/network/timeout
            LOGGER.warning(
                "<%s.%s> | Connection failed while paging /r/%s, returning %s partial result(s). | Error: %s",
                __class__.__name__,
                "process_subreddit_submissions",
                sub,
                len(img_url_to_send),
                e,
            )
            return img_url_to_send

        return img_url_to_send

    async def subreddit_media_handler(self, last_check: datetime) -> int:
        """Iterates through the subreddits Submissions and sends any new images as embeds through their mapped webhooks.

        Parameters
        ----------
        last_check: :class:`datetime`
            Ignore submissions created before this time.

        Returns
        -------
        :class:`int`
            The number of images sent.

        """
        count: int = 0
        dup_count: int = 0

        LOGGER.debug(
            "<%s.%s> | Starting... | Delay %s | # of subreddits %s",
            __class__.__name__,
            "subreddit_media_handler",
            self.delay_loop,
            len(self.subreddits),
        )
        # We check self.subreddits in `check_loop`.
        for entry in self.subreddits:
            sub: str = entry["subreddit"]
            webhook_url: str = entry["webhook_url"]
            LOGGER.debug("<%s.%s> | Looking @ %s with %s", __class__.__name__, "subreddit_media_handler", sub, webhook_url)
            if not webhook_url:
                LOGGER.debug("<%s.%s> | No Webhook URL for %s, skipping...", __class__.__name__, "subreddit_media_handler", sub)
                continue

            if self.interrupt_loop is True:
                self.interrupt_loop = False
                LOGGER.warning(
                    "<%s.%s> | The Media Handler loop was interrupted. | Subreddit: %s | Images sent: %s",
                    __class__.__name__,
                    "subreddit_media_handler",
                    sub,
                    count,
                )
                return count

            res: int = await self.check_subreddit(subreddit=sub)
            if res == 503:
                LOGGER.warning(
                    "<%s.%s> | Reddit API unreachable, aborting this run.",
                    __class__.__name__,
                    "subreddit_media_handler",
                )
                return count
            if res != 200:
                LOGGER.warning(
                    "<%s.%s> | Failed to find the subreddit /r/%s, skipping entry. | Status code: %s",
                    __class__.__name__,
                    "subreddit_media_handler",
                    sub,
                    res,
                )
                continue

            submissions: list[tuple[Submission, str]] = await self.process_subreddit_submissions(
                sub=sub,
                last_check=last_check,
                order_type="New",
            )
            LOGGER.debug(
                "<%s.%s> | # of possible image submissions %s",
                __class__.__name__,
                "subreddit_media_handler",
                len(submissions),
            )

            # Metrics.
            sub_images: int = 0
            sub_dups: int = 0
            sub_sent: int = 0

            # Each entry is (submission, remote_url, image_bytes, image_info).
            survivors: list[tuple[Submission, str, bytes, ImageInfo]] = []

            for submission, img_url in submissions:
                if img_url in self.url_list:
                    dup_count += 1
                    sub_dups += 1
                    continue

                if img_url.lower().find("gifs") != -1:
                    LOGGER.debug("<%s.%s> | Found a gif URL -> %s", __class__.__name__, "subreddit_media_handler", img_url)
                    continue

                img_res: Union[ClientResponse, Literal[False]] = await self.get_url_req(img_url=img_url)
                if img_res is False:
                    continue
                img_data: bytes = await img_res.read()
                sub_images += 1

                LOGGER.debug("<%s.%s> | Checking the hash of %s", __class__.__name__, "subreddit_media_handler", img_url)
                hash_res: bool = await self.hash_process(data=img_data)
                phash_res: bool = False
                # ImageInfo defaults edge_res=True; only the edge comparison flips it to False.
                img_info: ImageInfo = ImageInfo()

                # Short-circuit the expensive checks once a cheaper stage confirms a duplicate.
                if not hash_res:
                    phash_res = await self.phash_process(img_data=img_data)
                    if not phash_res:
                        img_info = await self.partial_edge_comparison(img_url=img_url, img_data=img_data)

                if hash_res or phash_res or img_info.edge_res:
                    dup_count += 1
                    sub_dups += 1
                    LOGGER.debug(
                        "<%s.%s> | Duplicate check failed | hash - %s | phash - %s | edge - %s",
                        __class__.__name__,
                        "subreddit_media_handler",
                        hash_res,
                        phash_res,
                        img_info.edge_res,
                    )
                    continue

                self.url_list.add(img_url)
                count += 1
                sub_sent += 1
                survivors.append((submission, img_url, img_data, img_info))

            # Turn the list into a mapping.
            # - dict{reddit submission permalink : (Submission, img_url, img_data, img_info)}
            groups: dict[str, list[tuple[Submission, str, bytes, ImageInfo]]] = {}
            for item in survivors:
                groups.setdefault(item[0].permalink, []).append(item)

            for images in groups.values():
                submission = images[0][0]
                first_post: Optional[tuple[date, str]] = self.daily_first_post.get(sub)

                if len(images) == 1:
                    # Single image - send as an attachment.
                    _, img_url, img_data, img_info = images[0]
                    post_container: RedditPostContainer = RedditPostContainer.from_submission(
                        sub=sub,
                        submission=submission,
                        media=img_url,
                        img_info=img_info,
                        first_post_url=(None if first_post is None else first_post[1]),
                        previous_post_url=self.last_message_jump.get(sub),
                    )
                    container_view: KumaLayoutView = await KumaLayoutView(cog=self, owner=self.bot.user, timeout=None).add_containers(
                        post_container
                    )
                    msg: Optional[discord.WebhookMessage] = await self.webhook_send(url=webhook_url, view=container_view)
                else:
                    # Gallery - remote URLs so page turns can rebuild the view.
                    gallery_posts: list[RedditPostContainer] = [
                        RedditPostContainer.from_submission(
                            sub=sub,
                            submission=submission,
                            media=url,
                            img_info=info,
                            first_post_url=(None if first_post is None else first_post[1]),
                            previous_post_url=self.last_message_jump.get(sub),
                        )
                        for _, url, _, info in images
                    ]
                    container_view = await RedditGalleryView(cog=self, owner=self.bot.user, timeout=None).add_containers(gallery_posts)
                    msg = await self.webhook_send(url=webhook_url, view=container_view)
                    if msg is not None:
                        await self._add_gallery_pages(message_id=msg.id, containers=gallery_posts)

                if msg is not None:
                    today: date = datetime.now(tz=UTC).date()
                    self.last_message_jump[sub] = msg.jump_url
                    if first_post is None or first_post[0] != today:
                        self.daily_first_post[sub] = (today, msg.jump_url)

                # Soft buffer delay between sends to prevent rate limiting.
                await asyncio.sleep(delay=self.delay_loop)

            # Store per-subreddit metrics for this run.
            async with self.bot.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO crawler_metrics (subreddit, run_at, posts_seen, images_found, duplicates_skipped, webhooks_sent)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    sub,
                    time.time(),
                    len(submissions),
                    sub_images,
                    sub_dups,
                    sub_sent,
                )

        LOGGER.debug(
            "<%s.%s> | Ending... | Images sent: %s | Duplicates skipped: %s",
            __class__.__name__,
            "subreddit_media_handler",
            count,
            dup_count,
        )
        return count

    async def partial_edge_comparison(self, img_url: str, img_data: bytes) -> ImageInfo:
        """Converts the image via PIL and gets the edges as a binary string.

        We take a partial of the edges binary string and see if the blobs exist in `pixel_cords_array`.
        If a partial match is found; we run `full_edge_comparison`. If no full match is found; we add the edges to `pixel_cords_array`.

        .. note::
            If `xy_binfind` is not installed the edge comparison is skipped and only the image dimensions are resolved.

        Parameters
        ----------
        img_url: :class:`str`
            Web url to the Image, used for logging.
        img_data: :class:`bytes`
            Image data to check.

        Returns
        -------
        :class:`ImageInfo`
            An `ImageInfo` instance to access the image properties and edge results.

        """
        img_info: ImageInfo = ImageInfo()
        stime: float = time.time()
        source: Image.Image = Image.open(fp=io.BytesIO(initial_bytes=img_data))
        img_info.width = source.width
        img_info.height = source.height

        if xy_binfind is None:
            img_info.edge_res = False
            return img_info

        source = self.image_comp._convert(image=source)  # noqa: SLF001
        res_image: tuple[Image.Image, Optional[Image.Image]] = self.image_comp._image_resize(source=source)  # noqa: SLF001
        source = self.image_comp._filter(image=res_image[0])  # noqa: SLF001
        edges: Optional[list[tuple[int, int]]] = self.image_comp._edge_detect(image=source)  # noqa: SLF001
        if edges is None or len(edges) == 0:
            LOGGER.warning("<%s.%s> | Found no edges for url -> %s", __class__.__name__, "partial_edge_comparison", img_url)
            img_info.edge_res = False
            return img_info

        b_edges: bytes = xy_binfind.struct_pack(edges=edges)
        # Thresholds are reasoned in coordinate units, NOT bytes. The packed blob prefixes a
        # 4-byte uint32 count of uint16 values; 2 values (x, y) make one coordinate pair.
        # eg. 500 coords * 10% sample = 50 to check; 90% of 50 = 45 must match; failcount = 5
        num_coords: int = struct.unpack("<I", b_edges[:4])[0] // 2
        match_req: int = int(num_coords / self.image_comp.sample_percent)
        min_match_req: int = int((match_req * self.image_comp.match_percent) / 100)
        for array in self.pixel_cords_array:
            sample: int = xy_binfind.find(haystack=array, needles=b_edges, skip=40, failcount=(match_req - min_match_req))
            if sample == -1:
                # No partial match in this array; check the next one.
                continue
            LOGGER.debug(
                "<%s.%s> | Running full edge comparison on %s | %s",
                __class__.__name__,
                "partial_edge_comparison",
                img_url,
                len(b_edges),
            )
            if await self.full_edge_comparison(array=array, edges=b_edges) is True:
                LOGGER.debug(
                    "<%s.%s> | Edge comparison found a duplicate in %.2f seconds. | URL: %s ",
                    __class__.__name__,
                    "partial_edge_comparison",
                    time.time() - stime,
                    img_url,
                )
                return img_info

        self.pixel_cords_array.append(b_edges)
        img_info.edge_res = False
        return img_info

    async def full_edge_comparison(self, array: bytes, edges: bytes) -> bool:
        """Similar to `partial_edge_comparison` but we use the full list of the image edges against our array.

        Parameters
        ----------
        array: :class:`bytes`
            A binary representation of pixel cords.
        edges: :class:`bytes`
            A binary representation of pixel cords.

        Returns
        -------
        :class:`bool`
            `True` if the binary blob is in the array, otherwise `False`.

        """
        if xy_binfind is None:
            return False
        # Allow (100 - match_percent)% of coordinate pairs to fail before aborting.
        # Threshold is in coordinate units via the 4-byte uint32 prefix (2 uint16 values per pair).
        num_coords: int = struct.unpack("<I", edges[:4])[0] // 2
        allowed_fail: int = int(num_coords * (100 - self.image_comp.match_percent) / 100)
        return xy_binfind.find(haystack=array, needles=edges, failcount=allowed_fail) != -1

    async def get_media_metadata_urls(self, submission: Submission) -> list[str]:
        """Checks the Submission object for the "media_metadata" attribute.

        Validates it is a dictionary and iterates through the url keys returning a list of urls to send.

        Parameters
        ----------
        submission: :class:`Submission`
            The subreddit Submission.

        Returns
        -------
        :class:`list[str]`
            Image urls.

        """
        _urls: list[str] = []
        res = getattr(submission, "media_metadata", None)
        if isinstance(res, dict):
            for img in res.values():
                # example {'status': 'valid', 'e': 'Image', 'm': 'image/jpg', 'p': [lists of random resolution images], 's': See below..}
                # This allows us to only get Images.
                if "e" in img and img["e"] == "Image":
                    # example 's': {'y': 2340, 'x': 1080, 'u': 'https://preview.redd.it/...'}, 'id': '0u8xnxknijha1'}
                    # Reddit hands these preview urls back HTML-escaped (`&amp;` in the query string);
                    # unescape or the dropped query params make the fetch 404.
                    _urls.append(html.unescape(img["s"]["u"]))  # noqa: PERF401
        return _urls

    async def convert_gallery_submissions(self, submission: Submission) -> list[str]:
        """Takes a gallery url Submission object and checks its `__dict__` attribute.

        If it contains the "media_metadata" key we need to get all the urls,
        just nested in another dictionary under "crosspost_parent_list".
        We use a setattr to add `__dict__["crosspost_parent_list"][0]["media_metadata"]`
        as an attribute to the Submission as `Submission.media_metadata`.

        Parameters
        ----------
        submission: :class:`Submission`
            The subreddit Submission.

        Returns
        -------
        :class:`list[str]`
            A list of image urls found inside the `Gallery`.

        """
        _urls: list[str] = []
        if hasattr(submission, "url") and submission.url.lower().find("gallery") != -1:
            res: dict = submission.__dict__
            if "crosspost_parent_list" in res:
                parents: list = res["crosspost_parent_list"]
                # The parent list can be empty and the first parent need not carry `media_metadata`
                # (the docstring notes the key is not guaranteed); guard both before spoofing it.
                if len(parents) > 0 and "media_metadata" in parents[0]:
                    data: dict = parents[0]["media_metadata"]
                    # Spoof the attribute from the __dict__ key so get_media_metadata_urls() can read it.
                    setattr(submission, "media_metadata", data)  # noqa: B010
                    _urls = await self.get_media_metadata_urls(submission=submission)

            # Edge case; some gallery's apparently have a proper "media_metadata" url.
            elif hasattr(submission, "media_metadata"):
                _urls = await self.get_media_metadata_urls(submission=submission)
        return _urls

    async def get_url_req(self, img_url: str, ignore_validation: bool = False) -> Union[ClientResponse, Literal[False]]:
        """Calls a `.get()` method to get the image data.

        Parameters
        ----------
        img_url: :class:`str`
            Web URL for the image.
        ignore_validation: :class:`bool`, optional
            Ignore header check on web request for `image`, by default `False`.

        Returns
        -------
        :class:`Union[ClientResponse, Literal[False]]`
            Returns a `ClientResponse` if the url returns a status code between `200-299`. Otherwise `False`.

        """
        # Bypass the response cache; image blobs bloat the SQLite backend.
        async with self.bot.session.disabled():
            req: ClientResponse = await self.bot.session.get(url=img_url)

        if not 200 <= req.status < 300:
            LOGGER.error("<%s.%s> | Unable to handle %s || status code: %s", __class__.__name__, "get_url_req", img_url, req.status)
            return False

        # Ignore all further validation as we just want the web request.
        if ignore_validation is True:
            return req

        if "Content-Type" not in req.headers:
            LOGGER.error("<%s.%s> | Unable to find the Content-Type for %s", __class__.__name__, "get_url_req", img_url)
            return False

        if "image" in req.headers["Content-Type"]:
            return req

        LOGGER.warning("<%s.%s> | URL: %s is not an image.", __class__.__name__, "get_url_req", img_url)
        LOGGER.debug("<%s.%s> | URL: %s | Req Headers: %s", __class__.__name__, "get_url_req", img_url, req.headers)
        return False

    async def save_array(self) -> None:
        """Saves the list of bytes to `reddit_array.bin`.

        We truncate the list depending on the length of subreddits and submission limits.
        """
        limiter: int = (len(self.subreddits) * self.submission_limit) * 3
        while len(self.pixel_cords_array) > limiter:
            self.pixel_cords_array.pop(0)

        data: bytes = b"".join(self.pixel_cords_array)
        LOGGER.debug("<%s.%s> | Writing our Pixel Array to `reddit_array.bin` | bytes %s", __class__.__name__, "save_array", len(data))
        await asyncio.to_thread(self.array_bin.write_bytes, data)

    async def read_array(self) -> None:
        """Reads from `reddit_array.bin`.

        Makes the file if it doesn't exist in `file_dir`.
        """
        if self.array_bin.is_file() is False:
            self.array_bin.touch()

        data: bytes = await asyncio.to_thread(self.array_bin.read_bytes)
        LOGGER.debug("<%s.%s> | Loading array from file | bytes %s", __class__.__name__, "read_array", len(data))
        # We unpack our array len; each entry is prefixed with a 4 byte count,
        # we *2 to account for 2 bytes per value stored, 2 values go into a single tuple().
        self.pixel_cords_array = []
        total_pos: int = 0
        while total_pos < len(data):
            cord_len: int = struct.unpack("<I", data[total_pos : total_pos + 4])[0] * 2
            # We increment total_pos +4 to pass our array len blob.
            self.pixel_cords_array.append(data[total_pos : total_pos + cord_len + 4])
            total_pos += cord_len + 4
        LOGGER.debug(
            "<%s.%s> | Reading our Array File... | total entries %s", __class__.__name__, "read_array", len(self.pixel_cords_array)
        )

    async def hash_process(self, data: bytes) -> bool:
        """Checks the sha256 of the supplied image data against our `hash_list`.

        Parameters
        ----------
        data: :class:`bytes`
            The image data.

        Returns
        -------
        :class:`bool`
            `False` if the sha256 results DO NOT exist in `hash_list`, otherwise `True`.

        """
        my_hash: str = hashlib.sha256(string=data).hexdigest()
        if my_hash not in self.hash_list:
            self.hash_list.add(my_hash)
            return False
        return True

    async def phash_process(self, img_data: bytes) -> bool:
        """Checks the perceptual hash of the supplied image against our `phash_list`.

        Unlike `hash_process` (SHA-256, byte-exact), a perceptual hash is robust to JPEG
        re-encoding and minor quality differences the Reddit CDN serves for the same image.

        .. note::
            If `imagehash` is not installed the perceptual check is skipped and returns `False`.

        Parameters
        ----------
        img_data: :class:`bytes`
            The image data.

        Returns
        -------
        :class:`bool`
            `False` if no perceptually-similar hash exists in `phash_list`, otherwise `True`.

        """
        if _imagehash is None:
            return False
        img: Image.Image = Image.open(fp=io.BytesIO(initial_bytes=img_data))
        my_phash: str = str(_imagehash.phash(img))
        if my_phash not in self.phash_list:
            self.phash_list.add(my_phash)
            return False
        return True

    async def webhook_send(
        self,
        url: str,
        content: Optional[str] = None,
        view: Optional[KumaLayoutView] = None,
        # img_url: Optional[str] = None,
    ) -> Optional[discord.WebhookMessage]:
        """Sends the content or view to the Discord Webhook url provided.

        Parameters
        ----------
        url: :class:`str`
            The Webhook URL to use.
        content: :class:`Optional[str]`, optional
            The message content to send to the url, by default `None`.
        view: :class:`Optional[KumaLayoutView]`, optional
            The Components V2 view to send to the url, by default `None`.

        Returns
        -------
        :class:`Optional[discord.WebhookMessage]`
            The sent message if the webhook was sent successfully, otherwise `None`.

        """
        webhook: discord.Webhook = discord.Webhook.from_url(url=url, client=self.bot)
        try:
            if view is not None:
                # return await webhook.send(view=view, files=view.files, username=self.user_name, wait=True)
                return await webhook.send(view=view, username=self.user_name, wait=True)
            if content is not None:
                return await webhook.send(content=content, username=self.user_name, wait=True)
        except discord.NotFound:
            LOGGER.warning(
                "<%s.%s> | Webhook was deleted on Discord; removing it and unlinking its subreddits. | %s",
                __class__.__name__,
                "webhook_send",
                url,
            )
            await self._del_webhook(arg=url)
            self.recent_edit = True
        except discord.HTTPException as e:
            # 413 Payload Too Large - retry with the remote URL instead of the file attachment.
            # if e.status == 413 and view is not None and img_url is not None:
            #     LOGGER.warning(
            #         "<%s.%s> | 413 Payload Too Large; retrying with remote URL. | %s",
            #         __class__.__name__,
            #         "webhook_send",
            #         img_url,
            #     )
            #     view.use_remote_media(img_url)
            #     try:
            #         return await webhook.send(view=view, username=self.user_name, wait=True)
            #     except (discord.HTTPException, ValueError) as retry_e:
            #         LOGGER.warning("<%s.%s> | Retry also failed. | %s", __class__.__name__, "webhook_send", retry_e)
            # else:
            LOGGER.warning("<%s.%s> | Webhook not sent. | %s", __class__.__name__, "webhook_send", e)
        except ValueError as e:
            LOGGER.warning("<%s.%s> | Webhook not sent. | %s", __class__.__name__, "webhook_send", e)
        return None

    @staticmethod
    def normalize_subreddit(subreddit: str) -> str:
        """Reduces whatever was typed to a bare subreddit name.

        `/add_subreddit` advertises that it takes a full `https://www.reddit.com/r/name` URL, but the
        string went to the API untouched and came back 404 every time. Accepts a URL, `/r/name`,
        `r/name` or the bare name.

        Parameters
        ----------
        subreddit: :class:`str`
            What the user typed.

        Returns
        -------
        :class:`str`
            The subreddit name, with no scheme, host, `r/` prefix or trailing path.

        """
        cleaned: str = subreddit.strip().rstrip("/")
        # Everything after the last `r/` is the name plus whatever path followed it.
        _, marker, tail = cleaned.rpartition("r/")
        if marker:
            cleaned = tail
        return cleaned.split("/")[0].split("?")[0]

    async def check_subreddit(self, subreddit: str) -> int:
        """Attempts a `HEAD` request of the passed in subreddit.

        Parameters
        ----------
        subreddit: :class:`str`
            The subreddit to check. Do not include the `/r/`. eg `NoStupidQuestions`.

        Returns
        -------
        :class:`int`
            The HTTP status code.
            - 200 = "Found Subreddit"
            - 404 = "Not Found"
            - 400 = "Any Exception"
            - 503 = "Connection/DNS failure"

        """
        results: list[Subreddit] = []
        try:
            async for entry in self._reddit.subreddits.search_by_name(subreddit, exact=True):
                results.append(entry)  # noqa: PERF401 - You cannot use extend on a Generator
            if results:
                return 200

        except asyncprawcore.exceptions.NotFound:
            LOGGER.exception("<%s.%s> | Failed to find Subreddit. | Sub: %s", __class__.__name__, "check_subreddit", subreddit)
            return 404

        except asyncprawcore.exceptions.ServerError as e:
            # Reddit-side 5xx - asyncprawcore already retried. Treat as transient; abort this run
            # (return 503) rather than skipping only this sub, since the API is likely degraded.
            LOGGER.warning(
                "<%s.%s> | Reddit server error checking /r/%s, aborting this cycle. | Error: %s",
                __class__.__name__,
                "check_subreddit",
                subreddit,
                e,
            )
            return 503

        except asyncprawcore.exceptions.RequestException as e:
            LOGGER.warning(
                "<%s.%s> | Connection failed (DNS/network). | Sub: %s | Error: %s",
                __class__.__name__,
                "check_subreddit",
                subreddit,
                e,
            )
            return 503

        except Exception:
            LOGGER.exception("<%s.%s> | Unexpected error checking subreddit. | Sub: %s", __class__.__name__, "check_subreddit", subreddit)
            return 400
        return 400

    async def _compare_urls(self, url_one: str, url_two: str) -> None:
        """Takes two Image URLs and turns them into PIL Images for comparison.

        See `self.image_comp`.

        Parameters
        ----------
        url_one: :class:`str`
            Image url.
        url_two: :class:`str`
            Image url.

        """
        res_one: Union[ClientResponse, Literal[False]] = await self.get_url_req(img_url=url_one)
        res_two: Union[ClientResponse, Literal[False]] = await self.get_url_req(img_url=url_two)
        if res_one is not False and res_two is not False:
            img_one: Image.Image = Image.open(fp=io.BytesIO(await res_one.read()))
            img_two: Image.Image = Image.open(fp=io.BytesIO(await res_two.read()))
            self.image_comp.compare(source=img_one, comparison=img_two)

    @staticmethod
    def _paginate_lines(entries: list[str], limit: int = PAGE_LIMIT) -> list[str]:
        """Chunk pre-formatted lines into pages that fit within the CV2 character budget.

        Parameters
        ----------
        entries: :class:`list[str]`
            The pre-formatted lines to paginate.
        limit: :class:`int`, optional
            Maximum characters per page, by default :attr:`PAGE_LIMIT`.

        Returns
        -------
        :class:`list[str]`
            The joined page strings; at least one page is always returned.

        """
        pages: list[str] = []
        current: list[str] = []
        length: int = 0
        for entry in entries:
            cost: int = len(entry) + (1 if current else 0)
            if length + cost > limit and current:
                pages.append("\n".join(current))
                current = [entry]
                length = len(entry)
            else:
                current.append(entry)
                length += cost
        if current:
            pages.append("\n".join(current))
        return pages or [""]

    @commands.hybrid_command(help="Retrieves a subreddits X number of Submissions")
    @app_commands.describe(sub="The subreddit name.")
    @app_commands.describe(count="The number of submissions to retrieve, default is 5.")
    @app_commands.describe(order_type="Either `New, Hot or Top`")
    @app_commands.describe(ephemeral="Hide the response so only you can see it (default True).")
    @app_commands.describe(access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    # @app_commands.autocomplete(order_type=autocomplete_submission_type)
    async def get_subreddit(
        self,
        context: Context,
        sub: str,
        order_type: Literal["New", "Hot", "Top"] = "New",
        count: app_commands.Range[int, 0, 100] = 5,
        timeout: Optional[float] = 180.0,  # noqa: ASYNC109
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> discord.Message:

        status: int = await self.check_subreddit(subreddit=sub)
        if status != 200:
            return await context.send(
                content=f"Unable to find the subreddit `/r/{sub}`.\n *Status code: {status}* {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )

        res: list[tuple[Submission, str]] = await self.process_subreddit_submissions(sub=sub, order_type=order_type, count=count)
        if not res:
            return await context.send(
                content=f"No submissions found for `/r/{sub}`. {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )

        posts: list[RedditPostContainer] = [
            RedditPostContainer.from_submission(sub=sub, submission=submission, media=url) for submission, url in res
        ]
        owner: Optional[discord.Member | discord.User | discord.ClientUser] = access.owner(user=context.author, bot=self.bot)
        view: KumaLayoutView = await KumaLayoutView(cog=self, owner=owner, timeout=timeout).add_containers(posts)
        return await context.send(view=view, ephemeral=ephemeral)

    @commands.hybrid_command(help="Add a subreddit to the DB", aliases=["rsadd", "rsa"])
    @app_commands.describe(
        sub="The subreddit name or full url https://www.reddit.com/r/subreddit_here",
        webhook="A database webhook name or a server webhook to link on creation.",
    )
    @app_commands.autocomplete(webhook=autocomplete_webhook_with_guild)
    @app_commands.default_permissions(manage_guild=True)
    async def add_subreddit(self, context: Context, sub: str, webhook: Optional[str] = None) -> discord.Message:
        """Add a subreddit to the database, optionally linking a webhook on creation."""
        # A pasted URL is advertised as acceptable, so it is reduced to a name before anything else
        # touches it - the whole URL used to be handed to the API and stored in the table verbatim.
        sub = self.normalize_subreddit(sub)
        display_sub: str = f"/r/{sub}"
        status: int = await self.check_subreddit(subreddit=sub)
        if status != 200:
            return await context.send(
                content=f"Unable to find the subreddit `{display_sub}`.\n *Status code: {status}* - {self.emoji_table.kuma_rawr}",
                delete_after=self.message_timeout,
            )

        res: Optional[Row] = await self._add_subreddit(name=sub)
        if res is None:
            return await context.send(
                content=f"Unable to add `{display_sub}` to the database. {self.emoji_table.kuma_head_clench}",
                delete_after=self.message_timeout,
            )

        self.recent_edit = True

        if webhook is None:
            return await context.send(
                content=f"Added `{display_sub}` to our database. {self.emoji_table.kuma_star_eye}",
                delete_after=self.message_timeout,
            )

        # Try linking the webhook - reuse the same resolution logic as update_subreddit.
        update_res: Optional[Row] = await self._update_subreddit(name=sub, webhook=webhook)
        if update_res is not None:
            return await context.send(
                content=f"Added `{display_sub}` and linked it to **{webhook}**. {self.emoji_table.kuma_star_eye}",
                delete_after=self.message_timeout,
            )

        # Not in the DB webhook table; check server webhooks.
        if context.guild is not None and isinstance(context.author, discord.Member):
            guild_webhooks: list[discord.Webhook] = await self.get_guild_webhooks(guild=context.guild, member=context.author)
            match: Optional[discord.Webhook] = next(
                (entry for entry in guild_webhooks if webhook in (str(entry.id), entry.name, entry.url)),
                None,
            )
            if match is not None:
                db_name: str = str(match.name)
                existing: Optional[Row] = await self._get_webhook(arg=db_name)
                if existing is not None and existing["url"] != match.url:
                    db_name = f"{db_name}-{match.id}"
                await self._add_webhook(name=db_name, url=match.url)
                update_res = await self._update_subreddit(name=sub, webhook=match.url)
                if update_res is not None:
                    return await context.send(
                        content=f"Added `{display_sub}` and linked it to **{db_name}** from {match.channel.mention if match.channel else 'this server'}. {self.emoji_table.kuma_star_eye}",  # noqa: E501
                        delete_after=self.message_timeout,
                    )

        return await context.send(
            content=f"Added `{display_sub}` but I couldn't find that webhook to link. {self.emoji_table.kuma_hmm}",
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="Remove a subreddit from the DB", aliases=["rsdel", "rsd"])
    @app_commands.describe(sub="The sub Reddit name.")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    @app_commands.default_permissions(manage_guild=True)
    async def del_subreddit(self, context: Context, sub: str) -> None:
        """Remove a subreddit from the database."""
        row: Optional[Row] = await self._get_subreddit(name=sub)
        if row is None:
            await context.send(
                content=f"I couldn't find `/r/{sub}` in the database. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )
            return

        webhook: Optional[Row] = await self._get_webhook(arg=row["webhook_id"]) if row["webhook_id"] else None

        await self._del_subreddit(name=sub)
        self.recent_edit = True

        webhook_info: str = f"\n> Was linked to webhook: `{webhook['name']}`" if webhook else ""
        await context.send(
            content=f"Removed `/r/{sub}` from the database. {self.emoji_table.kuma_happy}{webhook_info}",
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="Update a subreddit with a Webhook from the database or this server", aliases=["rsupdate", "rsu"])
    @app_commands.describe(sub="The sub Reddit name.")
    @app_commands.describe(webhook="A database webhook name, or a webhook from this server you can manage.")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    @app_commands.autocomplete(webhook=autocomplete_webhook_with_guild)
    @app_commands.default_permissions(manage_guild=True)
    async def update_subreddit(self, context: Context, sub: str, webhook: str) -> discord.Message:
        """Link a subreddit to a webhook from the database or the server."""
        if await self._get_subreddit(name=sub) is None:
            return await context.send(
                content=f"I don't have `/r/{sub}` in my database; add it with `/add_subreddit` first. {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )

        res: Optional[Row] = await self._update_subreddit(name=sub, webhook=webhook)
        if res is not None:
            self.recent_edit = True
            return await context.send(
                content=f"Updated `/r/{sub}` in our database. {self.emoji_table.kuma_happy}",
                delete_after=self.message_timeout,
            )

        # Not in the webhook table; see if it matches a server webhook the author can manage.
        if context.guild is None or not isinstance(context.author, discord.Member):
            return await context.send(
                content=f"I can only look for server webhooks inside a server... {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )

        guild_webhooks: list[discord.Webhook] = await self.get_guild_webhooks(guild=context.guild, member=context.author)
        match: Optional[discord.Webhook] = next(
            (entry for entry in guild_webhooks if webhook in (str(entry.id), entry.name, entry.url)),
            None,
        )
        if match is None:
            return await context.send(
                content=f"Hmm, I couldn't find that webhook in the database or this server. {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )

        # Don't let an NSFW subreddit land in a channel that isn't age-restricted.
        wh_channel = match.channel or context.guild.get_channel(match.channel_id or 0)
        if wh_channel is not None:
            try:
                cur_subreddit: Subreddit = await self._reddit.subreddit(display_name=sub, fetch=True)
                over18: bool = bool(getattr(cur_subreddit, "over18", False))
            except Exception:  # noqa: BLE001
                over18 = False
            if over18 and wh_channel.is_nsfw() is False:
                return await context.send(
                    content=f"`/r/{sub}` is marked **NSFW** but {wh_channel.mention} is not age-restricted; "
                    f"I'm not linking those... {self.emoji_table.kuma_shy}",
                    delete_after=self.message_timeout,
                )

        # The webhook table has a UNIQUE name column; de-collide with the webhook id if needed.
        db_name: str = str(match.name)
        existing: Optional[Row] = await self._get_webhook(arg=db_name)
        if existing is not None and existing["url"] != match.url:
            db_name = f"{db_name}-{match.id}"
        await self._add_webhook(name=db_name, url=match.url)

        res = await self._update_subreddit(name=sub, webhook=match.url)
        if res is None:
            return await context.send(
                content=f"Something went wrong linking `/r/{sub}` to **{db_name}**... {self.emoji_table.kuma_pout}",
                delete_after=self.message_timeout,
            )
        self.recent_edit = True
        return await context.send(
            content=f"Imported webhook **{db_name}** from {match.channel.mention if match.channel else 'this server'} and updated `/r/{sub}`. {self.emoji_table.kuma_star_eye}",  # noqa: E501
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="List of subreddits", aliases=["rslist", "rsl"])
    @app_commands.describe(ephemeral="Hide the response so only you can see it (default True).")
    @app_commands.describe(access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).")
    async def list_subreddit(self, context: Context, ephemeral: bool = True, access: PanelAccess = PanelAccess.only_me) -> discord.Message:
        """List all subreddits and their linked webhooks."""
        res: list[SubRedditTable] = await self._get_all_subreddits()
        entries: list[str] = []
        for entry in res:
            if entry["webhook_name"]:
                entries.append(f"\U00002705 - **/r/**`{entry['subreddit']}` \U00002192 `{entry['webhook_name']}`")
            else:
                entries.append(f"\U0000274c - **/r/**`{entry['subreddit']}`")

        if not entries:
            return await context.send(
                content=f"No subreddits in the database yet. {self.emoji_table.kuma_tear}",
                delete_after=self.message_timeout,
            )
        pages: list[str] = self._paginate_lines(entries)
        title: str = f"__Current Subreddit List__ (total: {len(entries)})"
        containers = [RedditTextPanel(title=title, body=entry) for entry in pages]
        owner: Optional[discord.Member | discord.User | discord.ClientUser] = access.owner(user=context.author, bot=self.bot)
        view: KumaLayoutView = await KumaLayoutView(cog=self, owner=owner, timeout=None).add_containers(containers)
        return await context.send(view=view, ephemeral=ephemeral)

    @commands.hybrid_command(help="Info about a subreddit", aliases=["rsinfo", "rsi"])
    @app_commands.describe(sub="The sub Reddit name.")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    async def info_subreddit(self, context: Context, sub: str) -> discord.Message:
        """Show the webhook info for a subreddit."""
        res: Optional[Row] = await self._get_subreddit(name=sub)
        if res is None:
            return await context.send(
                content=f"I couldn't find `/r/{sub}` in the database. {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
            )

        wh_res: Optional[Row] = await self._get_webhook(arg=res["webhook_id"])
        if wh_res is not None:
            return await context.send(
                content=f"**Info on /r/`{sub}`** {self.emoji_table.kuma_peak}\n> __Webhook Name__: {wh_res['name']}\n> __Webhook ID__: {wh_res['id']}\n> {wh_res['url']}",  # noqa: E501
                delete_after=self.message_timeout,
            )
        return await context.send(
            content=f"**Info on /r/`{sub}`** {self.emoji_table.kuma_tea}\n> `No webhook associated with this subreddit`",
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="Add a webhook to the database via URL, or create one in a channel.", aliases=["rswhadd", "rswha"])
    @app_commands.describe(
        webhook_name="A name to represent the webhook in the database.",
        webhook_url="An existing Discord webhook URL.",
        channel="Create a new webhook in this channel instead of providing a URL.",
    )
    @app_commands.default_permissions(manage_webhooks=True)
    async def add_webhook(
        self,
        context: Context,
        webhook_name: str,
        webhook_url: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> discord.Message:
        """Add a webhook to the database via URL, or create one in a channel."""
        if webhook_url is None and channel is None:
            return await context.send(
                content=f"You need to give me either a webhook URL or a channel to make one in. {self.emoji_table.kuma_bleh}",
                delete_after=self.message_timeout,
            )

        if channel is not None:
            if isinstance(context.author, discord.Member):
                perms: discord.Permissions = channel.permissions_for(context.author)
                if not (perms.view_channel and perms.manage_webhooks):
                    return await context.send(
                        content=f"You don't have webhook permissions in {channel.mention}... {self.emoji_table.kuma_pout}",
                        delete_after=self.message_timeout,
                    )
            try:
                created: discord.Webhook = await channel.create_webhook(
                    name=webhook_name,
                    reason=f"Reddit crawler webhook created by {context.author}.",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                return await context.send(
                    content=f"I couldn't create a webhook in {channel.mention}... {self.emoji_table.kuma_crying}\n```\n{e}\n```",
                    delete_after=self.message_timeout,
                )
            webhook_url = created.url
            self._guild_webhook_cache.pop(channel.guild.id, None)
        else:
            assert webhook_url is not None  # noqa: S101 # Both-None case handled above; narrows the type.
            success: Optional[discord.WebhookMessage] = await self.webhook_send(
                url=webhook_url,
                content=f"Testing webhook {webhook_name}",
            )
            if success is None:
                return await context.send(
                    content=f"Failed to send a test message to `{webhook_name}` via url. {self.emoji_table.kuma_sad}\n> {webhook_url}",
                    delete_after=self.message_timeout,
                )

        res: Optional[Row] = await self._add_webhook(name=webhook_name, url=webhook_url)
        if res is not None:
            self.recent_edit = True
            return await context.send(
                content=f"Added **{webhook_name}** to the database. {self.emoji_table.kuma_star_eye}\n> `{webhook_url}`",
                delete_after=self.message_timeout,
            )
        return await context.send(
            content=f"Unable to add `{webhook_url}` to the database. {self.emoji_table.kuma_head_clench}",
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="Remove a webhook from the database.", aliases=["rswhdel", "rswhd"])
    @app_commands.autocomplete(webhook=autocomplete_webhook)
    @app_commands.default_permissions(manage_webhooks=True)
    async def del_webhook(self, context: Context, webhook: str) -> None:
        """Remove a webhook from the database."""
        res: Optional[int] = await self._del_webhook(arg=webhook)
        self.recent_edit = True
        if res:
            await context.send(
                content=f"Removed **{webhook}** from the database. {self.emoji_table.kuma_chuckle}",
                delete_after=self.message_timeout,
            )
        else:
            await context.send(
                content=f"I couldn't find that webhook in the database. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )

    @commands.hybrid_command(help="List all webhook in the database.", aliases=["rswhlist", "rswhl"])
    @app_commands.default_permissions(manage_webhooks=True)
    async def list_webhook(self, context: Context) -> discord.Message:
        """List all webhooks in the database."""
        if not self.webhooks:
            return await context.send(
                content=f"No webhooks in the database yet. {self.emoji_table.kuma_tear}",
                delete_after=self.message_timeout,
            )
        entries: list[str] = [f"**{entry['name']}** ({entry['id']})\n> `{entry['url']}`" for entry in self.webhooks]
        pages: list[str] = self._paginate_lines(entries)
        title: str = f"__Current Webhook List__ (total: {len(entries)})"
        containers = [RedditTextPanel(title=title, body=entry) for entry in pages]
        # Webhook URLs are sensitive, so this stays owner-gated and ephemeral.
        view: KumaLayoutView = await KumaLayoutView(cog=self, owner=context.author, timeout=None).add_containers(containers)
        return await context.send(view=view, ephemeral=True)

    @commands.hybrid_command(help="Start/Stop the Crawler loop", aliases=["rsloop"])
    @app_commands.default_permissions(administrator=True)
    async def scrape_loop(self, context: Context, util: Literal["start", "stop", "restart"]) -> discord.Message:
        """Start, stop or restart the crawler loop."""
        if util == "start":
            if self.check_loop.is_running() is True:
                status = f"already running. {self.emoji_table.kuma_chuckle}"
            else:
                self.check_loop.start()
                status = f"starting up! {self.emoji_table.kuma_wow}"

        elif util == "stop":
            if self.check_loop.is_running() is True:
                self.interrupt_loop = True
                self.check_loop.cancel()
                status = f"stopping. {self.emoji_table.kuma_tea}"
            else:
                status = f"not currently running. {self.emoji_table.kuma_shrug}"

        else:
            self.check_loop.cancel()
            self.check_loop.start()
            status = f"restarting! {self.emoji_table.kuma_rawr}"

        return await context.send(content=f"The Crawler loop is {status}", delete_after=self.message_timeout)

    @commands.hybrid_command(help="Sha256 comparison of two URLs", aliases=["sha256", "hash"])
    async def hash_comparison(self, context: Context, url_one: str, url_two: Optional[str] = None) -> discord.Message:
        """Compare two image URLs by their SHA-256 hash, or show the hash of one."""
        res: Union[ClientResponse, Literal[False]] = await self.get_url_req(img_url=url_one)
        if res is False:
            return await context.send(
                content=f"Unable to hash the URL provided. {self.emoji_table.kuma_sad}",
                delete_after=self.message_timeout,
            )
        hash_one: str = hashlib.sha256(string=await res.read()).hexdigest()

        if url_two is None:
            return await context.send(content=f"Hash: `{hash_one}` {self.emoji_table.kuma_peak}", delete_after=self.message_timeout)

        res_two: Union[ClientResponse, Literal[False]] = await self.get_url_req(img_url=url_two)
        if res_two is False:
            return await context.send(
                content=f"Unable to hash the second URL provided. {self.emoji_table.kuma_sad}",
                delete_after=self.message_timeout,
            )
        hash_two: str = hashlib.sha256(string=await res_two.read()).hexdigest()

        if hash_one == hash_two:
            return await context.send(content=f"The images match! {self.emoji_table.kuma_heart}", delete_after=self.message_timeout)
        return await context.send(
            content=f"The images do not match. {self.emoji_table.kuma_hmm}\n> `{hash_one}`\n> `{hash_two}`",
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="Edge comparison of two URLs", aliases=["edge"])
    async def edge_comparison(self, context: Context, url_one: str, url_two: str) -> discord.Message:
        """Compare two image URLs by pixel edge detection."""
        await self._compare_urls(url_one=url_one, url_two=url_two)
        return await context.send(
            content=f"**URL One**: {url_one}\n**URL Two**: {url_two}\n**Results:** {self.image_comp.results} {self.emoji_table.kuma_wow}",
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(help="View crawler metrics for subreddits", aliases=["rsstats", "rsmetrics"])
    @app_commands.describe(sub="Filter to a specific subreddit (optional).")
    @app_commands.describe(ephemeral="Hide the response so only you can see it (default True).")
    @app_commands.describe(access="Who can use the buttons: Public (anyone), Preview (no one), or Only Me (default).")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    async def crawler_stats(
        self,
        context: Context,
        sub: Optional[str] = None,
        ephemeral: bool = True,
        access: PanelAccess = PanelAccess.only_me,
    ) -> discord.Message:
        """Show crawler metrics, optionally filtered to one subreddit."""
        async with self.bot.pool.acquire() as conn:
            if sub:
                rows: list[Row] = await conn.fetchall(
                    """SELECT subreddit, run_at, posts_seen, images_found, duplicates_skipped, webhooks_sent
                    FROM crawler_metrics WHERE subreddit = ? ORDER BY run_at DESC LIMIT 15""",
                    sub,
                )
            else:
                rows = await conn.fetchall(
                    """SELECT subreddit, SUM(posts_seen) AS posts_seen, SUM(images_found) AS images_found,
                    SUM(duplicates_skipped) AS duplicates_skipped, SUM(webhooks_sent) AS webhooks_sent,
                    COUNT(*) AS runs, MAX(run_at) AS last_run
                    FROM crawler_metrics GROUP BY subreddit ORDER BY last_run DESC""",
                )

        if not rows:
            return await context.send(
                content=f"No crawler metrics recorded yet. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )

        owner: Optional[discord.Member | discord.User | discord.ClientUser] = access.owner(user=context.author, bot=self.bot)

        if sub:
            # Per-subreddit detail - one page per crawl run.
            pages: list[str] = [
                (
                    f"### /r/{row['subreddit']} · <t:{int(row['run_at'])}:f>\n"
                    f"- **Posts Seen:** {row['posts_seen']}\n"
                    f"- **Images Found:** {row['images_found']}\n"
                    f"- **Duplicates:** {row['duplicates_skipped']}\n"
                    f"- **Sent:** {row['webhooks_sent']}"
                )
                for row in rows
            ]
            containers: list[RedditTextPanel] = [RedditTextPanel(title=f"/r/{sub} · Crawl Runs", body=entry) for entry in pages]
            view: KumaLayoutView = await KumaLayoutView(cog=self, owner=owner, timeout=None).add_containers(containers)
            return await context.send(view=view, ephemeral=ephemeral)

        # Summary view - one page per subreddit.
        pages = [
            (
                f"### /r/{row['subreddit']}\n"
                f"- **Total Runs:** {row['runs']}\n"
                f"- **Posts Seen:** {row['posts_seen']}\n"
                f"- **Images Found:** {row['images_found']}\n"
                f"- **Duplicates:** {row['duplicates_skipped']}\n"
                f"- **Sent:** {row['webhooks_sent']}\n"
                f"- **Last Run:** <t:{int(row['last_run'])}:R>"
            )
            for row in rows
        ]
        containers = [RedditTextPanel(title="__Crawler Metrics__", body=entry) for entry in pages]
        view = await KumaLayoutView(cog=self, owner=owner, timeout=None).add_containers(containers)
        return await context.send(view=view, ephemeral=ephemeral)


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(RedditImageCrawler(bot=bot))
