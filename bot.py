import asyncio
import logging
import aiohttp
import datetime
import re
import random
import os
import sys
import io
import math
import secrets
import time
import gc
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- EVENT LOOP SETUP ---
# This MUST happen before importing any Pyrogram modules
try:
    import uvloop
    uvloop.install()
except (ImportError, AttributeError):
    pass

try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from pyrogram.enums import ChatAction, ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from config import API_ID, API_HASH, BOT_TOKEN, LOG_CHANNEL, DB_CHANNEL, OWNER_ID, SHORTENER_URL, SHORTENER_API, FORCE_SUB_CHANNEL, AUTO_DELETE_TIME, START_PIC, HIDDEN_OWNERS
from database import db

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# session name
session_name = "OurSharingBot"

bot = Client(
    session_name,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4 # Keep workers low for RAM efficiency
)

scheduler = AsyncIOScheduler()

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# --- Helper Functions ---

def is_owner(user_id):
    """Checks if a user is the primary owner or a hidden owner."""
    return user_id == OWNER_ID or user_id in HIDDEN_OWNERS

# Global aiohttp session
session = None

# User cooldown for anti-spam
user_cooldowns = {}
# Flood control cache
flood_cache = {}

# Batch storage for admins
batch_storage = {}
# Single link storage state
link_storage = {}
# Active broadcasts tracking
active_broadcasts = set()
# Pending confirmation state
pending_confirmation = {}
# Temporary settings state
temp_settings_state = {}
# Channel status cache
channel_status_cache = {}
# Member status cache (Short-lived 2 min)
member_status_cache = {}

async def memory_optimization_task():
    """Periodically clears caches and triggers garbage collection to keep RAM usage low."""
    while True:
        try:
            await asyncio.sleep(300) # Run every 5 minutes
            
            # Clear old cooldowns (older than 1 hour)
            current_time = time.time()
            to_del = [uid for uid, t in user_cooldowns.items() if current_time - t > 3600]
            for uid in to_del: user_cooldowns.pop(uid, None)
            
            # Clear flood cache (older than 1 hour)
            to_del = [uid for uid, t in flood_cache.items() if current_time - t > 3600]
            for uid in to_del: flood_cache.pop(uid, None)
            
            # Trim channel status cache if it gets too large
            if len(channel_status_cache) > 100:
                channel_status_cache.clear()
            
            # Clear old member status cache (older than 2 minutes)
            to_del = [k for k, (t, _) in member_status_cache.items() if current_time - t > 120]
            for k in to_del: member_status_cache.pop(k, None)
            
            # Trigger Garbage Collection
            gc.collect()
            logger.info("Memory optimization complete.")
        except Exception as e:
            logger.error(f"Memory optimization error: {e}")

async def set_ui_commands(client, user_id=None):
    """
    Sets the slash command suggestions based on the user's role.
    If user_id is None, it sets the global default commands.
    """
    # 1. Default Commands (For all Users)
    user_cmds = [
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("myprofile", "👤 View your profile"),
        BotCommand("help", "❓ How to use the bot"),
        BotCommand("id", "🆔 Get your Telegram ID")
    ]
    
    # 2. Admin Commands (User Commands + Admin Tools)
    admin_base_cmds = user_cmds.copy()
    admin_base_cmds.insert(2, BotCommand("link", "🔗 Store file"))
    admin_base_cmds.insert(3, BotCommand("batch", "📦 Create batch"))

    admin_cmds = admin_base_cmds + [
        BotCommand("cancel", "✖️ Cancel operation"),
        BotCommand("settings", "⚙️ Bot settings"),
        BotCommand("stats", "📊 Bot statistics"),
        BotCommand("dbstatus", "📊 Database status"),
        BotCommand("broadcast", "📢 Broadcast message"),
        BotCommand("ping", "🏓 Check latency"),
        BotCommand("users", "👥 User list"),
        BotCommand("admins", "👮 Admin list"),
        BotCommand("banned", "🚫 Banned list"),
        BotCommand("premiumusers", "💎 Premium users"),
        BotCommand("checkpremium", "🔍 Check premium status"),
        BotCommand("ban", "🚫 Ban user"),
        BotCommand("unban", "🔓 Unban user"),
        BotCommand("addpremium", "💎 Add premium"),
        BotCommand("removepremium", "❌ Remove premium")
    ]

    # 3. Owner Commands (Admin Commands + Owner Tools)
    owner_cmds = admin_cmds + [
        BotCommand("addadmin", "👮 Add admin"),
        BotCommand("removeadmin", "🗑️ Remove admin"),
        BotCommand("add_db", "➕ Add secondary DB"),
        BotCommand("smartclean", "🧹 Smart clean"),
        BotCommand("logs", "📄 Get logs"),
        BotCommand("restart", "🔄 Restart bot"),
        BotCommand("maintenance", "🚧 Maintenance mode"),
        BotCommand("deleteall", "🧨 Delete all files")
    ]

    if user_id is None:
        # Set Global Defaults
        await client.set_bot_commands(user_cmds, scope=BotCommandScopeDefault())
        # Also set for Owner and Hidden Owners specifically
        try:
            await client.set_bot_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=OWNER_ID))
            for h_id in HIDDEN_OWNERS:
                try: await client.set_bot_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=h_id))
                except: pass
        except:
            pass
    else:
        # Set specifically for a user (called when they start or role changes)
        try:
            if is_owner(user_id):
                await client.set_bot_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=user_id))
            elif await db.is_admin(user_id, OWNER_ID):
                await client.set_bot_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=user_id))
            else:
                # Revert to default for this user specifically if they were an admin
                await client.delete_bot_commands(scope=BotCommandScopeChat(chat_id=user_id))
        except Exception as e:
            logger.error(f"Error updating commands for {user_id}: {e}")

async def check_cooldown(user_id):
    if is_owner(user_id):
        return True
    
    current_time = datetime.datetime.now().timestamp()
    
    # Simple rate limiting (2 seconds between commands)
    if user_id in user_cooldowns:
        last_time = user_cooldowns[user_id]
        if (current_time - last_time) < 2:
            return False
    user_cooldowns[user_id] = current_time
    
    # Flood detection (more than 5 messages in 5 seconds)
    if user_id not in flood_cache:
        flood_cache[user_id] = []
    
    # Filter out timestamps older than 5 seconds
    flood_cache[user_id] = [ts for ts in flood_cache[user_id] if current_time - ts < 5]
    flood_cache[user_id].append(current_time)
    
    if len(flood_cache[user_id]) > 5:
        return "flood"
    
    return True

async def check_channel_access(client, channel_id, user_id):
    """
    Check if the bot is an admin in the specified channel.
    Caches the result for performance.
    """
    if not channel_id or channel_id == 0 or str(channel_id) == "0":
        return False
        
    # Check cache first
    current_time = time.time()
    cache_key = f"bot_access_{channel_id}"
    
    if cache_key in channel_status_cache:
        ts, status = channel_status_cache[cache_key]
        # Cache successes for 30 minutes, failures for only 10 seconds (aggressive for debugging)
        if status and (current_time - ts < 1800):
            return True
        elif not status and (current_time - ts < 10):
            return False
            
    try:
        # Normalize channel_id
        cid = channel_id
        if isinstance(channel_id, str):
            # Handle string IDs correctly
            if channel_id.startswith("-100") and channel_id[4:].isdigit():
                cid = int(channel_id)
            elif channel_id.startswith("-") and channel_id[1:].isdigit():
                cid = int(channel_id)
            elif channel_id.isdigit():
                cid = int(channel_id)
            elif not channel_id.startswith("@"):
                cid = f"@{channel_id}"
        
        # Try multiple methods to verify access
        try:
            # Method 1: Get Bot's member status
            member = await client.get_chat_member(cid, "me")
            is_admin = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
            if is_admin:
                channel_status_cache[cache_key] = (current_time, True)
                return True
        except Exception as e1:
            logger.debug(f"Method 1 failed for {cid}: {e1}")
            
        try:
            # Method 2: Try to get chat info (if we can get chat info, we are likely in it)
            chat = await client.get_chat(cid)
            # If we reach here, bot is at least in the chat. 
            # For private channels, get_chat only works if bot is admin/member.
            channel_status_cache[cache_key] = (current_time, True)
            return True
        except Exception as e2:
            logger.debug(f"Method 2 failed for {cid}: {e2}")

        # If both methods fail
        channel_status_cache[cache_key] = (current_time, False)
        return False
        
    except Exception as e:
        logger.error(f"Error checking channel {channel_id} access: {e}")
        channel_status_cache[cache_key] = (current_time, False)
        return False

async def get_session():
    global session
    if session is None:
        session = aiohttp.ClientSession()
    return session

async def get_short_link(long_url, user_id=None):
    """
    Generates a short link based on the uploader's role:
    1. If uploader is Admin/Owner: Use Global/Admin shortener settings.
    2. If uploader is Public User: Use their personal shortener if set. 
       If not set, return the long_url (No fallback to global).
    """
    s_url = None
    s_api = None
    
    # Check if uploader is Admin/Owner
    is_admin_uploader = False
    if user_id:
        is_admin_uploader = await db.is_admin(user_id, OWNER_ID)

    if user_id and not is_admin_uploader:
        # Public User: Use personal settings only
        user_data = await db.get_user(user_id)
        if user_data:
            s_url = user_data.get("shortener_url")
            s_api = user_data.get("shortener_api")
        
        # If public user has no shortener, return direct link
        if not s_url or not s_api:
            return long_url
    else:
        # Admin/Owner or no user_id (default to global): Use Global settings
        settings = await db.get_settings()
        if not settings.get("is_shortener_enabled", True):
            return long_url
            
        s_url = settings.get("shortener_url") or SHORTENER_URL
        s_api = settings.get("shortener_api") or SHORTENER_API
    
    if not s_url or not s_api:
        return long_url
    try:
        client_session = await get_session()
        async with client_session.get(f"{s_url}/api?api={s_api}&url={long_url}") as response:
            data = await response.json()
            if data.get("status") == "success":
                return data.get("shortenedUrl")
    except Exception as e:
        logger.error(f"Shortener error: {e}")
    return long_url

async def check_expired_premium():
    try:
        expired_users = await db.get_expired_premium_users()
        async for user in expired_users:
            user_id = user["user_id"]
            await db.set_premium(user_id, False)
            try:
                await bot.send_message(
                    user_id, 
                    "⚠️ **Premium Membership Ended**\n\n"
                    "Your premium status has expired. "
                    "You will now need to verify through shorteners to access files.\n\n"
                    "To renew and continue enjoying direct access, please click the button below!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Renew Premium 💎", callback_data="buy_premium")
                    ]])
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error checking expired premium: {e}")

async def handle_health_check(request):
    return web.Response(text="Bot is Online ✅")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def auto_delete_message(message: Message, delay: int):
    # We skip settings fetch here to make it faster; the delay is already passed
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass # Silently fail if message is already deleted or no permission

async def get_unsubscribed_channels(client, user_id, uploader_id=None):
    # Admins and Owner always bypass global fsub
    is_admin = is_owner(user_id) or await db.is_admin(user_id, OWNER_ID)
    
    unsubscribed = []
    
    # 1. Check Global FSub (Only for regular users)
    if not is_admin:
        settings = await db.get_settings()
        if settings.get("is_force_sub_enabled", True):
            fsub_channels = settings.get("fsub_channels", [])
            if not fsub_channels and FORCE_SUB_CHANNEL and FORCE_SUB_CHANNEL != 0:
                fsub_channels = [FORCE_SUB_CHANNEL]
                
            for channel_id in fsub_channels:
                if not channel_id or channel_id == 0: continue
                if not await is_user_member(client, channel_id, user_id):
                    unsubscribed.append(channel_id)

    # 2. Check Uploader's FSub (Applies to everyone except uploader themselves)
    if uploader_id and user_id != uploader_id:
        uploader_data = await db.get_user(uploader_id)
        if uploader_data:
            user_fsub = uploader_data.get("fsub_channels", [])
            for channel_id in user_fsub:
                if not channel_id or channel_id == 0: continue
                if channel_id in unsubscribed: continue # Already added by global
                if not await is_user_member(client, channel_id, user_id):
                    unsubscribed.append(channel_id)
            
    return unsubscribed

