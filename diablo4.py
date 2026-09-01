"""Diablo 4 item parsing and tracking.

Integrates `d4-cauldron <https://github.com/k8thekat/D4Cauldron>`_ to extract
item data from in-game tooltip screenshots, persist items to the database, and
present them in a Components V2 panel with inline editing.

Requires ``d4-cauldron`` to be installed; the cog loads without it but the parse
command will tell the user what's missing.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

import discord
from discord import app_commands

from utils import KumaCog as Cog

if TYPE_CHECKING:
    from kuma_kuma import Kuma_Kuma

LOGGER: logging.Logger = logging.getLogger(__name__)

# Guarded imports — the cog degrades gracefully when d4-cauldron
# or its optional AI layer is absent.
try:
    from d4data.handler import Cauldron
    from d4data.vision import item_from_image

    HAS_D4_CAULDRON: bool = True
except ImportError:
    HAS_D4_CAULDRON = False

try:
    from d4data.tooltip import parse_tooltip

    HAS_D4_TOOLTIP: bool = True
except ImportError:
    HAS_D4_TOOLTIP = False

HAS_D4_VISION: bool = HAS_D4_CAULDRON
if HAS_D4_VISION:
    try:
        import anthropic  # presence check only
    except ImportError:
        HAS_D4_VISION = False

PARSE_COOLDOWN: float = 60.0
SUPPORTED_FORMATS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

SOURCE_INDICATOR: dict[str, str] = {
    "greater": "◆",
    "explicit": "◇",
    "tempered": "🔨",
    "implicit": "▸",
    "aspect": "✦",
    "gem_socket": "💎",
    "unknown": "·",
}

QUALITY_COLORS: dict[str, discord.Color] = {
    "Mythic": discord.Color.dark_red(),
    "Unique": discord.Color.gold(),
    "Legendary": discord.Color.orange(),
    "Rare": discord.Color.yellow(),
    "Magic": discord.Color.blue(),
}

D4_ITEMS_SETUP_SQL: str = """\
CREATE TABLE IF NOT EXISTS d4_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    userid      INTEGER NOT NULL,
    guildid     INTEGER NOT NULL,
    name        TEXT,
    slot        TEXT,
    item_power  INTEGER DEFAULT 800,
    quality     TEXT,
    data        TEXT    NOT NULL,
    source      TEXT    DEFAULT 'vision',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""


class DispatchedTask:
    """Tracks an async task dispatched by the cog (scaffolding)."""

    __slots__ = ("description", "guild_id", "started_at", "status", "task", "user_id")

    def __init__(
        self,
        *,
        task: asyncio.Task[Any],
        user_id: int,
        guild_id: int,
        started_at: float,
        description: str,
        status: str = "running",
    ) -> None:
        self.task: asyncio.Task[Any] = task
        self.user_id: int = user_id
        self.guild_id: int = guild_id
        self.started_at: float = started_at
        self.description: str = description
        self.status: str = status

    @property
    def elapsed(self) -> float:
        """Seconds since the task was dispatched."""
        return time.monotonic() - self.started_at

    @property
    def is_done(self) -> bool:
        """Whether the underlying task has finished."""
        return self.task.done()


class CooldownManager:
    """Per-user cooldown tracking backed by monotonic timestamps."""

    __slots__ = ("_timestamps", "rate")

    def __init__(self, rate: float = PARSE_COOLDOWN) -> None:
        self.rate: float = rate
        self._timestamps: dict[int, float] = {}

    def check(self, user_id: int) -> Optional[float]:
        """Remaining seconds, or ``None`` if the user is off cooldown."""
        last: Optional[float] = self._timestamps.get(user_id)
        if last is None:
            return None
        remaining: float = self.rate - (time.monotonic() - last)
        return remaining if remaining > 0 else None

    def trigger(self, user_id: int) -> None:
        """Record that the user just used a gated action."""
        self._timestamps[user_id] = time.monotonic()

    def reset(self, user_id: int) -> None:
        """Clear cooldown so the user can retry (e.g. after an error)."""
        self._timestamps.pop(user_id, None)


