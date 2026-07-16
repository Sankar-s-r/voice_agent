"""Fallback / engagement manager for the voice pipeline.

Why an Observer and not a FrameProcessor in the chain:

A FrameProcessor only sees frames that flow through *its* position in the
linear pipeline (transport.input -> stt -> user_aggregator -> llm -> tts ->
transport.output -> assistant_aggregator). The signal we need to react to
("the user just finished talking") comes from the STT/context side, but the
signal that tells us the *real* response has started ("the bot has started
speaking") comes from the TTS/transport side, further downstream. A single
in-chain processor can't naturally see both without frames being explicitly
routed upstream.

Pipecat's Observer API exists for exactly this: it sees every frame that
moves between every pair of processors in the pipeline, regardless of
position, without being wired into the chain itself. That's the correct
place for a cross-cutting concern like "how long has it been since the user
stopped talking, and has anything happened yet."

Verified against pipecat-ai 1.5.0: BaseObserver.on_push_frame(data), the
FramePushed dataclass, PipelineTask.queue_frames() semantics ("downstream
frames are pushed from the beginning of the pipeline"), and TTSSpeakFrame
were all confirmed by inspecting the installed package before writing this.
"""

import asyncio
import random

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed


class FallbackEngagementObserver(BaseObserver):
    """Watches turn timing and speaks a filler if the real response is slow.

    Two tiers, matching the assignment's "keep the user engaged, don't go
    silent or throw a generic error" requirement:

      - short_delay_secs: a quick backchannel ("Let me see...") if nothing
        has started playing yet by this point. Covers the common case of a
        slightly-slower-than-usual LLM/TTS round trip.
      - long_delay_secs: a longer "still working on it" filler for genuine
        slowdowns (cold model, provider rate limit, network hiccup).

    Both tiers currently go through TTSSpeakFrame, which asks the live
    Cartesia service to synthesize the phrase on the spot. At ~150-300ms
    that's fast enough to be a fine default. If you want genuinely zero-
    latency injection later, pre-render these phrases once to raw PCM and
    push TTSAudioRawFrame(audio=pcm_bytes, sample_rate=.., num_channels=1)
    directly instead — same attach_task() wiring, just a different frame
    type in _fire_after().
    """

    SHORT_FILLERS = [
        "Let me see.",
        "Good question, one moment.",
        "Hmm, let's see.",
    ]
    LONG_FILLERS = [
        "Still working on that, thanks for waiting.",
        "Just about there, hang tight.",
    ]

    def __init__(self, *, short_delay_secs: float = 0.8, long_delay_secs: float = 2.0):
        super().__init__()
        self._short_delay = short_delay_secs
        self._long_delay = long_delay_secs
        self._task = None  # wired in via attach_task() after PipelineTask exists
        self._turn_id = 0
        self._response_started = False
        self._pending_timers: list[asyncio.Task] = []

    def attach_task(self, task) -> None:
        """Call this once, right after constructing PipelineTask(...).

        Chicken-and-egg fix: the observer has to be passed into PipelineTask's
        constructor, but it also needs a reference *back* to that task so it
        can call task.queue_frames() later. Constructing the observer first
        and wiring the task in afterward breaks the cycle.
        """
        self._task = task

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        if isinstance(frame, UserStoppedSpeakingFrame):
            # User's turn just ended — start the clock.
            self._turn_id += 1
            self._response_started = False
            self._arm_timers(self._turn_id)

        elif isinstance(frame, UserStartedSpeakingFrame):
            # Barge-in, or the user started a new turn — whatever filler
            # timers were pending are no longer relevant.
            self._response_started = True
            self._cancel_timers()

        elif isinstance(frame, (BotStartedSpeakingFrame, TTSStartedFrame)):
            # The real response has started producing audio. Stand down.
            self._response_started = True
            self._cancel_timers()

    def _arm_timers(self, turn_id: int) -> None:
        self._cancel_timers()
        self._pending_timers = [
            asyncio.create_task(
                self._fire_after(self._short_delay, turn_id, self.SHORT_FILLERS)
            ),
            asyncio.create_task(
                self._fire_after(self._long_delay, turn_id, self.LONG_FILLERS)
            ),
        ]

    def _cancel_timers(self) -> None:
        for t in self._pending_timers:
            t.cancel()
        self._pending_timers = []

    async def _fire_after(self, delay: float, turn_id: int, phrases: list[str]) -> None:
        await asyncio.sleep(delay)

        # Bail out if the real response already started, or a newer turn
        # has begun (stale timer from a previous turn), or we're not wired
        # up yet.
        if self._response_started or turn_id != self._turn_id or self._task is None:
            return

        phrase = random.choice(phrases)
        logger.info(f"[fallback] {delay}s elapsed with no response — injecting: {phrase!r}")

        # append_to_context=False: fillers are UX, not conversation content —
        # we don't want "Let me see." polluting the LLM's memory of what was
        # actually said.
        await self._task.queue_frames(
            [TTSSpeakFrame(text=phrase, append_to_context=False)]
        )