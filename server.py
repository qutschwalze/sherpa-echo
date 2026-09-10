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
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.35),
        min_duration_on=0.1,
        min_duration_off=0.05,
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


# Phase 3 Step 2 – Rolling core (Port von RollingReconciler.kt + ChunkedAudioBuffer-TakeChunk)
# Handy-Parität: Chunk 15s + 5s Overlap, temporal voting, minMatch 0.3s, Fragment-Filter 0.4s.
CHUNK_SEC = 15.0
OVERLAP_SEC = 5.0
MIN_MATCH_OVERLAP_SEC = 0.3
MIN_FRAGMENT_SEC = 0.4


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def reconcile_chunk(local_segs, overlap_zone, prev_global):
    # local_segs: [{start,end,speaker}] absolute Zeiten; overlap_zone: (z0,z1); prev_global: [{start,end,speaker}]
    # 0) Fragment-Filter beidseitig
    sig_local = [s for s in local_segs if (s["end"] - s["start"]) >= MIN_FRAGMENT_SEC]
    if not sig_local:
        return [], {}, 0.0
    sig_global = [s for s in prev_global if (s["end"] - s["start"]) >= MIN_FRAGMENT_SEC]
    z0, z1 = overlap_zone
    # 1) globale IDs
    known = sorted({s["speaker"] for s in sig_global})
    max_gid = max(known) if known else -1
    # 2) votes pro lokaler ID in Zone
    votes = {}
    zone_total = 0.0
    for ls in sig_local:
        lid = ls["speaker"]
        zo = _overlap(ls["start"], ls["end"], z0, z1)
        if zo <= 0:
            continue
        zone_total += zo
        c0, c1 = max(ls["start"], z0), min(ls["end"], z1)
        lv = votes.setdefault(lid, {})
        for gs in sig_global:
            acc = _overlap(c0, c1, gs["start"], gs["end"])
            if acc > 0:
                lv[gs["speaker"]] = lv.get(gs["speaker"], 0.0) + acc
    # 3) greedy matching
    pairs = sorted(
        [(lid, gid, ov) for lid, gv in votes.items() for gid, ov in gv.items()],
        key=lambda x: x[2], reverse=True,
    )
    mapping, used_l, used_g = {}, set(), set()
    for lid, gid, ov in pairs:
        if ov < MIN_MATCH_OVERLAP_SEC:
            break
        if lid in used_l or gid in used_g:
            continue
        mapping[lid] = gid
        used_l.add(lid)
        used_g.add(gid)
    # 4) unmatched -> neue globale IDs
    new_ids = set()
    nxt = max_gid + 1
    for lid in sorted({s["speaker"] for s in sig_local}):
        if lid not in used_l:
            mapping[lid] = nxt
            new_ids.add(lid)
            nxt += 1
    mapped = [{"start": s["start"], "end": s["end"], "speaker": mapping.get(s["speaker"], s["speaker"])} for s in sig_local]
    return mapped, mapping, zone_total


_embed_extractor = None

def get_embed_extractor():
    global _embed_extractor
    if _embed_extractor is not None:
        return _embed_extractor
    if not EMBEDDING_MODEL.exists():
        log.warning("Embedding model missing: %s", EMBEDDING_MODEL)
        return None
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL), num_threads=2)
    if not cfg.validate():
        log.error("Embedding config validate failed")
        return None
    _embed_extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    log.info("Embedding extractor ready")
    return _embed_extractor

def _cosine(a, b):
    import math
    if len(a) == 0 or len(a) != len(b):
        return 0.0
    dot = sum(x*y for x, y in zip(a, b))
    na = sum(x*x for x in a)
    nb = sum(x*x for x in b)
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))