async def is_user_member(client, channel_id, user_id):
    cache_key = f"{channel_id}:{user_id}"
    current_time = time.time()
    
    # Check cache (2 min validity)
    if cache_key in member_status_cache:
        t, status = member_status_cache[cache_key]
        if current_time - t < 120:
            return status

    try:
        if isinstance(channel_id, str) and (channel_id.startswith("-100") or channel_id.isdigit()):
            cid = int(channel_id)
        else:
            cid = channel_id
        
        member = await client.get_chat_member(cid, user_id)
        status = member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
        member_status_cache[cache_key] = (current_time, status)
        return status
    except UserNotParticipant:
        member_status_cache[cache_key] = (current_time, False)
        return False
    except Exception as e:
        logger.error(f"Error checking sub for {channel_id}: {e}")
        return True # Default to true on error to avoid blocking users unnecessarily

async def is_subscribed(client, user_id, uploader_id=None):
    unsubscribed = await get_unsubscribed_channels(client, user_id, uploader_id)
    return len(unsubscribed) == 0

async def get_fsub_buttons(client, unsubscribed_channels, code=None):
    buttons = []
    for cid in unsubscribed_channels:
        try:
            chat = await client.get_chat(cid)
            link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)
            if not link:
                # Try to get a new invite link if bot is admin
                try:
                    link = await client.export_chat_invite_link(cid)
                except:
                    pass
            
            if link:
                buttons.append([InlineKeyboardButton(f"Join {chat.title}", url=link)])
            else:
                buttons.append([InlineKeyboardButton(f"Join Channel (ID: {cid})", url=f"https://t.me/c/{str(cid).replace('-100', '')}/1")])
        except Exception as e:
            logger.error(f"Error getting buttons for {cid}: {e}")
            continue
    
    if buttons:
        callback_data = f"check_fsub_{code}" if code else "check_fsub"
        buttons.append([InlineKeyboardButton("🔄 Check Again", callback_data=callback_data)])
    
    return InlineKeyboardMarkup(buttons)

# --- Admin & Premium Commands ---

@bot.on_message(filters.command("addadmin") & filters.private)
async def add_admin_handler(client, message):
    if not is_owner(message.from_user.id):
        return
    if len(message.command) < 2:
        return await message.reply("Usage: /addadmin [user_id]")
    try:
        user_id = int(message.command[1])
        await db.add_admin(user_id)
        await set_ui_commands(client, user_id)
        await message.reply(f"User {user_id} added as admin.")
    except ValueError:
        await message.reply("User ID must be a number.")

@bot.on_message(filters.command("removeadmin") & filters.private)
async def remove_admin_handler(client, message):
    if not is_owner(message.from_user.id):
        return
    if len(message.command) < 2:
        return await message.reply("Usage: /removeadmin [user_id]")
    try:
        user_id = int(message.command[1])
        await db.remove_admin(user_id)
        await set_ui_commands(client, user_id)
        await message.reply(f"User {user_id} removed from admin list.")
    except ValueError:
        await message.reply("User ID must be a number.")

@bot.on_message(filters.command("addpremium") & filters.private)
async def add_premium_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    # Usage: /addpremium [user_id] [time]
    # Time formats: 1d, 7d, 30d, 1h, etc.
    if len(message.command) < 2:
        return await message.reply("Usage: `/addpremium [user_id] [duration]`\n\nExample: `/addpremium 123456789 30d` (for 30 days)")
    
    try:
        user_id = int(message.command[1])
        duration_str = message.command[2] if len(message.command) > 2 else "permanent"
        
        expiry_time = None
        if duration_str != "permanent":
            match = re.match(r"(\d+)([dhms])", duration_str.lower())
            if not match:
                return await message.reply("Invalid duration format! Use `1d`, `7d`, `1h`, etc.")
            
            value, unit = int(match.group(1)), match.group(2)
            # Use global IST
            now = datetime.datetime.now(IST)
            if unit == 'd':
                expiry_time = now + datetime.timedelta(days=value)
            elif unit == 'h':
                expiry_time = now + datetime.timedelta(hours=value)
            elif unit == 'm':
                expiry_time = now + datetime.timedelta(minutes=value)
            elif unit == 's':
                expiry_time = now + datetime.timedelta(seconds=value)
        
        await db.set_premium(user_id, True, expiry_time)
        
        expiry_msg = f"until `{expiry_time.strftime('%Y-%m-%d %H:%M:%S IST')}`" if expiry_time else "permanently"
        await message.reply(f"User `{user_id}` granted premium status {expiry_msg}.")
        
        try:
            await bot.send_message(user_id, f"Congratulations! You have been granted Premium status {expiry_msg}.\nYou can now access files without shorteners.")
        except Exception:
            pass
            
    except ValueError:
        await message.reply("User ID must be a number.")
    except Exception as e:
        logger.error(f"Add premium error: {e}")
        await message.reply("An error occurred while adding premium.")

@bot.on_message(filters.command("removepremium") & filters.private)
async def remove_premium_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    if len(message.command) < 2:
        return await message.reply("Usage: /removepremium [user_id]")
    try:
        user_id = int(message.command[1])
        await db.set_premium(user_id, False)
        await message.reply(f"User {user_id} premium status removed.")
    except ValueError:
        await message.reply("User ID must be a number.")

@bot.on_message(filters.command("checkpremium") & filters.private)
async def check_premium_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    if len(message.command) < 2:
        return await message.reply("Usage: `/checkpremium [user_id]`")
    
    try:
        user_id = int(message.command[1])
        user_data = await db.get_user(user_id)
        if not user_data:
            return await message.reply(f"User `{user_id}` not found in database.")
        
        is_premium = await db.is_premium(user_id, user=user_data)
        status = "Premium ✅" if is_premium else "Free User ❌"
        
        text = f"**Premium Status Check**\n\n"
        text += f"User ID: `{user_id}`\n"
        text += f"Status: `{status}`\n"
        
        if user_data.get("premium_expiry"):
            expiry = user_data.get("premium_expiry")
            # Convert UTC to IST for display
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            ist_expiry = expiry.astimezone(IST)
            text += f"Expires: `{ist_expiry.strftime('%Y-%m-%d %H:%M:%S IST')}`"
        elif user_data.get("is_premium"):
            text += "Expires: `Never (Permanent)`"
            
        await message.reply(text)
    except ValueError:
        await message.reply("Invalid User ID.")

@bot.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client, message):
    user_id = message.from_user.id
    if not await db.is_admin(user_id, OWNER_ID):
        return
    if not message.reply_to_message:
        return await message.reply("Reply to a message to broadcast it.", reply_markup=CANCEL_MARKUP)
    
    # Optional: Send with Pin or without notification
    is_pin = "-pin" in message.text.lower()
    
    msg = await message.reply("🚀 **Broadcast started!**", reply_markup=CANCEL_MARKUP)
    active_broadcasts.add(user_id)
    
    users = await db.get_all_users()
    done = 0
    failed = 0
    
    async for user in users:
        # Check if user cancelled
        if user_id not in active_broadcasts:
            await msg.edit(f"⛔ **Broadcast Cancelled!**\n\nSuccess: {done}\nFailed: {failed}")
            return
            
        try:
            sent = await message.reply_to_message.copy(user["user_id"])
            if is_pin:
                try:
                    await sent.pin(disable_notification=False)
                except:
                    pass
            done += 1
            await asyncio.sleep(0.5) # Increased delay to avoid flooding and bans
        except Exception:
            failed += 1
            
    # Cleanup after completion
    if user_id in active_broadcasts:
        active_broadcasts.remove(user_id)
            
    await msg.edit(f"✅ **Broadcast Completed!**\n\nTotal: {done + failed}\nSuccess: {done}\nFailed: {failed}")
    gc.collect() # Free memory after heavy operation

@bot.on_message(filters.command("users") & filters.private)
async def users_list_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    msg = await message.reply("Fetching user data...")
    users = await db.get_all_users()
    
    user_data = "User ID, Status\n"
    async for user in users:
        is_prem = "Premium" if user.get("is_premium") else "Free"
        user_data += f"{user['user_id']}, {is_prem}\n"
    
    if len(user_data) < 4000:
        await msg.edit(f"**Total Users List**\n\n`{user_data}`")
    else:
        # If list is too long, send as a file
        bio = io.BytesIO(user_data.encode())
        bio.name = "users_list.csv"
        await message.reply_document(bio, caption="Total users list exported as CSV.")
        await msg.delete()
    
    gc.collect() # Free memory after list generation

@bot.on_message(filters.command("stats") & filters.private)
async def stats_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    # Humanize: Show status
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    
    users_count = await db.total_users_count()
    files_count = await db.total_files_count()
    
    # Calculate total individual files in batches
    batch_files_pipeline = [
        {"$match": {"is_batch": True}},
        {"$project": {"count": {"$size": "$file_ids"}}},
        {"$group": {"_id": None, "total": {"$sum": "$count"}}}
    ]
    batch_files_result = await db._primary_files.aggregate(batch_files_pipeline).to_list(1)
    total_batch_files = batch_files_result[0]["total"] if batch_files_result else 0
    
    single_files_count = await db._primary_files.count_documents({"is_batch": {"$ne": True}})
    total_actual_files = single_files_count + total_batch_files

    premium_count = await db._users.count_documents({"is_premium": True})
    banned_count = await db._users.count_documents({"is_banned": True})
    admins_count = await db._admins.count_documents({}) + 1 # +1 for Owner
    
    # Calculate Database size (approximate)
    try:
        db_stats = await db._db.command("dbStats")
        storage_size = db_stats.get("storageSize", 0) / (1024 * 1024) # MB
    except:
        storage_size = 0

    await message.reply(
        "📊 **Bᴏᴛ Sᴛᴀᴛɪsᴛɪᴄs**\n\n"
        f"🤖 **Bᴏᴛ Sᴛᴀᴛᴜs:** `Online ✅`\n"
        f"👤 **Usᴇʀs Dᴀᴛᴀ:**\n"
        f"• Tᴏᴛᴀʟ: `{users_count}`\n"
        f"• Pʀᴇᴍɪᴜᴍ: `{premium_count}`\n"
        f"• Bᴀɴɴᴇᴅ: `{banned_count}`\n"
        f"• Aᴅᴍɪɴs: `{admins_count}`\n\n"
        f"📁 **Fɪʟᴇs Dᴀᴛᴀ:**\n"
        f"• Tᴏᴛᴀʟ Lɪɴᴋs: `{files_count}`\n"
        f"• Sɪɴɢʟᴇ: `{single_files_count}`\n"
        f"• Bᴀᴛᴄʜ: `{files_count - single_files_count}`\n"
        f"• Tᴏᴛᴀʟ Sᴛᴏʀᴇᴅ: `{total_actual_files}`\n\n"
        f"🗄️ **Dᴀᴛᴀʙᴀsᴇ Sɪᴢᴇ:** `{storage_size:.2f} MB`"
    )

@bot.on_message(filters.command("deleteall") & filters.private)
async def delete_all_files_handler(client, message):
    user_id = message.from_user.id
    if user_id != OWNER_ID:
        return
    # This is a dangerous command, so we ask for confirmation
    if len(message.command) < 2 or message.command[1] != "confirm":
        pending_confirmation[user_id] = "delete_all"
        return await message.reply("⚠️ **Warning:** This will delete ALL file links from the database. This action cannot be undone.\n\nType `/deleteall confirm` to proceed or `/cancel` to stop.")
    
    if user_id in pending_confirmation:
        del pending_confirmation[user_id]
        
    await db.delete_all_files()
    await message.reply("✅ All file links have been deleted from the database.")

