import json
import os
import io
import asyncio
import discord
import chat_exporter

from discord import app_commands
from discord.ext import commands

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

TOKEN = CONFIG["token"]
GUILD_ID = int(CONFIG["guild_id"])
TICKET_CATEGORY_ID = int(CONFIG["ticket_category_id"])
SUPPORT_ROLE_ID = int(CONFIG["support_role_id"])
LOG_CHANNEL_ID = int(CONFIG["log_channel_id"])

TICKETS_DB_FILE = "tickets.json"
ticket_lock = asyncio.Lock()

SAVE_TRANSCRIPTS = bool(CONFIG.get("save_transcripts", False))
TRANSCRIPTS_DIR = CONFIG.get("transcripts_dir", "./transcripts")

TICKET_TYPES = CONFIG.get("ticket_types", ["Support", "Purchase", "Bug Report"])
CATEGORY_BY_TYPE_RAW = CONFIG.get("ticket_category_ids_by_type", {}) or {}
CATEGORY_BY_TYPE = {str(k): int(v) for k, v in CATEGORY_BY_TYPE_RAW.items()}

def save_transcript_to_disk(channel_name: str, html: str) -> str:
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{channel_name}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path

def load_tickets_db() -> dict:
    if not os.path.exists(TICKETS_DB_FILE):
        return {
            "last_ticket_number": 0,
            "open_tickets_by_user": {},
            "tickets_by_channel": {}
        }
    with open(TICKETS_DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)
    db.setdefault("last_ticket_number", 0)
    db.setdefault("open_tickets_by_user", {})
    db.setdefault("tickets_by_channel", {})
    return db

def save_tickets_db(db: dict) -> None:
    with open(TICKETS_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4)

async def get_next_ticket_number() -> int:
    async with ticket_lock:
        db = load_tickets_db()
        db["last_ticket_number"] = int(db.get("last_ticket_number", 0)) + 1
        save_tickets_db(db)
        return db["last_ticket_number"]

def format_ticket_name(n: int) -> str:
    return f"ticket-{n:04d}"

