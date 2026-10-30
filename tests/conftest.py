import itertools
import types

import pytest
from telegram.error import BadRequest

from smsgw.config import Settings
from smsgw.store import Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        voipms_api_username="me@example.com",
        voipms_api_password="pw",
        voipms_did="5145550000",
        telegram_bot_token="123:abc",
        telegram_chat_id=-1001234567890,
        webhook_secret="s" * 32,
        db_path=str(tmp_path / "t.db"),
        daily_send_limit=5,
    )


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "t.db"))
    yield s
    await s.close()


class FakeBot:
    """Records Bot API calls; threads in `deleted` raise like Telegram does."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.deleted: set[int] = set()
        self._ids = itertools.count(100)

    def _check(self, kw):
        if kw.get("message_thread_id") in self.deleted:
            raise BadRequest("Message thread not found")

    async def create_forum_topic(self, chat_id, name):
        tid = next(self._ids)
        self.calls.append(("create_forum_topic", {"name": name, "id": tid}))
        return types.SimpleNamespace(message_thread_id=tid)

    async def reopen_forum_topic(self, chat_id, thread_id):
        self._check({"message_thread_id": thread_id})
        self.calls.append(("reopen_forum_topic", {"thread_id": thread_id}))

    async def close_forum_topic(self, chat_id, thread_id):
        self.calls.append(("close_forum_topic", {"thread_id": thread_id}))

    async def edit_forum_topic(self, chat_id, thread_id, name):
        self.calls.append(("edit_forum_topic", {"thread_id": thread_id, "name": name}))

    def __getattr__(self, name):
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def send(*args, **kw):
            self._check(kw)
            self.calls.append((name, kw))

        return send


@pytest.fixture
def bot():
    return FakeBot()