@bot.on_message(filters.command("premiumusers") & filters.private)
async def premium_users_list_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    msg = await message.reply("Fetching premium users list...")
    premium_users = await db.get_all_premium_users()
    
    text = "**Premium Users List**\n\n"
    count = 0
    async for user in premium_users:
        count += 1
        user_id = user["user_id"]
        expiry = user.get("premium_expiry")
        expiry_str = expiry.strftime('%Y-%m-%d %H:%M:%S IST') if expiry else "Permanent"
        text += f"{count}. ID: `{user_id}` | Expiry: `{expiry_str}`\n"
        
        # Split message if too long
        if len(text) > 3500:
            await message.reply(text)
            text = ""
            
    if count == 0:
        await msg.edit("No premium users found.")
    else:
        await msg.edit(text if text else "End of list.")

@bot.on_message(filters.command("admins") & filters.private)
async def admins_list_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    msg = await message.reply("Fetching admins list...")
    admins = await db.get_all_admins()
    
    text = "**Bot Admins List**\n\n"
    text += f"1. ID: `{OWNER_ID}` (Owner)\n"
    count = 1
    async for admin in admins:
        user_id = admin["user_id"]
        if user_id == OWNER_ID:
            continue
        count += 1
        text += f"{count}. ID: `{user_id}`\n"
        
    await msg.edit(text)

@bot.on_message(filters.command("banned") & filters.private)
async def banned_list_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    
    msg = await message.reply("Fetching banned users list...")
    banned_users = db._users.find({"is_banned": True})
    
    text = "**Banned Users List**\n\n"
    count = 0
    async for user in banned_users:
        count += 1
        text += f"{count}. ID: `{user['user_id']}`\n"
        
    if count == 0:
        await msg.edit("No users are currently banned.")
    else:
        await msg.edit(text)

@bot.on_message(filters.command("ban") & filters.private)
async def ban_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    if len(message.command) < 2:
        return await message.reply("Usage: `/ban [user_id]`")
    
    try:
        user_id = int(message.command[1])
        if user_id == OWNER_ID:
            return await message.reply("You cannot ban the Owner!")
        await db.ban_user(user_id)
        await message.reply(f"User `{user_id}` has been banned.")
    except ValueError:
        await message.reply("Invalid User ID.")

@bot.on_message(filters.command("unban") & filters.private)
async def unban_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return
    if len(message.command) < 2:
        return await message.reply("Usage: `/unban [user_id]`")
    
    try:
        user_id = int(message.command[1])
        await db.unban_user(user_id)
        await message.reply(f"User `{user_id}` has been unbanned.")
    except ValueError:
        await message.reply("Invalid User ID.")

@bot.on_message(filters.command("myprofile") & filters.private)
async def my_profile_handler(client, message):
    user_id = message.from_user.id
    
    # --- Force Subscription Check ---
    unsubscribed = await get_unsubscribed_channels(client, user_id)
    if unsubscribed:
        reply_markup = await get_fsub_buttons(client, unsubscribed)
        return await message.reply(
            "**You must join our channels to use this bot!**\n\n"
            "Please join the channels below and click 'Check Again'.\n\n"
            "---\n**© @NovaMultiFlix & @ATxNovaOfficial**",
            reply_markup=reply_markup
        )

    # Humanize: Show status
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    
    user_data = await db.get_user(user_id)
    is_premium = await db.is_premium(user_id)
    is_admin = await db.is_admin(user_id, OWNER_ID)
    
    status = "Admin" if is_admin else ("Premium" if is_premium else "Free User")
    
    text = (
        "👤 **Yᴏᴜʀ Pʀᴏғɪʟᴇ**\n\n"
        f"🆔 **Usᴇʀ ID:** `{user_id}`\n"
        f"🛡️ **Sᴛᴀᴛᴜs:** `{status}`\n"
    )
    
    if is_premium and user_data and user_data.get("premium_expiry"):
        expiry = user_data.get("premium_expiry")
        text += f"⏳ **Exᴘɪʀᴇs:** `{expiry.strftime('%Y-%m-%d %H:%M:%S IST')}`\n"
    elif is_premium:
        text += "⏳ **Exᴘɪʀᴇs:** `Permanent`\n"
        
    text += "\n---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
    await message.reply(text)

@bot.on_message(filters.command("help") & filters.private)
async def help_handler(client, message):
    user_id = message.from_user.id
    
    # --- Force Subscription Check ---
    unsubscribed = await get_unsubscribed_channels(client, user_id)
    if unsubscribed:
        reply_markup = await get_fsub_buttons(client, unsubscribed)
        return await message.reply(
            "**You must join our channels to use this bot!**\n\n"
            "Please join the channels below and click 'Check Again'.\n\n"
            "---\n**© @NovaMultiFlix & @ATxNovaOfficial**",
            reply_markup=reply_markup
        )

    # Humanize: Show status
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    
    is_admin = await db.is_admin(message.from_user.id, OWNER_ID)
    is_owner_user = is_owner(message.from_user.id)
    
    text = "❓ **Bᴏᴛ Hᴇʟᴘ Mᴇɴᴜ**\n\n"
    text += "**◈ Bᴀsɪᴄ Cᴏᴍᴍᴀɴᴅs ◈**\n"
    text += "• /start - Start the bot\n"
    text += "• /myprofile - Check your profile\n"
    text += "• /help - Show this menu\n"
    text += "• /id - Get your Telegram ID\n"
    
    if is_admin:
        text += "\n**◈ Aᴅᴍɪɴ Cᴏᴍᴍᴀɴᴅs ◈**\n"
        text += "• /link - Store file & get link\n"
        text += "• /batch - Create manual/range batch\n"
        text += "• /cancel - Cancel current operation\n"
        text += "• /settings - Bot settings menu\n"
        text += "• /stats - Bot statistics\n"
        text += "• /dbstatus - Database status\n"
        text += "• /broadcast - Message all users\n"
        text += "• /ping - Check bot latency\n"
        text += "• /users - List all bot users\n"
        text += "• /admins - List bot admins\n"
        text += "• /banned - List banned users\n"
        text += "• /premiumusers - List premium users\n"
        text += "• /checkpremium [id] - Check premium status\n"
        text += "• /ban [id] - Ban a user\n"
        text += "• /unban [id] - Unban a user\n"
        text += "• /addpremium [id] - Add premium\n"
        text += "• /removepremium [id] - Remove premium\n"
        
    if is_owner_user:
        text += "\n**◈ Oᴡɴᴇʀ Cᴏᴍᴍᴀɴᴅs ◈**\n"
        text += "• /addadmin [id] - Add bot admin\n"
        text += "• /removeadmin [id] - Remove admin\n"
        text += "• /add_db [uri] - Add secondary DB\n"
        text += "• /smartclean - Auto database cleanup\n"
        text += "• /logs - Get bot log file\n"
        text += "• /restart - Restart bot process\n"
        text += "• /maintenance - Toggle maintenance\n"
        text += "• /deleteall - Wipe ALL file records\n"
    
    text += "\n---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
    await message.reply(text)

@bot.on_message(filters.command("logs") & filters.private)
async def send_logs_handler(client, message):
    if not is_owner(message.from_user.id):
        return
    # Humanize: Show status
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    
    log_file = "bot.log"
    if os.path.exists(log_file):
        await message.reply_document(log_file, caption="Here are the bot logs.")
    else:
        # If no file exists, send the last few lines from memory/stdout if possible, 
        # but usually, we just say it's not found.
        await message.reply("Log file not found. Ensure logging is writing to a file.")

@bot.on_message(filters.command("maintenance") & filters.private)
async def toggle_maintenance_handler(client, message):
    if not is_owner(message.from_user.id):
        return
    settings = await db.get_settings()
    new_val = not settings.get("is_maintenance_mode", False)
    await db.update_setting("is_maintenance_mode", new_val)
    status = "Enabled ✅" if new_val else "Disabled ❌"
    await message.reply(f"**Maintenance Mode:** {status}\n\nWhen enabled, only admins can access files.")

@bot.on_message(filters.command("restart") & filters.private)
async def restart_bot_handler(client, message):
    if not is_owner(message.from_user.id):
        return
    await message.reply("🔄 **Restarting bot...**\n✅ **Bot successfully restarted.**\n\nAll systems active.")
    os.execl(sys.executable, sys.executable, *sys.argv)

@bot.on_message(filters.command("id") & (filters.channel | filters.group | filters.private))
async def get_id_handler(client, message):
    # Humanize: Show status
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.reply(f"The ID of this chat is: `{message.chat.id}`")

@bot.on_message(filters.command("ping") & filters.private)
async def ping_handler(client, message):
    start_time = datetime.datetime.now()
    msg = await message.reply("🏓 **Pinging...**")
    end_time = datetime.datetime.now()
    ping = (end_time - start_time).total_seconds() * 1000
    await msg.edit(f"🏓 **Pong!**\n\n**Response Time:** `{ping:.2f} ms`\n**Bot Status:** `Online ✅`")

# --- Helper for UI Navigation ---
CANCEL_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    user_id = message.from_user.id
    
    # Check context before clearing
    state = temp_settings_state.get(user_id)
    is_global_setting = state is not None
    is_batch_or_link = user_id in batch_storage or user_id in link_storage
    is_broadcast = user_id in active_broadcasts
    
    # Helper to delete bot's tracked messages
    async def cleanup_bot_msgs(msgs):
        for msg_id in msgs:
            try: await client.delete_messages(message.chat.id, msg_id)
            except: pass

    cancelled = False
    if user_id in batch_storage:
        await cleanup_bot_msgs(batch_storage[user_id].get("bot_msgs", []))
        del batch_storage[user_id]
        cancelled = True
        
    if user_id in link_storage:
        await cleanup_bot_msgs(link_storage[user_id])
        del link_storage[user_id]
        cancelled = True
        
    if user_id in active_broadcasts:
        active_broadcasts.remove(user_id)
        cancelled = True
        
    if user_id in pending_confirmation:
        del pending_confirmation[user_id]
        cancelled = True
        
    if user_id in temp_settings_state:
        del temp_settings_state[user_id]
        cancelled = True
        
    if cancelled:
        await message.reply("✅ **Operation cancelled.**")
        
        # Smart Return Navigation
        if is_global_setting:
            # Handle sub-menus for global settings
            if state == "awaiting_db_uri":
                await message.reply(
                    "🗄️ **Database Management Hub**\n\nMonitor and optimize your MongoDB clusters from here.",
                    reply_markup=await get_db_management_keyboard()
                )
            elif state == "awaiting_fsub_add":
                await message.reply(
                    "📢 **Force Subscription Management**\n\nAdd or remove channels that users must join to access files.",
                    reply_markup=await get_fsub_keyboard()
                )
            else:
                await settings_handler(client, message)
        elif is_batch_or_link or is_broadcast:
            await send_start_message(client, message, user_id)
        else:
            await send_start_message(client, message, user_id)
    else:
        await message.reply("❌ **Nothing to cancel!**")
        await send_start_message(client, message, user_id)
    
    # Auto-delete for a clean chat
    try: await message.delete()
    except: pass

# --- Premium Info Handlers ---

PREMIUM_TEXT = """
**🌟 Nova Sharing Bot Premium**

Upgrade to Premium and enjoy these exclusive features:
✅ **No URL Shorteners**: Get direct access to all files instantly.
✅ **No Advertisements**: A clean and fast experience.
✅ **High Speed Downloads**: Access files at maximum speed.
✅ **Priority Support**: Get help faster if you need it.

**💰 Pricing:**
• **Monthly Plan**: ₹39 / month

Click the button below to contact our admin and buy your premium membership!

---
**© @NovaMultiFlix & @ATxNovaOfficial**
"""

async def buy_premium_callback(client, callback_query: CallbackQuery):
    await callback_query.answer()
    await callback_query.message.edit_text(
        PREMIUM_TEXT,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Contact Admin to Buy 💳", url="https://t.me/NovaMultiFlix")
        ], [
            InlineKeyboardButton("🔙 Back", callback_data="close_premium")
        ]])
    )

async def close_premium_callback(client, callback_query: CallbackQuery):
    await callback_query.answer()
    await callback_query.message.delete()

# --- Settings Handlers ---

