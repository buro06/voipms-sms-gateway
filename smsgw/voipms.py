"""Minimal async client for the voip.ms REST API (SMS/MMS methods only)."""
import asyncio
import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

SMS_MAX_CHARS = 160
MMS_MAX_CHARS = 2048
MMS_MAX_MEDIA = 3

_EMPTY_STATUSES = {"no_sms", "no_mms", "no_messages"}


class VoipMsError(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status

    @property
    def is_quota(self) -> bool:
        return self.status == "limit_reached"


@dataclass
class Message:
    id: str
    date: str
    number: str  # the external party ("contact")
    did: str
    text: str
    media: list[str] = field(default_factory=list)


def _media_urls(row: dict) -> list[str]:
    media = row.get("media")
    urls: list[str] = []
    if isinstance(media, list):
        urls = [m for m in media if isinstance(m, str)]
    elif isinstance(media, dict):
        urls = [m for m in media.values() if isinstance(m, str)]
    if not urls:
        urls = [row[k] for k in ("col_media1", "col_media2", "col_media3") if row.get(k)]
    return [u for u in urls if u.startswith("http")]


class VoipMsClient:
    def __init__(self, username: str, password: str, url: str, http: httpx.AsyncClient | None = None):
        self._auth = {"api_username": username, "api_password": password}
        self._url = url
        self._http = http or httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, params: dict, *, post: bool = False) -> dict:
        data = {**self._auth, "method": method, **{k: v for k, v in params.items() if v is not None}}
        delay = 1.0
        for attempt in range(5):
            try:
                if post:
                    resp = await self._http.post(self._url, data=data)
                else:
                    resp = await self._http.get(self._url, params=data)
            except httpx.ConnectError:
                # Nothing reached voip.ms, so retrying cannot double-send.
                if attempt == 4:
                    raise
                log.warning("voip.ms %s: connect error, retrying in %.0fs", method, delay)
            else:
                resp.raise_for_status()
                body = resp.json()
                status = body.get("status")
                if status == "success":
                    return body
                if status != "api_limit_exceeded" or attempt == 4:
                    raise VoipMsError(status or "unknown_error")
                log.warning("voip.ms %s: rate limited, retrying in %.0fs", method, delay)
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def send_sms(self, did: str, dst: str, message: str) -> str:
        body = await self._call("sendSMS", {"did": did, "dst": dst, "message": message})
        return str(body.get("sms", ""))

    async def send_mms(self, did: str, dst: str, message: str, media: Sequence[tuple[bytes, str]] = ()) -> str:
        """media: up to 3 (bytes, mime type) pairs, sent inline as base64 data URIs."""
        params = {"did": did, "dst": dst, "message": message}
        for i, (blob, mime) in enumerate(media[:MMS_MAX_MEDIA], start=1):
            params[f"media{i}"] = f"data:{mime};base64,{base64.b64encode(blob).decode()}"
        body = await self._call("sendMMS", params, post=True)
        return str(body.get("mms", ""))

    async def _get(self, method: str, list_key: str, did: str, days: int, limit: int) -> list[dict]:
        today = date.today()
        params = {
            "did": did,
            "type": 1,  # received
            "from": (today - timedelta(days=days)).isoformat(),
            "to": (today + timedelta(days=1)).isoformat(),  # covers account-timezone skew
            "limit": limit,
        }
        if method == "getSMS":
            params["all_messages"] = 1
        try:
            body = await self._call(method, params)
        except VoipMsError as e:
            if e.status in _EMPTY_STATUSES:
                return []
            raise
        rows = body.get(list_key) or body.get("sms") or body.get("mms") or []
        return rows if isinstance(rows, list) else []

    async def recent_inbound(self, did: str, days: int = 1, limit: int = 200) -> list[Message]:
        """Received SMS and MMS for the DID, oldest first, with MMS media merged in."""
        sms_rows = await self._get("getSMS", "sms", did, days, limit)
        mms_rows = await self._get("getMMS", "mms", did, days, limit)
        by_id: dict[str, Message] = {}
        for row in sms_rows + mms_rows:
            mid = str(row.get("id", ""))
            if not mid:
                continue
            msg = by_id.get(mid) or Message(
                id=mid,
                date=str(row.get("date", "")),
                number=str(row.get("contact", "")),
                did=str(row.get("did", did)),
                text=row.get("message") or "",
            )
            msg.media = msg.media or _media_urls(row)
            by_id[mid] = msg
        return sorted(by_id.values(), key=lambda m: (m.date, m.id))

    async def get_mms(self, mms_id: str) -> Message | None:
        try:
            body = await self._call("getMMS", {"mms": mms_id})
        except VoipMsError as e:
            if e.status in _EMPTY_STATUSES:
                return None
            raise
        rows = body.get("mms") or body.get("sms") or []
        if not rows:
            return None
        row = rows[0]
        return Message(
            id=str(row.get("id", mms_id)),
            date=str(row.get("date", "")),
            number=str(row.get("contact", "")),
            did=str(row.get("did", "")),
            text=row.get("message") or "",
            media=_media_urls(row),
        )

    async def download(self, url: str) -> tuple[bytes, str]:
        resp = await self._http.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "application/octet-stream").split(";")[0]
