import httpx
import pytest
import respx

from smsgw import voipms as voipms_mod
from smsgw.voipms import VoipMsClient, VoipMsError

URL = "https://voip.test/api"


@pytest.fixture
def client():
    return VoipMsClient("u", "p", URL, http=httpx.AsyncClient())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        pass

    monkeypatch.setattr(voipms_mod.asyncio, "sleep", instant)


@respx.mock
async def test_send_sms_returns_id(client):
    route = respx.get(URL).respond(json={"status": "success", "sms": 42})
    assert await client.send_sms("5145550000", "5145551234", "hi") == "42"
    params = route.calls.last.request.url.params
    assert params["method"] == "sendSMS" and params["dst"] == "5145551234" and params["message"] == "hi"


@respx.mock
async def test_send_mms_posts_base64(client):
    route = respx.post(URL).respond(json={"status": "success", "mms": "7"})
    assert await client.send_mms("5145550000", "5145551234", "pic", [(b"\xff\xd8jpeg", "image/jpeg")]) == "7"
    body = route.calls.last.request.content.decode()
    assert "method=sendMMS" in body and "media1=data%3Aimage%2Fjpeg%3Bbase64%2C" in body


@respx.mock
@pytest.mark.parametrize("status", ["limit_reached", "sms_toolong", "invalid_dst"])
async def test_errors_raise(client, status):
    respx.get(URL).respond(json={"status": status})
    with pytest.raises(VoipMsError) as e:
        await client.send_sms("5145550000", "5145551234", "hi")
    assert e.value.status == status
    assert e.value.is_quota == (status == "limit_reached")


@respx.mock
async def test_rate_limit_retries(client):
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"status": "api_limit_exceeded"}),
            httpx.Response(200, json={"status": "success", "sms": 1}),
        ]
    )
    assert await client.send_sms("d", "n", "m") == "1"
    assert route.call_count == 2


@respx.mock
async def test_recent_inbound_merges_mms_media(client):
    def responder(request):
        method = request.url.params["method"]
        if method == "getSMS":
            return httpx.Response(200, json={"status": "success", "sms": [
                {"id": "2", "date": "2026-10-29 10:01:00", "contact": "5145551234", "did": "5145550000", "message": "b"},
                {"id": "1", "date": "2026-10-29 10:00:00", "contact": "5145551234", "did": "5145550000", "message": "a"},
            ]})
        return httpx.Response(200, json={"status": "success", "mms": [
            {"id": "2", "date": "2026-10-29 10:01:00", "contact": "5145551234", "did": "5145550000", "message": "b",
             "media": ["https://voip.ms/media/x.jpg"]},
        ]})

    respx.get(URL).mock(side_effect=responder)
    msgs = await client.recent_inbound("5145550000")
    assert [m.id for m in msgs] == ["1", "2"]
    assert msgs[1].media == ["https://voip.ms/media/x.jpg"]


@respx.mock
async def test_recent_inbound_empty(client):
    respx.get(URL).respond(json={"status": "no_sms"})
    assert await client.recent_inbound("5145550000") == []