def safe_slug(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-").replace("--", "-")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=".?", intents=intents)

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    try:
        bot.add_view(TicketPanelView())
    except Exception:
        pass
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

def build_ticket_overwrites(guild: discord.Guild, opener: discord.Member) -> dict:
    support_role = guild.get_role(SUPPORT_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    return overwrites

def get_category_for_type(guild: discord.Guild, ticket_type: str):
    cat_id = CATEGORY_BY_TYPE.get(ticket_type, TICKET_CATEGORY_ID)
    ch = guild.get_channel(int(cat_id)) if cat_id else None
    return ch if isinstance(ch, discord.CategoryChannel) else None

def build_ticket_embed(ticket_no: int, opener: discord.Member, ticket_type: str) -> discord.Embed:
    return discord.Embed(
        title=f"🎫 {ticket_type} Ticket #{ticket_no}",
        description=(
            f"Hi {opener.mention}! Explain your issue and a staff member will respond.\n\n"
            "Use the buttons below to manage this ticket."
        ),
        color=0x2b2d31
    )

def build_open_log(channel: discord.TextChannel, opener: discord.Member, ticket_type: str) -> discord.Embed:
    e = discord.Embed(
        title="🟢 Ticket Opened",
        color=0x2b2d31,
        timestamp=discord.utils.utcnow()
    )
    e.add_field(name="Ticket", value=channel.name, inline=True)
    e.add_field(name="Opened by", value=opener.mention, inline=True)
    e.add_field(name="Type", value=ticket_type, inline=True)
    e.add_field(name="Status", value="Open", inline=True)
    e.set_footer(text="Ticket System")
    return e

def build_close_log(channel: discord.TextChannel, opener_id: int, claimed_by_text: str, closed_by: discord.Member, transcript_ok: bool, ticket_type: str) -> discord.Embed:
    e = discord.Embed(
        title="🔴 Ticket Closed",
        color=0x2b2d31,
        timestamp=discord.utils.utcnow()
    )
    e.add_field(name="Ticket", value=channel.name, inline=True)
    e.add_field(name="Opened by", value=f"<@{opener_id}>", inline=True)
    e.add_field(name="Type", value=ticket_type, inline=True)
    e.add_field(name="Claimed by", value=claimed_by_text, inline=True)
    e.add_field(name="Closed by", value=closed_by.mention, inline=True)
    e.add_field(name="Transcript", value="✅ Attached" if transcript_ok else "❌ Failed", inline=False)
    e.set_footer(text="Ticket System")
    return e

async def create_ticket(interaction: discord.Interaction, ticket_type: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    opener = interaction.user

    category = get_category_for_type(guild, ticket_type)
    if not category:
        return await interaction.followup.send("Ticket category is not set correctly for this type.", ephemeral=True)

    db = load_tickets_db()
    user_key = str(opener.id)
    existing_channel_id = db.get("open_tickets_by_user", {}).get(user_key)
    if existing_channel_id:
        ch = guild.get_channel(int(existing_channel_id))
        if isinstance(ch, discord.TextChannel):
            return await interaction.followup.send(f"You already have a ticket: {ch.mention}", ephemeral=True)

    overwrites = build_ticket_overwrites(guild, opener)
    ticket_no = await get_next_ticket_number()

    channel_name = f"{format_ticket_name(ticket_no)}-{safe_slug(ticket_type)}"
    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        reason=f"Ticket #{ticket_no} ({ticket_type}) opened by {opener} ({opener.id})",
    )

    db = load_tickets_db()
    db.setdefault("open_tickets_by_user", {})[str(opener.id)] = str(channel.id)
    db.setdefault("tickets_by_channel", {})[str(channel.id)] = {
        "ticket_number": ticket_no,
        "channel_id": str(channel.id),
        "opener_id": str(opener.id),
        "type": ticket_type,
        "claimed_by": None,
        "status": "open"
    }
    save_tickets_db(db)

    embed = build_ticket_embed(ticket_no, opener, ticket_type)
    await channel.send(
        content=f"{opener.mention}",
        embed=embed,
        view=TicketInsideView(opener_id=opener.id),
    )

    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    if isinstance(log_ch, discord.TextChannel):
        await log_ch.send(embed=build_open_log(channel, opener, ticket_type))

    return await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)

class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        emoji_map = {
            "Support": "🎫",
            "Purchase": "💳",
            "Bug Report": "🐛"
        }
        options = [
            discord.SelectOption(
                label=t,
                value=t,
                emoji=emoji_map.get(t),
            )
            for t in TICKET_TYPES
        ]
        super().__init__(
            placeholder="Select a ticket type…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket:type_select",
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_type = self.values[0]
        await create_ticket(interaction, ticket_type)

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())

class TicketInsideView(discord.ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.blurple, emoji="✅", custom_id="ticket:claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.channel:
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)

        guild = interaction.guild
        channel_id = str(interaction.channel.id)

        support_role = guild.get_role(SUPPORT_ROLE_ID)
        is_staff = support_role in getattr(interaction.user, "roles", []) if support_role else False
        if not is_staff:
            return await interaction.response.send_message("Only support staff can claim tickets.", ephemeral=True)

        db = load_tickets_db()
        ticket = db["tickets_by_channel"].get(channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)

        claimed_by = ticket.get("claimed_by")
        if claimed_by is not None:
            return await interaction.response.send_message(f"This ticket is already claimed by <@{claimed_by}>.", ephemeral=True)

        ticket["claimed_by"] = str(interaction.user.id)
        ticket["status"] = "claimed"
        db["tickets_by_channel"][channel_id] = ticket
        save_tickets_db(db)

        updated_embed = interaction.message.embeds[0].copy() if interaction.message and interaction.message.embeds else discord.Embed(title="🎫 Support Ticket", color=0x2b2d31)
        has_status = any((f.name or "").lower() == "status" for f in updated_embed.fields)
        has_claimed = any((f.name or "").lower() == "claimed by" for f in updated_embed.fields)

        if not has_status:
            updated_embed.add_field(name="Status", value="Claimed", inline=True)
        if not has_claimed:
            updated_embed.add_field(name="Claimed by", value=interaction.user.mention, inline=True)

        await interaction.response.send_message("✅ Ticket claimed.", ephemeral=True)
        try:
            await interaction.message.edit(embed=updated_embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, emoji="🔒", custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.channel:
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)

        guild = interaction.guild
        channel = interaction.channel
        channel_id = str(channel.id)

        support_role = guild.get_role(SUPPORT_ROLE_ID)
        is_staff = support_role in getattr(interaction.user, "roles", []) if support_role else False
        is_opener = interaction.user.id == self.opener_id
        is_admin = interaction.user.guild_permissions.manage_channels

        if not (is_staff or is_opener or is_admin):
            return await interaction.response.send_message("You don’t have permission to close this ticket.", ephemeral=True)

        db = load_tickets_db()
        ticket = db["tickets_by_channel"].get(channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data missing.", ephemeral=True)

        claimed_by = ticket.get("claimed_by")
        user_id = str(interaction.user.id)
        if claimed_by is not None:
            if claimed_by != user_id and not is_admin:
                return await interaction.response.send_message(
                    f"This ticket is claimed by <@{claimed_by}>. Only they (or an admin) can close it.",
                    ephemeral=True
                )

        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Closing ticket in 3 seconds...", ephemeral=True)

        transcript_html = None
        try:
            transcript_html = await chat_exporter.export(
                channel=channel,
                limit=500,
                tz_info="Europe/London",
                bot=interaction.client,
            )
        except Exception as e:
            try:
                await interaction.followup.send(f"Transcript error: {e}", ephemeral=True)
            except Exception:
                pass

        if transcript_html and SAVE_TRANSCRIPTS:
            try:
                save_transcript_to_disk(channel.name, transcript_html)
            except Exception:
                pass

        db = load_tickets_db()
        ticket = db["tickets_by_channel"].get(channel_id) or {}
        ticket_type = ticket.get("type", "Unknown")

        claimed_by_text = f"<@{ticket.get('claimed_by')}>" if ticket.get("claimed_by") else "Not claimed"

        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        close_log = build_close_log(
            channel=channel,
            opener_id=self.opener_id,
            claimed_by_text=claimed_by_text,
            closed_by=interaction.user,
            transcript_ok=bool(transcript_html),
            ticket_type=ticket_type
        )

        if isinstance(log_ch, discord.TextChannel):
            if transcript_html:
                buf = io.BytesIO(transcript_html.encode("utf-8"))
                buf.seek(0)
                log_file = discord.File(buf, filename=f"transcript-{channel.name}.html")
                await log_ch.send(embed=close_log, file=log_file)
            else:
                await log_ch.send(embed=close_log)

        if transcript_html:
            try:
                opener_member = guild.get_member(self.opener_id)
                opener_user = opener_member if opener_member else await interaction.client.fetch_user(self.opener_id)

                buf = io.BytesIO(transcript_html.encode("utf-8"))
                buf.seek(0)
                dm_file = discord.File(buf, filename=f"transcript-{channel.name}.html")

                send_embed = discord.Embed(
                    title="🎫 Ticket Closed",
                    description=(
                        f"Your support ticket **{channel.name}** has been successfully closed.\n\n"
                        "📄 **Transcript**\n"
                        "A full transcript of the conversation is attached for your records."
                    ),
                    color=0x2b2d31
                )
                send_embed.timestamp = discord.utils.utcnow()
                if guild.icon:
                    send_embed.set_thumbnail(url=guild.icon.url)
                send_embed.set_footer(text="Thank you for contacting support")

                await opener_user.send(embed=send_embed, file=dm_file)

            except discord.Forbidden:
                if isinstance(log_ch, discord.TextChannel):
                    await log_ch.send(f"⚠️ Could not DM transcript to <@{self.opener_id}> (DMs closed).")
            except Exception as e:
                if isinstance(log_ch, discord.TextChannel):
                    await log_ch.send(f"⚠️ Failed to DM transcript to <@{self.opener_id}>: {e}")

        db = load_tickets_db()
        db.get("open_tickets_by_user", {}).pop(str(self.opener_id), None)
        db.get("tickets_by_channel", {}).pop(str(channel.id), None)
        save_tickets_db(db)

        await asyncio.sleep(3)
        await channel.delete(reason=f"Ticket closed by {interaction.user} ({interaction.user.id})")

@bot.tree.command(name="panel", description="Post the ticket panel (staff only).")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def panel(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)

    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("You don’t have permission to use this.", ephemeral=True)

    types_list = "\n".join(f"• {t}" for t in TICKET_TYPES)
    embed = discord.Embed(
        title="Support Tickets",
        description=f"Select a ticket type from the dropdown below:\n\n{types_list}",
        color=0x2b2d31
    )

    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

@bot.tree.command(name="forceclose", description="Force close a ticket (works after restarts).")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(channel="Ticket channel to force close (leave empty to use current channel)")
async def forceclose(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This only works in a server.", ephemeral=True)

    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("You don’t have permission to use this.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        return await interaction.followup.send("Please run this in a ticket text channel, or provide a ticket channel.", ephemeral=True)

    channel_id = str(target_channel.id)

    db = load_tickets_db()
    ticket = db.get("tickets_by_channel", {}).get(channel_id)
    if not ticket:
        return await interaction.followup.send("That channel is not in my tickets database.", ephemeral=True)

    opener_id = int(ticket.get("opener_id"))
    ticket_type = ticket.get("type", "Unknown")
    claimed_by_id = ticket.get("claimed_by")
    claimed_by_text = f"<@{claimed_by_id}>" if claimed_by_id else "Not claimed"

    transcript_html = None
    try:
        transcript_html = await chat_exporter.export(
            channel=target_channel,
            limit=500,
            tz_info="Europe/London",
            bot=interaction.client,
        )
    except Exception as e:
        await interaction.followup.send(f"Transcript error: {e}", ephemeral=True)

    if transcript_html and SAVE_TRANSCRIPTS:
        try:
            save_transcript_to_disk(target_channel.name, transcript_html)
        except Exception:
            pass

    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    close_log = build_close_log(
        channel=target_channel,
        opener_id=opener_id,
        claimed_by_text=claimed_by_text,
        closed_by=interaction.user,
        transcript_ok=bool(transcript_html),
        ticket_type=ticket_type
    )

    if isinstance(log_ch, discord.TextChannel):
        if transcript_html:
            buf = io.BytesIO(transcript_html.encode("utf-8"))
            buf.seek(0)
            log_file = discord.File(buf, filename=f"transcript-{target_channel.name}.html")
            await log_ch.send(embed=close_log, file=log_file)
        else:
            await log_ch.send(embed=close_log)

    if transcript_html:
        try:
            opener_member = guild.get_member(opener_id)
            opener_user = opener_member if opener_member else await interaction.client.fetch_user(opener_id)

            buf = io.BytesIO(transcript_html.encode("utf-8"))
            buf.seek(0)
            dm_file = discord.File(buf, filename=f"transcript-{target_channel.name}.html")

            send_embed = discord.Embed(
                title="🎫 Ticket Closed",
                description=(
                    f"Your support ticket **{target_channel.name}** has been force-closed by staff.\n\n"
                    "📄 **Transcript**\n"
                    "A full transcript of the conversation is attached for your records."
                ),
                color=0x2b2d31
            )
            send_embed.timestamp = discord.utils.utcnow()
            if guild.icon:
                send_embed.set_thumbnail(url=guild.icon.url)
            send_embed.set_footer(text="Thank you for contacting support")

            await opener_user.send(embed=send_embed, file=dm_file)

        except discord.Forbidden:
            if isinstance(log_ch, discord.TextChannel):
                await log_ch.send(f"⚠️ Could not DM transcript to <@{opener_id}> (DMs closed).")
        except Exception as e:
            if isinstance(log_ch, discord.TextChannel):
                await log_ch.send(f"⚠️ Failed to DM transcript to <@{opener_id}>: {e}")

    db = load_tickets_db()
    db.get("open_tickets_by_user", {}).pop(str(opener_id), None)
    db.get("tickets_by_channel", {}).pop(channel_id, None)
    save_tickets_db(db)

    await interaction.followup.send(f"✅ Force closing {target_channel.mention} in 3 seconds…", ephemeral=True)
    await asyncio.sleep(3)
    await target_channel.delete(reason=f"Force closed by {interaction.user} ({interaction.user.id})")


bot.run(TOKEN)