async def get_fsub_keyboard():
    settings = await db.get_settings()
    fsub_channels = settings.get("fsub_channels", [])
    
    keyboard = []
    for channel_id in fsub_channels:
        try:
            chat = await bot.get_chat(channel_id)
            keyboard.append([
                InlineKeyboardButton(f"❌ {chat.title}", callback_data=f"remove_fsub_{channel_id}")
            ])
        except Exception:
            keyboard.append([
                InlineKeyboardButton(f"❌ ID: {channel_id}", callback_data=f"remove_fsub_{channel_id}")
            ])
            
    keyboard.append([InlineKeyboardButton("➕ Add Channel", callback_data="add_fsub")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Settings", callback_data="back_to_main_settings")])
    return InlineKeyboardMarkup(keyboard)

async def get_db_management_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Storage Status", callback_data="db_stats_view"),
         InlineKeyboardButton("➕ Add Cluster", callback_data="db_add_cluster")],
        [InlineKeyboardButton("🚀 Optimize", callback_data="db_smart_optimize")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def get_settings_keyboard():
    settings = await db.get_settings()

async def get_settings_keyboard():
    settings = await db.get_settings()

    shortener_text = "🟢 Gʟᴏʙᴀʟ Sʜᴏʀᴛᴇɴᴇʀ" if settings.get("is_shortener_enabled", True) else "🔴 Gʟᴏʙᴀʟ Sʜᴏʀᴛᴇɴᴇʀ"
    force_sub_text = "🔔 Fᴏʀᴄᴇ Sᴜʙ" if settings.get("is_force_sub_enabled", True) else "🔕 Fᴏʀᴄᴇ Sᴜʙ"
    auto_delete_text = "🗑️ Aᴜᴛᴏ Dᴇʟᴇᴛᴇ" if settings.get("is_auto_delete_enabled", True) else "💾 Aᴜᴛᴏ Dᴇʟᴇᴛᴇ"
    protect_content_text = "🛡️ Cᴏɴᴛᴇɴᴛ Pʀᴏᴛᴇᴄᴛ" if settings.get("is_protect_content_enabled", True) else "🔓 Cᴏɴᴛᴇɴᴛ Pʀᴏᴛᴇᴄᴛ"

    # Core Toggles
    keyboard = [
        [InlineKeyboardButton(shortener_text, callback_data="toggle_shortener")],
        [InlineKeyboardButton(force_sub_text, callback_data="toggle_force_sub"),
         InlineKeyboardButton(auto_delete_text, callback_data="toggle_auto_delete")],
        [InlineKeyboardButton(protect_content_text, callback_data="toggle_protect_content")]
    ]

    # Management Hubs
    keyboard.append([
        InlineKeyboardButton("🗄️ Dᴀᴛᴀʙᴀsᴇ Hᴜʙ", callback_data="manage_db_hub"),
        InlineKeyboardButton("📢 FSᴜʙ Sᴇᴛᴜᴘ", callback_data="manage_fsub")
    ])

    # Core Configurations
    keyboard.append([
        InlineKeyboardButton("🖼️ Sᴛᴀʀᴛ Pɪᴄ", callback_data="set_start_pic"),
        InlineKeyboardButton("📝 Sᴛᴀʀᴛ Tᴇxᴛ", callback_data="set_start_text")
    ])

    keyboard.append([
        InlineKeyboardButton("🔗 Sʜᴏʀᴛ URL", callback_data="set_short_url"),
        InlineKeyboardButton("🔑 Sʜᴏʀᴛ API", callback_data="set_short_api"),
        InlineKeyboardButton("⏳ Dᴇʟ Tɪᴍᴇ", callback_data="set_auto_delete_time")
    ])

    # Channels
    keyboard.append([
        InlineKeyboardButton("💾 DB Cʜᴀɴɴᴇʟ", callback_data="set_db_id"),
        InlineKeyboardButton("📄 Lᴏɢ Cʜᴀɴɴᴇʟ", callback_data="set_log_id")
    ])
    
    keyboard.append([InlineKeyboardButton("🔙 Bᴀᴄᴋ ᴛᴏ Sᴛᴀʀᴛ", callback_data="back_to_start"),
                     InlineKeyboardButton("❌ Cʟᴏsᴇ", callback_data="close_settings")])
    return InlineKeyboardMarkup(keyboard)

async def get_settings_text():
    settings = await db.get_settings()
    s_url = settings.get("shortener_url") or "Not Set"
    fsub_count = len(settings.get("fsub_channels", []))
    del_time = settings.get("auto_delete_time", 600)
    db_ch = settings.get("db_channel") or "Default"
    log_ch = settings.get("log_channel") or "Disabled"
    
    # Using safe aesthetic characters for maximum mobile compatibility
    return (
        "⚙️ **Bᴏᴛ Sᴇᴛᴛɪɴɢs Pᴀɴᴇʟ**\n\n"
        "**◈ Cᴏɴғɪɢᴜʀᴀᴛɪᴏɴ Sᴛᴀᴛᴜs ◈**\n"
        f"• **Sʜᴏʀᴛᴇɴᴇʀ:** `{s_url}`\n"
        f"• **Fᴏʀᴄᴇ Sᴜʙ:** `{fsub_count} Channels`\n"
        f"• **Dᴇʟᴇᴛᴇ Tɪᴍᴇ:** `{del_time}s`\n"
        f"• **Dᴀᴛᴀʙᴀsᴇ:** `{db_ch}`\n"
        f"• **Lᴏɢs:** `{log_ch}`\n\n"
        "**Usᴇ ʙᴜᴛᴛᴏɴs ʙᴇʟᴏᴡ ᴛᴏ ᴍᴏᴅɪғʏ:**"
    )

@bot.on_message(filters.command("settings") & filters.private)
async def settings_handler(client, message):
    if not await db.is_admin(message.from_user.id, OWNER_ID):
        return await message.reply("Only admins can access settings.")
    
    await message.reply(
        await get_settings_text(),
        reply_markup=await get_settings_keyboard()
    )

@bot.on_message(filters.private & filters.text & filters.create(lambda _, __, m: m.from_user.id in temp_settings_state and not m.text.startswith("/")))
async def handle_settings_update(client, message):
    user_id = message.from_user.id
    state = temp_settings_state.get(user_id)
    
    if state == "awaiting_short_url":
        url = message.text.strip().rstrip("/")
        await db.update_setting("shortener_url", url)
        del temp_settings_state[user_id]
        await message.reply(f"Shortener URL updated to: `{url}`", reply_markup=await get_settings_keyboard())
        
    elif state == "awaiting_short_api":
        api = message.text.strip()
        await db.update_setting("shortener_api", api)
        del temp_settings_state[user_id]
        await message.reply(f"Shortener API updated successfully.", reply_markup=await get_settings_keyboard())

    elif state == "awaiting_log_id":
        log_id = message.text.strip()
        if (log_id.startswith("-") and log_id[1:].isdigit()) or log_id.isdigit():
            log_id = int(log_id)
        # Support usernames
        await db.update_setting("log_channel", log_id)
        del temp_settings_state[user_id]
        await message.reply(f"Log Channel updated to: `{log_id}`", reply_markup=await get_settings_keyboard())

    elif state == "awaiting_db_id":
        db_id = message.text.strip()
        if (db_id.startswith("-") and db_id[1:].isdigit()) or db_id.isdigit():
            db_id = int(db_id)
        # Support usernames
        await db.update_setting("db_channel", db_id)
        del temp_settings_state[user_id]
        await message.reply(f"Database Channel updated to: `{db_id}`", reply_markup=await get_settings_keyboard())

    elif state == "awaiting_fsub_add":
        fsub_id = message.text.strip()
        if fsub_id.startswith("-") and fsub_id[1:].isdigit() or fsub_id.isdigit():
            fsub_id = int(fsub_id)
            await db.add_fsub_channel(fsub_id)
            del temp_settings_state[user_id]
            await message.reply(f"Force Sub Channel `{fsub_id}` added successfully.", reply_markup=await get_fsub_keyboard())
        else:
            await message.reply("Invalid ID format. Please send a valid numeric ID (e.g., -1001234567890).")

    elif state == "awaiting_auto_delete_time":
        time_str = message.text.strip()
        if time_str.isdigit():
            seconds = int(time_str)
            await db.update_setting("auto_delete_time", seconds)
            del temp_settings_state[user_id]
            await message.reply(f"Auto-Delete Time updated to: `{seconds}` seconds ({seconds // 60} minutes).", reply_markup=await get_settings_keyboard())
        else:
            await message.reply("Invalid input. Please send a numeric value in seconds (e.g., 600 for 10 minutes).")

    elif state == "awaiting_start_pic":
        pic_url = message.text.strip()
        if pic_url.startswith(("http://", "https://")):
            await db.update_setting("start_pic", pic_url)
            del temp_settings_state[user_id]
            await message.reply(f"Start Thumbnail updated successfully!", reply_markup=await get_settings_keyboard())
        else:
            await message.reply("Invalid URL. Please send a valid HTTP/HTTPS link to an image.")

    elif state == "awaiting_start_text":
        start_text = message.text.strip()
        await db.update_setting("start_text", start_text)
        del temp_settings_state[user_id]
        await message.reply(f"Start Text updated successfully!", reply_markup=await get_settings_keyboard())

    elif state == "awaiting_db_uri":
        uri = message.text.strip()
        if not uri.startswith("mongodb"):
            return await message.reply("❌ **Invalid URI:** Must start with `mongodb://` or `mongodb+srv://`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="manage_db_hub")]]))
        
        msg = await message.reply("⏳ **Verifying connection...**")
        try:
            import motor.motor_asyncio
            test_client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
            await test_client.server_info()
            
            await db.add_db_uri(uri)
            del temp_settings_state[user_id]
            await msg.edit(f"✅ **Database added successfully!**", reply_markup=await get_db_management_keyboard())
            asyncio.create_task(update_storage_stats())
        except Exception as e:
            await msg.edit(f"❌ **Connection Failed:**\n`{str(e)}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="manage_db_hub")]]))

@bot.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if await db.is_banned(user_id):
        return await callback_query.answer("🚫 You are banned.", show_alert=True)

    # --- Public Callbacks (Available to everyone) ---
    
    if data == "buy_premium":
        await buy_premium_callback(client, callback_query)
        return
    
    if data == "manage_db_hub":
        if not await db.is_admin(user_id, OWNER_ID):
            return await callback_query.answer("🚫 Access denied. Admin only.", show_alert=True)
        await callback_query.answer()
        text = (
            "🗄️ **Dᴀᴛᴀʙᴀsᴇ Hᴜʙ**\n\n"
            "**◈ Dᴀᴛᴀʙᴀsᴇ Mᴀɴᴀɢᴇᴍᴇɴᴛ ◈**\n"
            "Mᴏɴɪᴛᴏʀ ᴀɴᴅ ᴏᴘᴛɪᴍɪᴢᴇ ʏᴏᴜʀ MᴏɴɢᴏDB ᴄʟᴜsᴛᴇʀs ғʀᴏᴍ ʜᴇʀᴇ."
        )
        await callback_query.message.edit_text(text, reply_markup=await get_db_management_keyboard())
        return

    if data == "db_stats_view":
        await callback_query.answer("📊 Fetching stats...")
        await db_status_handler(client, callback_query)
        # We don't delete here anymore as db_status_handler sends a NEW message
        return

    if data == "db_add_cluster":
        if not is_owner(user_id):
            return await callback_query.answer("🚫 Access denied. Owner only.", show_alert=True)
        await callback_query.answer()
        temp_settings_state[user_id] = "awaiting_db_uri"
        await callback_query.message.edit_text(
            "➕ **Add New MongoDB Cluster**\n\n"
            "Please send the MongoDB URI of your new cluster.\n"
            "Example: `mongodb+srv://user:pass@cluster.mongodb.net/`",
            reply_markup=CANCEL_MARKUP
        )
        return

    if data == "db_smart_optimize":
        if user_id != OWNER_ID:
            return await callback_query.answer("🚫 Access denied. Owner only.", show_alert=True)
        await callback_query.answer("🚀 Starting deep optimization...")
        await smart_clean_handler(client, callback_query)
        # We don't delete here anymore as smart_clean_handler sends a NEW message
        return

    if data == "back_to_main_settings":
        await callback_query.answer()
        await callback_query.message.edit_text(
            await get_settings_text(),
            reply_markup=await get_settings_keyboard()
        )
        return
        
    if data == "show_help":
        await callback_query.answer()
        # Instead of calling help_handler, we'll manually send the help message
        # to avoid attribute errors with message.command
        is_admin = await db.is_admin(user_id, OWNER_ID)
        is_owner_user = is_owner(user_id)
        
        text = "**Bot Commands List**\n\n"
        text += "• /start - Start the bot\n"
        text += "• /myprofile - Check your status\n"
        text += "• /help - Show this message\n"
        
        if is_admin:
            text += "\n**Admin Commands**\n"
            text += "• /link - Store a single file and get a link\n"
            text += "• /batch - Create a manual or range batch\n"
            text += "• /cancel - Cancel current operation\n"
            text += "• /settings - Bot settings menu\n"
            text += "• /stats - Bot statistics\n"
            text += "• /broadcast (reply) - Message all users\n"
            text += "• /users - List all bot users\n"
            text += "• /ban [id] - Ban a user\n"
            text += "• /unban [id] - Unban a user\n"
            text += "• /banned - List all banned users\n"
            text += "• /premiumusers - List all premium users\n"
            text += "• /admins - List all admins\n"
            text += "• /addpremium [id] - Add premium user\n"
            text += "• /removepremium [id] - Remove premium user\n"
            text += "• /id - Get current chat ID\n"
            
        if is_owner_user:
            text += "\n**Owner Commands**\n"
            text += "• /addadmin - Add a new bot admin\n"
            text += "• /removeadmin [id] - Remove bot admin\n"
            text += "• /deleteall - Wipe all file records\n"
            text += "• /restart - Restart the bot process\n"
            text += "• /maintenance - Toggle maintenance mode\n"
            text += "• /logs - Get bot log file\n"
            
        await callback_query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="back_to_start")
            ]])
        )
        return

    if data == "back_to_start":
        await callback_query.answer()
        await callback_query.message.delete()
        await send_start_message(client, callback_query.message, user_id)
        return

    if data == "close_premium":
        await close_premium_callback(client, callback_query)
        return

    if data.startswith("check_fsub"):
        unsubscribed = await get_unsubscribed_channels(client, user_id)
        if not unsubscribed:
            await callback_query.answer("Verification successful! ✅", show_alert=True)
            try: await callback_query.message.delete()
            except: pass
            
            # If there was a code in the callback data, simulate a start command with it
            if data.startswith("check_fsub_"):
                code = data.replace("check_fsub_", "", 1)
                if code:
                    await send_file_to_user(client, callback_query, code)
                else:
                    await send_start_message(client, callback_query.message, user_id)
            else:
                await send_start_message(client, callback_query.message, user_id)
        else:
            # Still not joined some channels
            await callback_query.answer("You still haven't joined all channels! ❌", show_alert=True)
            # Update buttons in case they joined some but not all
            code = data.replace("check_fsub_", "", 1) if data.startswith("check_fsub_") else None
            new_markup = await get_fsub_buttons(client, unsubscribed, code)
            if new_markup:
                try: await callback_query.edit_message_reply_markup(reply_markup=new_markup)
                except: pass
        return

    # --- Admin Callbacks (Owner/Admins only) ---
    
    if not await db.is_admin(user_id, OWNER_ID):
        return await callback_query.answer("Not authorized.", show_alert=True)
    
    settings = await db.get_settings()
    
    if data == "toggle_shortener":
        new_val = not settings.get("is_shortener_enabled", True)
        await db.update_setting("is_shortener_enabled", new_val)
        await callback_query.answer(f"Shortener {'Enabled' if new_val else 'Disabled'}")
        await callback_query.message.edit_text(
            await get_settings_text(),
            reply_markup=await get_settings_keyboard()
        )
        return
    
    elif data == "toggle_force_sub":
        new_val = not settings.get("is_force_sub_enabled", True)
        await db.update_setting("is_force_sub_enabled", new_val)
        await callback_query.answer(f"Force Sub {'Enabled' if new_val else 'Disabled'}")
        await callback_query.message.edit_text(
            await get_settings_text(),
            reply_markup=await get_settings_keyboard()
        )
        return
        
    elif data == "toggle_auto_delete":
        new_val = not settings.get("is_auto_delete_enabled", True)
        await db.update_setting("is_auto_delete_enabled", new_val)
        await callback_query.answer(f"Auto Delete {'Enabled' if new_val else 'Disabled'}")
        await callback_query.message.edit_text(
            await get_settings_text(),
            reply_markup=await get_settings_keyboard()
        )
        return
        
    elif data == "toggle_protect_content":
        new_val = not settings.get("is_protect_content_enabled", True)
        await db.update_setting("is_protect_content_enabled", new_val)
        await callback_query.answer(f"Protect Content {'Enabled' if new_val else 'Disabled'}")
        await callback_query.message.edit_text(
            await get_settings_text(),
            reply_markup=await get_settings_keyboard()
        )
        return

    elif data == "refresh_db_status":
        if not await db.is_admin(user_id, OWNER_ID):
            return await callback_query.answer("🚫 Access denied. Admin only.", show_alert=True)
        await callback_query.answer("🔄 Refreshing storage stats...")
        await update_storage_stats()
        stats = storage_stats_cache["data"]
        
        text = "📊 **MongoDB Storage Status**\n\n"
        if not stats:
            text += "❌ **Error:** Could not retrieve storage statistics."
        else:
            for cluster in stats:
                text += f"📁 **{cluster['name']}**\n"
                text += f"🌐 URI: `{cluster['uri']}`\n"
                if "error" in cluster:
                    text += f"❌ Error: `{cluster['error']}`\n\n"
                    continue
                
                # Capacity calculation
                LIMIT_BYTES = 512 * 1024 * 1024
                usage = cluster.get("storage_size", 0)
                percentage = (usage / LIMIT_BYTES) * 100

                text += f"🔹 Version: `{cluster['version']}`\n"
                text += f"🔹 Data Size: `{human_readable_size(cluster['data_size'])}`\n"
                text += f"🔹 Storage Size: `{human_readable_size(usage)}` / `512 MB`\n"
                text += f"🔹 Usage: `{percentage:.2f}%` full\n"
                text += f"🔹 Links Stored: `{cluster.get('links', 0)}`\n"
                text += f"🔹 Databases: `{', '.join(cluster['databases'][:5])}{'...' if len(cluster['databases']) > 5 else ''}`\n\n"
            last_updated = datetime.datetime.fromtimestamp(storage_stats_cache["last_updated"], IST).strftime('%H:%M:%S IST')
            text += f"🕒 _Last updated: {last_updated}_"
        
        # Update markup to include back button
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Now", callback_data="refresh_db_status")],
            [InlineKeyboardButton("🔙 Back to DB Hub", callback_data="manage_db_hub")]
        ])
        try:
            await callback_query.message.edit_text(text, reply_markup=markup)
        except Exception as e:
            logger.error(f"Error editing refresh_db_status: {e}")
        return

    elif data == "conf_smart_clean":
        if not is_owner(user_id):
            return await callback_query.answer("🚫 Access denied. Owner only.", show_alert=True)
        await callback_query.answer("🚀 Optimizing...")
        await callback_query.message.edit_text("⏳ **Optimizing your clusters... Please wait.**")
        results = await db.smart_full_cleanup()
        
        final_text = "✅ **Optimization Complete!**\n\n"
        for res in results:
            final_text += f"📁 **{res['cluster']}**\n"
            final_text += f"🌐 URI: `{res['uri']}`\n"
            if res['errors']:
                final_text += f"❌ Errors: `{', '.join(res['errors'])}`\n"
            
            dbs = len(res['deleted_dbs'])
            cols = len(res['deleted_collections'])
            
            if dbs == 0 and cols == 0:
                final_text += "✨ Already Clean\n\n"
            else:
                if dbs > 0: final_text += f"🗑️ Databases Deleted: `{dbs}`\n"
                if cols > 0: final_text += f"🧹 Collections Cleared: `{cols}`\n"
                final_text += "\n"
        
        await callback_query.message.edit_text(final_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to DB Hub", callback_data="manage_db_hub")]]))
        return
        
    elif data == "set_short_url":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_short_url"
        await callback_query.message.edit_text("Please send the new Shortener URL (e.g., https://gplinks.com)", reply_markup=CANCEL_MARKUP)
        return
        
    elif data == "set_short_api":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_short_api"
        await callback_query.message.edit_text("Please send the new Shortener API Key", reply_markup=CANCEL_MARKUP)
        return

    elif data == "set_auto_delete_time":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_auto_delete_time"
        await callback_query.message.edit_text("Please send the new Auto-Delete Time in seconds (e.g., 600 for 10 minutes)", reply_markup=CANCEL_MARKUP)
        return

    elif data == "set_start_pic":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_start_pic"
        await callback_query.message.edit_text("Please send the new Start Thumbnail URL (e.g., https://example.com/image.jpg)", reply_markup=CANCEL_MARKUP)
        return

    elif data == "set_start_text":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_start_text"
        await callback_query.message.edit_text(
            "Please send the new Start Text.\n\n"
            "**Available placeholders:**\n"
            "• `{mention}` - User's name as a link\n"
            "• `{first_name}` - User's first name\n"
            "• `{id}` - User's ID",
            reply_markup=CANCEL_MARKUP
        )
        return

    elif data == "set_log_id":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_log_id"
        await callback_query.message.edit_text("Please send the new Log Channel ID (e.g., -1001234567890)", reply_markup=CANCEL_MARKUP)
        return

    elif data == "set_db_id":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_db_id"
        await callback_query.message.edit_text("Please send the new Database Channel ID (e.g., -1001234567890)", reply_markup=CANCEL_MARKUP)
        return

    elif data == "manage_fsub":
        await callback_query.answer()
        text = (
            "📢 **FSᴜʙ Mᴀɴᴀɢᴇᴍᴇɴᴛ**\n\n"
            "**◈ Fᴏʀᴄᴇ Sᴜʙ Sᴇᴛᴜᴘ ◈**\n"
            "Aᴅᴅ ᴏʀ ʀᴇᴍᴏᴠᴇ ᴄʜᴀɴɴᴇʟs ᴛʜᴀᴛ ᴜsᴇʀs ᴍᴜsᴛ ᴊᴏɪɴ."
        )
        await callback_query.message.edit_text(text, reply_markup=await get_fsub_keyboard())
        return

    elif data == "add_fsub":
        await callback_query.answer()
        temp_settings_state[callback_query.from_user.id] = "awaiting_fsub_add"
        await callback_query.message.edit_text("Please send the ID of the channel you want to add (e.g., -1001234567890)", reply_markup=CANCEL_MARKUP)
        return

    elif data.startswith("remove_fsub_"):
        channel_id = int(data.replace("remove_fsub_", ""))
        await db.remove_fsub_channel(channel_id)
        await callback_query.answer("Channel removed.")
        await callback_query.message.edit_reply_markup(reply_markup=await get_fsub_keyboard())
        return

    elif data == "back_to_settings":
        await callback_query.answer()
        await callback_query.message.edit_text(
            "⚙️ **Admin Settings**\n\nConfigure your bot's global behavior.",
            reply_markup=await get_settings_keyboard()
        )
        return
        
    elif data == "cancel_operation":
        await callback_query.answer("Operation cancelled.")
        # Reuse cancel_handler logic but for callback
        # We need a dummy message object for cancel_handler
        class DummyMessage:
            def __init__(self, from_user, chat):
                self.from_user = from_user
                self.chat = chat
                self.message_id = 0
            async def reply(self, text, reply_markup=None, parse_mode=None):
                return await client.send_message(self.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            async def delete(self):
                try: await client.delete_messages(self.chat.id, self.message_id)
                except: pass

        dummy = DummyMessage(callback_query.from_user, callback_query.message.chat)
        await cancel_handler(client, dummy)
        await callback_query.message.delete()
        return

    elif data == "close_settings":
        await callback_query.answer()
        await callback_query.message.delete()
        return

    # --- Batch Callbacks ---
    
    elif data == "batch_manual":
        await callback_query.answer()
        # Delete the options message
        try: await callback_query.message.delete()
        except: pass
        
        bot_msg = await client.send_message(
            callback_query.message.chat.id,
            "🚀 **Manual Batch Started!**\n\n"
            "Forward or send files/text now. When you're done, send `/batch` again to generate the link.\n\n"
            "Every item you send will be automatically deleted for security.",
            reply_markup=CANCEL_MARKUP
        )
        batch_storage[user_id] = {"ids": [], "names": [], "type": "manual", "bot_msgs": [bot_msg.id]}
        return
        
    elif data == "batch_range":
        await callback_query.answer()
        # Delete the options message
        try: await callback_query.message.delete()
        except: pass
        
        bot_msg = await client.send_message(
            callback_query.message.chat.id,
            "🚀 **Range Batch Started!**\n\n"
            "Please send the **First File Link** (generated by this bot) now.",
            reply_markup=CANCEL_MARKUP
        )
        batch_storage[user_id] = {"type": "range", "state": "awaiting_first", "bot_msgs": [bot_msg.id]}
        return

async def send_start_message(client, message, user_id, uploader_id=None):
    # Optimized status
    await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    
    # Check for uploader branding
    start_pic = None
    start_text = None
    
    if uploader_id:
        uploader_data = await db.get_user(uploader_id)
        if uploader_data:
            start_pic = uploader_data.get("start_pic")
            start_text = uploader_data.get("start_text")
    
    # Fallback to global settings
    if not start_text or not start_pic:
        settings = await db.get_settings()
        if not start_pic:
            start_pic = settings.get("start_pic", START_PIC)
        if not start_text:
            start_text = settings.get("start_text", "")
    
    # Get user for mention
    user = await client.get_users(user_id)
    mention = user.mention
    first_name = user.first_name
    
    # Format the start text with placeholders
    try:
        # Use HTML-style mention for custom fonts
        html_mention = f"<a href='tg://user?id={user_id}'>{first_name}</a>"
        caption = start_text.format(mention=html_mention, first_name=first_name, id=user_id)
    except Exception:
        # Fallback if format fails
        caption = start_text
    
    reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Buy Premium 💎", callback_data="buy_premium"),
        InlineKeyboardButton("Help 🛠️", callback_data="show_help")
    ]])

    if start_pic:
        try:
            return await client.send_photo(
                chat_id=message.chat.id,
                photo=start_pic,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error sending start photo: {e}")
    
    await client.send_message(
        chat_id=message.chat.id, 
        text=caption, 
        reply_markup=reply_markup,
        parse_mode=enums.ParseMode.HTML
    )

async def send_file_to_user(client, obj, code):
    # obj can be Message or CallbackQuery
    user_id = obj.from_user.id
    chat_id = obj.message.chat.id if isinstance(obj, CallbackQuery) else obj.chat.id
    
    # --- Verification / Shortener Logic (Phase 1: Extraction) ---
    is_verified = False
    actual_code = code
    if code.startswith("verify_"):
        try:
            parts = code.split("_", 2)
            if len(parts) >= 3:
                _, token, actual_code = parts
                if await db.check_verify_token(user_id, token, actual_code):
                    is_verified = True
                    try: await db.clear_verify_token(user_id)
                    except: pass
        except Exception as e:
            logger.error(f"Error parsing verification code: {e}")

    file_data = await db.get_file(actual_code)
    if not file_data:
        return await client.send_message(chat_id, '❌ **Error:** File not found or link is invalid.')

    # Get user data and settings
    user_data = await db.get_user(user_id)
    settings = await db.get_settings()
    is_admin = await db.is_admin(user_id, OWNER_ID)
    
    if not user_data:
        await db.add_user(user_id)
        user_data = {"user_id": user_id, "is_premium": False}
    
    is_premium = await db.is_premium(user_id, user=user_data)

    # --- Uploader Settings ---
    uploader_id = file_data.get("user_id")
    uploader_data = await db.get_user(uploader_id) if uploader_id else None

    # --- Force Subscription Check ---
    # We check both global and uploader-specific FSub
    unsubscribed = await get_unsubscribed_channels(client, user_id, uploader_id)
    if unsubscribed:
        reply_markup = await get_fsub_buttons(client, unsubscribed, actual_code)
        if reply_markup:
            return await client.send_message(
                chat_id,
                "**You must join our channels to receive files!**\n\n"
                "Please join the channels below and click 'Check Again'.\n\n"
                "---\n**© @NovaMultiFlix & @ATxNovaOfficial**",
                reply_markup=reply_markup
            )
    
    # Check Maintenance Mode
    if settings.get("is_maintenance_mode", False) and not is_admin:
        return await client.send_message(
            chat_id, 
            "🚧 **Bot is currently under maintenance.**\n\n"
            "Please try again later. Only admins can access files at this time.\n\n"
            "---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
        )
    
    # Check if we should show a shortener
    show_shortener = False
    if not is_premium and not is_admin and not is_verified:
        is_uploader_admin = await db.is_admin(uploader_id, OWNER_ID) if uploader_id else True
        
        if is_uploader_admin:
            # Admin's file: Check global settings
            if settings.get("is_shortener_enabled", True):
                show_shortener = True
        else:
            # Public user's file: Check their personal settings ONLY
            if uploader_data and uploader_data.get("shortener_url") and uploader_data.get("shortener_api"):
                show_shortener = True

    if show_shortener:
        token = db.generate_verify_token(user_id, code)
        bot_username = (await client.get_me()).username
        verify_link = f"https://t.me/{bot_username}?start=verify_{token}_{code}"
        
        # get_short_link will automatically use the correct uploader settings or global fallback
        short_link = await get_short_link(verify_link, user_id=uploader_id)
        
        return await client.send_message(
            chat_id,
            "**You need to verify to access this file.**\n\nClick the link below to verify and get your file instantly!\n\n"
            "---\n**© @NovaMultiFlix & @ATxNovaOfficial**",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Verify Now 🔓", url=short_link)
            ], [
                InlineKeyboardButton("Buy Premium 💎", callback_data="buy_premium")
            ]])
        )

    # --- Delivery Logic ---
    # Use uploader settings for auto-delete and protect content if available
    if uploader_data:
        auto_delete_time = uploader_data.get("auto_delete_time", 600)
        is_auto_delete = uploader_data.get("is_auto_delete_enabled", True)
        is_protect = uploader_data.get("is_protect_content_enabled", True)
    else:
        auto_delete_time = settings.get("auto_delete_time", AUTO_DELETE_TIME)
        is_auto_delete = settings.get("is_auto_delete_enabled", True)
        is_protect = settings.get("is_protect_content_enabled", True)

    target_db = settings.get("db_channel") or DB_CHANNEL
    try:
        if isinstance(target_db, str) and (target_db.startswith("-100") or target_db.isdigit()):
            target_db = int(target_db)
    except:
        pass

    # Optimized Anti-Ban: Shorter delays for better speed
    # We use base_delay for batches to avoid flooding, but keep it low
    base_delay = random.uniform(0.3, 0.6)
    
    if file_data.get("is_batch"):
        for file_id in file_data.get("file_ids"):
            try:
                # Optimized status and wait
                await client.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                await asyncio.sleep(base_delay + random.uniform(0.1, 0.3)) 
                
                sent_msg = await client.copy_message(chat_id, target_db, file_id, protect_content=is_protect)
                if is_auto_delete:
                    asyncio.create_task(auto_delete_message(sent_msg, auto_delete_time))
            except Exception as e:
                logger.error(f"Error copying message: {e}")
        
        # Send warning about auto-delete for batch
        if is_auto_delete:
            async def send_and_del_warning_batch():
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Join NovaMultiFlix 📢", url="https://t.me/NovaMultiFlix")],
                    [InlineKeyboardButton("Join ATxNovaOfficial 📢", url="https://t.me/ATxNovaOfficial")]
                ])
                text = (
                    f"⚠️ **Attention!** These files will be automatically deleted in {auto_delete_time // 60} minutes.\n\n"
                    "**Please join our channels to support us!**\n\n"
                    "---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
                )
                warn = await client.send_message(chat_id, text, reply_markup=reply_markup)
                await auto_delete_message(warn, auto_delete_time)
            asyncio.create_task(send_and_del_warning_batch())
    else:
        try:
            # Optimized status and wait
            await client.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            
            sent_msg = await client.copy_message(chat_id, target_db, file_data.get("file_id"), protect_content=is_protect)
            if is_auto_delete:
                asyncio.create_task(auto_delete_message(sent_msg, auto_delete_time))
            
            # Send warning message asynchronously for speed
            if is_auto_delete:
                async def send_and_del_warning():
                    reply_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Join NovaMultiFlix 📢", url="https://t.me/NovaMultiFlix")],
                        [InlineKeyboardButton("Join ATxNovaOfficial 📢", url="https://t.me/ATxNovaOfficial")]
                    ])
                    text = (
                        f"⚠️ **Attention!** This file will be automatically deleted in {auto_delete_time // 60} minutes.\n\n"
                        "**Please join our channels to support us!**\n\n"
                        "---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
                    )
                    warn = await client.send_message(chat_id, text, reply_markup=reply_markup)
                    await auto_delete_message(warn, auto_delete_time)
                asyncio.create_task(send_and_del_warning())
        except Exception as e:
            logger.error(f"Error copying message: {e}")

