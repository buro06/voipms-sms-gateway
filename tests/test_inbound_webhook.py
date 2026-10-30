import pytest
from aiohttp.test_utils import TestClient, TestServer

from smsgw.inbound import Inbound
from smsgw.reconcile import Reconciler
from smsgw.topics import Topics
from smsgw.voipms import Message
from smsgw.webhook import make_app

CHAT = -1001234567890


class FakeVoipMs:
    def __init__(self):
        self.mms_lookups = []
        self.inbox: list[Message] = []

    async def download(self, url):
        return b"\xff\xd8img", "image/jpeg"

    async def get_mms(self, mms_id):
        self.mms_lookups.append(mms_id)
        return None

    async def recent_inbound(self, did):
        return list(self.inbox)


@pytest.fixture
def voipms():
    return FakeVoipMs()


@pytest.fixture
def inbound(bot, store, voipms):
    return Inbound(bot, CHAT, store, Topics(bot, CHAT, store), voipms)


def msg(mid="1", number="5145551234", text="hi", media=()):
    return Message(id=mid, date="", number=number, did="5145550000", text=text, media=list(media))


def sends(bot):
    return [(name, kw) for name, kw in bot.calls if name.startswith("send_")]


async def test_creates_topic_once_and_dedupes(bot, store, inbound):
    assert await inbound.deliver(msg("1"))
    assert await inbound.deliver(msg("2", text="again"))
    assert not await inbound.deliver(msg("1"))
    topics = [c for c in bot.calls if c[0] == "create_forum_topic"]
    assert len(topics) == 1 and topics[0][1]["name"] == "+1 (514) 555-1234"
    assert [kw["text"] for _, kw in sends(bot)] == ["hi", "again"]
    contact = await store.contact("5145551234")
    assert all(kw["message_thread_id"] == contact.thread_id for _, kw in sends(bot))


async def test_recreates_deleted_topic(bot, store, inbound):
    await inbound.deliver(msg("1"))
    old = (await store.contact("5145551234")).thread_id
    bot.deleted.add(old)
    await inbound.deliver(msg("2"))
    new = (await store.contact("5145551234")).thread_id
    assert new != old
    assert sends(bot)[-1][1]["message_thread_id"] == new


async def test_reopens_closed_topic(bot, store, inbound):
    await inbound.deliver(msg("1"))
    await store.set_closed("5145551234", True)
    await inbound.deliver(msg("2"))
    assert any(c[0] == "reopen_forum_topic" for c in bot.calls)
    assert not (await store.contact("5145551234")).closed


async def test_mms_posts_photo_with_caption(bot, inbound):
    await inbound.deliver(msg("1", text="look", media=["https://voip.ms/a.jpg"]))
    name, kw = sends(bot)[-1]
    assert name == "send_photo" and kw["caption"] == "look"


async def test_reconciler_seeds_then_delivers(bot, store, inbound, voipms):
    rec = Reconciler(voipms, store, inbound, "5145550000", 300)
    voipms.inbox = [msg("old")]
    assert await rec.once() == 0 and not sends(bot)
    voipms.inbox.append(msg("new", text="missed"))
    assert await rec.once() == 1
    assert [kw["text"] for _, kw in sends(bot)] == ["missed"]


@pytest.fixture
async def http(settings, voipms, inbound):
    client = TestClient(TestServer(make_app(settings, voipms, inbound)))
    await client.start_server()
    yield client
    await client.close()


async def test_webhook_wrong_secret(http):
    resp = await http.get("/voipms/wrong?id=1&from=5145551234&message=x")
    assert resp.status == 404


async def test_webhook_ok_and_idempotent(http, settings, bot, voipms):
    url = f"/voipms/{settings.webhook_secret}?id=9&from=5145551234&to=5145550000&message=hello&media="
    for _ in range(2):
        resp = await http.get(url)
        assert resp.status == 200 and await resp.text() == "ok"
    assert [kw["text"] for _, kw in sends(bot)] == ["hello"]
    assert voipms.mms_lookups == []


async def test_webhook_without_media_param_checks_mms(http, settings, voipms):
    resp = await http.get(f"/voipms/{settings.webhook_secret}?id=10&from=5145551234&message=hi&media={{MEDIA}}")
    assert await resp.text() == "ok"
    assert voipms.mms_lookups == ["10"]


async def test_webhook_other_did_ignored(http, settings, bot):
    resp = await http.get(f"/voipms/{settings.webhook_secret}?id=11&from=5145551234&to=5145559999&message=x")
    assert await resp.text() == "ok"
    assert not sends(bot)
