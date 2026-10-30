from datetime import date, timedelta

import pytest

from smsgw.outbound import Outbound, QuotaExceeded, plan_parts
from smsgw.voipms import VoipMsError


def test_short_text_is_sms():
    assert [(p.kind, p.text) for p in plan_parts("hello", [])] == [("sms", "hello")]


def test_long_text_is_one_mms():
    parts = plan_parts("x " * 200, [])
    assert [p.kind for p in parts] == ["mms"]


def test_long_text_split_mode():
    parts = plan_parts("word " * 100, [], long_text_mode="split")
    assert {p.kind for p in parts} == {"sms"} and len(parts) == 4


def test_images_grouped_by_three():
    parts = plan_parts("caption", [b"1", b"2", b"3", b"4"])
    assert [(p.kind, p.text, len(p.media)) for p in parts] == [("mms", "caption", 3), ("mms", "", 1)]


def test_empty_is_nothing():
    assert plan_parts("   ", []) == []


class FakeVoipMs:
    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    async def send_sms(self, did, dst, message):
        if self.fail_with:
            raise VoipMsError(self.fail_with)
        self.sent.append(("sms", dst, message))
        return str(len(self.sent))

    async def send_mms(self, did, dst, message, media=()):
        self.sent.append(("mms", dst, message, len(media)))
        return str(len(self.sent))


@pytest.fixture
def notices():
    return []


def make(settings, store, voipms, notices):
    async def notify(text):
        notices.append(text)

    return Outbound(settings, store, voipms, notify)


async def test_send_counts_quota_and_warns(settings, store, notices):
    voipms = FakeVoipMs()
    out = make(settings, store, voipms, notices)  # limit 5, warn at 4
    for i in range(5):
        await out.send("5145551234", f"msg {i}")
    assert len(voipms.sent) == 5
    assert (await store.sends_today())[0] == 5
    assert len(notices) == 2 and notices[0].startswith("⚠️") and notices[1].startswith("⛔")
    with pytest.raises(QuotaExceeded):
        await out.send("5145551234", "one too many")
    assert len(voipms.sent) == 5


async def test_limit_reached_marks_exhausted(settings, store, notices):
    out = make(settings, store, FakeVoipMs(fail_with="limit_reached"), notices)
    with pytest.raises(VoipMsError):
        await out.send("5145551234", "hi")
    assert (await store.sends_today()) == (0, True)
    with pytest.raises(QuotaExceeded):
        await out.send("5145551234", "hi")


async def test_quota_rolls_over(store):
    yesterday = date.today() - timedelta(days=1)
    await store.count_send(5, today=yesterday)
    await store.mark_exhausted(today=yesterday)
    assert await store.sends_today() == (0, False)