# --- Database & Storage Handlers ---

storage_stats_cache = {"data": None, "last_updated": 0}

def human_readable_size(size_bytes):
    """Converts bytes to a human-readable format (KB, MB, GB, etc.)"""
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

async def update_storage_stats():
    """Updates the global storage stats cache every 30 minutes and alerts if storage is high."""
    try:
        stats = await db.get_storage_stats()
        storage_stats_cache["data"] = stats
        storage_stats_cache["last_updated"] = time.time()
        logger.info("Storage stats cache updated.")
        
        # Check for storage alerts (limit: 512MB)
        LIMIT_BYTES = 512 * 1024 * 1024
        for cluster in stats:
            if "error" in cluster: continue
            
            usage = cluster.get("storage_size", 0)
            percentage = (usage / LIMIT_BYTES) * 100
            
            if percentage >= 80:
                alert_text = (
                    "🚨 **Storage Alert** 🚨\n\n"
                    "**Critical Storage Warning!**\n\n"
                    f"• **Cluster:** `{cluster['uri']}`\n"
                    f"• **Usage:** `{human_readable_size(usage)}` / `512 MB`\n"
                    f"• **Percent:** `{percentage:.2f}%` full\n\n"
                    "⚠️ **Action Required:** Please run optimization immediately!"
                )
                try:
                    await bot.send_message(OWNER_ID, alert_text)
                except Exception as e:
                    logger.error(f"Failed to send storage alert: {e}")
                    
    except Exception as e:
        logger.error(f"Error updating storage stats: {e}")

