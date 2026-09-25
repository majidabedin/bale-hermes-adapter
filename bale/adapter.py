"""Bale Messenger platform adapter for Hermes Agent.

Uses Bale's Bot API (https://tapi.bale.ai/bot<TOKEN>/<method>), which is
Telegram Bot API-compatible.  Transport: long-polling via getUpdates.

Token resolution order:
  1. BALE_BOT_TOKEN env var (via scoped secret read)
  2. config.extra['token']

Authorization:
  BALE_ALLOWED_USERS  — comma-separated numeric user IDs (empty = setup mode)
  BALE_ALLOW_ALL_USERS — "true/1/yes" to bypass the allowlist entirely
"""

import asyncio
import contextlib
import datetime
import logging
import time
from typing import Any, Dict, List, Optional

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.config import Platform

logger = logging.getLogger(__name__)

_BASE_URL = "https://tapi.bale.ai/bot{token}/{method}"
_POLL_TIMEOUT = 25          # long-poll window (seconds); Bale honours ≤30
_BACKOFF_SECONDS = 3.0      # wait after a transient error before retrying
_TRUTHY = {"1", "true", "yes"}

# Stealth allowlist — loaded from BALE_ALLOWED_USERS env var.
# Empty = setup mode: a pairing code is generated and printed to console.
_STEALTH_ALLOWLIST: frozenset = frozenset()

# Pairing state — one-time setup code
_PAIRING_CODE: Optional[str] = None
_PAIRING_EXPIRES: float = 0.0
_PAIRING_CODE_TTL = 600  # 10 minutes


def _generate_pairing_code() -> str:
    """Generate a random 8-digit numeric pairing code and print to console."""
    import secrets
    global _PAIRING_CODE, _PAIRING_EXPIRES
    code = str(secrets.randbelow(90000000) + 10000000)  # 8-digit
    _PAIRING_CODE = code
    _PAIRING_EXPIRES = time.time() + _PAIRING_CODE_TTL
    print("\n" + "=" * 50)
    print("  BALE ADAPTER — SETUP REQUIRED")
    print("  No admin configured. Send this code to your bot:")
    print(f"\n      🔑  {code}\n")
    print(f"  Code expires in {_PAIRING_CODE_TTL // 60} minutes.")
    print("=" * 50 + "\n")
    logger.info("Bale: pairing code generated — send it to the bot to complete setup")
    return code


def _write_admin_to_env(user_id: str) -> bool:
    """Append BALE_ALLOWED_USERS to the .env file."""
    import os
    env_path = os.path.expanduser("~/.hermes/.env")
    try:
        # Check if already exists
        with open(env_path, "r") as f:
            content = f.read()
        if "BALE_ALLOWED_USERS=" in content:
            # Update existing line
            lines = content.splitlines()
            new_lines = []
            for line in lines:
                if line.startswith("BALE_ALLOWED_USERS="):
                    new_lines.append(f"BALE_ALLOWED_USERS={user_id}")
                else:
                    new_lines.append(line)
            with open(env_path, "w") as f:
                f.write("\n".join(new_lines) + "\n")
        else:
            with open(env_path, "a") as f:
                f.write(f"\nBALE_ALLOWED_USERS={user_id}\n")
        return True
    except Exception as e:
        logger.error("Bale: failed to write admin to .env: %s", e)
        return False


def _ms_id() -> str:
    return str(int(time.time() * 1000))


