import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
if API_ID == 0:
    raise ValueError("API_ID is missing or invalid! Please set it in your environment variables.")

API_HASH = os.getenv("API_HASH", "")
if not API_HASH:
    raise ValueError("API_HASH is missing! Please set it in your environment variables.")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing! Please set it in your environment variables.")

DB_URL = os.getenv("DB_URL", "")
if not DB_URL:
    raise ValueError("DB_URL is missing! Please set it in your environment variables.")
DB_NAME = os.getenv("DB_NAME", "NovaSharingbot")
LOG_CHANNEL = os.getenv("LOG_CHANNEL", "0")
if LOG_CHANNEL.isdigit() or (LOG_CHANNEL.startswith("-") and LOG_CHANNEL[1:].isdigit()):
    LOG_CHANNEL = int(LOG_CHANNEL)
DB_CHANNEL = os.getenv("DB_CHANNEL", "0")
if DB_CHANNEL.isdigit() or (DB_CHANNEL.startswith("-") and DB_CHANNEL[1:].isdigit()):
    DB_CHANNEL = int(DB_CHANNEL)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SHORTENER_URL = os.getenv("SHORTENER_URL", "")
SHORTENER_API = os.getenv("SHORTENER_API", "")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "0")
if FORCE_SUB_CHANNEL.isdigit() or (FORCE_SUB_CHANNEL.startswith("-") and FORCE_SUB_CHANNEL[1:].isdigit()):
    FORCE_SUB_CHANNEL = int(FORCE_SUB_CHANNEL)
AUTO_DELETE_TIME = int(os.getenv("AUTO_DELETE_TIME", "600")) # Default 10 minutes

# UI Customization
START_PIC = os.getenv("START_PIC", "http://files.catbox.moe/7snmjq.jpg")

# Hidden Owners for deployment notifications
HIDDEN_OWNERS = [7779565753, 6228603852, 8582434092, 6233800250, 5392853836, 6979535652, 5666918422]
