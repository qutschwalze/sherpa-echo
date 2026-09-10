"""
sherpa-server: LAN WebSocket ASR (+ später Diarization)
Separat vom Hauptprojekt /root/sherpa-app – reine Server-Implementierung.

Protokoll (einfach, FireOS 6 TLS umgeht ws://):
  Client -> Server: binary PCM 16kHz mono int16 LE, 20-100ms Chunks
  Server -> Client: JSON {type:"partial"|"final", text:"...", t_ms:int, is_final:bool}
  Client -> Server: JSON {type:"stop"}  => Server flushed, antwortet {type:"done"}

Diarization: Phase 2 – erst ASR stabil, dann OfflineSpeakerDiarization pro Session-Chunk.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import asyncio
import concurrent.futures
import numpy as np
import sherpa_onnx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sherpa-server")

MODELS_DIR = Path("/models")
LOGS_DIR = Path("/logs")

# Kroko DE – gleiche Files wie Android ModelDownloadManager
KROKO_REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-de-kroko-2025-08-06"
KROKO_FILES = ["encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"]
# Diarization – gleiche wie SpeakerModelDownloadManager (gecached auf Show: 47M)
SEGMENTATION_MODEL = MODELS_DIR / "segmentation.onnx"
EMBEDDING_MODEL = MODELS_DIR / "embedding.onnx"

app = FastAPI(title="sherpa-server")


def model_path(model: str = "kroko-de") -> Path:
    # /models/kroko-de/encoder.onnx ...
    return MODELS_DIR / model


def ensure_models():
    base = model_path("kroko-de")
    if not base.exists():
        log.warning(f"Model dir {base} missing – mount ./models or run download_models.sh")
        return False
    for f in KROKO_FILES:
        if not (base / f).exists():
            log.warning(f"Missing {base/f}")
            return False
    return True


class SherpaSession:
    def __init__(self):
        base = model_path("kroko-de")
        tokens = str(base / "tokens.txt")
        encoder = str(base / "encoder.onnx")
        decoder = str(base / "decoder.onnx")
        joiner = str(base / "joiner.onnx")
        num_threads = int(os.getenv("SHERPA_NUM_THREADS", "2"))
        # API analog zu SherpaOnnxEngine.kt: streaming zipformer
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tokens,
            encoder=encoder,
            decoder=decoder,
            joiner=joiner,
            num_threads=num_threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )
        self.stream = self.recognizer.create_stream()
        self.t_ms = 0  # kumulierte Audiozeit für Client

    def accept(self, pcm_int16: bytes):
        samples = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        self.t_ms += int(len(samples) * 1000 / 16000)
        self.stream.accept_waveform(16000, samples)
        # Antrieb für partials
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_streams([self.stream])

    def get_result(self):
        res = self.recognizer.get_result(self.stream)
        text = res.text.strip() if hasattr(res, "text") else str(res).strip()
        return text

    def is_endpoint(self) -> bool:
        return self.recognizer.is_endpoint(self.stream)

    def reset_endpoint(self):
        self.recognizer.reset(self.stream)


_sherpa_singleton = None
_diarizer = None
_diarizer_lock = asyncio.Lock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="diar")

# Phase 3 Step 1 – Härtung: Token-Auth, Limits, No-Retention (nur Ableger)
SHERPA_TOKEN = os.getenv("SHERPA_TOKEN", "")
MAX_CONNECTIONS = int(os.getenv("SHERPA_MAX_CONN", "2"))
MAX_BUFFER_BYTES = int(os.getenv("SHERPA_MAX_BUFFER", str(120 * 1024 * 1024)))
_active_connections = 0

def _check_token(ws: WebSocket) -> bool:
    if not SHERPA_TOKEN:
        return True
    q = ""
    try:
        q = ws.query_params.get("token", "")
    except Exception:
        q = ""
    h = ""
    try:
        h = ws.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            h = h[7:].strip()
        else:
            h = ""
    except Exception:
        h = ""
    return q == SHERPA_TOKEN or h == SHERPA_TOKEN

def get_diarizer():
    global _diarizer
    if _diarizer is not None:
        return _diarizer
    if not SEGMENTATION_MODEL.exists() or not EMBEDDING_MODEL.exists():
        log.warning(f"Diarization models missing: {SEGMENTATION_MODEL} {EMBEDDING_MODEL}")
        return None
    log.info("Loading diarization: %s + %s", SEGMENTATION_MODEL, EMBEDDING_MODEL)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEGMENTATION_MODEL)),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.85),
        min_duration_on=0.6,
        min_duration_off=0.8,
    )
    if not config.validate():
        log.error("Diarization config validate failed")
        return None
    _diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    log.info("Diarization ready, sample_rate=%s", _diarizer.sample_rate)
    return _diarizer

def run_diarization_sync(pcm_bytes: bytes):
    d = get_diarizer()
    if d is None:
        return []
    if len(pcm_bytes) < 16000:  # <0.5s
        return []
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    target_sr = d.sample_rate
    result = d.process(audio)
    # OfflineSpeakerDiarizationResult -> list via sort_by_start_time()
    try:
        segments = result.sort_by_start_time()
    except Exception:
        segments = result
    segs = []
    for r in segments:
        try:
            segs.append({"start": float(r.start), "end": float(r.end), "speaker": int(r.speaker)})
        except Exception:
            # fallback attribute names
            segs.append({"start": float(getattr(r, 'start', 0)), "end": float(getattr(r, 'end', 0)), "speaker": int(getattr(r, 'speaker', 0))})
    segs = sorted(segs, key=lambda x: x["start"])
    from collections import Counter
    dist = Counter(s["speaker"] for s in segs)
    log.info("Diarization done: %s segments for %.1fs speakers=%s dist=%s first=%s", len(segs), len(audio)/target_sr, len(dist), dict(dist), segs[:3])
    return segs


def get_sherpa():
    global _sherpa_singleton
    if _sherpa_singleton is None:
        if not ensure_models():
            raise RuntimeError("Models not found – run download_models.sh first")
        log.info("Loading sherpa-onnx kroko-de …")
        _sherpa_singleton = SherpaSession()
        log.info("Sherpa ready")
    return _sherpa_singleton


@app.get("/health")
async def health():
    ok = ensure_models()
    return {"ok": ok, "model": "kroko-de", "ready": _sherpa_singleton is not None}


@app.get("/")
async def root():
    return PlainTextResponse("sherpa-server ws://<host>:8010/ws  POST /health")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global _active_connections
    await ws.accept()
    if not _check_token(ws):
        log.warning("WS rejected: bad token from %s", ws.client)
        try:
            await ws.send_text(json.dumps({"type": "error", "msg": "forbidden"}))
        except Exception:
            pass
        try:
            await ws.close(code=4403)
        except Exception:
            pass
        return
    if _active_connections >= MAX_CONNECTIONS:
        log.warning("WS rejected: overloaded (%s)", _active_connections)
        try:
            await ws.send_text(json.dumps({"type": "error", "msg": "busy"}))
        except Exception:
            pass
        try:
            await ws.close(code=4413)
        except Exception:
            pass
        return
    _active_connections += 1
    log.info("WS connected %s (active=%s)", ws.client, _active_connections)
    # Pro Verbindung eigene Session (kein Mischen)
    try:
        base = model_path("kroko-de")
        # Eigene Recognizer-Instanz pro Client
        session = SherpaSession()
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "msg": str(e)}))
        await ws.close()
        return

    # Phase 2: buffer full session for offline diarization
    audio_buffer = bytearray()
    try:
        while True:
            msg = await ws.receive()
            if "text" in msg and msg["text"] is not None:
                try:
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "stop":
                        text = session.get_result()
                        if text:
                            await ws.send_text(json.dumps({"type": "final", "text": text, "t_ms": session.t_ms, "is_final": True}))
                        # Phase 2: offline diarization VOR done senden (Client wartet bis 30s)
                        # Client disconnect() schließt WS wenn er done bekommt -> diarization danach = WebSocketDisconnect.
                        # Also: diarization zuerst, dann done, dann break.
                        if len(audio_buffer) > 16000:
                            try:
                                await ws.send_text(json.dumps({"type": "diarization_started"}))
                            except Exception as e:
                                log.warning(f"diar started send failed: {e!r}")
                            try:
                                loop = asyncio.get_running_loop()
                                segs = await loop.run_in_executor(_executor, run_diarization_sync, bytes(audio_buffer))
                                if segs:
                                    await ws.send_text(json.dumps({"type": "diarization", "segments": segs}))
                                    log.info("Diarization sent: %s segs", len(segs))
                                else:
                                    log.warning("Diarization empty result")
                            except RuntimeError as e:
                                # WebSocketDisconnect während langer Diarization -> Client hat zu früh geschlossen
                                log.warning(f"diar send WebSocketDisconnect (client zu früh weg): {e!r}")
                            except Exception as e:
                                log.exception("diarization failed: %s", e)
                        try:
                            await ws.send_text(json.dumps({"type": "done"}))
                        except RuntimeError:
                            log.warning("done send WebSocketDisconnect")
                        except Exception:
                            pass
                        break
                    elif ctrl.get("type") == "reset":
                        session.stream = session.recognizer.create_stream()
                        session.t_ms = 0
                        audio_buffer.clear()
                        await ws.send_text(json.dumps({"type": "reset_ok"}))
                        continue
                except json.JSONDecodeError:
                    pass
                continue
            if "bytes" in msg and msg["bytes"] is not None:
                pcm: bytes = msg["bytes"]
                if len(pcm) == 0:
                    continue
                if len(audio_buffer) + len(pcm) > MAX_BUFFER_BYTES:
                    log.warning("WS buffer limit hit (%s bytes)", len(audio_buffer))
                    try:
                        await ws.send_text(json.dumps({"type": "error", "msg": "too_long"}))
                    except Exception:
                        pass
                    break
                audio_buffer.extend(pcm)
                session.accept(pcm)
                text = session.get_result()
                is_final = session.is_endpoint()
                if text:
                    await ws.send_text(json.dumps({
                        "type": "final" if is_final else "partial",
                        "text": text,
                        "t_ms": session.t_ms,
                        "is_final": is_final,
                    }))
                    if is_final:
                        session.reset_endpoint()
    except WebSocketDisconnect:
        log.info("WS disconnect")
    except Exception as e:
        log.warning("WS error: %s", type(e).__name__)
        try:
            await ws.send_text(json.dumps({"type": "error", "msg": "internal"}))
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        # No-Retention: RAM-Wipe + Slot freigeben (nur Zähler loggen, nie Text/Audio)
        try:
            audio_buffer.clear()
        except Exception:
            pass
        _active_connections = max(0, _active_connections - 1)
        log.info("WS closed (active=%s)", _active_connections)
