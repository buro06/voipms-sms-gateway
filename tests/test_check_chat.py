import types

import pytest
from telegram.constants import ChatMemberStatus
from telegram.error import ChatMigrated

from smsgw.topics import check_chat


class Bot:
    id = 42

    def __init__(self, is_forum=True, status=ChatMemberStatus.ADMINISTRATOR, can_manage_topics=True, migrated=None):
        self.is_forum, self.status, self.can_manage_topics, self.migrated = is_forum, status, can_manage_topics, migrated

    async def get_chat(self, chat_id):
        if self.migrated:
            raise ChatMigrated(self.migrated)
        return types.SimpleNamespace(is_forum=self.is_forum, title="SMS")

    async def get_chat_member(self, chat_id, user_id):
        return types.SimpleNamespace(status=self.status, can_manage_topics=self.can_manage_topics)


async def test_ok():
    assert await check_chat(Bot(), -1001) == []


@pytest.mark.parametrize(
    "bot, expected",
    [
        (Bot(is_forum=False), "Topics"),
        (Bot(status=ChatMemberStatus.MEMBER), "not an admin"),
        (Bot(can_manage_topics=False), "Manage Topics"),
        (Bot(migrated=-100999), "TELEGRAM_CHAT_ID=-100999"),
    ],
)
async def test_problems(bot, expected):
    problems = await check_chat(bot, -1)
    assert len(problems) == 1 and expected in problems[0]
