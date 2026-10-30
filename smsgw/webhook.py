"""HTTP endpoint for the voip.ms SMS/MMS URL callback.

Configure the DID's URL callback in the voip.ms portal as:
  https://<host>/voipms/<WEBHOOK_SECRET>?id={ID}&date={DATE}&from={FROM}&to={TO}&message={MESSAGE}&media={MEDIA}
"""
import hashlib
import hmac
import logging
import re

from aiohttp import web

from .config import Settings
from .inbound import Inbound
from .numbers import normalize_lenient
from .voipms import Message, VoipMsClient

log = logging.getLogger(__name__)


def _param(params, key: str) -> str | None:
    """Query value, or None if absent or still an unexpanded {PLACEHOLDER}."""
    value = params.get(key)
    if value is None or re.fullmatch(r"\{[A-Z_]+\}", value):
        return None
    return value


def make_app(settings: Settings, voipms: VoipMsClient, inbound: Inbound) -> web.Application:
    async def callback(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.match_info["secret"].encode(), settings.webhook_secret.encode()):
            raise web.HTTPNotFound()
        params = dict(request.query)
        if request.method == "POST":
            params.update(await request.post())

        sender = _param(params, "from")
        if not sender:
            log.warning("callback without sender: %s", sorted(params))
            return web.Response(text="ok")
        to = _param(params, "to")
        if to and normalize_lenient(to) != settings.voipms_did:
            log.warning("callback for other DID %s ignored", to)
            return web.Response(text="ok")

        text = _param(params, "message") or ""
        date = _param(params, "date") or ""
        mid = _param(params, "id")
        if not mid:
            log.warning("callback without {ID}; add id={ID} to the callback URL to avoid duplicates")
            mid = "cb-" + hashlib.sha1(f"{sender}|{date}|{text}".encode()).hexdigest()[:16]

        media_param = _param(params, "media")
        media = [u for u in re.split(r"[\s,]+", media_param or "") if u.startswith("http")]
        if media_param is None and not mid.startswith("cb-"):
            # No {MEDIA} in the callback URL: ask the API whether this was an MMS.
            try:
                mms = await voipms.get_mms(mid)
                if mms:
                    media, text = mms.media, mms.text or text
            except Exception:
                log.exception("getMMS lookup for %s failed", mid)

        msg = Message(id=mid, date=date, number=sender, did=to or settings.voipms_did, text=text, media=media)
        try:
            await inbound.deliver(msg)
        except Exception:
            log.exception("failed to deliver inbound %s; voip.ms/reconciler will retry", mid)
            return web.Response(status=500, text="error")
        return web.Response(text="ok")

    async def healthz(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_route("GET", "/voipms/{secret}", callback)
    app.router.add_route("POST", "/voipms/{secret}", callback)
    app.router.add_get("/healthz", healthz)
    return app
