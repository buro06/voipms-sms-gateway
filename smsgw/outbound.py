"""Send Telegram topic messages out as SMS/MMS, enforcing the daily API send quota."""
import asyncio
import logging
import math
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable, Sequence

from .config import Settings
from .media import chunk_text, compress_image
from .store import Store
from .voipms import MMS_MAX_CHARS, MMS_MAX_MEDIA, SMS_MAX_CHARS, VoipMsClient, VoipMsError

log = logging.getLogger(__name__)

WARN_FRACTION = 0.8


class QuotaExceeded(Exception):
    pass


@dataclass
class Part:
    kind: str  # "sms" | "mms"
    text: str
    media: list[bytes] = field(default_factory=list)


def plan_parts(text: str, images: list[bytes], long_text_mode: str = "mms") -> list[Part]:
    """Decide how a Telegram message maps onto voip.ms sends."""
    text = text.strip()
    if images:
        texts = chunk_text(text, MMS_MAX_CHARS) or [""]
        groups = [images[i : i + MMS_MAX_MEDIA] for i in range(0, len(images), MMS_MAX_MEDIA)]
        parts = [Part("mms", texts[0], groups[0])]
        parts += [Part("mms", "", g) for g in groups[1:]]
        parts += [Part("mms", t) for t in texts[1:]]
        return parts
    if len(text) <= SMS_MAX_CHARS:
        return [Part("sms", text)] if text else []
    if long_text_mode == "mms":
        return [Part("mms", t) for t in chunk_text(text, MMS_MAX_CHARS)]
    return [Part("sms", t) for t in chunk_text(text, SMS_MAX_CHARS)]


class Outbound:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        voipms: VoipMsClient,
        notify: Callable[[str], Awaitable[None]],
    ):
        self.settings = settings
        self.store = store
        self.voipms = voipms
        self.notify = notify
        self._lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        return self.settings.daily_send_limit

    async def send(self, number: str, text: str, images: Sequence[bytes] = (), tg_user_id: int | None = None) -> int:
        """Send to number; returns the number of voip.ms messages used. Raises QuotaExceeded / VoipMsError."""
        compressed = [
            await asyncio.to_thread(compress_image, blob, self.settings.mms_max_image_bytes) for blob in images
        ]
        parts = plan_parts(text, compressed, self.settings.long_text_mode)
        if not parts:
            return 0
        async with self._lock:
            count, exhausted = await self.store.sends_today()
            if exhausted or count + len(parts) > self.limit:
                raise QuotaExceeded(f"{count}/{self.limit} sends used today; this message needs {len(parts)}")
            for part in parts:
                try:
                    if part.kind == "sms":
                        mid = await self.voipms.send_sms(self.settings.voipms_did, number, part.text)
                    else:
                        media = [(blob, "image/jpeg") for blob in part.media]
                        mid = await self.voipms.send_mms(self.settings.voipms_did, number, part.text, media)
                except VoipMsError as e:
                    if e.is_quota:
                        await self.store.mark_exhausted()
                        await self.notify(f"⛔ voip.ms daily send limit reached ({count}/{self.limit} counted locally).")
                    raise
                previous, count = count, await self.store.count_send()
                await self.store.log_out(mid, number, part.text, len(part.media), tg_user_id)
                log.info("sent %s %s to %s", part.kind, mid, number)
                await self._maybe_warn(previous, count)
        return len(parts)

    async def _maybe_warn(self, previous: int, count: int) -> None:
        threshold = math.ceil(self.limit * WARN_FRACTION)
        if previous < threshold <= count < self.limit:
            await self.notify(f"⚠️ {count}/{self.limit} SMS/MMS sends used today.")
        elif previous < self.limit <= count:
            await self.notify(f"⛔ Daily send limit reached ({count}/{self.limit}). Sending resumes tomorrow.")