class PySessionVoiceBank:
    # Port von SessionVoiceBank.kt: match 0.62, pending 0.35, minEnroll/Identify 2s, Quick-Confirm 4s
    def __init__(self):
        self.voiceprints = {}
        self.counts = {}
        self.pending = {}
        self.match_thr = 0.62
        self.pending_thr = 0.35
        self.min_enroll_sec = 2.0
        self.min_ident_sec = 2.0
        self.quick_sec = 4.0

    def _embed(self, samples_f32):
        import numpy as np
        if len(samples_f32) == 0:
            return None
        ex = get_embed_extractor()
        if ex is None:
            return None
        try:
            import numpy as _np
            arr = _np.ascontiguousarray(_np.array(samples_f32, dtype=_np.float32))
            st = ex.create_stream()
            st.accept_waveform(sample_rate=16000, waveform=arr)
            st.input_finished()
            if not ex.is_ready(st):
                return None
            emb = ex.compute(st)
            return list(emb)
        except Exception:
            log.exception("embed failed")
            return None

    def identify(self, samples_f32):
        if len(samples_f32) < int(self.min_ident_sec * 16000):
            return None
        if not self.voiceprints and not self.pending:
            return None
        emb = self._embed(samples_f32)
        if emb is None:
            return None
        best_id, best_sim, best_pending = None, 0.0, False
        for gid, vp in self.voiceprints.items():
            s = _cosine(emb, vp)
            if s > best_sim:
                best_sim, best_id, best_pending = s, gid, False
        for gid, pe in self.pending.items():
            s = _cosine(emb, pe)
            if s > best_sim:
                best_sim, best_id, best_pending = s, gid, True
        thr = self.pending_thr if best_pending else self.match_thr
        if best_id is not None and best_sim > thr:
            if best_pending:
                # Drift-Vorprüfung: gehört zu anderer bestehender Stimme?
                for gid, vp in self.voiceprints.items():
                    if gid != best_id and _cosine(emb, vp) >= self.pending_thr:
                        del self.pending[best_id]
                        log.info("VB_DRIFT_ABFANG pending=%s -> global=%s", best_id, gid)
                        return gid
                for gid, pe in list(self.pending.items()):
                    if gid != best_id and _cosine(emb, pe) >= self.pending_thr:
                        del self.pending[best_id]
                        return gid
                # confirm: Ø aus beiden Kontakten
                old = self.pending.pop(best_id)
                merged = [(a+b)/2 for a, b in zip(old, emb)]
                self.voiceprints[best_id] = merged
                self.counts[best_id] = 2
            return best_id
        return None

    def enroll(self, gid, samples_f32, dur_ms, allow_quick=True):
        if dur_ms < int(self.min_enroll_sec * 1000):
            log.info("VB enroll skip global=%s: nur %sms (< %ss)", gid, dur_ms, self.min_enroll_sec)
            return False
        if len(samples_f32) == 0:
            return False
        emb = self._embed(samples_f32)
        if emb is None:
            return False
        if gid in self.voiceprints:
            c = self.counts.get(gid, 1)
            old = self.voiceprints[gid]
            self.voiceprints[gid] = [(a*c+b)/(c+1) for a, b in zip(old, emb)]
            self.counts[gid] = c+1
            return True
        if gid in self.pending:
            old = self.pending[gid]
            _sim_pc = _cosine(old, emb)
            log.info("VB pending-check global=%s sim=%.3f thr=%.2f dur=%s", gid, _sim_pc, self.pending_thr, dur_ms)
            if _sim_pc >= self.pending_thr:
                self.pending.pop(gid)
                self.voiceprints[gid] = [(a+b)/2 for a, b in zip(old, emb)]
                self.counts[gid] = 2
                return True
            self.pending[gid] = emb
            return False
        self.pending[gid] = emb
        if allow_quick and dur_ms >= int(self.quick_sec*1000):
            sims_v = {oid: round(_cosine(emb, vp), 3) for oid, vp in self.voiceprints.items() if oid != gid}
            sims_p = {oid: round(_cosine(emb, pe), 3) for oid, pe in self.pending.items() if oid != gid}
            log.info("VB quickcheck global=%s sims_v=%s sims_p=%s thr=%.2f", gid, sims_v, sims_p, self.pending_thr)
            for oid, vp in self.voiceprints.items():
                if oid != gid and _cosine(emb, vp) >= self.pending_thr:
                    log.info("VB quick-DRIFT global=%s -> %s sim=%.3f", gid, oid, _cosine(emb, vp))
                    return False
            for oid, pe in self.pending.items():
                if oid != gid and _cosine(emb, pe) >= self.pending_thr:
                    log.info("VB quick-DRIFT-pending global=%s -> %s", gid, oid)
                    return False
            old = self.pending.pop(gid)
            self.voiceprints[gid] = old
            self.counts[gid] = 1
            return True
        return False

    def has(self, gid):
        return gid in self.voiceprints or gid in self.pending