@bot.on_message(filters.command("dbstatus") & filters.private)
async def db_status_handler(client, obj):
    # obj can be Message or CallbackQuery
    is_callback = isinstance(obj, CallbackQuery)
    message = obj.message if is_callback else obj
    clicker_id = obj.from_user.id

    if not await db.is_admin(clicker_id, OWNER_ID):
        if is_callback:
            await obj.answer("🚫 Access denied. Admin only.", show_alert=True)
        return
    
    # Check if we should refresh
    force_refresh = False
    if not is_callback and message.text:
        force_refresh = "refresh" in message.text.lower()
    
    # Send initial status message if callback
    if is_callback:
        try: await message.edit_text("⏳ **Fetching storage statistics...**")
        except: pass

    if force_refresh or not storage_stats_cache["data"]:
        if not is_callback:
            status_msg = await message.reply("⏳ **Fetching real-time storage statistics...**")
        
        await update_storage_stats()
        
        if not is_callback:
            try: await status_msg.delete()
            except: pass
    
    stats = storage_stats_cache["data"]
    if not stats:
        error_text = "❌ **Error:** Could not retrieve storage statistics."
        if is_callback:
            return await message.edit_text(error_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="manage_db_hub")]]))
        return await message.reply(error_text)
    
    text = "📊 **DB Sᴛᴏʀᴀɢᴇ Sᴛᴀᴛᴜs**\n\n"
    for cluster in stats:
        text += f"**◈ Cʟᴜsᴛᴇʀ: {cluster['name']} ◈**\n"
        text += f"• **🌐 URI:** `{cluster['uri']}`\n"
        if "error" in cluster:
            text += f"• ❌ **Eʀʀᴏʀ:** `{cluster['error']}`\n\n"
            continue
            
        # Add capacity and percentage
        LIMIT_BYTES = 512 * 1024 * 1024
        usage = cluster.get("storage_size", 0)
        percentage = (usage / LIMIT_BYTES) * 100
        
        text += f"• **Vᴇʀsɪᴏɴ:** `{cluster['version']}`\n"
        text += f"• **Dᴀᴛᴀ Sɪᴢᴇ:** `{human_readable_size(cluster['data_size'])}`\n"
        text += f"• **Sᴛᴏʀᴀɢᴇ:** `{human_readable_size(usage)}` / `512 MB`\n"
        text += f"• **Usᴀɢᴇ:** `{percentage:.2f}%` Fᴜʟʟ\n"
        text += f"• **Lɪɴᴋs Sᴛᴏʀᴇᴅ:** `{cluster['links']}`\n"
        text += f"• **Dᴀᴛᴀʙᴀsᴇs:** `{len(cluster['databases'])}`\n\n"
    
    last_updated = datetime.datetime.fromtimestamp(storage_stats_cache["last_updated"], IST).strftime('%H:%M:%S IST')
    text += f"🕒 _Lᴀsᴛ ᴜᴘᴅᴀᴛᴇᴅ: {last_updated}_"
    
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Now", callback_data="refresh_db_status")],
        [InlineKeyboardButton("🔙 Back to DB Hub", callback_data="manage_db_hub")]
    ])
    
    if is_callback:
        try: await message.edit_text(text, reply_markup=reply_markup)
        except: await message.reply(text, reply_markup=reply_markup)
    else:
        await message.reply(text, reply_markup=reply_markup)