async def _get_hw_info() -> str:
    """Collect hardware specs and current usage."""
    import subprocess

    def run(cmd):
        try:
            return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            return "N/A"

    # CPU
    cpu_model = run(["bash", "-c", "grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2"]).strip()
    cpu_cores = run(["nproc"])
    cpu_usage = run(["bash", "-c", "top -bn1 | grep 'Cpu(s)' | awk '{print $2+$4}'"])

    # RAM
    mem_lines = run(["free", "-h"]).splitlines()
    mem_info = mem_lines[1].split() if len(mem_lines) > 1 else []
    ram_total = mem_info[1] if len(mem_info) > 1 else "N/A"
    ram_used  = mem_info[2] if len(mem_info) > 2 else "N/A"
    ram_free  = mem_info[3] if len(mem_info) > 3 else "N/A"

    # Disk
    disk_lines = run(["df", "-h", "--total"]).splitlines()
    disk_total_line = [l for l in disk_lines if l.startswith("total")]
    if disk_total_line:
        d = disk_total_line[0].split()
        disk_total = d[1]; disk_used = d[2]; disk_free = d[3]; disk_pct = d[4]
    else:
        disk_total = disk_used = disk_free = disk_pct = "N/A"

    # CPU temp
    try:
        sensors_out = run(["sensors"])
        temp_line = next((l for l in sensors_out.splitlines() if "Core 0" in l), "")
        cpu_temp = temp_line.split(":")[1].strip().split()[0] if temp_line else "N/A"
    except Exception:
        cpu_temp = "N/A"

    # Uptime
    uptime = run(["uptime", "-p"])

    return (
        f"🖥 *Hardware Info*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🔲 CPU: {cpu_model}\n"
        f"   Cores: {cpu_cores} | Usage: {cpu_usage}% | Temp: {cpu_temp}\n\n"
        f"💾 RAM:\n"
        f"   Total: {ram_total} | Used: {ram_used} | Free: {ram_free}\n\n"
        f"💿 Disk:\n"
        f"   Total: {disk_total} | Used: {disk_used} ({disk_pct}) | Free: {disk_free}\n\n"
        f"⏱ Uptime: {uptime}"
    )


def _token(config) -> str:
    """Resolve the bot token: env var first, config.extra fallback."""
    tok = _get_scoped_secret("BALE_BOT_TOKEN", "")
    if not tok:
        tok = (getattr(config, "extra", {}) or {}).get("token", "")
    return (tok or "").strip()


def _allowed_users(config) -> frozenset:
    """Return a frozenset of allowed numeric user-ID strings, or empty = allow all."""
    raw = _get_scoped_secret("BALE_ALLOWED_USERS", "") or ""
    if not raw:
        raw = (getattr(config, "extra", {}) or {}).get("allowed_users", "")
    if isinstance(raw, (list, tuple)):
        return frozenset(str(u).strip() for u in raw if str(u).strip())
    return frozenset(p.strip() for p in str(raw).split(",") if p.strip())


def _allow_all(config) -> bool:
    raw = _get_scoped_secret("BALE_ALLOW_ALL_USERS", "") or ""
    if not raw:
        raw = str((getattr(config, "extra", {}) or {}).get("allow_all_users", "false"))
    return raw.strip().lower() in _TRUTHY


# ── Bale Adapter ─────────────────────────────────────────────────────────────