def diarize_window_sync(window_int16: bytes):
    # Ein Chunk-Fenster (20s) diarizieren, Zeiten relativ->absolut macht der Caller
    d = get_diarizer()
    if d is None or len(window_int16) < 16000:
        return []
    audio = np.frombuffer(window_int16, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        res = d.process(audio)
        try:
            segments = res.sort_by_start_time()
        except Exception:
            segments = res
        out = []
        for r in segments:
            out.append({"start": float(r.start), "end": float(r.end), "speaker": int(r.speaker)})
        return sorted(out, key=lambda x: x["start"])
    except Exception:
        log.exception("chunk diarization failed")
        return []


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

    # Phase 2+3: Rolling core – Chunk 15s+5s Overlap, Reconciler, Live-Speaker (Handy-Parität)
    # Session-only, RAM-only (No-Retention). Privacy: nur Zähler loggen, nie Text/Audio.
    audio_buffer = bytearray()
    global_segments: list = []  # [{start,end,speaker}] globale Session-IDs
    next_chunk_end_sec = CHUNK_SEC  # erster Chunk [0,15], danach [prevEnd-5, prevEnd+15]
    chunk_index = 0
    diar_task: asyncio.Task | None = None
    voice_bank = PySessionVoiceBank()  # Phase 3 Step 3: session-only, RAM-only
    last_bank_end = -1e9
    last_bank_gid = None

    def _window_samples(snap: bytes):
        import numpy as np
        return np.frombuffer(snap, dtype=np.int16).astype(np.float32) / 32768.0

    def _slice_for(seg_start_abs, seg_end_abs, w0, snap_f32):
        s0 = max(0, int((seg_start_abs - w0) * 16000))
        s1 = min(len(snap_f32), int((seg_end_abs - w0) * 16000))
        if s1 - s0 < int(0.5 * 16000):
            return []
        return snap_f32[s0:s1]

    async def _run_chunk(idx: int, w0: float, w1: float, snap: bytes):
        nonlocal last_bank_end, last_bank_gid
        # Hintergrund-Chunk: Fenster diarizieren, reconcilen, Bank, live senden
        try:
            loop = asyncio.get_running_loop()
            local = await loop.run_in_executor(_executor, diarize_window_sync, snap)
            if not local:
                return
            abs_local = [{"start": s["start"] + w0, "end": s["end"] + w0, "speaker": s["speaker"]} for s in local]
            zone = (w0, w0 + (OVERLAP_SEC if idx > 0 else 0.0))
            mapped, mapping, _z = reconcile_chunk(abs_local, zone, global_segments)
            if not mapped:
                return
            # Phase 3 Step 3: Voice-Bank pro lokaler ID (Port von DiarizationChunkWorker 4b)
            try:
                import numpy as np
                snap_f32 = _window_samples(snap)
                max_gid = max([g["speaker"] for g in global_segments], default=-1)
                fresh = max_gid + 1
                final_map = dict(mapping)
                # alle lokalen IDs: Gesamt-Redezeit + konkatenierte Samples je ID
                # (Fix Step 3b: vorher nur längster Block -> kurze Turn-Takings <2s
                #  konnten nie enrollen, Bank blieb bei 1)
                for lid in sorted({s["speaker"] for s in abs_local}):
                    cand = sorted([s for s in abs_local if s["speaker"] == lid], key=lambda s: s["start"])
                    best = max(cand, key=lambda s: s["end"] - s["start"])
                    total_dur_ms = int(sum(s["end"] - s["start"] for s in cand) * 1000)
                    best_dur_ms = int((best["end"] - best["start"]) * 1000)
                    # Embedding NUR aus längstem zusammenhängenden Block (max 8s):
                    # Konkatenieren mischt zwei Stimmen in einen Vektor -> sim zu
                    # allem hoch -> Drift-Schutz löscht jedes Pending (Todesspirale).
                    # total_dur zählt weiter für die 2s/4s-Gates.
                    samples = _slice_for(best["start"], best["end"], w0, snap_f32)
                    if len(samples) > 8 * 16000:
                        samples = samples[:8 * 16000]
                    dur_ms = total_dur_ms
                    if len(samples) == 0:
                        log.info("VB skip local=%s: keine Samples (Blöcke=%s)", lid, len(cand))
                        continue
                    # 1) Bank-Identify (bekannte Stimme -> mappen)
                    bank_hit = await loop.run_in_executor(_executor, voice_bank.identify, list(samples))
                    if bank_hit is not None:
                        final_map[lid] = bank_hit
                        last_bank_end, last_bank_gid = best["end"], bank_hit
                        log.info("VB resolve local=%s -> global=%s (statt %s)", lid, bank_hit, mapping.get(lid))
                        continue
                    # 2) Kontinuitätserbe: direkter Anschluss <12s, Block >=1s
                    gap = best["start"] - last_bank_end
                    if last_bank_gid is not None and 0 <= gap <= 12 and dur_ms >= 1000:
                        final_map[lid] = last_bank_gid
                        last_bank_end = best["end"]
                        continue
                    tgt = mapping.get(lid)
                    if tgt is None:
                        continue
                    # 3) wirklich neu -> enroll unter Ziel-ID
                    is_new = lid not in [k for k in mapping.keys() if k in final_map and final_map[k] == tgt] or True
                    # vereinfacht: wenn Ziel nicht in Bank -> enroll (Phantom/Neu)
                    if not voice_bank.has(tgt):
                        ok = await loop.run_in_executor(_executor, voice_bank.enroll, tgt, list(samples), dur_ms, True)
                        log.info("VB enroll global=%s ok=%s total_dur=%sms best=%sms blocks=%s", tgt, ok, dur_ms, best_dur_ms, len(cand))
                    else:
                        # Fehlzuordnung auf echte Bank-ID -> frische ID, kein Quick-Confirm
                        while voice_bank.has(fresh):
                            fresh += 1
                        final_map[lid] = fresh
                        ok = await loop.run_in_executor(_executor, voice_bank.enroll, fresh, list(samples), dur_ms, True)
                        log.info("VB fresh global=%s ok=%s total_dur=%sms blocks=%s (Fehlzuordnung von %s)", fresh, ok, dur_ms, len(cand), tgt)
                        fresh += 1
                        last_bank_end, last_bank_gid = best["end"], final_map[lid]
                        continue
                    last_bank_end, last_bank_gid = best["end"], final_map.get(lid, tgt)
                # korrigierte Segmente anwenden
                if final_map != mapping:
                    mapped = [{"start": s["start"], "end": s["end"], "speaker": final_map.get(s["speaker"], s["speaker"])} for s in abs_local if (s["end"]-s["start"]) >= MIN_FRAGMENT_SEC]
                    mapping = final_map
            except Exception:
                log.exception("voice-bank step failed, nutze Reconciler-Mapping")
            kept = [g for g in global_segments if not (g["end"] > zone[0] and g["start"] < zone[1])]
            kept.extend(mapped)
            global_segments.clear()
            global_segments.extend(sorted(kept, key=lambda x: x["start"]))
            try:
                await ws.send_text(json.dumps({"type": "diarization_live", "segments": list(global_segments), "chunk": idx}))
            except RuntimeError:
                pass
            log.info("Rolling chunk %s: window=%.1f-%.1fs local=%s speakers -> global=%s speakers mapping=%s bank=%s", idx, w0, w1, len(abs_local), len(mapped), mapping, len(voice_bank.voiceprints))
        except Exception:
            log.exception("rolling chunk %s failed", idx)

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
                        # Stop: Rolling-Bestand ist primär (Handy-Parität); Full-Pass nur Fallback
                        # wenn Rolling leer (z.B. sehr kurze Session). done immer danach.
                        if diar_task is not None and not diar_task.done():
                            try:
                                await asyncio.wait_for(asyncio.shield(diar_task), timeout=8)
                            except Exception:
                                pass
                        if global_segments:
                            try:
                                await ws.send_text(json.dumps({"type": "diarization", "segments": list(global_segments), "rolling": True}))
                                log.info("Diarization sent (rolling): %s segs", len(global_segments))
                            except RuntimeError:
                                log.warning("rolling send WebSocketDisconnect")
                            except Exception:
                                pass
                        elif len(audio_buffer) > 16000:
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
                # Rolling-Trigger: pro 15s neuem Audio einen Chunk mit 5s Overlap anstoßen
                buffered_sec = len(audio_buffer) / 2 / 16000
                if buffered_sec >= next_chunk_end_sec and (diar_task is None or diar_task.done()):
                    w1 = min(buffered_sec, next_chunk_end_sec)
                    w0 = 0.0 if chunk_index == 0 else max(0.0, w1 - CHUNK_SEC - OVERLAP_SEC)
                    start_byte = int(w0 * 16000) * 2
                    end_byte = int(w1 * 16000) * 2
                    snap = bytes(audio_buffer[start_byte:end_byte])
                    diar_task = asyncio.create_task(_run_chunk(chunk_index, w0, w1, snap))
                    chunk_index += 1
                    next_chunk_end_sec += CHUNK_SEC
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