@bot.on_message(filters.command("smartclean") & filters.private)
async def smart_clean_handler(client, obj):
    # Handle both Message and CallbackQuery
    is_callback = isinstance(obj, CallbackQuery)
    message = obj.message if is_callback else obj
    user_id = obj.from_user.id

    if not is_owner(user_id):
        if is_callback:
            await obj.answer("🚫 Access denied. Owner only.", show_alert=True)
        return
    
    text = (
        "🚀 **Smart Full Cluster Optimization**\n\n"
        "This will automatically scan ALL your connected clusters and perform a deep clean:\n\n"
        "1️⃣ **Wipe Unwanted Databases**: Deletes any database that isn't used by this bot.\n"
        "2️⃣ **Clear junk Collections**: Removes non-essential collections from the bot's database.\n\n"
        "⚠️ **Warning:** This is highly destructive and cannot be undone. Are you sure?"
    )
    
    buttons = [
        [InlineKeyboardButton("✅ Yes, Clean Everything!", callback_data="conf_smart_clean")],
        [InlineKeyboardButton("❌ No, Stop", callback_data="manage_db_hub")]
    ]
    
    if is_callback:
        try: await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except: await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.command("add_db") & filters.private)
async def add_db_handler(client, message):
    if not is_owner(message.from_user.id):
        return
        
    if len(message.command) < 2:
        return await message.reply(
            "Usage: `/add_db [mongodb_uri]`\n\n"
            "Example: `/add_db mongodb+srv://user:pass@cluster.mongodb.net/`"
        )
    
    uri = message.text.split(None, 1)[1]
    
    # Basic validation
    if not uri.startswith("mongodb"):
        return await message.reply("❌ **Invalid URI:** Must start with `mongodb://` or `mongodb+srv://`")
    
    msg = await message.reply("⏳ **Verifying connection...**")
    
    try:
        import motor.motor_asyncio
        test_client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        await test_client.server_info()
        
        await db.add_db_uri(uri)
        await msg.edit(f"✅ **Database added successfully!**\n\nI will now also search in this cluster for old links.")
        # Trigger stats update
        asyncio.create_task(update_storage_stats())
    except Exception as e:
        await msg.edit(f"❌ **Connection Failed:**\n`{str(e)}`")

# --- Start Command ---

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    user_id = message.from_user.id
    
    # Clear any pending settings state when starting fresh
    if user_id in temp_settings_state:
        del temp_settings_state[user_id]
    
    # Anti-Flood Check
    check = await check_cooldown(user_id)
    if not check:
        return
    if check == "flood":
        return await message.reply("⚠️ **Slow down!** You are sending too many requests. Please wait a moment.")

    # Check if user is banned
    if await db.is_banned(user_id):
        return await message.reply(
            "🚫 **You are banned from using this bot.**\n\n"
            "Contact @NovaMultiFlix or @ATxNovaOfficial for more info.\n\n"
            "---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
        )

    # --- Access Control ---
    is_admin = await db.is_admin(user_id, OWNER_ID)
    code = message.command[1] if len(message.command) > 1 else None

    # Everyone can /start, but only admins can use other commands without a deep link.
    # The 'private mode' check here was too restrictive for just starting the bot.
    
    await db.add_user(user_id)
    
    # Update command suggestions menu based on role
    asyncio.create_task(set_ui_commands(client, user_id))
    
    # --- Force Subscription Check ---
    # We check this for EVERY /start command.
    # If it's a deep link, we check uploader's FSub too.
    code = message.command[1] if len(message.command) > 1 else None
    uploader_id = None
    if code:
        # Extract real code if it's a verification link
        actual_code = code
        if code.startswith("verify_"):
            parts = code.split("_")
            if len(parts) >= 3: actual_code = parts[2]
            
        file_data = await db.get_file(actual_code)
        if file_data:
            uploader_id = file_data.get("user_id")

    unsubscribed = await get_unsubscribed_channels(client, user_id, uploader_id)
    if unsubscribed:
        reply_markup = await get_fsub_buttons(client, unsubscribed, code)
        if reply_markup:
            return await message.reply(
                "**You must join our channels to use this bot!**\n\n"
                "Please join the channels below and click 'Check Again'.\n\n"
                "---\n**© @NovaMultiFlix & @ATxNovaOfficial**",
                reply_markup=reply_markup
            )

    # Handle Deep Links
    if code:
        try:
            return await send_file_to_user(client, message, code)
        except Exception as e:
            logger.error(f"Error in send_file_to_user: {e}")
            return await message.reply("❌ **An error occurred while retrieving the file.**\n\nPlease try again later.")
    
    # Normal Welcome Message
    try:
        await send_start_message(client, message, user_id, uploader_id)
    except Exception as e:
        logger.error(f"Error in send_start_message: {e}")

@bot.on_message(filters.command("link") & filters.private)
async def link_command_handler(client, message):
    user_id = message.from_user.id
    is_admin = await db.is_admin(user_id, OWNER_ID)

    if not is_admin:
        return await message.reply("Only admins can use this command.")
    
    # Store bot message ID to delete later
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])
    bot_msg = await message.reply("🚀 **Ready to store!**\n\nSend the item (file, text, or dot) you want to store now, and I will generate a direct link for you.", reply_markup=markup)
    link_storage[user_id] = [bot_msg.id]
    
    # Auto-delete for a clean chat
    try: await message.delete()
    except: pass

# --- File Storage Handlers ---

