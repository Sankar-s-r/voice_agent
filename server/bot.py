"""Real-time voice assistant — Deepgram STT, Groq LLM, Cartesia TTS.

Drop this in as server/bot.py in your `pipecat create` scaffolded project
(it replaces whatever bot.py the wizard generated). fallback.py must sit
next to it in the same server/ folder.

Verified against pipecat-ai 1.5.0 by importing every class used here and
checking its actual constructor signature — not written from memory, since
Pipecat's API moves fast enough that older tutorials/blog code drifts.

Run:
    uv run bot.py              # all transports (webrtc/daily/telephony)
    uv run bot.py -t webrtc    # webrtc only -> http://localhost:7860/client
"""

import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.run import main
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import TransportParams

from fallback import FallbackEngagementObserver

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Transport params — one factory per transport type. Only webrtc is wired up
# here since that's what the CLI scaffolded for you; add "daily"/"twilio"
# factories later if you enable those transports too.
# ---------------------------------------------------------------------------
transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)

    # ---- Speech-to-Text (Deepgram) ----------------------------------
    # Nova-3 is the current streaming model (Nova-2 is previous-gen).
    # The SDK's DeepgramSTTService defaults to a Nova-3 tier already, so no
    # explicit model override is required unless you want a specific variant.
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
    )

    # ---- LLM (Groq) --------------------------------------------------
    # IMPORTANT: pipecat's GroqLLMService currently defaults to
    # "llama-3.3-70b-versatile", which Groq deprecated on 2026-06-17.
    # Override explicitly to a model Groq is still actively serving.
    llm = GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        settings=GroqLLMService.Settings(model="openai/gpt-oss-20b"),
    )

    # ---- TTS (Cartesia) ------------------------------------------------
    # voice is account-specific — copy a voice ID from your Cartesia
    # console (Voices tab) and put it in .env as CARTESIA_VOICE_ID.
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(voice=os.getenv("CARTESIA_VOICE_ID")),
    )

    # ---- Conversation context + turn management ----------------------
    # VAD lives on the user aggregator (not the transport) in current
    # pipecat — it's what decides "the user has finished their turn."
    # stop_secs is your end-of-speech silence window: the single biggest
    # lever on perceived latency. Lower = snappier but risks cutting
    # people off; 0.5-0.7s is a reasonable starting point to tune from.
    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful, friendly voice assistant. Keep replies "
                "short - 1 to 3 sentences unless the user asks for more "
                "detail. Never use markdown, bullet points, or emoji: your "
                "output is spoken aloud, not read on a screen."
            ),
        }
    ]
    context = LLMContext(messages)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.6)),
        ),
    )

    # ---- Fallback / engagement manager ---------------------------------
    # Runs as a pipeline Observer (not an in-chain processor) so it can see
    # frames from both the STT/LLM side AND the TTS/transport side without
    # sitting between them. See fallback.py for the full explanation.
    fallback = FallbackEngagementObserver(
        short_delay_secs=0.8,
        long_delay_secs=2.0,
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_heartbeats=True,
            heartbeats_period_secs=1.0,
        ),
        observers=[fallback],
    )
    # The observer needs a live reference to the worker so it can inject
    # filler frames back into the pipeline when it decides to fire.
    fallback.attach_task(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — kicking off the conversation")
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(worker)


if __name__ == "__main__":
    main()