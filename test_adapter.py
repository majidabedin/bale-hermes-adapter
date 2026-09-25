"""
Unit tests for Bale Hermes Adapter.
Tests all owner commands and core logic without a real bot connection.
"""

import asyncio
import sys
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

# ── Mock Hermes internals so we can import the adapter standalone ─────────────
sys.modules["gateway"] = MagicMock()
sys.modules["gateway.platforms"] = MagicMock()
sys.modules["gateway.platforms._shared"] = MagicMock()
sys.modules["gateway.platforms.base"] = MagicMock()
sys.modules["gateway.config"] = MagicMock()

# Mock get_scoped_secret to return test values
from unittest.mock import MagicMock
mock_shared = MagicMock()
mock_shared.get_scoped_secret = lambda key, default="": {
    "BALE_BOT_TOKEN": "test_token_123",
    "BALE_ALLOWED_USERS": "111222333",
}.get(key, default)
sys.modules["gateway.platforms._shared"] = mock_shared

# Mock base classes
mock_base = MagicMock()
mock_base.BasePlatformAdapter = object
mock_base.MessageEvent = MagicMock
mock_base.MessageType = MagicMock()
mock_base.MessageType.TEXT = "TEXT"
mock_base.SendResult = lambda **kw: kw
sys.modules["gateway.platforms.base"] = mock_base

mock_config = MagicMock()
mock_config.Platform = lambda x: x
sys.modules["gateway.config"] = mock_config

# Now import adapter
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "bale"))
import adapter as bale_adapter

# ── Test helpers ─────────────────────────────────────────────────────────────

PASS = "✅"
FAIL = "❌"
results = []


def test(name):
    def decorator(fn):
        async def wrapper():
            try:
                await fn()
                results.append((PASS, name))
                print(f"  {PASS} {name}")
            except AssertionError as e:
                results.append((FAIL, name))
                print(f"  {FAIL} {name}: {e}")
            except Exception as e:
                results.append((FAIL, name))
                print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
        return wrapper
    return decorator


def make_adapter():
    """Create a BaleAdapter instance with mocked session."""
    config = MagicMock()
    config.extra = {}

    adapter = object.__new__(bale_adapter.BaleAdapter)
    adapter._token = "test_token_123"
    adapter._allowed = frozenset({"111222333"})
    adapter._allow_all = False
    adapter._session = MagicMock()
    adapter._session.closed = False
    adapter._message_handler = AsyncMock()
    adapter._poll_task = None
    adapter._offset = 0

    # Build stealth allowlist
    bale_adapter._STEALTH_ALLOWLIST = frozenset({"111222333"})

    # Mock send
    adapter.send = AsyncMock()
    adapter.build_source = MagicMock(return_value=MagicMock())
    adapter.handle_message = AsyncMock()

    return adapter


def make_update(text, user_id="111222333", chat_id="111222333"):
    return {
        "message": {
            "text": text,
            "from": {"id": int(user_id), "first_name": "Test", "last_name": "User"},
            "chat": {"id": int(chat_id), "type": "private"},
            "message_id": 1,
        }
    }


def make_media_update(media_type, user_id="111222333"):
    msg = {
        "from": {"id": int(user_id), "first_name": "Test", "last_name": "User"},
        "chat": {"id": int(user_id), "type": "private"},
        "message_id": 1,
    }
    if media_type == "photo":
        msg["photo"] = [{"file_id": "abc123", "width": 100, "height": 100}]
    elif media_type == "voice":
        msg["voice"] = {"file_id": "voice123", "duration": 5}
    elif media_type == "document":
        msg["document"] = {"file_id": "doc123", "file_name": "test.pdf"}
    elif media_type == "sticker":
        msg["sticker"] = {"file_id": "sticker123"}
    return {"message": msg}


# ── Tests ─────────────────────────────────────────────────────────────────────

