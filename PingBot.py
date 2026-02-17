import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
import pymysql
from discord import app_commands
from discord.ext import commands, tasks


def load_dotenv_file(path: str = ".env"):
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


load_dotenv_file()

TOKEN = require_env("DISCORD_TOKEN")
DB_CONFIG = {
    "host": require_env("DB_HOST"),
    "user": require_env("DB_USER"),
    "password": require_env("DB_PASSWORD"),
    "database": require_env("DB_NAME"),
}
db_port = os.getenv("DB_PORT")
if db_port:
    DB_CONFIG["port"] = int(db_port)

FOOTER_ICON_URL = os.getenv(
    "FOOTER_ICON_URL",
    "https://cdn.discordapp.com/avatars/1139580736685482076/6c8c830942e38ff33418ffbf396c5448",
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

SNOOZE_CHOICES = {
    "1h": 3600,
    "24h": 86400,
    "48h": 172800,
}
AUTO_SNOOZE_SEQUENCE = [600, 3600, 10800]  # 10m, 1h, 3h
AUTO_SNOOZE_REPEAT_SECONDS = 86400  # 24h
AUTO_SNOOZE_MAX_DAYS = 7


def parse_time(time_str):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        amount = int(time_str[:-1])
        unit = time_str[-1].lower()
        return amount * units[unit]
    except Exception:
        return None


def format_timedelta(td: timedelta):
    total_seconds = int(td.total_seconds())
    if total_seconds <= 0:
        return "now"

    parts = []
    for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        value, total_seconds = divmod(total_seconds, div)
        if value > 0:
            parts.append(f"{value}{unit}")

    return "in " + " ".join(parts)


def get_db():
    return pymysql.connect(**DB_CONFIG, autocommit=True)


def as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_auto_snooze_seconds(stage: int) -> int:
    if stage < len(AUTO_SNOOZE_SEQUENCE):
        return AUTO_SNOOZE_SEQUENCE[stage]
    return AUTO_SNOOZE_REPEAT_SECONDS


def ensure_schema():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM pings LIKE 'message_link'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE pings ADD COLUMN message_link VARCHAR(255) NULL")

            cur.execute("SHOW COLUMNS FROM pings LIKE 'snooze_active'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE pings ADD COLUMN snooze_active TINYINT(1) NOT NULL DEFAULT 0")

            cur.execute("SHOW COLUMNS FROM pings LIKE 'snooze_stage'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE pings ADD COLUMN snooze_stage INT NOT NULL DEFAULT 0")

            cur.execute("SHOW COLUMNS FROM pings LIKE 'snooze_started_at'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE pings ADD COLUMN snooze_started_at DATETIME NULL")
    finally:
        conn.close()


class SnoozeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="1 hour", value="1h"),
            discord.SelectOption(label="24 hours", value="24h"),
            discord.SelectOption(label="48 hours", value="48h"),
        ]
        super().__init__(placeholder="Snooze for...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, SnoozeView):
            return

        if interaction.user.id != view.user_id:
            await interaction.response.send_message("Only the reminder owner can snooze this reminder.", ephemeral=True)
            return

        key = self.values[0]
        seconds = SNOOZE_CHOICES.get(key)
        if not seconds:
            await interaction.response.send_message("Invalid snooze duration.", ephemeral=True)
            return

        remind_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pings "
                    "SET remind_at = %s, reminded = 0, snooze_active = 1, snooze_stage = 0, snooze_started_at = NULL "
                    "WHERE id = %s AND user_id = %s",
                    (remind_at, view.reminder_id, view.user_id),
                )
        finally:
            conn.close()

        for child in view.children:
            child.disabled = True

        await interaction.response.edit_message(view=view)
        await interaction.followup.send(f"Snoozed for **{key}**.", ephemeral=True)


class SnoozeView(discord.ui.View):
    def __init__(self, reminder_id: int, user_id: int):
        super().__init__(timeout=604800)  # 7 days
        self.reminder_id = reminder_id
        self.user_id = user_id
        self.add_item(SnoozeSelect())

    @discord.ui.button(label="Don't snooze", style=discord.ButtonStyle.secondary)
    async def no_snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the reminder owner can control this reminder.", ephemeral=True)
            return

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pings SET reminded = 1, snooze_active = 0 WHERE id = %s AND user_id = %s",
                    (self.reminder_id, self.user_id),
                )
        finally:
            conn.close()

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Reminder closed. No snooze set.", ephemeral=True)


@bot.event
async def on_ready():
    ensure_schema()

    try:
        await bot.tree.sync()
        print("Slash commands synced globally.")
    except Exception as e:
        print(f"Sync failed: {e}")

    print(f"Logged in as {bot.user}")
    print(f"Connected to {len(bot.guilds)} guilds.")

    if not check_reminders.is_running():
        check_reminders.start()