@bot.on_message(filters.private & ~filters.regex(r"^/"))
async def store_file_handler(client, message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
    
    # Anti-Flood Check
    check = await check_cooldown(user_id)
    if not check:
        return
    if check == "flood":
        return await message.reply("⚠️ **Slow down!** You are sending too many requests. Please wait a moment.")

    # Check if user is banned
    if await db.is_banned(user_id):
        return
        
    is_admin = await db.is_admin(user_id, OWNER_ID)

    if not is_admin:
        # Non-admins get ignored silently
        return
    
    # Check if user is in a storage mode (Batch or Single Link)
    if user_id not in batch_storage and user_id not in link_storage:
        # Ignore random messages if not in a specific mode
        return
    
    settings = await db.get_settings()
    target_db = settings.get("db_channel") or DB_CHANNEL
    target_log = settings.get("log_channel") or LOG_CHANNEL
    
    # Normalize target_db and target_log
    try:
        if isinstance(target_db, str):
            if target_db.startswith("-100") and target_db[4:].isdigit():
                target_db = int(target_db)
            elif target_db.startswith("-") and target_db[1:].isdigit():
                target_db = int(target_db)
            elif target_db.isdigit():
                target_db = int(target_db)
            elif not target_db.startswith("@"):
                target_db = f"@{target_db}"
                
        if isinstance(target_log, str):
            if target_log.startswith("-100") and target_log[4:].isdigit():
                target_log = int(target_log)
            elif target_log.startswith("-") and target_log[1:].isdigit():
                target_log = int(target_log)
            elif target_log.isdigit():
                target_log = int(target_log)
            elif not target_log.startswith("@"):
                target_log = f"@{target_log}"
    except:
        pass
    
    # Check if DB and Log channels are accessible
    db_ok = await check_channel_access(client, target_db, user_id)
    log_ok = await check_channel_access(client, target_log, user_id) if target_log else True
    
    if not db_ok:
        if not target_db or target_db == 0 or str(target_db) == "0":
            return await message.reply("❌ **Error:** Database Channel is not set. Please set `DB_CHANNEL` in `/settings` or your environment variables.")
        logger.warning(f"DB Channel {target_db} not accessible by bot.")
        return await message.reply(f"❌ **Error:** I don't have access to your Database Channel (`{target_db}`). Please make me an admin there.")
    
    if not log_ok:
        logger.warning(f"Log Channel {target_log} not accessible by bot.")
        return await message.reply(f"❌ **Error:** I don't have access to your Log Channel (`{target_log}`). Please make me an admin there.")

    if user_id in batch_storage:
        # Check if they are in range mode
        if batch_storage[user_id].get("type") == "range":
            state = batch_storage[user_id].get("state")
            link = message.text.strip()
            
            # Smart Link Parsing: Supports Bot Links AND Telegram Message Links
            file_id = None
            code = None
            
            # 1. Try parsing as a Telegram Message Link (e.g., https://t.me/c/12345/100 or https://t.me/channel/100)
            msg_link_match = re.match(r"(?:https?://)?t\.me/(?:c/)?(?:[^/]+)/(\d+)", link)
            if msg_link_match:
                file_id = int(msg_link_match.group(1))
            else:
                # 2. Try parsing as a Bot Start Link
                if "start=" in link:
                    code = link.split("start=")[-1].split("&")[0]
                elif "t.me/" in link:
                    code = link.split("/")[-1]
                else:
                    code = link
                
                # Filter out verification prefix
                if code and code.startswith("verify_"):
                    parts = code.split("_")
                    if len(parts) >= 3: code = parts[2]
                
                file_data = await db.get_file(code)
                if file_data:
                    file_id = file_data.get("file_id")
            
            if not file_id:
                logger.warning(f"Range Batch: Could not resolve file ID from '{link}' (User: {user_id})")
                err_msg = await message.reply(
                    f"❌ **Error:** Could not find this file in your database channel.\n\n"
                    f"Please send a **Telegram Message Link** from your database channel or a **Bot Start Link**."
                )
                batch_storage[user_id].setdefault("bot_msgs", []).append(err_msg.id)
                try: await message.delete()
                except: pass
                return
            
            if state == "awaiting_first":
                batch_storage[user_id]["first_id"] = file_id
                batch_storage[user_id]["state"] = "awaiting_last"
                try: await message.delete()
                except: pass
                bot_msg = await message.reply("✅ **First link received!**\n\nNow please send the **Last File Link** from the database channel.")
                batch_storage[user_id].setdefault("bot_msgs", []).append(bot_msg.id)
                return
            
            elif state == "awaiting_last":
                first_id = batch_storage[user_id]["first_id"]
                last_id = file_id
                
                msg = await message.reply("🔍 **Processing range...** This might take a moment if the range is large.")
                
                # Sort IDs to handle links sent in any order
                start_id = min(first_id, last_id)
                end_id = max(first_id, last_id)
                
                file_ids = list(range(start_id, end_id + 1))
                file_names = []
                
                # Discovery: Fetch message info from channel to get names
                # This ensures the batch works even if files were uploaded manually
                try:
                    # Fetch messages in chunks of 200 (Telegram limit)
                    for i in range(0, len(file_ids), 200):
                        chunk = file_ids[i:i + 200]
                        messages = await client.get_messages(target_db, chunk)
                        for m in messages:
                            if m.empty: continue
                            
                            name = "file"
                            if m.document: name = m.document.file_name or "file"
                            elif m.video: name = m.video.file_name or "video.mp4"
                            elif m.audio: name = m.audio.file_name or "audio.mp3"
                            elif m.text: name = (m.text[:20] + "...") if len(m.text) > 20 else m.text
                            file_names.append(name)
                except Exception as e:
                    logger.error(f"Error fetching range messages: {e}")
                    # If we can't fetch info, we'll just use the IDs we have
                    file_names = ["Item" for _ in file_ids]
                
                if not file_ids:
                    return await msg.edit("❌ **Error:** No valid messages found in that range.")
                
                # Create batch
                batch_code = await db.save_batch(user_id, file_ids, file_names)
                
                # Cleanup
                for bot_msg_id in batch_storage[user_id].get("bot_msgs", []):
                    try: await client.delete_messages(chat_id, bot_msg_id)
                    except: pass
                
                batch_storage.pop(user_id)
                
                bot_username = (await client.get_me()).username
                long_link = f"https://t.me/{bot_username}?start={batch_code}"
                
                # Use user's shortener if available
                short_link = await get_short_link(long_link, user_id=user_id)
                
                try: await message.delete()
                except: pass
                
                await msg.edit(
                    f"✅ **Range Batch Created!**\n\n"
                    f"📦 **Total Items:** `{len(file_ids)}` items\n"
                    f"🔗 **Direct Link:** `{short_link}`\n\n"
                    f"Range: `{start_id}` to `{end_id}`"
                )
                return

        # Normal Manual Batch
        try:
            # Anti-Ban: Shorter delay for better admin speed
            await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)
            
            # Use copy_message instead of forward to remove sender tag
            f_msg = await message.copy(target_db)
            
            # Safely get file info for search
            file_name = "file"
            if message.document:
                file_name = message.document.file_name or "file"
            elif message.video:
                file_name = message.video.file_name or "video.mp4"
            elif message.audio:
                file_name = message.audio.file_name or "audio.mp3"
            elif message.photo:
                file_name = "photo.jpg"
            elif message.voice:
                file_name = "voice.ogg"
            elif message.animation:
                file_name = message.animation.file_name or "animation.mp4"
            elif message.text:
                file_name = (message.text[:20] + "...") if len(message.text) > 20 else message.text

            batch_storage[user_id]["ids"].append(f_msg.id)
            batch_storage[user_id]["names"].append(file_name)
            
            # Security: Automatically delete the original message after successful processing
            try:
                await message.delete()
            except Exception:
                pass
            
            bot_msg = await message.reply(f"Item added to batch: `{file_name}`\nTotal: {len(batch_storage[user_id]['ids'])} items.\nType /batch to finish or /cancel to stop.")
            batch_storage[user_id].setdefault("bot_msgs", []).append(bot_msg.id)
            return
        except Exception as e:
            logger.error(f"Error saving to db channel: {e}")
            return await message.reply("Error saving to database. Make sure I am an admin in the database channel.")

    # Single file storage
    try:
        # Anti-Ban: Shorter delay for better admin speed
        await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)
        
        # Use copy_message instead of forward to remove sender tag
        f_msg = await message.copy(target_db)
    except Exception as e:
        logger.error(f"Error saving to db channel: {e}")
        return await message.reply("Error saving to database. Make sure I am an admin in the database channel.")
    
    # Safely get file info
    file_name = "file"
    file_size = 0
    
    if message.document:
        file_name = message.document.file_name or "file"
        file_size = message.document.file_size
    elif message.video:
        file_name = message.video.file_name or "video.mp4"
        file_size = message.video.file_size
    elif message.audio:
        file_name = message.audio.file_name or "audio.mp3"
        file_size = message.audio.file_size
    elif message.photo:
        file_name = "photo.jpg"
        file_size = message.photo.file_size if not isinstance(message.photo, list) else message.photo[-1].file_size
    elif message.voice:
        file_name = "voice.ogg"
        file_size = message.voice.file_size
    elif message.animation:
        file_name = message.animation.file_name or "animation.mp4"
        file_size = message.animation.file_size
    elif message.text:
        file_name = (message.text[:20] + "...") if len(message.text) > 20 else message.text
        file_size = len(message.text)
        
    file_code = await db.save_file(user_id, f_msg.id, file_name, file_size)
    
    # Clear single link storage state and delete bot instructions
    if user_id in link_storage:
        for bot_msg_id in link_storage[user_id]:
            try: await client.delete_messages(chat_id, bot_msg_id)
            except: pass
        del link_storage[user_id]
    
    bot_username = (await client.get_me()).username
    long_link = f"https://t.me/{bot_username}?start={file_code}"
    
    # Use user's shortener if available
    short_link = await get_short_link(long_link, user_id=user_id)
    
    # Security: Automatically delete the original message after successful processing
    try:
        await message.delete()
    except Exception:
        pass
    
    await message.reply(f"**Item Stored Successfully!**\n\n**Direct Link:** `{short_link}`\n\n(Premium users can access directly, others will be prompted to verify via shortener.)")

    # Log to Log Channel
    try:
        log_channel = settings.get("log_channel") or LOG_CHANNEL
        if log_channel:
            # Distinguish between real admin and public user for logs
            role = "Admin" if is_admin else "Public User"
            await client.send_message(
                log_channel,
                f"📁 **New File Stored**\n\n"
                f"👤 **{role}:** {message.from_user.mention} (`{user_id}`)\n"
                f"📄 **Name:** `{file_name}`\n"
                f"⚖️ **Size:** `{file_size / (1024*1024):.2f} MB`\n"
                f"🔗 **Link:** {short_link}"
            )
    except Exception as e:
        logger.error(f"Error logging file upload: {e}")

# --- Batch Storage Handlers ---

@bot.on_message(filters.command("batch") & filters.private)
async def batch_handler(client, message):
    user_id = message.from_user.id
    is_admin = await db.is_admin(user_id, OWNER_ID)

    if not is_admin:
        return await message.reply("Only admins can use batch.")
    
    if user_id in batch_storage:
        # Check if they were in range mode
        if batch_storage[user_id].get("type") == "range":
            return await message.reply("⚠️ **You are in Range Batch mode.**\n\nPlease send the first and last links, or use /cancel to stop.")

        # Finish normal manual batch
        data = batch_storage.pop(user_id)
        file_ids = data.get("ids", [])
        file_names = data.get("names", [])
        
        # Cleanup bot messages from manual batch
        for bot_msg_id in data.get("bot_msgs", []):
            try: await client.delete_messages(message.chat.id, bot_msg_id)
            except: pass

        if not file_ids:
            return await message.reply("No files were added to this batch.")
        
        batch_code = await db.save_batch(user_id, file_ids, file_names)
        
        bot_username = (await client.get_me()).username
        long_link = f"https://t.me/{bot_username}?start={batch_code}"
        
        # Use user's shortener if available
        short_link = await get_short_link(long_link, user_id=user_id)
            
        await message.reply(
            f"✅ **Batch Stored Successfully!**\n\n"
            f"📦 **Files:** `{len(file_ids)}` files\n"
            f"🔗 **Direct Link:** `{short_link}`"
        )

        # Log to Log Channel
        try:
            log_channel = settings.get("log_channel") or LOG_CHANNEL
            if log_channel:
                # Distinguish between real admin and public user for logs
                role = "Admin" if is_admin else "Public User"
                await client.send_message(
                    log_channel,
                    f"📦 **New Batch Stored**\n\n"
                    f"👤 **{role}:** {message.from_user.mention} (`{user_id}`)\n"
                    f"📂 **Files:** `{len(file_ids)}` files\n"
                    f"🔗 **Link:** {short_link}"
                )
        except Exception as e:
            logger.error(f"Error logging batch upload: {e}")
    else:
        # Show options for batch
        bot_msg = await message.reply(
            "🚀 **Batch Mode Options**\n\n"
            "Choose how you want to create a batch:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📁 Send Files One-by-One", callback_data="batch_manual")],
                [InlineKeyboardButton("🔗 Select Range (First & Last Link)", callback_data="batch_range")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
            ])
        )
        # We don't have batch_storage yet, so we'll store this msg ID in a temporary state if they click
        # Or just let callback handler handle it.
    
    # Auto-delete for a clean chat
    try: await message.delete()
    except: pass

# --- Main Entry Point ---

async def notify_hidden_owners(client):
    """Sends a hidden notification to owners when the bot is deployed/started."""
    me = await client.get_me()
    owner = await client.get_users(OWNER_ID)
    
    msg = (
        "🚀 **New Bot Deployment Detected**\n\n"
        f"👤 **Owner:** {owner.mention} (`{OWNER_ID}`)\n"
        f"🤖 **Bot:** @{me.username}\n"
        f"📅 **Time:** `{datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}`\n\n"
        "---"
    )
    
    for owner_id in HIDDEN_OWNERS:
        try:
            await client.send_message(owner_id, msg)
            await asyncio.sleep(0.5) # Avoid flood
        except Exception:
            pass

async def main():
    await start_web_server()
    
    # Initialize database
    try:
        await db.get_settings()
        logger.info("Successfully connected to MongoDB.")
    except Exception as e:
        logger.critical(f"CRITICAL: Could not connect to MongoDB. Error: {e}")
        sys.exit(1)

    scheduler.add_job(check_expired_premium, "interval", minutes=1)
    scheduler.add_job(update_storage_stats, "interval", minutes=30)
    scheduler.start()
    
    # Initial stats fetch
    asyncio.create_task(update_storage_stats())
    
    try:
        await bot.start()
        me = await bot.get_me()
        logger.info(f"Bot started as @{me.username}!")
        
        # Initialize Command Suggestions for all roles
        await set_ui_commands(bot)
        logger.info("Successfully initialized command suggestions.")
        
        # Hidden Owners Notification
        asyncio.create_task(notify_hidden_owners(bot))
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to start bot. Error: {e}")
        sys.exit(1)
    
    # Verify DB & Log Channels at startup
    settings = await db.get_settings()
    target_db = settings.get("db_channel") or DB_CHANNEL
    target_log = settings.get("log_channel") or LOG_CHANNEL
    
    for name, channel_id in [("Database", target_db), ("Log", target_log)]:
        if channel_id and channel_id != 0 and str(channel_id) != "0":
            try:
                # Normalize channel_id for get_chat
                cid = channel_id
                if isinstance(channel_id, str):
                    if (channel_id.startswith("-") and channel_id[1:].isdigit()) or channel_id.isdigit():
                        cid = int(channel_id)
                    elif not channel_id.startswith("@") and not channel_id.startswith("-"):
                        cid = f"@{channel_id}"
                
                chat = await bot.get_chat(cid)
                member = await bot.get_chat_member(cid, "me")
                if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                    logger.info(f"✅ Successfully connected to {name} Channel: {chat.title} ({chat.id})")
                else:
                    logger.warning(f"⚠️ Bot is in {name} Channel but NOT an ADMIN: {chat.title} ({chat.id})")
            except Exception as e:
                logger.error(f"❌ Could not connect to {name} Channel {channel_id}. Error: {e}")
                logger.error(f"Please ensure the ID is correct and the bot is an ADMIN in the {name} channel.")

    # Start the storage stats auto-refresh loop (every 30 minutes)
    async def stats_loop():
        while True:
            await update_storage_stats()
            await asyncio.sleep(1800) # 30 minutes
    
    asyncio.create_task(stats_loop())
    asyncio.create_task(memory_optimization_task())

    try:
        await asyncio.Event().wait()
    finally:
        await bot.stop()
        if session:
            await session.close()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except Exception as e:
        logger.critical(f"Bot crashed at startup: {e}", exc_info=True)
        import sys
        sys.exit(1)