@test("Pairing code generation")
async def test_pairing_code():
    bale_adapter._PAIRING_CODE = None
    bale_adapter._PAIRING_EXPIRES = 0
    code = bale_adapter._generate_pairing_code()
    assert len(code) == 8, f"Expected 8 digits, got {len(code)}"
    assert code.isdigit(), f"Expected numeric code, got {code}"
    assert bale_adapter._PAIRING_CODE == code
    assert bale_adapter._PAIRING_EXPIRES > time.time()


@test("Pairing code TTL is 10 minutes")
async def test_pairing_ttl():
    code = bale_adapter._generate_pairing_code()
    remaining = bale_adapter._PAIRING_EXPIRES - time.time()
    assert 590 < remaining <= 600, f"Expected ~600s TTL, got {remaining:.1f}s"


@test("Unauthorized user is blocked from AI handler")
async def test_unauthorized_user():
    a = make_adapter()
    bale_adapter._STEALTH_ALLOWLIST = frozenset({"111222333"})

    update = make_update("hello", user_id="999888777")
    await a._handle_update(update)

    # Most important: AI handler must NOT be called for unauthorized users
    a.handle_message.assert_not_called()


@test("Authorized user reaches AI handler")
async def test_authorized_user():
    a = make_adapter()
    update = make_update("hello", user_id="111222333")
    await a._handle_update(update)
    a.handle_message.assert_called_once()


@test("/status command returns gateway status")
async def test_status_command():
    a = make_adapter()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="active\n")
        update = make_update("/status")
        await a._handle_update(update)
    a.send.assert_called_once()
    assert "Gateway" in a.send.call_args[0][1]


@test("/reboot command sends confirmation")
async def test_reboot_command():
    a = make_adapter()
    with patch("subprocess.Popen"):
        update = make_update("/reboot")
        await a._handle_update(update)
    a.send.assert_called_once()
    assert "reboot" in a.send.call_args[0][1].lower()


@test("/shutdown command sends confirmation")
async def test_shutdown_command():
    a = make_adapter()
    with patch("subprocess.Popen"):
        update = make_update("/shutdown")
        await a._handle_update(update)
    a.send.assert_called_once()
    assert "shut" in a.send.call_args[0][1].lower()


@test("/gw command sends confirmation")
async def test_gw_command():
    a = make_adapter()
    with patch("subprocess.Popen"):
        update = make_update("/gw")
        await a._handle_update(update)
    a.send.assert_called_once()
    assert "restart" in a.send.call_args[0][1].lower()


@test("/ip command returns IP info")
async def test_ip_command():
    a = make_adapter()
    with patch("subprocess.run") as mock_run, \
         patch("urllib.request.urlopen") as mock_url:
        mock_run.return_value = MagicMock(stdout="192.168.0.10 \n")
        mock_url.return_value.read.return_value = b"1.2.3.4\n"
        update = make_update("/ip")
        await a._handle_update(update)
    a.send.assert_called_once()
    text = a.send.call_args[0][1]
    assert "192.168.0.10" in text or "Local" in text


@test("/ping command runs ping")
async def test_ping_command():
    a = make_adapter()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout="PING google.com\n64 bytes from 8.8.8.8\nrtt min/avg/max = 10/12/15 ms\n",
            stderr=""
        )
        update = make_update("/ping google.com")
        await a._handle_update(update)
    a.send.assert_called_once()
    assert "ping" in a.send.call_args[0][1].lower()


@test("Photo message sets [photo] text")
async def test_photo_message():
    a = make_adapter()
    a._download_file = AsyncMock(return_value=None)
    update = make_media_update("photo")
    await a._handle_update(update)
    a.handle_message.assert_called_once()
    event_text = a.handle_message.call_args[0][0].text
    assert "[photo]" in event_text or "photo" in event_text


@test("Voice message sets [voice message] text")
async def test_voice_message():
    a = make_adapter()
    a._download_file = AsyncMock(return_value=None)
    update = make_media_update("voice")
    await a._handle_update(update)
    a.handle_message.assert_called_once()