class ParseResult:
    """Normalised output from any image-processing pipeline."""

    __slots__ = ("confidence", "item_data", "method", "notes", "raw_tooltip")

    def __init__(
        self,
        *,
        item_data: dict[str, Any],
        method: str,
        notes: Optional[list[str]] = None,
        raw_tooltip: Optional[dict[str, Any]] = None,
        confidence: Optional[float] = None,
    ) -> None:
        self.item_data: dict[str, Any] = item_data
        self.method: str = method
        self.notes: list[str] = notes if notes is not None else []
        self.raw_tooltip: Optional[dict[str, Any]] = raw_tooltip
        self.confidence: Optional[float] = confidence


def _quality_color(quality: Optional[str]) -> discord.Color:
    """Accent colour for an item's quality tier."""
    if quality is None:
        return discord.Color.greyple()
    return QUALITY_COLORS.get(quality, discord.Color.greyple())


def _format_affix_line(affix: dict[str, Any]) -> str:
    """One affix dict as a single display line."""
    source: str = affix.get("source", "explicit")
    indicator: str = SOURCE_INDICATOR.get(source, "·")
    name: str = affix.get("name", affix.get("key", "Unknown"))

    # Pull values from the first stat line; the primary is what the tooltip shows.
    stats: list[dict[str, Any]] = affix.get("stats", [])
    if not stats:
        return f"{indicator} **{name}**"

    first: dict[str, Any] = stats[0]
    value: Optional[float] = first.get("value")
    greater: bool = first.get("greater", False)
    roll_quality: Optional[float] = first.get("roll_quality")
    natural_range: Optional[list[float]] = first.get("natural_range")

    parts: list[str] = [f"{indicator} **{name}**"]

    if value is not None:
        parts.append(f"— {value:g}")

    if natural_range:
        lo, hi = natural_range
        parts.append(f"`[{lo:g} – {hi:g}]`")

    if roll_quality is not None:
        parts.append(f"· {roll_quality:.0%}")

    if greater:
        parts.append("*(Greater)*")

    return " ".join(parts)


class ItemButton(discord.ui.Button["ItemView"]):
    """Action button on the item panel."""

    def __init__(self, *, action: str, label: str, emoji: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, emoji=emoji, style=style, custom_id=f"d4:{action}")
        self._action: str = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Dispatch press to the owning view."""
        view: Optional[ItemView] = self.view
        if view is None:
            return
        await view._dispatch(interaction=interaction, action=self._action)  # noqa: SLF001


class ItemFieldSelect(discord.ui.Select["ItemView"]):
    """Dropdown listing every editable field on the parsed item."""

    def __init__(self, *, item_data: dict[str, Any]) -> None:
        options: list[discord.SelectOption] = [
            discord.SelectOption(label="Name", value="name", description=str(item_data.get("name") or "—")[:100]),
            discord.SelectOption(label="Item Power", value="item_power", description=str(item_data.get("item_power", 800))),
            discord.SelectOption(label="Quality", value="quality", description=str(item_data.get("quality") or "—")[:100]),
            discord.SelectOption(label="Upgrade Level", value="upgrade", description=str(item_data.get("upgrade", 0))),
        ]

        for idx, affix in enumerate(item_data.get("affixes", [])):
            affix_name: str = affix.get("name", affix.get("key", f"Affix {idx + 1}"))
            source: str = affix.get("source", "explicit")
            desc: str = source
            stats: list[dict[str, Any]] = affix.get("stats", [])
            if stats and stats[0].get("value") is not None:
                desc += f" — {stats[0]['value']:g}"
            options.append(discord.SelectOption(label=affix_name[:100], value=f"affix:{idx}", description=desc[:100]))
            if len(options) >= 25:
                break

        super().__init__(placeholder="Edit a field…", options=options, custom_id="d4:edit_field", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Dispatch selection to the owning view."""
        view: Optional[ItemView] = self.view
        if view is None:
            return
        await view._dispatch(interaction=interaction, action=f"edit:{self.values[0]}")  # noqa: SLF001


