# Phase 3 – Thin-Client-Parität + Privacy/Security by Design

Separat vom Hauptprojekt `/root/sherpa-app` (offline, minSdk 26).
Betrifft nur `/root/sherpa-server` + `/root/sherpa-fireos6-client`.

## 1. Paritäts-Lücke (Stand v13e)

| Handy (offline) | Server (jetzt) |
|---|---|
| Threshold 0.3, minOn 0.1/off 0.05 | Threshold 0.85, min 0.6/0.8 |
| Chunk 15s+5s Overlap, RollingReconciler (temporal voting, minMatch 0.3s, Fragment-Filter 0.4s) | Ein Offline-Pass übers Ganze bei Stop |
| SessionVoiceBank akustisch (match 0.62, pending 0.35, minEnroll/Identify 2s, Quick-Confirm 4s) via embedding.onnx | Keine Voice-Bank |
| TimelineComposer compact+split+assign live + nach Stop, Fragment-Merges, GlobalVoiceBank opt-in | Nur compact+split+assign bei Stop, keine Bank |
| Live Speaker-Farben während Aufnahme | Live nur Text, Speaker erst nach Stop |

Folge: 2 klare Sprecher ok, 3+ / Kurzbeiträge / Drift schlechter als Handy.

## 2. Threat Model (LAN, kein Cloud)

- **Abhören im LAN:** ws:// Klartext, jeder im 172.16.120.0/24 kann PCM + Transkript mitschneiden.
- **Unbefugter Client:** offener Port 8010 ohne Auth – Nachbar kann transkribieren / Server belegen.
- **Retention:** Audio-Buffer im RAM, Transkript-Text in Logs? Derzeit keine Text-Logs, aber nicht garantiert.
- **DoS:** unbegrenzte Session-Länge / Verbindungen blockieren i5-7400T (2 Kerne).
- **Persistenz:** globale Voiceprints sind Biometrie – dürfen nicht ohne Opt-in auf Server liegen.

## 3. Maßnahmen (Security by Design)

1. **Token-Auth:** `SHERPA_TOKEN` (env, 32B), Client `?token=` + `Authorization: Bearer`. Ohne Token → 403. Kein Default-Token im Repo.
2. **LAN-Bindung + Doku:** `8010` nur LAN, kein Caddy/Internet. Optional `wss://` mit selbstsigniertem Zertifikat + Pinning (FireOS-TLS ist für Internet-CAs kaputt, für LAN-Pinning tauglich – prüfen).
3. **No-Retention:** Audio nur RAM (`audio_buffer` pro Verbindung), nach `done`/Disconnect `clear()` + `del`. Keine WAV/md auf Server-Disk. Logs nur Zähler (Segmente, Dauer, Speakerzahl), nie Text, nie Embeddings.
4. **Limits:** max Session 30min / 250MB Buffer, max 2 parallele WS, `max_queue` begrenzt. Darüber 403/413.
5. **Voice-Bank session-only:** Bank lebt nur pro Verbindung, wird nach Disconnect verworfen. Persistente Profile (falls gewünscht) nur auf Client (Room), nie Server-Disk. Default OFF.
6. **Client-Regeln bleiben:** keine Meeting-Daten in Git (bestehende Repo-Regel), Transkripte nur Room, Debug-Upload nur opt-in mit Key.

## 4. Umsetzung

- **Step 1 (Härtung):** Token-Auth Server+Client, Buffer-/Conn-Limits, Log-Redaktion, RAM-Wipe. Klein, sofort.
- **Step 2 (Rolling core):** Server chunked Diarization 15s+5s Overlap während Streaming, Reconciler-Port (temporal voting wie Handy), interim `diarization` Events → Client zeigt live Speaker-Farben via gleichem TimelineComposer-Pfad.
- **Step 3 (Voice-Bank):** Server SessionVoiceBank mit embedding.onnx (match 0.62 / pending 0.35 / 2s-Gates / Quick-Confirm 4s), Threshold zurück auf 0.3-Niveau. Dann Handy-Parität.
- **Step 4 (optional):** persistente Profile nur Client-side, Opt-in.

## 5. Offene Entscheidungen

- Token-Wert: generiere ich und lege ihn in Server-env + Client-BuildConfig (nicht ins Repo)?
- Persistente Profile: vorerst NEIN (session-only) oder doch Client-opt-in?