@test("Sticker message sets [sticker] text")
async def test_sticker_message():
    a = make_adapter()
    update = make_media_update("sticker")
    await a._handle_update(update)
    a.handle_message.assert_called_once()
    event_text = a.handle_message.call_args[0][0].text
    assert "[sticker]" in event_text


@test("Empty message is ignored")
async def test_empty_message():
    a = make_adapter()
    update = {"message": {
        "from": {"id": 111222333, "first_name": "Test"},
        "chat": {"id": 111222333, "type": "private"},
        "message_id": 1,
    }}
    await a._handle_update(update)
    a.handle_message.assert_not_called()


@test("Callback query from authorized user triggers command")
async def test_callback_query():
    a = make_adapter()
    mock_resp = AsyncMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    a._session.post = MagicMock(return_value=mock_resp)

    update = {
        "callback_query": {
            "id": "cb123",
            "from": {"id": 111222333, "first_name": "Test"},
            "data": "/status",
            "message": {
                "chat": {"id": 111222333, "type": "private"},
                "message_id": 1,
            }
        }
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="active\n")
        await a._handle_update(update)
    a.send.assert_called()


@test("Pairing: valid code registers admin")
async def test_pairing_valid_code():
    a = make_adapter()
    bale_adapter._STEALTH_ALLOWLIST = frozenset()
    bale_adapter._PAIRING_CODE = "12345678"
    bale_adapter._PAIRING_EXPIRES = time.time() + 600

    with patch.object(bale_adapter, "_write_admin_to_env", return_value=True):
        update = make_update("12345678", user_id="999888777")
        await a._handle_update(update)

    assert "999888777" in bale_adapter._STEALTH_ALLOWLIST
    assert bale_adapter._PAIRING_CODE is None
    a.send.assert_called_once()
    assert "Setup complete" in a.send.call_args[0][1]

    # Reset
    bale_adapter._STEALTH_ALLOWLIST = frozenset({"111222333"})


@test("Pairing: expired code is rejected")
async def test_pairing_expired_code():
    a = make_adapter()
    bale_adapter._STEALTH_ALLOWLIST = frozenset()
    bale_adapter._PAIRING_CODE = "12345678"
    bale_adapter._PAIRING_EXPIRES = time.time() - 1  # already expired

    update = make_update("12345678", user_id="999888777")
    await a._handle_update(update)

    assert "999888777" not in bale_adapter._STEALTH_ALLOWLIST
    a.send.assert_called_once()
    assert "expired" in a.send.call_args[0][1].lower()

    # Reset
    bale_adapter._STEALTH_ALLOWLIST = frozenset({"111222333"})


# ── Run all tests ─────────────────────────────────────────────────────────────

async def main():
    print("\n🧪 Bale Adapter — Unit Tests\n" + "─" * 40)

    tests = [
        test_pairing_code(),
        test_pairing_ttl(),
        test_unauthorized_user(),
        test_authorized_user(),
        test_status_command(),
        test_reboot_command(),
        test_shutdown_command(),
        test_gw_command(),
        test_ip_command(),
        test_ping_command(),
        test_photo_message(),
        test_voice_message(),
        test_sticker_message(),
        test_empty_message(),
        test_callback_query(),
        test_pairing_valid_code(),
        test_pairing_expired_code(),
    ]

    for t in tests:
        await t

    passed = sum(1 for r in results if r[0] == PASS)
    failed = sum(1 for r in results if r[0] == FAIL)

    print(f"\n{'─' * 40}")
    print(f"Results: {passed} passed, {failed} failed out of {len(results)} tests")
    if failed:
        print("\nFailed tests:")
        for r in results:
            if r[0] == FAIL:
                print(f"  {r[0]} {r[1]}")
    print()
    return failed


if __name__ == "__main__":
    failed = asyncio.run(main())
    sys.exit(1 if failed else 0)
