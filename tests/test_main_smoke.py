"""Run the real entrypoint against a mocked Telegram/voip.ms and check a /help command gets answered."""
import asyncio
import json
import os
import signal
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from smsgw.__main__ import main

CHAT = -100123
BOT_USER = {"id": 42, "is_bot": True, "first_name": "T", "username": "tbot"}
HUMAN = {"id": 7, "is_bot": False, "first_name": "U"}
GROUP = {"id": CHAT, "type": "supergroup", "title": "SMS", "is_forum": True}


def help_update():
    return {"update_id": 1, "message": {"message_id": 5, "date": 0, "chat": GROUP, "from": HUMAN,
                                        "text": "/help", "entities": [{"type": "bot_command", "offset": 0, "length": 5}]}}


def _params(request: httpx.Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return json.loads(request.content or b"{}")
    if ctype.startswith("application/x-www-form-urlencoded"):
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    return {"raw": request.content.decode(errors="replace")}


def telegram_api(sent: list, delivered: list):
    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = _params(request)
        if method == "getUpdates":
            await asyncio.sleep(0.05)
            result = [] if delivered else [help_update()]
            delivered.append(True)
        elif method == "getMe":
            result = {**BOT_USER, "can_join_groups": True, "can_read_all_group_messages": False,
                      "supports_inline_queries": False}
        elif method == "getChat":
            result = {**GROUP, "accent_color_id": 0, "max_reaction_count": 11,
                      "accepted_gift_types": {"unlimited_gifts": False, "limited_gifts": False,
                                              "unique_gifts": False, "premium_subscription": False,
                                              "gifts_from_channels": False}}
        elif method == "getChatMember":
            result = {"status": "administrator", "user": BOT_USER, "can_be_edited": False, "is_anonymous": False,
                      "can_manage_chat": True, "can_delete_messages": True, "can_manage_video_chats": True,
                      "can_restrict_members": True, "can_promote_members": False, "can_change_info": True,
                      "can_invite_users": True, "can_post_stories": False, "can_edit_stories": False,
                      "can_delete_stories": False, "can_manage_topics": True, "can_pin_messages": True}
        elif method == "sendMessage":
            sent.append(body)
            result = {"message_id": 99, "date": 0, "chat": GROUP, "from": BOT_USER, "text": body.get("text", "")}
            os.kill(os.getpid(), signal.SIGTERM)  # answered: shut the gateway down
        else:  # deleteWebhook, deleteMyCommands, setMyCommands, ...
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    return handler


@pytest.mark.parametrize("chat_id", [0, CHAT], ids=["discovery", "configured"])
async def test_gateway_answers_commands(tmp_path, monkeypatch, chat_id):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for key, value in {
        "VOIPMS_API_USERNAME": "u", "VOIPMS_API_PASSWORD": "p", "VOIPMS_DID": "5145550000",
        "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": str(chat_id), "WEBHOOK_SECRET": "s" * 32,
        "WEBHOOK_PORT": "18089", "DB_PATH": str(tmp_path / "t.db"),
    }.items():
        monkeypatch.setenv(key, value)

    sent, delivered = [], []
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.telegram.org").mock(side_effect=telegram_api(sent, delivered))
        mock.route(host="voip.ms").respond(json={"status": "no_sms"})
        assert await asyncio.wait_for(main(), timeout=15) == 0

    assert len(sent) == 1
    expected = "chat id: -100123" if chat_id == 0 else "SMS gateway"
    assert expected in sent[0].get("text", sent[0].get("raw", ""))
