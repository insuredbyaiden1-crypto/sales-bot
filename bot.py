"""
Discord Sales Leaderboard Bot

- Watches one sales channel for sale messages like  $651AP / Core GIWL / 12/03
  or  His / 1707 / Chubb substandard / 12/4  (one message can hold several sales)
- Stores every sale (with a link back to the original message) in SQLite
- At 3:30 PM and 11:00 PM every day, posts a NEW Daily, Weekly and Monthly board,
  each in its own channel, rebuilt from the latest sales
- Boards of any size: long boards continue across several messages, so nobody is cut off
- Before every post the bot re-reads the sales channel, so a sale is never missed
  (even if the bot or the computer was off for a while)
- /leaderboard day|week|month   -> on-demand board
- /postboards                   -> post all three boards right now (admin only)
- /backfill days                -> import older messages from the sales channel (admin only)
- Edited sale messages update the sale; deleted messages remove it
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, time, timedelta, timezone, date
from decimal import Decimal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ---------- Config ----------
TOKEN = os.environ["DISCORD_TOKEN"]
SALES_CHANNEL_ID = int(os.environ["SALES_CHANNEL_ID"])
BOARD_CHANNEL_ID = int(os.environ["LEADERBOARD_CHANNEL_ID"])
# Optional: separate channels for weekly / monthly boards (default to the daily channel)
WEEKLY_CHANNEL_ID = int(os.getenv("WEEKLY_CHANNEL_ID") or BOARD_CHANNEL_ID)
MONTHLY_CHANNEL_ID = int(os.getenv("MONTHLY_CHANNEL_ID") or BOARD_CHANNEL_ID)
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

MIDDAY_POST = time(15, 30, tzinfo=TZ)   # 3:30 PM
FINAL_POST = time(23, 0, tzinfo=TZ)     # 11:00 PM
# A "sales day" runs from the previous cutoff to this cutoff. Sales logged after
# the cutoff count toward the NEXT day. Set to 24 for plain midnight-to-midnight.
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "23"))

MEDALS = ["🥇", "🥈", "🥉"]
MAX_LINKS_PER_REP = 3

# ---------- Database ----------
db = sqlite3.connect(os.getenv("DB_PATH", "sales.db"))

# Older versions stored one sale per message. Upgrade the table if needed (keeps old data).
_cols = [r[1] for r in db.execute("PRAGMA table_info(sales)")]
if _cols and "item" not in _cols:
    db.execute("ALTER TABLE sales RENAME TO sales_old")
db.execute(
    """CREATE TABLE IF NOT EXISTS sales (
        message_id    INTEGER NOT NULL,
        item          INTEGER NOT NULL DEFAULT 0,
        user_id       INTEGER NOT NULL,
        display_name  TEXT NOT NULL,
        cents         INTEGER NOT NULL,
        customer      TEXT,
        created_at    TEXT NOT NULL,
        business_date TEXT NOT NULL,
        link          TEXT NOT NULL,
        PRIMARY KEY (message_id, item)
    )"""
)
if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_old'").fetchone():
    db.execute(
        """INSERT INTO sales
           (message_id, item, user_id, display_name, cents, customer, created_at, business_date, link)
           SELECT message_id, 0, user_id, display_name, cents, customer, created_at, business_date, link
           FROM sales_old"""
    )
    db.execute("DROP TABLE sales_old")
db.execute("CREATE INDEX IF NOT EXISTS idx_sales_date ON sales (business_date)")
db.commit()

# ---------- Parsing ----------
# Layouts the bot understands (capitalization and bold/italics don't matter):
#
#   $651AP               His                 (one message can hold several sales,
#   Core GIWL            1707                 like His / Hers)
#   12/03                Chubb substandard
#                        12/4
#                        Hers
#                        878
#                        Chubb graded
#                        12/4
#
# A sale is either an amount written like "$651AP" / "$1,402.44 AP", or a line that is
# just a number (1707) with a date like 12/4 within the next 3 lines. A short line right
# before the amount (His, Hers) is saved as a label. Everything else is saved as details.
AMOUNT = r"(\d[\d,]*(?:\.\d{1,2})?)"
AP_RE = re.compile(r"\$\s*" + AMOUNT + r"\s*AP\b", re.IGNORECASE)
NUMBER_LINE_RE = re.compile(r"^\$?\s*" + AMOUNT + r"\s*(?:AP)?$", re.IGNORECASE)
DATE_LINE_RE = re.compile(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?$")

# Older test format, still accepted:   Sale: $100 | Test
SALE_RE = re.compile(
    r"^\s*sale\s*[:\-]?\s*\$?\s*" + AMOUNT + r"\s*(?:\|\s*(.+?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def to_cents(amount: str) -> int:
    return int(Decimal(amount.replace(",", "")) * 100)


def _is_amount_line(line: str) -> bool:
    return bool(AP_RE.search(line) or NUMBER_LINE_RE.match(line))


def parse_sales(content: str):
    """Returns a list of (cents, details), one per sale in the message (empty if none)."""
    text = re.sub(r"[*_`~]", "", content)  # ignore bold/italic formatting
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    sales = []
    free = 0  # first line not yet used by an earlier sale
    i = 0
    while i < len(lines):
        line = lines[i]
        ap = AP_RE.search(line)
        num = None if ap else NUMBER_LINE_RE.match(line)
        if not (ap or num):
            i += 1
            continue

        # Collect the lines after the amount (product, date), up to 3 lines.
        details, found_date, j = [], False, i + 1
        while j < len(lines) and j <= i + 3:
            if _is_amount_line(lines[j]):
                break
            details.append(lines[j])
            j += 1
            if DATE_LINE_RE.match(details[-1]):
                found_date = True
                break

        if num and not found_date:
            i += 1  # a bare number with no date nearby is just chat, not a sale
            continue

        cents = to_cents((ap or num).group(1))
        if cents > 0:
            label = ""
            if i - 1 >= free:
                prev = lines[i - 1]
                if len(prev) <= 20 and not _is_amount_line(prev) and not DATE_LINE_RE.match(prev):
                    label = prev
            rest = AP_RE.sub("", line).strip() if ap else ""
            parts = [p for p in [label, rest, *details] if p]
            sales.append((cents, " · ".join(parts) or None))
        free = j
        i = j

    if sales:
        return sales

    m = SALE_RE.match(text)
    if m:
        cents = to_cents(m.group(1))
        if cents > 0:
            return [(cents, (m.group(2) or "").strip() or None)]
    return []


def business_date(dt_utc: datetime) -> date:
    local = dt_utc.astimezone(TZ)
    return (local + timedelta(hours=24 - CUTOFF_HOUR)).date()


def current_business_date() -> date:
    # Subtract a minute so the 11:00 PM post still reports the day that just closed.
    return business_date(datetime.now(timezone.utc) - timedelta(minutes=1))


# ---------- Storage helpers ----------
async def sync_message(message: discord.Message, commit: bool = True) -> int:
    """Create/update/remove the sale rows for a message. Returns how many sales it holds."""
    if message.author.bot or message.channel.id != SALES_CHANNEL_ID:
        return 0
    sales = parse_sales(message.content)
    db.execute("DELETE FROM sales WHERE message_id = ?", (message.id,))
    for item, (cents, details) in enumerate(sales):
        db.execute(
            """INSERT INTO sales
               (message_id, item, user_id, display_name, cents, customer, created_at, business_date, link)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.id,
                item,
                message.author.id,
                message.author.display_name,
                cents,
                details,
                message.created_at.isoformat(),
                business_date(message.created_at).isoformat(),
                message.jump_url,
            ),
        )
    if commit:
        db.commit()
    return len(sales)