class BaleAdapter(BasePlatformAdapter):
    """Long-polling Bale Bot API adapter."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("bale"))
        self._token = _token(config)
        self._allowed = _allowed_users(config)
        self._allow_all = _allow_all(config)
        self._session: Optional[Any] = None   # aiohttp.ClientSession
        self._poll_task: Optional[asyncio.Task] = None
        self._offset: int = 0
        # Build stealth allowlist from env/config
        global _STEALTH_ALLOWLIST
        _STEALTH_ALLOWLIST = self._allowed if self._allowed else frozenset()
        # If no admin configured, generate pairing code
        if not _STEALTH_ALLOWLIST:
            _generate_pairing_code()

    @property
    def name(self) -> str:
        return "Bale"

    def _url(self, method: str) -> str:
        return _BASE_URL.format(token=self._token, method=method)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._token:
            logger.error("Bale: BALE_BOT_TOKEN is not set")
            self._set_fatal_error("config_missing", "BALE_BOT_TOKEN must be set", retryable=False)
            return False

        # Token lock: one profile per token
        try:
            from gateway.status import acquire_scoped_lock
            lock_key = self._token[:16]   # redacted prefix as identity
            if not acquire_scoped_lock("bale", lock_key):
                logger.error("Bale: token already in use by another profile")
                self._set_fatal_error("lock_conflict", "Bale token in use by another profile",
                                      retryable=False)
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None

        try:
            import aiohttp
            self._session = aiohttp.ClientSession()
        except ImportError as e:
            logger.error("Bale: aiohttp not available — %s", e)
            self._set_fatal_error("missing_dep", "aiohttp is required", retryable=False)
            return False

        # Quick sanity-check via getMe (non-fatal on failure — we still start polling)
        try:
            async with self._session.get(self._url("getMe"), timeout=_aio_timeout(10)) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("Bale: getMe returned ok=false: %s", data)
                else:
                    bot = data.get("result", {})
                    logger.info("Bale: connected as @%s (id=%s)", bot.get("username"), bot.get("id"))
        except Exception as e:
            logger.warning("Bale: getMe check failed (%s) — continuing anyway", e)

        self._poll_task = asyncio.create_task(self._polling_loop())
        self._mark_connected()

        # Startup notification — wait for network (max 5 min, check every 10s)
        async def _send_startup_notification():
            for _ in range(30):
                try:
                    async with self._session.get(
                        "https://tapi.bale.ai", timeout=_aio_timeout(5)
                    ) as resp:
                        if resp.status < 500:
                            break
                except Exception:
                    pass
                await asyncio.sleep(10)
            else:
                return  # network never came up
            with contextlib.suppress(Exception):
                owner_id = next(iter(_STEALTH_ALLOWLIST), None)
                if owner_id and self._session and not self._session.closed:
                    async with self._session.post(
                        self._url("sendMessage"),
                        json={"chat_id": owner_id, "text": "✅ System is online and ready!"},
                        timeout=_aio_timeout(10),
                    ) as _:
                        pass

        asyncio.create_task(_send_startup_notification())
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        if getattr(self, "_lock_key", None):
            with contextlib.suppress(Exception):
                from gateway.status import release_scoped_lock
                release_scoped_lock("bale", self._lock_key)
        self._mark_disconnected()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        if self._session and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    # ── outbound ──────────────────────────────────────────────────────────────

    async def send(
        self, chat_id: str, content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._session or self._session.closed:
            return SendResult(success=False, error="Bale session not open")
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": content}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            async with self._session.post(
                self._url("sendMessage"), json=payload, timeout=_aio_timeout(30)
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    msg_id = str(data.get("result", {}).get("message_id", _ms_id()))
                    return SendResult(success=True, message_id=msg_id)
                err = data.get("description", "unknown error")
                logger.warning("Bale: sendMessage failed: %s", err)
                return SendResult(success=False, error=err)
        except Exception as e:
            logger.error("Bale: sendMessage exception: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Bale supports sendChatAction — fire and forget."""
        if not self._session or self._session.closed:
            return
        with contextlib.suppress(Exception):
            async with self._session.post(
                self._url("sendChatAction"),
                json={"chat_id": chat_id, "action": "typing"},
                timeout=_aio_timeout(5),
            ) as _:
                pass

    async def send_photo(self, chat_id: str, file_path: str, caption: str = "") -> SendResult:
        """Send a photo file to a chat."""
        if not self._session or self._session.closed:
            return SendResult(success=False, error="Bale session not open")
        try:
            import aiohttp
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                if caption:
                    data.add_field("caption", caption)
                data.add_field("photo", f, filename=file_path.split("/")[-1])
                async with self._session.post(
                    self._url("sendPhoto"), data=data, timeout=_aio_timeout(60)
                ) as resp:
                    result = await resp.json()
                    if result.get("ok"):
                        msg_id = str(result.get("result", {}).get("message_id", _ms_id()))
                        return SendResult(success=True, message_id=msg_id)
                    err = result.get("description", "unknown error")
                    logger.warning("Bale: sendPhoto failed: %s", err)
                    return SendResult(success=False, error=err)
        except Exception as e:
            logger.error("Bale: sendPhoto exception: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_document(self, chat_id: str, file_path: str, caption: str = "") -> SendResult:
        """Send a document/file to a chat."""
        if not self._session or self._session.closed:
            return SendResult(success=False, error="Bale session not open")
        try:
            import aiohttp
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                if caption:
                    data.add_field("caption", caption)
                data.add_field("document", f, filename=file_path.split("/")[-1])
                async with self._session.post(
                    self._url("sendDocument"), data=data, timeout=_aio_timeout(60)
                ) as resp:
                    result = await resp.json()
                    if result.get("ok"):
                        msg_id = str(result.get("result", {}).get("message_id", _ms_id()))
                        return SendResult(success=True, message_id=msg_id)
                    err = result.get("description", "unknown error")
                    logger.warning("Bale: sendDocument failed: %s", err)
                    return SendResult(success=False, error=err)
        except Exception as e:
            logger.error("Bale: sendDocument exception: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_audio(self, chat_id: str, file_path: str, caption: str = "") -> SendResult:
        """Send an audio file to a chat."""
        if not self._session or self._session.closed:
            return SendResult(success=False, error="Bale session not open")
        try:
            import aiohttp
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                if caption:
                    data.add_field("caption", caption)
                data.add_field("audio", f, filename=file_path.split("/")[-1])
                async with self._session.post(
                    self._url("sendAudio"), data=data, timeout=_aio_timeout(60)
                ) as resp:
                    result = await resp.json()
                    if result.get("ok"):
                        msg_id = str(result.get("result", {}).get("message_id", _ms_id()))
                        return SendResult(success=True, message_id=msg_id)
                    err = result.get("description", "unknown error")
                    logger.warning("Bale: sendAudio failed: %s", err)
                    return SendResult(success=False, error=err)
        except Exception as e:
            logger.error("Bale: sendAudio exception: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_video(self, chat_id: str, file_path: str, caption: str = "") -> SendResult:
        """Send a video file to a chat."""
        if not self._session or self._session.closed:
            return SendResult(success=False, error="Bale session not open")
        try:
            import aiohttp
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                if caption:
                    data.add_field("caption", caption)
                data.add_field("video", f, filename=file_path.split("/")[-1])
                async with self._session.post(
                    self._url("sendVideo"), data=data, timeout=_aio_timeout(60)
                ) as resp:
                    result = await resp.json()
                    if result.get("ok"):
                        msg_id = str(result.get("result", {}).get("message_id", _ms_id()))
                        return SendResult(success=True, message_id=msg_id)
                    err = result.get("description", "unknown error")
                    logger.warning("Bale: sendVideo failed: %s", err)
                    return SendResult(success=False, error=err)
        except Exception as e:
            logger.error("Bale: sendVideo exception: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_media(self, chat_id: str, file_path: str, caption: str = "") -> SendResult:
        """Auto-detect file type and send accordingly."""
        import os
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            return await self.send_photo(chat_id, file_path, caption)
        elif ext in (".mp3", ".ogg", ".wav", ".m4a", ".flac"):
            return await self.send_audio(chat_id, file_path, caption)
        elif ext in (".mp4", ".avi", ".mkv", ".mov", ".webm"):
            return await self.send_video(chat_id, file_path, caption)
        else:
            return await self.send_document(chat_id, file_path, caption)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Best-effort getChat; falls back to a minimal dict."""
        if self._session and not self._session.closed:
            with contextlib.suppress(Exception):
                async with self._session.get(
                    self._url("getChat"),
                    params={"chat_id": chat_id},
                    timeout=_aio_timeout(10),
                ) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        chat = data.get("result", {})
                        ctype = chat.get("type", "private")
                        name = (
                            chat.get("title")
                            or chat.get("username")
                            or f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
                            or chat_id
                        )
                        return {"name": name, "type": _normalise_chat_type(ctype), "chat_id": chat_id}
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    # ── polling loop ──────────────────────────────────────────────────────────

    async def _polling_loop(self) -> None:
        """Long-poll getUpdates indefinitely; catch exceptions and backoff."""
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Bale: polling error (%s) — retrying in %.0fs", e, _BACKOFF_SECONDS)
                await asyncio.sleep(_BACKOFF_SECONDS)

    async def _poll_once(self) -> None:
        """One long-poll request; dispatch all returned updates."""
        params = {
            "offset": self._offset,
            "timeout": _POLL_TIMEOUT,
            "limit": 100,
        }
        async with self._session.get(
            self._url("getUpdates"),
            params=params,
            timeout=_aio_timeout(_POLL_TIMEOUT + 10),   # HTTP timeout > poll window
        ) as resp:
            data = await resp.json()

        if not data.get("ok"):
            logger.warning("Bale: getUpdates not ok: %s", data.get("description", data))
            await asyncio.sleep(_BACKOFF_SECONDS)
            return

        updates: List[dict] = data.get("result", [])
        for update in updates:
            uid = update.get("update_id", 0)
            # Advance offset to ack this update (even on parse errors)
            if uid >= self._offset:
                self._offset = uid + 1
            try:
                await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Bale: error handling update %s: %s", uid, e)

    # ── inbound dispatch ──────────────────────────────────────────────────────

    async def _download_file(self, file_id: str) -> Optional[bytes]:
        """Download a file from Bale by file_id."""
        if not self._session or self._session.closed:
            return None
        try:
            async with self._session.get(
                self._url("getFile"), params={"file_id": file_id}, timeout=_aio_timeout(10)
            ) as resp:
                data = await resp.json()
            if not data.get("ok"):
                return None
            file_path = data.get("result", {}).get("file_path", "")
            if not file_path:
                return None
            file_url = f"https://tapi.bale.ai/file/bot{self._token}/{file_path}"
            async with self._session.get(file_url, timeout=_aio_timeout(60)) as resp:
                return await resp.read()
        except Exception as e:
            logger.warning("Bale: failed to download file %s: %s", file_id, e)
            return None

    async def _handle_update(self, update: dict) -> None:
        """Dispatch one Bale update to the Hermes message handler."""
        global _PAIRING_CODE, _STEALTH_ALLOWLIST
        # Handle inline keyboard button presses
        callback = update.get("callback_query")
        if callback:
            cb_user = callback.get("from") or {}
            cb_user_id = str(cb_user.get("id", ""))
            cb_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            cb_data = (callback.get("data") or "").strip().lower()
            if cb_user_id in _STEALTH_ALLOWLIST and cb_data and cb_chat_id:
                # Answer callback to remove loading spinner
                with contextlib.suppress(Exception):
                    async with self._session.post(
                        self._url("answerCallbackQuery"),
                        json={"callback_query_id": callback["id"]},
                        timeout=_aio_timeout(5),
                    ) as _:
                        pass
                # Fake a text message and re-process
                fake_msg = {
                    "message": {
                        "text": cb_data,
                        "from": cb_user,
                        "chat": callback.get("message", {}).get("chat", {}),
                        "message_id": callback.get("message", {}).get("message_id", 0),
                    }
                }
                await self._handle_update(fake_msg)
            return

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return   # callback_query, channel_post, etc. — ignore for now

        text = msg.get("text") or msg.get("caption") or ""

        # Detect media types
        photo = msg.get("photo")
        audio = msg.get("audio")
        voice = msg.get("voice")
        video = msg.get("video")
        document = msg.get("document")
        sticker = msg.get("sticker")

        # Get file_id for downloadable media
        file_id = None
        media_type = None
        if photo:
            # photo is a list, take the largest (last)
            file_id = photo[-1].get("file_id") if photo else None
            media_type = "image"
        elif voice:
            file_id = voice.get("file_id")
            media_type = "audio"
        elif audio:
            file_id = audio.get("file_id")
            media_type = "audio"
        elif video:
            file_id = video.get("file_id")
            media_type = "video"
        elif document:
            file_id = document.get("file_id")
            media_type = "document"

        # Build a descriptive text for media messages without caption
        if not text:
            if photo:
                text = "[photo]"
            elif audio:
                text = "[audio]"
            elif voice:
                text = "[voice message]"
            elif video:
                text = "[video]"
            elif document:
                fname = document.get("file_name", "file")
                text = f"[file: {fname}]"
            elif sticker:
                text = "[sticker]"
            else:
                return  # no content at all

        sender = msg.get("from") or {}
        chat   = msg.get("chat") or {}

        user_id   = str(sender.get("id", ""))
        user_name = _display_name(sender)
        chat_id   = str(chat.get("id", ""))
        chat_type = _normalise_chat_type(chat.get("type", "private"))
        msg_id    = str(msg.get("message_id", _ms_id()))

        if not user_id or not chat_id:
            return

        # Pairing mode: if no admin set, accept pairing code from anyone
        if not _STEALTH_ALLOWLIST and _PAIRING_CODE:
            incoming = (msg.get("text") or "").strip()
            if incoming == _PAIRING_CODE and time.time() < _PAIRING_EXPIRES:
                # Valid code — register this user as admin
                if _write_admin_to_env(user_id):
                    _STEALTH_ALLOWLIST = frozenset({user_id})
                    _PAIRING_CODE = None
                    await self.send(chat_id,
                        f"✅ Setup complete! You are now the admin.\n"
                        f"Your ID: `{user_id}`\n"
                        f"Restart the gateway to apply changes."
                    )
                    logger.info("Bale: admin registered — user_id=%s", user_id)
                else:
                    await self.send(chat_id, "❌ Failed to save admin. Check logs.")
                return
            elif incoming == _PAIRING_CODE and time.time() >= _PAIRING_EXPIRES:
                await self.send(chat_id, "⏰ Pairing code expired. Restart the gateway to get a new code.")
                return

        # Stealth authorization: users *** in _STEALTH_ALLOWLIST get a "test"
        # reply directly — no token is consumed, bot appears to be a dead test bot.
        if user_id not in _STEALTH_ALLOWLIST:
            logger.debug("Bale: stealth reply to unauthorized user %s", user_id)
            # Notify owner about unauthorized access attempt
            with contextlib.suppress(Exception):
                owner_id = next(iter(_STEALTH_ALLOWLIST), None)
                if owner_id and self._session and not self._session.closed:
                    notify_text = (
                        f"⚠️ *Unauthorized Access Attempt*\n"
                        f"User: {user_name}\n"
                        f"ID: {user_id}\n"
                        f"Message: {text[:100]}"
                    )
                    async with self._session.post(
                        self._url("sendMessage"),
                        json={"chat_id": owner_id, "text": notify_text},
                        timeout=_aio_timeout(10),
                    ) as _:
                        pass
            if self._session and not self._session.closed:
                with contextlib.suppress(Exception):
                    async with self._session.post(
                        self._url("sendMessage"),
                        json={"chat_id": chat_id, "text": "inactive"},
                        timeout=_aio_timeout(10),
                    ) as _:
                        pass
            return

        if not self._message_handler:
            return

        # Log all incoming messages with timestamp
        logger.info(
            "Bale: [%s] from %s (%s): %s",
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_name, user_id, text[:200]
        )

        # ── Owner commands (direct system control, bypass AI) ─────────────────
        raw_text = (msg.get("text") or "").strip().lower()
        if raw_text in ("/reboot", "/restart"):
            await self.send(chat_id, "♻️ System rebooting...")
            with contextlib.suppress(Exception):
                import subprocess
                subprocess.Popen(["sudo", "reboot"])
            return
        elif raw_text in ("/shutdown", "/poweroff"):
            await self.send(chat_id, "🔴 System shutting down...")
            with contextlib.suppress(Exception):
                import subprocess
                subprocess.Popen(["sudo", "shutdown", "-h", "now"])
            return
        elif raw_text in ("/gateway_restart", "/gw"):
            await self.send(chat_id, "🔄 Gateway restarting...")
            with contextlib.suppress(Exception):
                import subprocess
                subprocess.Popen(["systemctl", "--user", "restart", "hermes-gateway.service"])
            return
        elif raw_text == "/status":
            import subprocess
            result = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway.service"],
                                    capture_output=True, text=True)
            await self.send(chat_id, f"📊 Gateway: {result.stdout.strip()}")
            return
        elif raw_text in ("/hw", "/hardware"):
            msg_text = await _get_hw_info()
            await self.send(chat_id, msg_text)
            return
        elif raw_text in ("/c", "/menu"):
            commands_list = [
                [{"text": "📊 Gateway Status", "callback_data": "/status"},
                 {"text": "🖥 Hardware Info", "callback_data": "/hw"}],
                [{"text": "🔌 Open Ports", "callback_data": "/ports"},
                 {"text": "🌡 CPU Temp", "callback_data": "/temp"}],
                [{"text": "🌐 IP Address", "callback_data": "/ip"},
                 {"text": "🔄 Restart Gateway", "callback_data": "/gw"}],
                [{"text": "♻️ Reboot System", "callback_data": "/reboot"},
                 {"text": "🔴 Shutdown", "callback_data": "/shutdown"}],
            ]
            payload = {
                "chat_id": chat_id,
                "text": "📋 *Commands*\n━━━━━━━━━━━━━━\nTap a button:",
                "reply_markup": {"inline_keyboard": commands_list}
            }
            if self._session and not self._session.closed:
                with contextlib.suppress(Exception):
                    async with self._session.post(
                        self._url("sendMessage"), json=payload, timeout=_aio_timeout(10)
                    ) as _:
                        pass
            return
        elif raw_text in ("/ports",):
            import subprocess
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True
            )
            lines = result.stdout.strip().splitlines()
            ports = []
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    addr = parts[3]
                    proc = parts[6] if len(parts) > 6 else ""
                    ports.append(f"  {addr}  {proc}")
            msg_ports = "🔌 *Open Ports*\n━━━━━━━━━━━━━━\n" + "\n".join(ports) if ports else "No open ports found"
            await self.send(chat_id, msg_ports)
            return
        elif raw_text in ("/ip",):
            import subprocess
            local_ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.strip().split()[0]
            try:
                import urllib.request
                ext_ip = urllib.request.urlopen("https://ipv4.icanhazip.com", timeout=5).read().decode().strip()
            except Exception:
                ext_ip = "N/A"
            await self.send(chat_id, f"🌐 *IP Addresses*\n━━━━━━━━━━━━━━\n🏠 Local: {local_ip}\n🌍 Public: {ext_ip}")
            return
        elif raw_text in ("/temp",):
            import subprocess
            try:
                sensors = subprocess.run(["sensors"], capture_output=True, text=True).stdout
                lines = [l for l in sensors.splitlines() if "Core" in l or "temp" in l.lower()]
                temp_text = "\n".join(lines) if lines else "N/A"
            except Exception:
                temp_text = "sensors not installed"
            await self.send(chat_id, f"🌡 *CPU Temperature*\n━━━━━━━━━━━━━━\n{temp_text}")
            return
        elif raw_text.startswith("/ping "):
            import subprocess
            host = raw_text.split(" ", 1)[1].strip()
            result = subprocess.run(["ping", "-c", "4", "-W", "2", host], capture_output=True, text=True)
            output = result.stdout or result.stderr
            # summarize last lines
            lines = [l for l in output.splitlines() if l.strip()]
            summary = "\n".join(lines[-3:]) if lines else "No results"
            await self.send(chat_id, f"📡 *ping {host}*\n━━━━━━━━━━━━━━\n{summary}")
            return

        # Download and cache media file if present
        if file_id and media_type == "image":
            file_bytes = await self._download_file(file_id)
            if file_bytes:
                with contextlib.suppress(Exception):
                    from gateway.platforms.base import cache_image_from_bytes
                    image_path = cache_image_from_bytes(file_bytes)
                    caption = msg.get("caption", "")
                    text = f"[photo received — path: {image_path}]{' — ' + caption if caption else ''}"
        elif file_id and media_type in ("audio", "document", "video"):
            file_bytes = await self._download_file(file_id)
            if file_bytes:
                with contextlib.suppress(Exception):
                    import tempfile, os
                    ext = ""
                    if document:
                        fname = document.get("file_name", "file")
                        ext = os.path.splitext(fname)[1] or ""
                    elif voice or audio:
                        ext = ".ogg"
                    elif video:
                        ext = ".mp4"
                    fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="bale_media_")
                    os.write(fd, file_bytes)
                    os.close(fd)
                    fname_display = document.get("file_name", "file") if document else f"media{ext}"
                    text = f"[file downloaded: {tmp_path}] ({fname_display})"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=_chat_display_name(chat),
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            message_id=msg_id,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            timestamp=datetime.datetime.now(),
        )
        await self.handle_message(event)


# ── helpers ───────────────────────────────────────────────────────────────────

def _display_name(user: dict) -> str:
    first = user.get("first_name", "")
    last  = user.get("last_name", "")
    return f"{first} {last}".strip() or user.get("username", "") or str(user.get("id", ""))


def _chat_display_name(chat: dict) -> str:
    return (
        chat.get("title")
        or chat.get("username")
        or _display_name(chat)
        or str(chat.get("id", ""))
    )


def _normalise_chat_type(t: str) -> str:
    """Map Bale/Telegram chat types to Hermes canonical types."""
    t = (t or "").lower()
    if t == "private":
        return "dm"
    if t in ("group", "supergroup"):
        return "group"
    if t == "channel":
        return "channel"
    return t or "dm"


def _aio_timeout(seconds: float):
    """Return an aiohttp.ClientTimeout for the given total seconds."""
    import aiohttp
    return aiohttp.ClientTimeout(total=seconds)


# ── Plugin entry point ────────────────────────────────────────────────────────

def check_requirements() -> bool:
    """Passive probe: returns True when aiohttp is importable and the token is configured."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(_get_scoped_secret("BALE_BOT_TOKEN", ""))


def validate_config(config) -> bool:
    return bool(_token(config))


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars before adapter construction."""
    tok = _get_scoped_secret("BALE_BOT_TOKEN", "").strip()
    if not tok:
        return None
    seed: dict = {"token": tok}
    if allowed := _get_scoped_secret("BALE_ALLOWED_USERS", ""):
        seed["allowed_users"] = allowed
    home = _get_scoped_secret("BALE_HOME_CHANNEL", "")
    if home:
        seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


def register(ctx) -> None:
    """Plugin entry point — register the Bale platform adapter."""

    def _factory(config):
        return BaleAdapter(config)

    ctx.register_platform(
        name="bale",
        label="Bale",
        adapter_factory=_factory,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["BALE_BOT_TOKEN"],
        allowed_users_env="BALE_ALLOWED_USERS",
        max_message_length=4096,
        platform_hint=(
            "You are on Bale, an Iranian messaging platform. "
            "Plain text is recommended; Bale renders basic markdown (bold, italic, code). "
            "Keep replies concise."
        ),
        emoji="📱",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="BALE_HOME_CHANNEL",
    )