class ItemEditModal(discord.ui.Modal):
    """Text input for correcting a parsed item field."""

    def __init__(self, *, field_key: str, current_value: str, item_view: ItemView) -> None:
        pretty: str = field_key.replace("affix:", "Affix #")
        super().__init__(title=f"Edit — {pretty}"[:45])
        self.field_key: str = field_key
        self._item_view: ItemView = item_view
        self._text_input: discord.ui.TextInput = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=True,
            default=current_value,
        )
        self.add_item(discord.ui.Label(text="New Value", component=self._text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Apply the edit and re-render the panel."""
        await self._item_view._apply_edit(  # noqa: SLF001
            interaction=interaction,
            field_key=self.field_key,
            new_value=self._text_input.value,
        )


class ItemView(discord.ui.LayoutView):
    """Displays a parsed D4 item with edit and save controls.

    .. warning::
        Components V2 — cannot carry ``content`` or ``embeds``.

    """

    def __init__(
        self,
        *,
        cog: Diablo4Cog,
        owner: Union[discord.User, discord.Member],
        item_data: dict[str, Any],
        notes: Optional[list[str]] = None,
        image_url: Optional[str] = None,
        item_db_id: Optional[int] = None,
    ) -> None:
        super().__init__(timeout=cog.message_timeout)
        self.cog: Diablo4Cog = cog
        self.owner: Union[discord.User, discord.Member] = owner
        self.item_data: dict[str, Any] = item_data
        self.notes: list[str] = notes or []
        self.image_url: Optional[str] = image_url
        self.item_db_id: Optional[int] = item_db_id

        self._build_layout()

    def _build_layout(self) -> None:
        """Construct the CV2 component tree from the current item data."""
        self.clear_items()

        data: dict[str, Any] = self.item_data
        quality: Optional[str] = data.get("quality")
        color: discord.Color = _quality_color(quality)

        container: discord.ui.Container = discord.ui.Container(accent_colour=color)

        # Header — name, slot, power, quality, masterwork
        name: str = data.get("name") or "Unknown Item"
        slot: str = data.get("slot") or "—"
        item_power: int = data.get("item_power", 0)
        upgrade: int = data.get("upgrade", 0)
        quality_label: str = quality or "Unknown"

        header_text: str = f"## {self.cog.emoji_table.kuma_peak} {name}"
        subtext_parts: list[str] = [quality_label, slot, f"{item_power} IP"]
        if upgrade:
            subtext_parts.append(f"MW {upgrade}")
        header_text += f"\n-# {' · '.join(subtext_parts)}"

        # TODO: Replace with the actual in-game item icon URL once an icon
        # resolver is available (e.g. from d4-cauldron's asset tables or a
        # Blizzard render CDN).
        if self.image_url:
            container.add_item(
                discord.ui.Section(
                    header_text,
                    accessory=discord.ui.Thumbnail(media=self.image_url),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header_text))

        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        # Affixes — split regular affixes from aspects
        affixes: list[dict[str, Any]] = data.get("affixes", [])
        if affixes:
            regular_lines: list[str] = []
            aspect_lines: list[str] = []

            for affix in affixes:
                source: str = affix.get("source", "explicit")
                if source == "aspect" or affix.get("magic_type") == "Legendary":
                    aspect_lines.append(_format_affix_line(affix))
                else:
                    regular_lines.append(_format_affix_line(affix))

            if regular_lines:
                container.add_item(discord.ui.TextDisplay("### Affixes\n" + "\n".join(regular_lines)))

            if aspect_lines:
                container.add_item(discord.ui.Separator())
                effect_text: str = ""
                for affix in affixes:
                    if affix.get("effect"):
                        effect_text = f"\n-# *{affix['effect'][:200]}*"
                        break
                container.add_item(discord.ui.TextDisplay("### Aspect\n" + "\n".join(aspect_lines) + effect_text))
        else:
            container.add_item(discord.ui.TextDisplay("-# No affixes parsed."))

        # Sockets and gems
        sockets: list[str] = data.get("sockets", [])
        gems: list[dict[str, Any]] = data.get("socketed_gems", [])
        if sockets or gems:
            container.add_item(discord.ui.Separator())
            socket_parts: list[str] = [f"**Sockets:** {len(sockets)}"]
            for gem in gems:
                gem_name: str = gem.get("name", "Unknown Gem")
                socket_parts.append(f"💎 {gem_name}")
            container.add_item(discord.ui.TextDisplay("\n".join(socket_parts)))

        # Footer — roll quality, extraction notes, saved status
        container.add_item(discord.ui.Separator())
        footer_parts: list[str] = []
        avg_quality: Optional[float] = data.get("average_roll_quality")
        if avg_quality is not None:
            footer_parts.append(f"Avg roll quality: **{avg_quality:.0%}**")
        if self.notes:
            footer_parts.extend(f"⚠ {note[:120]}" for note in self.notes[:3])
            if len(self.notes) > 3:
                footer_parts.append(f"-# …and {len(self.notes) - 3} more")
        if self.item_db_id is not None:
            footer_parts.append(f"-# Saved · ID {self.item_db_id}")

        if footer_parts:
            container.add_item(discord.ui.TextDisplay("\n".join(footer_parts)))

        self.add_item(container)

        # Edit select
        self.add_item(discord.ui.ActionRow().add_item(ItemFieldSelect(item_data=self.item_data)))

        # Action buttons
        button_row: discord.ui.ActionRow = discord.ui.ActionRow()
        button_row.add_item(ItemButton(action="save", label="Save", emoji="💾", style=discord.ButtonStyle.success))
        button_row.add_item(ItemButton(action="export", label="Export JSON", emoji="📋", style=discord.ButtonStyle.secondary))
        if self.item_db_id is not None:
            button_row.add_item(ItemButton(action="delete", label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger))
        self.add_item(button_row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the triggering user can interact."""
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                content=f"That isn't your item panel {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    async def _dispatch(self, *, interaction: discord.Interaction, action: str) -> None:
        """Route button and select actions."""
        if action == "save":
            await self._handle_save(interaction=interaction)
        elif action == "export":
            await self._handle_export(interaction=interaction)
        elif action == "delete":
            await self._handle_delete(interaction=interaction)
        elif action.startswith("edit:"):
            field_key: str = action.removeprefix("edit:")
            current: str = self._current_value(field_key)
            modal: ItemEditModal = ItemEditModal(field_key=field_key, current_value=current, item_view=self)
            await interaction.response.send_modal(modal)

    def _current_value(self, field_key: str) -> str:
        """Look up the live display value for a given field key."""
        if field_key.startswith("affix:"):
            idx: int = int(field_key.removeprefix("affix:"))
            affixes: list[dict[str, Any]] = self.item_data.get("affixes", [])
            if 0 <= idx < len(affixes):
                stats: list[dict[str, Any]] = affixes[idx].get("stats", [])
                if stats and stats[0].get("value") is not None:
                    return str(stats[0]["value"])
                return affixes[idx].get("name", "")
            return ""
        value: Any = self.item_data.get(field_key, "")
        return str(value) if value is not None else ""

    async def _apply_edit(self, *, interaction: discord.Interaction, field_key: str, new_value: str) -> None:
        """Write the edited value back into item_data and re-render."""
        if field_key.startswith("affix:"):
            idx = int(field_key.removeprefix("affix:"))
            affixes: list[dict[str, Any]] = self.item_data.get("affixes", [])
            if 0 <= idx < len(affixes):
                stats: list[dict[str, Any]] = affixes[idx].get("stats", [])
                try:
                    numeric: float = float(new_value)
                    if stats:
                        stats[0]["value"] = numeric
                except ValueError:
                    # Not a number — update the affix name instead.
                    affixes[idx]["name"] = new_value
        elif field_key in ("item_power", "upgrade"):
            try:
                self.item_data[field_key] = int(new_value)
            except ValueError:
                pass
        else:
            self.item_data[field_key] = new_value

        self._build_layout()
        await interaction.response.edit_message(view=self)

    async def _handle_save(self, *, interaction: discord.Interaction) -> None:
        """Persist the item to the database, or update if already saved."""
        guild_id: int = interaction.guild_id or 0
        try:
            if self.item_db_id is not None:
                await self.cog.update_item(item_id=self.item_db_id, item_data=self.item_data)
            else:
                self.item_db_id = await self.cog.save_item(
                    user_id=interaction.user.id,
                    guild_id=guild_id,
                    item_data=self.item_data,
                )
            self._build_layout()
            await interaction.response.edit_message(view=self)
        except Exception as exc:  # noqa: BLE001 — user-facing recovery; we log and show a message
            LOGGER.warning("<%s.%s> | Save failed | Error: %s", "ItemView", "_handle_save", exc)
            await interaction.response.send_message(
                content=f"Failed to save that item {self.cog.emoji_table.kuma_sad}",
                ephemeral=True,
            )

    async def _handle_export(self, *, interaction: discord.Interaction) -> None:
        """Send the raw JSON as a file attachment."""
        raw: str = json.dumps(self.item_data, indent=2)
        name: str = self.item_data.get("name") or "item"
        safe_name: str = "".join(c if c.isalnum() or c in "-_ " else "" for c in name).strip().replace(" ", "_")
        file: discord.File = discord.File(fp=io.BytesIO(raw.encode()), filename=f"{safe_name or 'item'}.json")
        await interaction.response.send_message(file=file, ephemeral=True)

    async def _handle_delete(self, *, interaction: discord.Interaction) -> None:
        """Remove the item from the database."""
        if self.item_db_id is None:
            return
        try:
            await self.cog.delete_item(item_id=self.item_db_id)
            self.item_db_id = None
            self._build_layout()
            await interaction.response.edit_message(view=self)
        except Exception as exc:  # noqa: BLE001 — user-facing recovery; we log and show a message
            LOGGER.warning("<%s.%s> | Delete failed | Error: %s", "ItemView", "_handle_delete", exc)
            await interaction.response.send_message(
                content=f"Failed to delete that item {self.cog.emoji_table.kuma_sad}",
                ephemeral=True,
            )


class Diablo4Cog(Cog, name="Diablo4"):
    """Diablo 4 item parsing, tracking, and display.

    Wraps ``d4-cauldron``'s vision and tooltip pipelines behind Discord slash
    commands, with per-user cooldowns, task tracking, and SQLite persistence.

    """

    d4 = app_commands.Group(name="d4", description="Diablo 4 item tools.", guild_only=True)

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        self._handler: Optional[Any] = None
        self._cooldowns: CooldownManager = CooldownManager()
        # TODO: Key by a unique task ID to support multiple concurrent tasks per user.
        self._tasks: dict[int, DispatchedTask] = {}

    async def cog_load(self) -> None:
        """Create the item table and initialise the d4-cauldron handler."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute(D4_ITEMS_SETUP_SQL)
        LOGGER.info("<%s.%s> | d4_items table ready", __class__.__name__, "cog_load")

        if HAS_D4_CAULDRON:
            try:
                self._handler = await Cauldron().build()
                LOGGER.info("<%s.%s> | d4-cauldron handler loaded", __class__.__name__, "cog_load")
            except Exception:  # noqa: BLE001 — startup recovery; handler is optional
                LOGGER.warning(
                    "<%s.%s> | Failed to initialise d4-cauldron handler",
                    __class__.__name__,
                    "cog_load",
                    exc_info=True,
                )
        else:
            LOGGER.warning("<%s.%s> | d4-cauldron not installed; parse commands unavailable", __class__.__name__, "cog_load")

    async def cog_unload(self) -> None:
        """Cancel any running tasks on unload."""
        for task_info in self._tasks.values():
            if not task_info.is_done:
                task_info.task.cancel()
        self._tasks.clear()

    @d4.command(name="parse", description="Extract item data from a tooltip screenshot.")
    @app_commands.describe(
        image="A screenshot of the item tooltip.",
        method="Processing method — Auto picks the best available.",
    )
    @app_commands.choices(
        method=[
            app_commands.Choice(name="Auto (best available)", value="auto"),
            app_commands.Choice(name="AI Vision (Claude API)", value="vision"),
            app_commands.Choice(name="Pixel Analysis (Pillow)", value="tooltip"),
        ]
    )
    async def parse_item(self, interaction: discord.Interaction, image: discord.Attachment, method: str = "auto") -> None:
        """Parse a D4 tooltip screenshot into structured item data.

        Parameters
        ----------
        interaction: :class:`discord.Interaction`
            The invoking interaction.
        image: :class:`discord.Attachment`
            Screenshot of the in-game tooltip.
        method: :class:`str`, optional
            Which pipeline to use, by default ``"auto"``.

        """
        # Resolve auto before validation so the check matches the actual pipeline.
        if method == "auto":
            if HAS_D4_VISION and self._handler is not None:
                method = "vision"
            elif HAS_D4_TOOLTIP:
                method = "tooltip"
            else:
                await interaction.response.send_message(
                    f"No processing backend available — install `d4-cauldron` or `Pillow` {self.emoji_table.kuma_sad}",
                    ephemeral=True,
                )
                return

        # Library availability for an explicit choice.
        if method == "vision" and (not HAS_D4_VISION or self._handler is None):
            missing: str = "d4-cauldron" if not HAS_D4_CAULDRON else "anthropic (`pip install d4-cauldron[ai]`)"
            await interaction.response.send_message(
                f"AI Vision requires {missing} {self.emoji_table.kuma_sad}",
                ephemeral=True,
            )
            return

        if method == "tooltip" and not HAS_D4_TOOLTIP:
            await interaction.response.send_message(
                f"Pixel Analysis requires Pillow — `pip install Pillow` {self.emoji_table.kuma_sad}",
                ephemeral=True,
            )
            return

        # Per-user cooldown
        remaining: Optional[float] = self._cooldowns.check(interaction.user.id)
        if remaining is not None:
            await interaction.response.send_message(
                f"On cooldown — try again in **{remaining:.0f}s** {self.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return

        # File format validation
        suffix: str = Path(image.filename).suffix.lower()
        if suffix not in SUPPORTED_FORMATS:
            await interaction.response.send_message(
                f"Unsupported image format `{suffix}` {self.emoji_table.kuma_sad}\n-# Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self._cooldowns.trigger(interaction.user.id)

        try:
            result: ParseResult = await self._process_image(image=image, method=method)
        except Exception as exc:  # noqa: BLE001 — parse pipeline can fail many ways; reset cooldown and report
            self._cooldowns.reset(interaction.user.id)
            LOGGER.warning("<%s.%s> | Parse failed | Error: %s", __class__.__name__, "parse_item", exc)
            await interaction.followup.send(
                content=f"Failed to parse the image: {exc} {self.emoji_table.kuma_sad}",
                ephemeral=True,
            )
            return

        view: ItemView = ItemView(
            cog=self,
            owner=interaction.user,
            item_data=result.item_data,
            notes=result.notes,
            image_url=image.url,
        )
        await interaction.followup.send(view=view)

    @d4.command(name="inventory", description="Browse your saved items.")
    async def inventory(self, interaction: discord.Interaction) -> None:
        """List items saved to the database.

        .. note::
            Scaffolding — the full inventory browser with filtering and
            comparison is a future addition.

        """
        guild_id: int = interaction.guild_id or 0
        items: list[dict[str, Any]] = await self.list_items(user_id=interaction.user.id, guild_id=guild_id)

        if not items:
            await interaction.response.send_message(
                f"No saved items yet — use `/d4 parse` to get started {self.emoji_table.kuma_happy}",
                ephemeral=True,
            )
            return

        # TODO: Build a proper paginated inventory browser with KumaView.
        lines: list[str] = [f"## {self.emoji_table.kuma_peak} Your Items"]
        for item in items[:25]:
            name: str = item.get("name") or "Unknown"
            quality: str = item.get("quality") or "—"
            power: int = item.get("item_power", 0)
            row_id: int = item["id"]
            lines.append(f"**{name}** — {quality} · {power} IP · `#{row_id}`")

        if len(items) > 25:
            lines.append(f"-# …and {len(items) - 25} more")

        await interaction.response.send_message(content="\n".join(lines), ephemeral=True)

    async def _process_image(self, *, image: discord.Attachment, method: str) -> ParseResult:
        """Dispatch to the selected processing pipeline.

        Parameters
        ----------
        image: :class:`discord.Attachment`
            The uploaded screenshot.
        method: :class:`str`
            ``"vision"`` for AI extraction or ``"tooltip"`` for Pillow pixel
            analysis. Auto-resolution happens in the command before this is
            called.

        """
        image_bytes: bytes = await image.read()

        if method == "vision":
            return await self._process_vision(image_bytes=image_bytes, filename=image.filename)
        if method == "tooltip":
            return await self._process_tooltip(image_bytes=image_bytes)
        # TODO: Local LLM pipeline (Ollama) — would require a vision-capable
        # model (e.g. llava) and the /api/generate endpoint with the `images`
        # parameter. The extraction prompt would mirror vision.py's.
        msg: str = f"Unknown processing method: {method}"
        raise ValueError(msg)

    async def _process_vision(self, *, image_bytes: bytes, filename: str) -> ParseResult:
        """AI vision extraction via Claude API → full Item."""
        if self._handler is None:
            msg: str = "d4-cauldron handler not initialised"
            raise RuntimeError(msg)

        suffix: str = Path(filename).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            temp_path: Path = Path(tmp.name)

        try:
            item, notes = await item_from_image(str(temp_path), self._handler)
        finally:
            temp_path.unlink(missing_ok=True)

        if item is None:
            msg = "Vision extraction returned no item — " + "; ".join(notes)
            raise RuntimeError(msg)

        return ParseResult(item_data=item.to_dict(), method="vision", notes=notes)

    async def _process_tooltip(self, *, image_bytes: bytes) -> ParseResult:
        """Pillow-based structural extraction — no external API needed."""
        from PIL import Image as PILImage  # noqa: PLC0415 — guarded optional import

        img: PILImage.Image = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
        # parse_tooltip is synchronous pixel work; keep the event loop free.
        tooltip_data = await asyncio.to_thread(parse_tooltip, img)

        # Normalise TooltipData into the same dict shape the vision path produces.
        item_data: dict[str, Any] = {
            "key": "",
            "name": tooltip_data.item_name,
            "slot": tooltip_data.item_type,
            "item_power": tooltip_data.item_power or 0,
            "quality": None,
            "upgrade": 0,
            "sockets": [],
            "average_roll_quality": None,
            "affixes": [],
        }

        for affix_line in tooltip_data.affix_lines:
            item_data["affixes"].append(
                {
                    "name": affix_line.stat_name or "Unknown",
                    "key": "",
                    "power": None,
                    "source": affix_line.source.name.lower(),
                    "magic_type": None,
                    "effect": None,
                    "scaling": None,
                    "stats": [
                        {
                            "stat": affix_line.stat_name,
                            "value": float(affix_line.value) if affix_line.value else None,
                            "natural_range": None,
                            "roll_quality": None,
                            "greater": affix_line.is_greater,
                        }
                    ],
                }
            )

        return ParseResult(
            item_data=item_data,
            method="tooltip",
            notes=tooltip_data.notes,
            raw_tooltip=tooltip_data.summary(),
        )

    async def save_item(self, *, user_id: int, guild_id: int, item_data: dict[str, Any]) -> int:
        """Persist an item to the database; returns the new row ID."""
        async with self.bot.pool.acquire() as conn:
            cursor = await conn.execute(
                """INSERT INTO d4_items (userid, guildid, name, slot, item_power, quality, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    guild_id,
                    item_data.get("name"),
                    item_data.get("slot"),
                    item_data.get("item_power", 800),
                    item_data.get("quality"),
                    json.dumps(item_data),
                ),
            )
            LOGGER.info("<%s.%s> | Item saved | User: %s, ID: %s", __class__.__name__, "save_item", user_id, cursor.lastrowid)
            return cursor.lastrowid  # type: ignore[return-value]

    async def load_item(self, *, item_id: int) -> Optional[dict[str, Any]]:
        """Load a single item from the database by row ID."""
        async with self.bot.pool.acquire() as conn:
            row = await conn.fetchone("""SELECT * FROM d4_items WHERE id = ?""", (item_id,))
        if row is None:
            return None
        result: dict[str, Any] = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    async def list_items(self, *, user_id: int, guild_id: int) -> list[dict[str, Any]]:
        """List all items for a user in a guild, newest first."""
        async with self.bot.pool.acquire() as conn:
            rows = await conn.fetchall(
                """SELECT * FROM d4_items WHERE userid = ? AND guildid = ? ORDER BY updated_at DESC""",
                (user_id, guild_id),
            )
        items: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = dict(row)
            item["data"] = json.loads(item["data"])
            items.append(item)
        return items

    async def update_item(self, *, item_id: int, item_data: dict[str, Any]) -> bool:
        """Update an existing item's data; returns whether the row existed."""
        async with self.bot.pool.acquire() as conn:
            cursor = await conn.execute(
                """UPDATE d4_items
                   SET name = ?, slot = ?, item_power = ?, quality = ?, data = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    item_data.get("name"),
                    item_data.get("slot"),
                    item_data.get("item_power", 800),
                    item_data.get("quality"),
                    json.dumps(item_data),
                    item_id,
                ),
            )
            return cursor.rowcount > 0

    async def delete_item(self, *, item_id: int) -> bool:
        """Remove an item from the database; returns whether the row existed."""
        async with self.bot.pool.acquire() as conn:
            cursor = await conn.execute("""DELETE FROM d4_items WHERE id = ?""", (item_id,))
            return cursor.rowcount > 0


async def setup(bot: Kuma_Kuma) -> None:
    """Load the Diablo 4 cog."""
    await bot.add_cog(Diablo4Cog(bot=bot))
