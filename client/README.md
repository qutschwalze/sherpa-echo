# Thin Client (Android, Fire OS)

Android thin client for `sherpa-echo`: streams microphone audio to the server over WebSocket and renders segments with speaker colors. No on-device ML in the thin path.

**Language / Sprache:** [English](#english) · [Deutsch](#deutsch)

---

<a name="english"></a>
## English

- Connects via `SHERPA_SERVER_URL` and `SHERPA_TOKEN` (both from build environment, never hardcoded).
- Same assign pipeline as the offline app (compact, split long segments at diarization boundaries, assign), gap-free speaker numbering, history with speaker colors.
- Guards: permission check instead of crash, UI state cleared on restart, stop-race ignored.

Build: export both variables, then `./gradlew assembleDebug` inside `client/`.

---

<a name="deutsch"></a>
## Deutsch

- Verbindet sich über `SHERPA_SERVER_URL` und `SHERPA_TOKEN` (beides aus der Build-Umgebung, niemals fest verdrahtet).
- Gleiche Zuweisungs-Pipeline wie die Offline-App (kompaktieren, lange Segmente an Diarization-Grenzen teilen, zuweisen), lückenlose Sprechernummern, Verlauf mit Sprecherfarben.
- Schutzmechanismen: Berechtigungsprüfung statt Absturz, UI-State wird bei Neustart geleert, Stop-Race wird ignoriert.

Bauen: beide Variablen exportieren, dann `./gradlew assembleDebug` in `client/`.