@bot.tree.command(name="ping", description="Schedule a reminder")
@app_commands.describe(
    time="Time like 10s, 5m, 2h, 1d",
    message="Optional reminder message",
    snooze="Optional snooze controls when the reminder is sent",
)
@app_commands.choices(
    snooze=[
        app_commands.Choice(name="inactive", value="inactive"),
        app_commands.Choice(name="active", value="active"),
    ]
)
@app_commands.default_permissions()
async def ping(
    interaction: discord.Interaction,
    time: str,
    message: str = None,
    snooze: app_commands.Choice[str] = None,
):
    seconds = parse_time(time)
    if not seconds:
        await interaction.response.send_message("Invalid time format. Use s, m, h, or d.", ephemeral=True)
        return

    remind_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    user_id = interaction.user.id
    channel_id = interaction.channel.id
    snooze_active = 1 if snooze and snooze.value == "active" else 0

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pings (user_id, channel_id, message, remind_at, reminded, created_at, message_link, snooze_active, snooze_stage, snooze_started_at) "
                "VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s)",
                (user_id, channel_id, message, remind_at, datetime.now(timezone.utc), None, snooze_active, 0, None),
            )
            reminder_id = cur.lastrowid

        embed = discord.Embed(
            title="Reminder Set",
            description=f"Will ping {interaction.user.mention} in **{time}**.",
            color=discord.Color.blurple(),
        )
        if message:
            embed.add_field(name="Message", value=message, inline=False)
        footer_text = "PingBot - Active Snooze" if snooze_active else "PingBot"
        embed.set_footer(text=footer_text, icon_url=FOOTER_ICON_URL)

        await interaction.response.send_message(embed=embed)

        response_message = await interaction.original_response()
        message_link = response_message.jump_url

        with conn.cursor() as cur:
            cur.execute("UPDATE pings SET message_link = %s WHERE id = %s", (message_link, reminder_id))
    finally:
        conn.close()


@bot.tree.command(name="reminders", description="Show your pending reminders")
@app_commands.default_permissions()
async def reminders(interaction: discord.Interaction):
    user_id = interaction.user.id
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, message, remind_at FROM pings WHERE user_id = %s AND reminded = 0 ORDER BY remind_at ASC",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        await interaction.response.send_message("You have no active reminders.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    pages = []
    chunk = []

    for index, (id_, msg, remind_at) in enumerate(rows, start=1):
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        delta = remind_at - now
        display_msg = msg if msg else "*(no message)*"
        value = f"{format_timedelta(delta)}\n{display_msg}"
        chunk.append((f"ID: {id_}", value))

        if len(chunk) == 10 or index == len(rows):
            embed = discord.Embed(title="Your Reminders", color=discord.Color.teal())
            for name, field_value in chunk:
                embed.add_field(name=name, value=field_value, inline=False)
            embed.set_footer(text="PingBot", icon_url=FOOTER_ICON_URL)
            pages.append(embed)
            chunk = []

    for embed in pages:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel", description="Cancel a reminder by its ID")
@app_commands.describe(id="The ID of the reminder to cancel")
@app_commands.default_permissions()
async def cancel(interaction: discord.Interaction, id: int):
    user_id = interaction.user.id
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM pings WHERE id = %s AND reminded = 0", (id,))
            row = cur.fetchone()

            if not row:
                await interaction.response.send_message("Reminder not found or already sent.", ephemeral=True)
                return

            if row[0] != user_id:
                await interaction.response.send_message("You can only cancel your own reminders.", ephemeral=True)
                return

            cur.execute("DELETE FROM pings WHERE id = %s", (id,))
    finally:
        conn.close()

    embed = discord.Embed(
        title="Reminder Cancelled",
        description=f"Reminder **#{id}** has been cancelled.",
        color=discord.Color.red(),
    )
    embed.set_footer(text="PingBot", icon_url=FOOTER_ICON_URL)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Show help information")
@app_commands.default_permissions()
async def help_command(interaction: discord.Interaction):
    try:
        with open("help.txt", "r", encoding="utf-8") as f:
            help_text = f.read()

        embed = discord.Embed(title="Help", description=help_text, color=discord.Color.green())
        embed.set_footer(text="PingBot", icon_url=FOOTER_ICON_URL)
        await interaction.response.send_message(embed=embed)
    except FileNotFoundError:
        await interaction.response.send_message("Help file not found!", ephemeral=True)


@tasks.loop(seconds=15)
async def check_reminders():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, channel_id, message, message_link, snooze_active, snooze_stage, snooze_started_at "
                "FROM pings "
                "WHERE reminded = 0 AND remind_at <= UTC_TIMESTAMP()"
            )
            rows = cur.fetchall()

            for id_, user_id, channel_id, message, message_link, snooze_active, snooze_stage, snooze_started_at in rows:
                try:
                    channel = bot.get_channel(int(channel_id))
                    if not channel:
                        continue

                    mention = f"<@{user_id}>"
                    content = f"{message_link}: {mention}" if message_link else mention

                    should_embed = bool(message) or bool(snooze_active)
                    if should_embed:
                        embed = discord.Embed(color=discord.Color.green())
                        if message:
                            embed.add_field(name="Message", value=message, inline=False)

                        footer_text = "PingBot - Active Snooze" if snooze_active else "PingBot"
                        embed.set_footer(text=footer_text, icon_url=FOOTER_ICON_URL)
                        view = SnoozeView(id_, user_id) if snooze_active else None
                        await channel.send(content=content, embed=embed, view=view)
                    else:
                        await channel.send(content=content)

                    if snooze_active:
                        now_utc = datetime.now(timezone.utc)
                        stage = int(snooze_stage or 0)
                        start_at = as_utc(snooze_started_at) or now_utc
                        stop_at = start_at + timedelta(days=AUTO_SNOOZE_MAX_DAYS)
                        next_seconds = get_auto_snooze_seconds(stage)
                        next_remind_at = now_utc + timedelta(seconds=next_seconds)

                        if next_remind_at <= stop_at:
                            cur.execute(
                                "UPDATE pings "
                                "SET remind_at = %s, reminded = 0, snooze_stage = %s, snooze_started_at = %s "
                                "WHERE id = %s",
                                (next_remind_at, stage + 1, start_at, id_),
                            )
                        else:
                            cur.execute(
                                "UPDATE pings SET reminded = 1, snooze_active = 0 WHERE id = %s",
                                (id_,),
                            )
                    else:
                        cur.execute("UPDATE pings SET reminded = 1 WHERE id = %s", (id_,))
                except Exception as e:
                    print(f"Error sending reminder {id_}: {e}")
    finally:
        conn.close()


bot.run(TOKEN)
