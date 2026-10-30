"""Periodically pull recent inbound messages from the API to catch any missed callbacks."""
import asyncio
import logging

from .inbound import Inbound
from .numbers import normalize_lenient
from .store import Store
from .voipms import VoipMsClient

log = logging.getLogger(__name__)

SEEDED_KEY = "reconcile_seeded"


class Reconciler:
    def __init__(self, voipms: VoipMsClient, store: Store, inbound: Inbound, did: str, interval: int):
        self.voipms = voipms
        self.store = store
        self.inbound = inbound
        self.did = did
        self.interval = interval

    async def once(self) -> int:
        messages = await self.voipms.recent_inbound(self.did)
        if await self.store.get(SEEDED_KEY) is None:
            # First run: treat existing history as already seen instead of flooding the group.
            for m in messages:
                await self.store.log_in(m.id, normalize_lenient(m.number), m.text, len(m.media))
            await self.store.put(SEEDED_KEY, "1")
            log.info("reconciler seeded with %d existing messages", len(messages))
            return 0
        delivered = 0
        for m in messages:
            if await self.inbound.deliver(m):
                delivered += 1
        if delivered:
            log.warning("reconciler delivered %d message(s) the callback missed", delivered)
        return delivered

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.once()
            except Exception:
                log.exception("reconcile failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
