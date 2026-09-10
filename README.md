# sherpa-echo — LAN Transcription Server for Thin Clients

**Language / Sprache:** [English](#english) · [Deutsch](#deutsch)

---

<a name="english"></a>
## English

Docker-based speech-to-text server for thin clients on the local network (e.g. low-power display devices without on-device ML).

- Streaming ASR over WebSocket plus speaker diarization (segmentation + embedding models)
- Rolling diarization during streaming: short overlapping windows reconciled into a session-wide speaker inventory, stabilized by a session-only voice bank
- Security by design: token auth, LAN-only port, no retention (audio lives in RAM only and is wiped after disconnect), connection and session limits, count-only logs (never transcript text)
- Models are downloaded separately and mounted read-only; they are never committed

Quick start: copy `.env.example` to `.env`, set a token, fetch models via `download_models.sh`, then `docker compose up -d`. Thin clients connect via `SHERPA_SERVER_URL` and `SHERPA_TOKEN` (both from environment, never hardcoded).

See `PHASE3.md` for the architecture notes and `CHANGELOG.md` for version history.

---

<a name="deutsch"></a>
## Deutsch

Docker-basierter Speech-to-Text-Server für Thin Clients im lokalen Netz (z. B. stromsparende Anzeigegeräte ohne On-Device-ML).

- Streaming-ASR über WebSocket plus Sprecher-Diarization (Segmentierungs- + Embedding-Modelle)
- Rolling-Diarization während des Streamings: kurze überlappende Fenster werden zu einem sitzungsweiten Sprecherbestand zusammengeführt, stabilisiert durch eine sitzungslokale Voice-Bank
- Security by Design: Token-Auth, nur LAN-Port, keine Speicherung (Audio nur im RAM, nach Disconnect gelöscht), Verbindungs- und Sitzungslimits, nur Zähler-Logs (niemals Transkripttext)
- Modelle werden separat geladen und read-only gemountet; sie werden niemals committet

Schnellstart: `.env.example` nach `.env` kopieren, Token setzen, Modelle per `download_models.sh` laden, dann `docker compose up -d`. Thin Clients verbinden sich über `SHERPA_SERVER_URL` und `SHERPA_TOKEN` (beides aus der Umgebung, niemals fest verdrahtet).

Siehe `PHASE3.md` für Architekturnotizen und `CHANGELOG.md` für die Versionshistorie.