def aggregate(start: date, end: date):
    rows = db.execute(
        """SELECT user_id, display_name, cents, link FROM sales
           WHERE business_date BETWEEN ? AND ?
           ORDER BY created_at DESC""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    reps = {}
    for user_id, name, cents, link in rows:
        rep = reps.setdefault(user_id, {"name": name, "total": 0, "sales": []})
        rep["total"] += cents
        rep["sales"].append((cents, link))
    return sorted(reps.values(), key=lambda r: (-r["total"], r["name"].lower()))


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ---------- Date ranges ----------
def week_range(d: date):
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def month_range(d: date):
    first = d.replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return first, next_first - timedelta(days=1)


# ---------- Boards ----------
DETAIL_TOP = 10      # top reps show up to 3 sale links; everyone else gets one "latest" link
PAGE_LIMIT = 3800    # characters of rep rows per message (Discord's hard limit is 4096)

BOARDS = [  # (title, channel, date range for a given day, color)
    ("📊 Daily Leaderboard", BOARD_CHANNEL_ID, lambda d: (d, d), discord.Color.gold()),
    ("🏆 Weekly Leaderboard", WEEKLY_CHANNEL_ID, week_range, discord.Color.blue()),
    ("👑 Monthly Leaderboard", MONTHLY_CHANNEL_ID, month_range, discord.Color.purple()),
]


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def rep_row(rank: int, rep: dict) -> str:
    marker = MEDALS[rank - 1] if rank <= 3 else f"{rank}."
    name = discord.utils.escape_markdown(rep["name"])
    n = len(rep["sales"])
    summary = f"{money(rep['total'])} | {plural(n, 'Sale')}"
    if rank <= DETAIL_TOP:
        links = " · ".join(f"[{money(c)}]({link})" for c, link in rep["sales"][:MAX_LINKS_PER_REP])
        if n > MAX_LINKS_PER_REP:
            links += f" · +{n - MAX_LINKS_PER_REP} more"
        return f"{marker} **{name}**\n{summary}\n↳ {links}"
    return f"{marker} **{name}** — {summary} · [latest]({rep['sales'][0][1]})"


def build_embeds(title: str, start: date, end: date, color=None, max_pages=None):
    """Returns a list of embeds (one per message). Long boards continue on extra pages."""
    ranked = aggregate(start, end)
    total_cents = sum(r["total"] for r in ranked)
    total_sales = sum(len(r["sales"]) for r in ranked)
    color = color or discord.Color.gold()

    subtitle = start.strftime("%a %b %d") if start == end else f"{start.strftime('%b %d')} – {end.strftime('%b %d')}"
    header = f"*{subtitle}*\n**Total: {money(total_cents)} | {plural(total_sales, 'Sale')} | {plural(len(ranked), 'Rep')}**"

    if not ranked:
        return [discord.Embed(title=title, description=f"{header}\n\nNo sales logged yet.", color=color)]

    pages, cur = [], header
    for rank, rep in enumerate(ranked, start=1):
        row = rep_row(rank, rep)
        sep = "\n" if rank > DETAIL_TOP + 1 else "\n\n"
        if len(cur) + len(sep) + len(row) > PAGE_LIMIT:
            pages.append(cur)
            cur = row
        else:
            cur += sep + row
    pages.append(cur)

    if max_pages and len(pages) > max_pages:
        pages = pages[:max_pages]
        pages[-1] += "\n\n*Showing the top of the board. The full board posts at 3:30 PM and 11:00 PM.*"

    embeds = []
    for i, text in enumerate(pages, start=1):
        embed = discord.Embed(title=title if i == 1 else f"{title} (continued)", description=text, color=color)
        if len(pages) > 1:
            embed.set_footer(text=f"Page {i} of {len(pages)}")
        embeds.append(embed)
    return embeds


def window_start_utc(d: date) -> datetime:
    """Start of the stretch of history we re-read before posting: the earlier of this
    week's / this month's first day, minus a day for the late-night cutoff."""
    first = min(week_range(d)[0], month_range(d)[0]) - timedelta(days=1)
    return datetime.combine(first, time(0, 0), tzinfo=TZ).astimezone(timezone.utc)


# ---------- Bot ----------
intents = discord.Intents.default()
intents.message_content = True  # also enable "Message Content Intent" in the Developer Portal


class SalesBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        post_boards.start()


client = SalesBot()


_started = False


@client.event
async def on_ready():
    global _started
    logging.info("Logged in as %s", client.user)
    if not _started:  # on_ready can fire again after a reconnect; only catch up once
        _started = True
        try:
            await resync(window_start_utc(current_business_date()))
            logging.info("Caught up on sales from the channel history")
        except Exception:
            logging.exception("Startup catch-up failed")


@client.event
async def on_message(message: discord.Message):
    if await sync_message(message):
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.channel_id != SALES_CHANNEL_ID:
        return
    channel = client.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    if await sync_message(message):
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    db.execute("DELETE FROM sales WHERE message_id = ?", (payload.message_id,))
    db.commit()


# ---------- Catch-up sync ----------
async def resync(since_utc: datetime):
    """Re-read the sales channel from `since_utc`: add anything missed, fix edits,
    and drop sales whose messages were deleted while the bot was away."""
    channel = client.get_channel(SALES_CHANNEL_ID)
    if channel is None:
        logging.error("Sales channel not found, so the catch-up was skipped")
        return
    seen = set()
    async for message in channel.history(after=since_utc, limit=None, oldest_first=True):
        seen.add(message.id)
        await sync_message(message, commit=False)
    stored = {
        r[0]
        for r in db.execute("SELECT DISTINCT message_id FROM sales WHERE created_at >= ?", (since_utc.isoformat(),))
    }
    for message_id in stored - seen:
        db.execute("DELETE FROM sales WHERE message_id = ?", (message_id,))
    db.commit()


# ---------- Scheduled posts ----------
async def post_all_boards(is_final_time: bool):
    d = current_business_date()
    try:
        await resync(window_start_utc(d))
    except Exception:
        logging.exception("Catch-up before posting failed; posting from the sales already saved")

    for title, channel_id, range_fn, color in BOARDS:
        start, end = range_fn(d)
        if not is_final_time:
            suffix = "So Far"
        elif d >= end:
            suffix = "Final"       # last day of the day / week / month
        else:
            suffix = "End of Day"
        channel = client.get_channel(channel_id)
        if channel is None:
            logging.error("%s: channel %s not found. Is the bot allowed in that channel?", title, channel_id)
            continue
        try:
            for embed in build_embeds(f"{title} — {suffix}", start, end, color):
                await channel.send(embed=embed)
        except Exception:
            logging.exception("Could not post %s", title)


@tasks.loop(time=[MIDDAY_POST, FINAL_POST])
async def post_boards():
    await post_all_boards(is_final_time=datetime.now(TZ).hour == FINAL_POST.hour)


@post_boards.before_loop
async def before_post_boards():
    await client.wait_until_ready()


# ---------- Slash commands ----------
@client.tree.command(name="leaderboard", description="Show the sales leaderboard")
@app_commands.describe(period="Which leaderboard to show")
@app_commands.choices(
    period=[
        app_commands.Choice(name="Today", value="day"),
        app_commands.Choice(name="This week", value="week"),
        app_commands.Choice(name="This month", value="month"),
    ]
)
async def leaderboard(interaction: discord.Interaction, period: app_commands.Choice[str]):
    title, _, range_fn, color = BOARDS[{"day": 0, "week": 1, "month": 2}[period.value]]
    embeds = build_embeds(title, *range_fn(current_business_date()), color=color, max_pages=5)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@client.tree.command(name="postboards", description="Post the daily, weekly and monthly boards right now")
@app_commands.default_permissions(manage_guild=True)
async def postboards(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await post_all_boards(is_final_time=False)
    await interaction.followup.send("Posted the daily, weekly and monthly boards to their channels.")


@client.tree.command(name="backfill", description="Import older sales from the sales channel")
@app_commands.describe(days="How many days back to scan")
@app_commands.default_permissions(manage_guild=True)
async def backfill(interaction: discord.Interaction, days: app_commands.Range[int, 1, 90]):
    await interaction.response.defer(ephemeral=True)
    channel = client.get_channel(SALES_CHANNEL_ID)
    after = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    async for message in channel.history(after=after, limit=None, oldest_first=True):
        count += await sync_message(message, commit=False)
    db.commit()
    await interaction.followup.send(f"Imported {count} sales from the last {days} days.")


client.run(TOKEN)
