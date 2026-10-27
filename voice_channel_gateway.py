#!/usr/bin/env python3
"""
Sam voice-channel gateway — a standalone push-to-talk transport.

Connects to wss://zello.io/ws as a named user, joins a private channel,
decodes inbound Opus audio, hands transcriptions to the Sam brain,
and ships TTS responses back as Opus frames on the channel.

This is a sibling of the browser widget, NOT a component of the bridge
process. It talks to the same Sam brain over the same protocol, but it
runs standalone.

Protocol reference:
  https://github.com/zelloptt/zello-channel-api/blob/main/API.md

Binary packet: {type(8)=0x01, stream_id(32 big-endian), packet_id(32 big-endian), data[]}
Control:       JSON text frames. Server pings every 30s; we pong (websockets lib handles).
Opus header:   4 bytes: {sample_rate_hz(16 LE), frames_per_packet(8), frame_size_ms(8)}
               Example gD4BPA== => 16000 Hz, 1 frame/packet, 60ms frame.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("websockets missing. pip install websockets", file=sys.stderr)
    raise

try:
    import opuslib
except ImportError:
    opuslib = None  # Lazy failure: surface when actually used.

from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Constants from API.md
# ---------------------------------------------------------------------------
WS_URL_FRIENDS = "wss://zello.io/ws"           # Zello Friends and Family
WS_URL_WORK_TPL = "wss://zellowork.io/ws/{net}" # Zello Work (substitute network name)

SAMPLE_RATE_HZ = 16000      # Zello expects 16 kHz Opus
FRAMES_PER_PACKET = 1
FRAME_SIZE_MS = 60          # 60 ms frames => 960 samples @ 16 kHz
PACKET_DURATION_MS = FRAME_SIZE_MS * FRAMES_PER_PACKET  # 60

# Built once: base64("16000, 1, 60") -> "gD4BPA=="
CODEC_HEADER = base64.b64encode(
    struct.pack("<HBB", SAMPLE_RATE_HZ, FRAMES_PER_PACKET, FRAME_SIZE_MS)
).decode("ascii")
assert CODEC_HEADER == "gD4BPA==", f"unexpected codec header {CODEC_HEADER}"

AUDIO_MSG_TYPE = 0x01  # first byte of every binary audio packet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@dataclass
class IncomingVoice:
    stream_id: int
    channel: str
    sender: str
    codec_header: bytes       # raw 3-byte Opus header
    sample_rate: int
    frames_per_packet: int
    frame_size_ms: int
    opus_frames: list[bytes]  # raw Opus frames as they arrive
    started_at: float

    def decoded_pcm16(self) -> bytes:
        """Decode all collected Opus frames into a single PCM16 @ sample_rate mono buffer."""
        if opuslib is None:
            raise RuntimeError("opuslib not installed; run: pip install opuslib")
        decoder = opuslib.Decoder(self.sample_rate, 1)
        out = bytearray()
        for frame in self.opus_frames:
            try:
                pcm = decoder.decode(frame, self.frame_size_samples)
                out.extend(pcm)
            except Exception as e:
                logging.warning("opus decode failed: %s (frame %d bytes)", e, len(frame))
        return bytes(out)

    @property
    def frame_size_samples(self) -> int:
        # Opus frame size in samples at given sample rate = ms * rate / 1000
        return self.sample_rate * self.frame_size_ms // 1000


def parse_binary_header(data: bytes) -> tuple[int, int, int, bytes]:
    """Return (msg_type, stream_id, packet_id, payload) for an incoming audio frame."""
    if len(data) < 9:
        raise ValueError(f"binary frame too short: {len(data)} bytes")
    msg_type = data[0]
    (stream_id, packet_id) = struct.unpack(">II", data[1:9])
    return msg_type, stream_id, packet_id, data[9:]


def build_outgoing_audio(stream_id: int, packet_id: int, opus_payload: bytes) -> bytes:
    """Build a 9-byte-header + Opus payload binary frame."""
    if len(opus_payload) > 0xFFFF:
        # Zello allows up to ~64KB but be safe
        raise ValueError(f"opus payload too large: {len(opus_payload)}")
    return bytes([AUDIO_MSG_TYPE]) + struct.pack(">II", stream_id, packet_id) + opus_payload


def parse_codec_header(b64: str) -> tuple[int, int, int]:
    """
    Parse base64 codec header into (sample_rate_hz, frames_per_packet, frame_size_ms).

    Note on endianness: API.md claims `<HBB` (little-endian for sample_rate).
    Decoding gD4BPA== yields bytes [0x80, 0x3E, 0x01, 0x3C]; little-endian
    read of [0x80, 0x3E] is 0x3E80 = 16000. Confirmed little-endian.
    """
    raw = base64.b64decode(b64)
    if len(raw) != 4:
        raise ValueError(f"bad codec header length {len(raw)}")
    sample_rate, fpp, fms = struct.unpack("<HBB", raw)
    return sample_rate, fpp, fms


# ---------------------------------------------------------------------------
# Core client
# ---------------------------------------------------------------------------
class VoiceChannelGateway:
    """
    Connects to Zello as a named user (the bot account), joins the configured channel,
    and dispatches events to a pluggable handler.

    The handler is the brain layer — it receives IncomingVoice objects (when transcriptions
    are disabled on the server) or transcription strings (when server-side STT is enabled),
    and returns Opus bytes to transmit back on the channel.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        username: str,
        password: str,
        channels: list[str],
        auth_token: str = "",
        listen_only: bool = False,
        transcriptions: bool = True,
        on_text: Optional[Callable[[str, str], None]] = None,
        on_transcription: Optional[Callable[[str, str, int, float], None]] = None,
        on_voice: Optional[Callable[[IncomingVoice], None]] = None,
        on_channel_status: Optional[Callable[[str, str, int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        log_level: int = logging.INFO,
    ):
        self.ws_url = ws_url
        self.username = username
        self.password = password
        self.channels = channels
        self.auth_token = auth_token
        self.listen_only = listen_only
        self.transcriptions = transcriptions

        # Event hooks — set from outside or defaulted to no-ops.
        self.on_text = on_text or (lambda ch, t: None)
        self.on_transcription = on_transcription or (lambda sender, text, sid, conf: None)
        self.on_voice = on_voice or (lambda iv: None)
        self.on_channel_status = on_channel_status or (lambda ch, status, n: None)
        self.on_error = on_error or (lambda e: logging.error("zello error: %s", e))

        self._ws = None
        self._seq = 0
        self._inflight: dict[int, asyncio.Future] = {}
        self._incoming_streams: dict[int, IncomingVoice] = {}
        self._outgoing_seq = 0
        self._stopped = asyncio.Event()
        self._reconnect_delay = 1.0

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s sam-zello %(levelname)s %(message)s",
        )

    # --- command helpers -------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _send_cmd(self, payload: dict, wait_response: bool = False) -> Optional[dict]:
        if not self._ws:
            raise RuntimeError("not connected")
        seq = payload.get("seq")
        if seq is None:
            seq = self._next_seq()
            payload["seq"] = seq
        msg = json.dumps(payload)
        logging.debug("send %s", msg)
        await self._ws.send(msg)
        if wait_response and seq:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[seq] = fut
                try:
                    return await asyncio.wait_for(fut, timeout=15)
                except asyncio.TimeoutError:
                    self._inflight.pop(seq, None)
                    raise
        return None

    # --- lifecycle -------------------------------------------------------
    async def run(self):
        """Connect with retry, logon, dispatch events, reconnect on drops."""
        attempt = 0
        while not self._stopped.is_set():
            attempt += 1
            try:
                await self._run_once()
                attempt = 0  # successful session resets
            except ConnectionClosed as e:
                logging.warning("ws closed: %s", e)
            except Exception as e:
                logging.exception("session error: %s", e)
            if self._stopped.is_set():
                break
            delay = min(30, self._reconnect_delay * (2 ** min(attempt, 5)))
            logging.info("reconnecting in %.1fs (attempt %d)", delay, attempt + 1)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self._stopped.set()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _run_once(self):
        logging.info("connecting to %s as %s", self.ws_url, self.username)
        async with websockets.connect(
            self.ws_url,
            max_size=64 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws

            # 1) Logon
            logon = {
                "command": "logon",
                "seq": self._next_seq(),
                "username": self.username,
                "password": self.password,
                "channels": self.channels,
                "version": "ai-agent-chat-bridge/0.1",
                "platform_type": "ai-agent-chat-bridge",
                "platform_name": "ai_agent_chat Zello Bridge",
                "language": "en",
            }
            if self.auth_token:
                logon["auth_token"] = self.auth_token
            if self.listen_only:
                logon["listen_only"] = True
            if self.transcriptions:
                logon["features"] = {"transcriptions": True}

            await self._send_cmd(logon, wait_response=True)
            logging.info("logged on to channels: %s", self.channels)

            # 2) Loop on inbound messages until disconnect
            async for raw in ws:
                if isinstance(raw, bytes):
                    self._handle_binary(raw)
                else:
                    self._handle_text(raw)

            # ws iterator exits when connection drops; loop in run() will reconnect.
            self._ws = None

    # --- inbound ---------------------------------------------------------
    def _handle_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logging.warning("non-json text frame: %r", raw[:200])
            return
        cmd = msg.get("command")
        logging.debug("recv %s", {k: msg[k] for k in list(msg)[:6]})

        # Resolve any waiting future for this seq
        seq = msg.get("seq")
        if seq and seq in self._inflight:
            fut = self._inflight.pop(seq)
            if not fut.done():
                fut.set_result(msg)

        if cmd == "on_channel_status":
            self.on_channel_status(
                msg.get("channel", ""),
                msg.get("status", ""),
                int(msg.get("users_online", 0)),
            )
        elif cmd == "on_stream_start":
            self._begin_stream(msg)
        elif cmd == "on_stream_stop":
            self._end_stream(msg)
        elif cmd == "on_text_message":
            self.on_text(msg.get("channel", ""), msg.get("text", ""))
        elif cmd == "on_transcription":
            self.on_transcription(
                msg.get("sender", ""),
                msg.get("text", ""),
                int(msg.get("stream_id", 0)),
                float(msg.get("confidence", 0.0)),
            )
        elif cmd == "on_error":
            self.on_error(msg.get("error", "unknown"))
        # logon ack / keepalive ack fall through silently.

    def _handle_binary(self, raw: bytes):
        try:
            msg_type, stream_id, packet_id, payload = parse_binary_header(raw)
        except ValueError as e:
            logging.warning("bad binary frame: %s", e)
            return
        if msg_type != AUDIO_MSG_TYPE:
            logging.debug("ignoring binary msg_type=%d", msg_type)
            return
        iv = self._incoming_streams.get(stream_id)
        if iv is None:
            logging.warning("audio for unknown stream %d", stream_id)
            return
        iv.opus_frames.append(payload)

    def _begin_stream(self, msg: dict):
        sid = int(msg.get("stream_id", 0))
        hdr_b64 = msg.get("codec_header", CODEC_HEADER)
        try:
            sr, fpp, fms = parse_codec_header(hdr_b64)
        except Exception as e:
            logging.warning("bad codec header %r: %s", hdr_b64, e)
            sr, fpp, fms = SAMPLE_RATE_HZ, FRAMES_PER_PACKET, FRAME_SIZE_MS
        iv = IncomingVoice(
            stream_id=sid,
            channel=msg.get("channel", ""),
            sender=msg.get("from", ""),
            codec_header=base64.b64decode(hdr_b64),
            sample_rate=sr,
            frames_per_packet=fpp,
            frame_size_ms=fms,
            opus_frames=[],
            started_at=time.time(),
        )
        self._incoming_streams[sid] = iv

    def _end_stream(self, msg: dict):
        sid = int(msg.get("stream_id", 0))
        iv = self._incoming_streams.pop(sid, None)
        if iv is None:
            return
        # Hand the completed voice to whoever's listening.
        try:
            self.on_voice(iv)
        except Exception as e:
            logging.exception("on_voice handler failed: %s", e)

    # --- outbound --------------------------------------------------------
    async def send_text(self, channel: str, text: str):
        await self._send_cmd({
            "command": "send_text_message",
            "channel": channel,
            "text": text[:30 * 1024],  # 30 KB cap per API.md
        })

    async def send_voice(self, channel: str, pcm16_mono: bytes, sample_rate: int = SAMPLE_RATE_HZ):
        """
        Encode pcm16 mono at sample_rate (default 16k) as Opus, send as a single stream
        to `channel`.

        Splits into FRAME_SIZE_MS frames, each Opus-encoded as one packet.
        """
        if opuslib is None:
            raise RuntimeError("opuslib not installed; pip install opuslib")

        encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
        # Adjust frame_size to match encoder's expected input
        frame_samples = sample_rate * FRAME_SIZE_MS // 1000  # 960 @ 16k
        start_msg = {
            "command": "start_stream",
            "seq": self._next_seq(),
            "channel": channel,
            "type": "audio",
            "codec": "opus",
            "codec_header": CODEC_HEADER,
            "packet_duration": PACKET_DURATION_MS,
        }
        resp = await self._send_cmd(start_msg, wait_response=True)
        if not resp or not resp.get("success"):
            raise RuntimeError(f"start_stream failed: {resp}")
        stream_id = int(resp["stream_id"])

        # Encode & send
        packet_id = 0
        try:
            for offset in range(0, len(pcm16_mono), frame_samples * 2):
                chunk = pcm16_mono[offset:offset + frame_samples * 2]
                if len(chunk) < frame_samples * 2:
                    # Pad with zeros to a full frame
                    chunk = chunk + b"\x00" * (frame_samples * 2 - len(chunk))
                opus_frame = encoder.encode(bytes(chunk), frame_samples)
                frame = build_outgoing_audio(stream_id, packet_id, opus_frame)
                await self._ws.send(frame)
                packet_id += 1
        finally:
            await self._send_cmd({
                "command": "stop_stream",
                "seq": self._next_seq(),
                "stream_id": stream_id,
                "channel": channel,
            })
        logging.info("sent %d opus frames on channel=%s stream_id=%d",
                     packet_id, channel, stream_id)


# ---------------------------------------------------------------------------
# CLI / smoke test
# ---------------------------------------------------------------------------
def _load_config_from_env() -> dict:
    """Read ZELLO_* env vars and return config suitable for VoiceChannelGateway."""
    return {
        "ws_url": os.environ.get("ZELLO_WS_URL", WS_URL_FRIENDS),
        "username": os.environ.get("ZELLO_USERNAME", ""),
        "password": os.environ.get("ZELLO_PASSWORD", ""),
        "channels": [c.strip() for c in os.environ.get("ZELLO_CHANNEL", "").split(",") if c.strip()],
        "auth_token": os.environ.get("ZELLO_AUTH_TOKEN", ""),
        "transcriptions": os.environ.get("ZELLO_TRANSCRIPTIONS", "true").lower() in ("1", "true", "yes"),
    }


async def _smoke():
    cfg = _load_config_from_env()
    if not cfg["username"] or not cfg["password"] or not cfg["channels"]:
        print("ZELLO_USERNAME, ZELLO_PASSWORD, ZELLO_CHANNEL must be set", file=sys.stderr)
        sys.exit(2)

    received = []

    def on_transcription(sender, text, sid, conf):
        received.append(("transcription", sender, text, sid, conf))
        logging.info("TRANSCRIPTION from %s: %r (conf=%.2f, sid=%d)", sender, text, conf, sid)

    def on_channel_status(channel, status, users):
        logging.info("CHANNEL %s -> %s (%d users)", channel, status, users)

    def on_error(err):
        logging.error("ZELLO ERROR: %s", err)

    def on_voice(iv):
        logging.info("VOICE from %s on %s: %d opus frames, %.2fs",
                     iv.sender, iv.channel, len(iv.opus_frames),
                     len(iv.opus_frames) * iv.frame_size_ms / 1000)

    client = VoiceChannelGateway(
        ws_url=cfg["ws_url"],
        username=cfg["username"],
        password=cfg["password"],
        channels=cfg["channels"],
        auth_token=cfg["auth_token"],
        transcriptions=cfg["transcriptions"],
        on_transcription=on_transcription,
        on_channel_status=on_channel_status,
        on_voice=on_voice,
        on_error=on_error,
        log_level=logging.INFO,
    )
    await client.run()


def main():
    parser = argparse.ArgumentParser(description="Sam voice-channel gateway")
    parser.add_argument("--smoke", action="store_true", help="Run a 60-second smoke test (no brain)")
    args = parser.parse_args()
    if args.smoke:
        try:
            asyncio.run(_smoke())
        except KeyboardInterrupt:
            pass
    else:
        parser.print_help()


if __name__ == "__main__":
    main()