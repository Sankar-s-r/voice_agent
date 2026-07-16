# Voice Agent — Real-Time Audio-In, Audio-Out Conversational AI Assistant

A voice assistant that listens to spoken queries and responds in natural
speech, built to keep end-to-end latency under 2 seconds and to stay
conversational — never silent, never a generic error — when a response is
slow.

Built for the AI Engineering Intern assignment.

## Table of contents

- [Overview](#overview)
- [How this meets the assignment requirements](#how-this-meets-the-assignment-requirements)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Setup](#setup)
- [Running it](#running-it)
- [Verifying it works](#verifying-it-works)
- [Design notes: the fallback/engagement system](#design-notes-the-fallbackengagement-system)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Use of AI in this project](#use-of-ai-in-this-project)

## Overview

The assistant runs a streaming pipeline — not a sequential one — because
that's what actually makes a sub-2-second voice loop possible:

```
mic → VAD (turn detection) → STT → LLM (streamed) → TTS (streamed) → speaker
```

Each stage starts consuming the previous stage's output as soon as partial
output exists, rather than waiting for each stage to fully finish before the
next one starts. A sentence-level chunker sends the LLM's first completed
sentence to TTS immediately, so the assistant starts speaking before its
full response has finished generating.

Running alongside that pipeline — not after it — is a fallback engagement
manager that watches turn timing and speaks a short filler phrase if the
real response is taking longer than expected, so the assistant never goes
silent or drops into a generic error message while it's thinking.

## How this meets the assignment requirements

| Requirement                               | How it's met                                                                                                                                                                          |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Accept voice input                        | WebRTC mic capture via the Pipecat client, streamed to Deepgram Nova-3                                                                                                                |
| Respond with voice output                 | Cartesia streaming TTS, played back over WebRTC                                                                                                                                       |
| End-to-end response time ≤ 2s             | Streaming pipeline architecture (see [Architecture](#architecture)); see [Known limitations](#known-limitations) for current verification status                                      |
| Fallback flow for delays/interruptions    | `fallback.py` — two-tier filler system, detailed below                                                                                                                                |
| Offline preferred, online if not feasible | Evaluated offline first; local hardware couldn't hit the 2s target (see rationale below), so this is the online implementation, chosen deliberately and documented, not as a shortcut |
| No generic error messages, no silence     | Fallback manager speaks a natural filler instead of erroring out or going quiet                                                                                                       |

**Why online instead of offline:** an offline pipeline (local Whisper + local
LLM + local TTS) was evaluated first, per the assignment's stated
preference. On the available hardware (no dedicated GPU), the offline stack
measured well outside the 2-second budget — CPU-bound STT and LLM inference
alone routinely exceeded 2-3 seconds before TTS was even in the picture. The
online stack (Deepgram + Groq + Cartesia) was adopted specifically because
it meets the latency requirement the assignment prioritizes, not out of
convenience.

## Architecture

```
┌──────────┐   ┌─────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐   ┌─────────┐
│ Microphone│──▶│ VAD │──▶│ Streaming STT│──▶│ Streaming LLM│──▶│ Streaming TTS │──▶│ Speaker │
│  (WebRTC) │   │Silero│  │  (Deepgram)  │   │    (Groq)    │   │  (Cartesia)   │   │(WebRTC) │
└──────────┘   └─────┘   └──────────────┘   └──────────────┘   └───────────────┘   └─────────┘
                                                      ▲
                                     ┌────────────────┴────────────────┐
                                     │   Fallback / Engagement Manager  │
                                     │  (Pipecat Observer — watches all │
                                     │   frames, injects filler speech  │
                                     │   if the real response is slow)  │
                                     └───────────────────────────────────┘
```

VAD lives on the user-turn aggregator and decides when the user has
finished speaking (a tunable silence window — currently 0.6s — that is the
single biggest lever on perceived latency). The fallback manager runs as an
**Observer**, not an in-chain processor, specifically so it can see frames
from both the STT/LLM side and the TTS/transport side without sitting
between them — see `fallback.py` for the full reasoning.

## Tech stack

| Stage                | Choice                                           | Why                                                                                                                                           |
| -------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Orchestration        | [Pipecat](https://github.com/pipecat-ai/pipecat) | Purpose-built streaming pipeline framework for real-time voice agents; handles backpressure, interruption/barge-in, and transport abstraction |
| VAD / turn detection | Silero VAD                                       | Small, CPU-only, near-zero latency                                                                                                            |
| STT                  | Deepgram Nova-3 (streaming)                      | Sub-300ms streaming transcription                                                                                                             |
| LLM                  | Groq — `openai/gpt-oss-20b`                      | LPU inference gives very low time-to-first-token; see [Known limitations](#known-limitations) for a note on model selection                   |
| TTS                  | Cartesia (streaming)                             | Low time-to-first-audio, strong P99 consistency                                                                                               |
| Transport            | Pipecat WebRTC (SmallWebRTC)                     | Local dev-friendly, bundled prebuilt client at `/client`                                                                                      |
| Client               | React (Pipecat client SDK)                       | Scaffolded via `pipecat create`                                                                                                               |

## Project structure

```
voice-agent/
├── README.md
├── server/
│   ├── bot.py            # main pipeline: STT → LLM → TTS + fallback wiring
│   ├── fallback.py        # the fallback/engagement Observer
│   ├── .env.example        # copy to .env and fill in API keys
│   ├── pyproject.toml      # scaffolded by `pipecat create`
│   └── uv.lock
            # React WebRTC client, scaffolded by `pipecat create`
```

## Setup

**Prerequisites:** [uv](https://docs.astral.sh/uv/), API keys for Deepgram,
Groq, and Cartesia.

```bash
cd server
cp .env.example .env
```

Fill in `.env`:

```
DEEPGRAM_API_KEY=your_key
GROQ_API_KEY=your_key
CARTESIA_API_KEY=your_key
CARTESIA_VOICE_ID=your_voice_id   # from the Cartesia console's Voices tab
```

Install dependencies:

```bash
uv add "pipecat-ai[deepgram,groq,cartesia,webrtc,silero,runner]"
uv sync
```

## Running it

```bash
cd server
uv run bot.py -t webrtc
```

Open **http://localhost:7860/client**, allow microphone access, click
**Connect**, and start talking.

## Verifying it works

- **Latency:** time from when you stop talking to when you hear the first
  word back. Do this a few times manually to get a real number.
- **Fallback firing:** watch the terminal for `[fallback]` log lines. To
  force it to fire on every turn for a demo, temporarily lower
  `short_delay_secs`/`long_delay_secs` in `bot.py` (e.g. to `0.05`/`0.15`),
  confirm you hear the filler before the real answer, then restore the
  defaults (`0.8`/`2.0`).
- **Barge-in:** talk over the bot mid-response — it should stop immediately.
- **Frame-level trace:** run `uv run pipecat tail` alongside `bot.py` for a
  live terminal dashboard of every frame moving through the pipeline.

## Design notes: the fallback/engagement system

Two tiers, both triggered from a single Observer that watches for
`UserStoppedSpeakingFrame` (start the clock) and
`BotStartedSpeakingFrame`/`TTSStartedFrame` (stop the clock):

- **0.8s** — a short backchannel ("Let me see...") for an ordinary slightly-
  slow round trip.
- **2.0s** — a longer "still working on it" filler for a genuine slowdown
  (rate limiting, cold start, network hiccup).

Both are spoken via `TTSSpeakFrame` with `append_to_context=False`, so
fillers never pollute the LLM's memory of the actual conversation. The
timer logic correctly handles barge-in (cancels pending fillers) and
same-turn cleanup (a new turn starting doesn't let a stale timer from the
previous turn fire) — covered by unit tests during development.

## Known limitations

- **Filler synthesis isn't pre-rendered yet.** Both filler tiers currently
  call Cartesia live (~150-300ms) rather than playing pre-rendered cached
  audio. True zero-latency injection is possible by pre-rendering the
  phrases once to raw PCM and pushing `TTSAudioRawFrame` directly — noted
  as a next step, not yet built.
- **Client-side network-drop filler is not wired into the React client.**
  The fallback manager above covers slow _responses_; a dropped _connection_
  (user's WiFi/network failing entirely) needs a client-side cached audio
  file, since the browser can't reach this backend at all in that case. The
  approach is documented but not yet implemented in `client/`.
- **Only the WebRTC transport is configured.** Phone/telephony transports
  (Twilio, Exotel, etc.) are supported by Pipecat but not wired into
  `bot.py`'s `transport_params`.
- **Groq's default model in Pipecat's `GroqLLMService` is stale.** The
  library defaults to `llama-3.3-70b-versatile`, which Groq deprecated;
  `bot.py` explicitly overrides this to `openai/gpt-oss-20b`. Worth
  rechecking Groq's model catalog periodically, since this has already
  shifted once.
- **End-to-end live-audio verification is in progress.** The pipeline's
  object graph and the fallback timer logic are unit-tested; a full live
  conversation (mic → response you can hear) is being verified against real
  hardware/network as of this writing.

## Troubleshooting

| Symptom                                                                                     | Likely cause                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'fallback'`                                           | File is misnamed (check for a stray capital letter or a `.txt` extension) or not directly inside `server/`                                                                                                                      |
| `ImportError: ... install pipecat-ai[deepgram]` (or similar)                                | Missing extra — rerun the `uv add` command above                                                                                                                                                                                |
| WebRTC connects (ICE reaches `completed`) but nothing happens, connection closes after ~20s | Overall peer connection likely never reached `connected` — check Windows Firewall allowed `python.exe`/`uv.exe` on Private+Public networks, and try disabling extra virtual network adapters (VirtualBox/WSL/VMware) if present |
| No mic prompt in browser                                                                    | Site must be `localhost` or HTTPS — browsers block mic access on plain HTTP for non-localhost origins                                                                                                                           |
| TTS/no audio at all                                                                         | `CARTESIA_VOICE_ID` is empty, a placeholder, or not a real ID from your Cartesia console                                                                                                                                        |
| Connects, but no bot response                                                               | Check `GROQ_API_KEY`, and confirm `openai/gpt-oss-20b` is still live on your Groq account — their catalog has shifted before                                                                                                    |
| `Address already in use` on port 7860                                                       | A previous `bot.py` instance is still running                                                                                                                                                                                   |

## Use of AI in this project

Anthropic's Claude was used throughout this project's design and
implementation, specifically for:

- **Architecture design** — proposing the streaming-pipeline structure,
  the latency budget breakdown per stage, and the offline-vs-online
  hardware tradeoff analysis that justified this project's online
  implementation.
- **Technical review** — checking a proposed cloud-services architecture
  (Deepgram/Groq/Cartesia) against current, searched documentation, which
  caught a deprecated Groq model default and a misunderstanding about which
  layer (`reconnect_on_error`) actually covers reconnection.
- **Code generation** — `bot.py` and `fallback.py` were written by Claude,
  using the Pipecat framework. Before being handed off, every service
  constructor and API pattern used was checked against the real, installed
  `pipecat-ai` package (via `inspect.signature()` in a sandboxed
  environment) rather than written from memory, and the fallback timer
  logic was covered by unit tests (fast response, slow response, medium
  delay, user barge-in, and a same-turn stale-timer edge case) before
  delivery.
- **Debugging support** — diagnosing a case-sensitive filename import error
  and a WebRTC connection-establishment issue from raw server logs.

All code was reviewed and run by the project author; AI assistance covered
design, drafting, and verification support rather than autonomous,
unreviewed implementation.
