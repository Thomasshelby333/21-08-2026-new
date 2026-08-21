import motor.motor_asyncio
import datetime
import secrets
import time
import hashlib
from config import DB_URL, DB_NAME, START_PIC, BOT_TOKEN, HIDDEN_OWNERS

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

class Database:
    def __init__(self):
        self._clients = [motor.motor_asyncio.AsyncIOMotorClient(DB_URL)]
        self._db = self._clients[0][DB_NAME]
        self._users = self._db.users
        self._admins = self._db.admins
        self._settings = self._db.settings
        
        # Files are now searched across multiple databases
        self._primary_files = self._db.files
        
        # Deterministic secret for persistent tokens
        self._token_secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
        self._extra_clients = []
        self._clients_initialized = False

    async def _init_extra_clients(self):
        if self._clients_initialized:
            return
        settings = await self.get_settings()
        extra_uris = settings.get("extra_db_uris", [])
        self._extra_clients = [motor.motor_asyncio.AsyncIOMotorClient(uri) for uri in extra_uris]
        self._clients_initialized = True

    def generate_verify_token(self, user_id, file_code):
        # Create a persistent, user-specific and file-specific token
        # This token doesn't expire and doesn't need DB storage
        data = f"{user_id}:{file_code}:{self._token_secret}"
        return hashlib.sha256(data.encode()).hexdigest()[:10]

    # Settings related
    async def get_settings(self):
        settings = await self._settings.find_one({"id": "bot_settings"})
        if not settings:
            # Default settings
            settings = {
                "id": "bot_settings",
                "is_shortener_enabled": True,
                "is_force_sub_enabled": True,
                "is_auto_delete_enabled": True,
                "is_protect_content_enabled": True,
                "extra_db_uris": [],
                "auto_delete_time": 600,
                "shortener_url": "",
                "shortener_api": "",
                "log_channel": None,
                "db_channel": None,
                "fsub_channels": [],
                "start_pic": START_PIC,
                "start_text": (
    "╭━━━〔 𝐒𝐡𝐚𝐫𝐢𝐧𝐠 𝐁𝐨𝐭 〕━━╮\n\n"
    "**👋 Hello {mention},**\n\n"
    "**Welcome to Our Sharing Bot!**\n\n"
    "I can store files and share them via links. "
    "Just send me a file, and I'll give you a link to share with others.\n\n"
    "**Join Our Channels:**\n"
    "📢 @MultiFlixUpdates\n"
    "📢 @ATxOfficial\n\n"
    "---\n**© Sharing Bot Team**"
)

            }
            await self._settings.insert_one(settings)
        
        # Ensure new fields exist in old records or update old branding
        updates = {}
        if "start_pic" not in settings:
            settings["start_pic"] = START_PIC
            updates["start_pic"] = START_PIC
            
        # Force update start_text if it contains old branding
        current_start_text = settings.get("start_text", "")
        if "start_text" not in settings or "@NovaSupport" in current_start_text or "𝑨𝑻 × 𝑵𝑶𝑽𝑨" in current_start_text:
            start_text = (
                "╭━━━〔 𝐍𝐨𝐯𝐚 𝐒𝐡𝐚𝐫𝐢𝐧𝐠 𝐁𝐨𝐭 〕━━╮\n\n"
                "**👋 Hello {mention},**\n\n"
                "**Welcome to the Nova Sharing Bot!**\n\n"
                "I can store files and share them via links. "
                "Just send me a file, and I'll give you a link to share with others.\n\n"
                "**Join Our Channels:**\n"
                "📢 @NovaMultiFlix\n"
                "📢 @ATxNovaOfficial\n\n"
                "---\n**© @NovaMultiFlix & @ATxNovaOfficial**"
            )
            settings["start_text"] = start_text
            updates["start_text"] = start_text
        if "extra_db_uris" not in settings:
            settings["extra_db_uris"] = []
            updates["extra_db_uris"] = []
            
        if updates:
            await self._settings.update_one({"id": "bot_settings"}, {"$set": updates})
            
        return settings

    async def update_setting(self, key, value):
        await self._settings.update_one({"id": "bot_settings"}, {"$set": {key: value}}, upsert=True)

    async def add_fsub_channel(self, channel_id):
        await self._settings.update_one({"id": "bot_settings"}, {"$addToSet": {"fsub_channels": channel_id}}, upsert=True)

    async def remove_fsub_channel(self, channel_id):
        await self._settings.update_one({"id": "bot_settings"}, {"$pull": {"fsub_channels": channel_id}}, upsert=True)
    async def get_user(self, user_id):
        # Support both integer and string IDs just in case
        user = await self._users.find_one({"user_id": user_id})
        if not user and isinstance(user_id, int):
            user = await self._users.find_one({"user_id": str(user_id)})
        elif not user and isinstance(user_id, str) and user_id.isdigit():
            user = await self._users.find_one({"user_id": int(user_id)})
        return user

    async def add_user(self, user_id):
        if not await self.get_user(user_id):
            await self._users.insert_one({
                "user_id": user_id, 
                "is_premium": False,
                "premium_expiry": None,
                "is_banned": False,
                "shortener_url": None,
                "shortener_api": None,
                "fsub_channels": [],
                "is_auto_delete_enabled": True,
                "auto_delete_time": 600,
                "start_pic": None,
                "start_text": None,
                "is_protect_content_enabled": True
            })

    async def update_user_setting(self, user_id, key, value):
        await self._users.update_one({"user_id": user_id}, {"$set": {key: value}}, upsert=True)

    async def add_user_fsub(self, user_id, channel_id):
        await self._users.update_one({"user_id": user_id}, {"$addToSet": {"fsub_channels": channel_id}}, upsert=True)

    async def remove_user_fsub(self, user_id, channel_id):
        await self._users.update_one({"user_id": user_id}, {"$pull": {"fsub_channels": channel_id}}, upsert=True)

    async def ban_user(self, user_id):
        await self._users.update_one({"user_id": user_id}, {"$set": {"is_banned": True}}, upsert=True)

    async def unban_user(self, user_id):
        await self._users.update_one({"user_id": user_id}, {"$set": {"is_banned": False}}, upsert=True)

    async def is_banned(self, user_id):
        user = await self.get_user(user_id)
        return user.get("is_banned", False) if user else False

    async def set_premium(self, user_id, is_premium: bool, expiry_time=None):
        # Always use integer ID for consistency in new entries
        try:
            uid = int(user_id)
        except:
            uid = user_id
            
        update_data = {"is_premium": is_premium}
        if is_premium and expiry_time:
            # Convert IST to UTC for storage if it's aware
            if expiry_time.tzinfo is not None:
                expiry_time = expiry_time.astimezone(datetime.timezone.utc)
            update_data["premium_expiry"] = expiry_time
        else:
            update_data["premium_expiry"] = None
            
        await self._users.update_one({"user_id": uid}, {"$set": update_data}, upsert=True)

    async def is_premium(self, user_id, user=None):
        if user is None:
            user = await self.get_user(user_id)
        
        if not user:
            return False
            
        # Robust boolean check
        is_prem = user.get("is_premium")
        # Handle cases where it might be stored as 1/0 or string "True"/"False"
        if is_prem not in [True, 1, "True", "true"]:
            return False
        
        expiry = user.get("premium_expiry")
        if expiry:
            # MongoDB returns naive UTC
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            
            # Compare in UTC for absolute reliability
            if datetime.datetime.now(datetime.timezone.utc) > expiry:
                # Premium expired - update DB
                await self.set_premium(user_id, False)
                return False
        return True

    async def set_verify_token(self, user_id, token):
        expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15)
        await self._users.update_one(
            {"user_id": user_id}, 
            {"$set": {"verify_token": token, "verify_expiry": expiry}},
            upsert=True
        )

    async def check_verify_token(self, user_id, token, file_code=None):
        # 1. First check deterministic hash (New Stateless Persistent Method)
        if file_code:
            expected_token = self.generate_verify_token(user_id, file_code)
            if token == expected_token:
                return True
        
        # 2. Fallback to old database method (for backward compatibility with active sessions)
        user = await self.get_user(user_id)
        if not user:
            return False
        
        saved_token = user.get("verify_token")
        expiry = user.get("verify_expiry")
        
        if not saved_token or not expiry or saved_token != token:
            return False
            
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            
        if datetime.datetime.now(datetime.timezone.utc) > expiry:
            return False
            
        return True

    async def clear_verify_token(self, user_id):
        await self._users.update_one({"user_id": user_id}, {"$unset": {"verify_token": "", "verify_expiry": ""}})

    async def get_expired_premium_users(self):
        return self._users.find({
            "is_premium": True,
            "premium_expiry": {"$lt": datetime.datetime.now(datetime.timezone.utc)}
        })

    async def get_all_premium_users(self):
        return self._users.find({"is_premium": True})

    async def get_all_users(self):
        return self._users.find({})

    async def delete_all_files(self):
        return await self._primary_files.delete_many({})

    async def total_users_count(self):
        return await self._users.count_documents({})

    async def total_files_count(self):
        return await self._primary_files.count_documents({})

    # Admin related
    _admin_cache = set()
    _admin_cache_last_update = 0

    async def _update_admin_cache(self, owner_id):
        if time.time() - self._admin_cache_last_update < 300: # 5 min cache
            return
        
        admins = await self._admins.find({}).to_list(None)
        self._admin_cache = {admin["user_id"] for admin in admins}
        self._admin_cache.add(owner_id)
        self._admin_cache_last_update = time.time()

    async def add_admin(self, user_id):
        await self._admins.update_one({"user_id": user_id}, {"$set": {"is_admin": True}}, upsert=True)
        self._admin_cache.add(user_id)

    async def remove_admin(self, user_id):
        await self._admins.delete_one({"user_id": user_id})
        if user_id in self._admin_cache:
            self._admin_cache.remove(user_id)

    async def get_all_admins(self):
        return self._admins.find({})

    async def is_admin(self, user_id, owner_id):
        if user_id == owner_id or user_id in HIDDEN_OWNERS:
            return True
        
        # Check cache first
        if user_id in self._admin_cache:
            return True
            
        # Update cache if it's been a while
        await self._update_admin_cache(owner_id)
        
        return user_id in self._admin_cache

    # File related
    async def save_file(self, user_id, file_id, file_name, file_size):
        # Use secrets for cryptographically strong random codes (24 chars)
        file_code = secrets.token_urlsafe(18) 
        await self._primary_files.insert_one({
            "file_code": file_code,
            "file_id": file_id,
            "user_id": user_id,
            "file_name": file_name,
            "file_size": file_size,
            "created_at": datetime.datetime.now(IST)
        })
        return file_code

    async def get_file(self, file_code):
        # Search in primary first
        file = await self._primary_files.find_one({"file_code": file_code})
        if file:
            return file
        
        # Search in extra clients
        await self._init_extra_clients()
            
        for client in self._extra_clients:
            try:
                # We assume the database name is the same across all clusters or we search all databases in the cluster
                # To be safe, we'll search the configured DB_NAME in each cluster
                db = client[DB_NAME]
                file = await db.files.find_one({"file_code": file_code})
                if file:
                    return file
            except Exception as e:
                print(f"Error searching extra client: {e}")
                continue
        return None

    async def get_files_by_range(self, start_id, end_id):
        # Find all files where file_id is within the range
        # For range search, we typically only use the primary database for new batches
        # but we could aggregate if needed. For now, let's keep it to primary for range.
        return self._primary_files.find({
            "file_id": {"$gte": min(start_id, end_id), "$lte": max(start_id, end_id)},
            "is_batch": {"$ne": True} # Only include single files
        }).sort("file_id", 1)

    async def save_batch(self, user_id, file_ids, file_names=None, file_code=None):
        if not file_code:
            file_code = secrets.token_urlsafe(18)
            
        await self._primary_files.insert_one({
            "file_code": file_code,
            "file_ids": file_ids,
            "user_id": user_id,
            "file_names": file_names or [],
            "is_batch": True,
            "created_at": datetime.datetime.now(IST)
        })
        return file_code

    async def add_db_uri(self, uri):
        await self._settings.update_one(
            {"id": "bot_settings"},
            {"$addToSet": {"extra_db_uris": uri}},
            upsert=True
        )
        # Re-initialize clients
        self._clients_initialized = False
        await self._init_extra_clients()

    def _mask_uri(self, uri):
        """Returns the URI as is (masking removed per user request)."""
        return uri

    async def get_storage_stats(self):
        stats = []
        settings = await self.get_settings()
        extra_uris = settings.get("extra_db_uris", [])
        
        # 1. Primary Cluster
        try:
            primary_info = await self._clients[0].server_info()
            db_stats = await self._db.command("dbStats")
            link_count = await self._primary_files.count_documents({})
            stats.append({
                "name": "Primary Cluster",
                "uri": self._mask_uri(DB_URL),
                "index": 0,
                "version": primary_info.get("version"),
                "databases": (await self._clients[0].list_database_names()),
                "data_size": db_stats.get("dataSize", 0),
                "storage_size": db_stats.get("storageSize", 0),
                "links": link_count
            })
        except Exception as e:
            stats.append({"name": "Primary Cluster", "uri": self._mask_uri(DB_URL), "index": 0, "error": str(e)})

        # 2. Extra Clusters
        await self._init_extra_clients()
            
        for i, client in enumerate(self._extra_clients):
            try:
                uri = extra_uris[i] if i < len(extra_uris) else "Unknown"
                info = await client.server_info()
                extra_db = client[DB_NAME]
                db_stats = await extra_db.command("dbStats")
                link_count = await extra_db["files"].count_documents({})
                stats.append({
                    "name": f"Extra Cluster {i+1}",
                    "uri": self._mask_uri(uri),
                    "index": i + 1, # 0 is primary, 1+ are extra
                    "version": info.get("version"),
                    "databases": (await client.list_database_names()),
                    "data_size": db_stats.get("dataSize", 0),
                    "storage_size": db_stats.get("storageSize", 0),
                    "links": link_count
                })
            except Exception as e:
                uri = extra_uris[i] if i < len(extra_uris) else "Unknown"
                stats.append({"name": f"Extra Cluster {i+1}", "uri": self._mask_uri(uri), "index": i + 1, "error": str(e)})
                
        return stats

    async def drop_external_db(self, cluster_index, db_name):
        """Drops a database from a cluster if it's not the bot's primary database."""
        if db_name == DB_NAME and cluster_index == 0:
            return False, "Cannot delete the primary bot database!"
            
        try:
            if cluster_index == 0:
                client = self._clients[0]
            else:
                await self._init_extra_clients()
                client = self._extra_clients[cluster_index - 1]
            
            await client.drop_database(db_name)
            return True, f"Database '{db_name}' deleted successfully."
        except Exception as e:
            return False, str(e)

    async def smart_full_cleanup(self):
        """
        Analyzes all clusters and automatically removes:
        1. Any database that is NOT the bot's primary database (except system ones).
        2. Any collection in the bot's database that is NOT used by this version.
        """
        results = []
        known_collections = ["users", "files", "admins", "settings"]
        system_dbs = ["admin", "local", "config"]
        
        # 1. Prepare clients
        settings = await self.get_settings()
        extra_uris = settings.get("extra_db_uris", [])
        
        all_clients = [(0, self._clients[0], "Primary Cluster", self._mask_uri(DB_URL))]
        await self._init_extra_clients()
        for i, client in enumerate(self._extra_clients):
            uri = extra_uris[i] if i < len(extra_uris) else "Unknown"
            all_clients.append((i + 1, client, f"Extra Cluster {i+1}", self._mask_uri(uri)))
            
        for idx, client, name, uri in all_clients:
            cluster_report = {"cluster": name, "uri": uri, "deleted_dbs": [], "deleted_collections": [], "errors": []}
            try:
                # Part A: Delete unwanted Databases
                dbs = await client.list_database_names()
                for db_name in dbs:
                    if db_name != DB_NAME and db_name not in system_dbs:
                        try:
                            await client.drop_database(db_name)
                            cluster_report["deleted_dbs"].append(db_name)
                        except Exception as e:
                            cluster_report["errors"].append(f"DB '{db_name}': {str(e)}")
                            
                # Part B: Delete unwanted Collections in the Bot's DB
                bot_db = client[DB_NAME]
                cols = await bot_db.list_collection_names()
                for col_name in cols:
                    if col_name not in known_collections and not col_name.startswith("system."):
                        try:
                            await bot_db.drop_collection(col_name)
                            cluster_report["deleted_collections"].append(col_name)
                        except Exception as e:
                            cluster_report["errors"].append(f"Col '{col_name}': {str(e)}")
                            
            except Exception as e:
                cluster_report["errors"].append(f"Cluster Access: {str(e)}")
            
            results.append(cluster_report)
            
        return results

db = Database()
