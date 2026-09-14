# Changelog — sherpa-echo

All entries are in English and deliberately free of device-, person- or meeting-specific details (no LAN addresses, hardware, names, or recording content) — the repository is public.

## Client v30 — Stop race fix (thin client)

- Problem: the stop wait loop only checked whether the segment list was non-empty — but rolling interim results fill it continuously, so the client tore down the connection before the final answer arrived. The server then logged a disconnect instead of sending the final result (seen twice on long sessions); the save fell back to interim state.
- Fix: protocol now distinguishes interim (`diarization_live`) from final (`diarization`) results; the client waits for the final answer or the done signal (30 s, stop re-sent at 5/12/20 s silence) and only then closes and saves. Verified on-device: final received, done received, no resend needed, clean server close.

## Client v28/v29 — Durable microphone grant (thin client)

- Problem: the OS silently revoked the runtime microphone grant on sideloaded builds. Recovery was a dead end on every path: the system permission dialog dies instantly, the platform has no per-app settings page, the permission manager lists no microphone toggle, and the rationale API misreports the state — every revocation needed manual shell access.
- Fix (v28): every start path checks first and re-requests on demand; any denial shows a one-tap retry button (checked on every resume, auto-starts on grant).
- Fix (v29, fork-only): `targetSdk 22` restores the install-time permission model — the grant is issued at install and there is nothing left to revoke. Requires one clean reinstall (the platform rejects a target downgrade over an existing install). Verified on-device: install-time `granted=true` with no manual grant, recording streams to the server. Main project stays on target 35.

## Step 8b — Persistent server voice anchors, names on device (server)

- Problem: the session bank starts empty on every connection — recurring voices re-enroll as new identities each session, drifting the inventory.
- Fix: file-based persistent bank (`voicebank.json` in the logs volume, embeddings + stable UUIDs, no names). The session flow checks the persistent anchor (0.62, handset parity) after the session bank misses and before continuity inheritance; confirmed session enrolls are learned persistently. The final diarization message carries `voice_profiles` (session-id → UUID, ignored by older clients); name mapping stays on the device. Server logs only counts and similarities, never audio or text.

## Step 8a — Drift-threshold parity with handset (server)

- Problem: the server drift guard (0.50) was stricter than the handset (0.35) — a late-joining voice just below 0.50 was kept as a new identity instead of being drift-merged.
- Fix: new `drift_thr = 0.35` used only for drift queries (`identify` drift pre-check, `quick-DRIFT`); enroll/confirm thresholds stay 0.50/0.62. One variable, A/B against prior build on bank size and phantom count.

## Step 4 — Phantom fallback (server)

- Problem: short audio fragments that never produced a voiceprint stayed in the speaker inventory as phantom identities, inflating the speaker count on long sessions.
- Fix: failed enrollments (too short or drift-rejected) and sample-less fragments now fall back to continuity with the last confirmed voice instead of keeping a new identity.

## Step 3d — Overlap clipping + pending threshold (server)

- Problem: each rolling window fully replaced overlapping inventory entries, leaving gaps; a genuine second voice was rejected by the drift guard.
- Fix: only the overlapped zone portion is replaced, surviving parts are clipped and kept; pending threshold relaxed so a clean second voice confirms while real drift is still rejected.

## Step 3c — Clean embedding source (server)

- Problem: concatenating all blocks of a voice into one embedding vector mixed voices and tripped the drift guard for every new identity.
- Fix: embeddings come from the longest contiguous block only (capped length); total speaking time still counts toward confirmation gates; quick-confirm also applies to fresh identities.

## Step 3b — Enrollment on total speaking time (server)

- Problem: short turn-takings never passed the minimum-duration gate, so the voice bank stayed at a single identity.
- Fix: enrollment uses total speaking time and concatenated samples per voice per window.

## Step 3 — Session voice bank (server)

- Added a session-only, RAM-only voice bank (embedding-based identification, continuity inheritance, quick-confirm for long clean blocks). Discarded on disconnect; nothing persisted.

## Step 2 — Rolling core (server + thin client)

- Server diarizes overlapping windows during streaming, reconciles them into a session inventory (temporal voting, fragment filter), and sends interim results; the thin client colors speakers live through the same assign pipeline as the offline app.

## Step 1 — Hardening (server + thin client)

- Token auth, per-connection audio buffers with size and session limits, capped parallel connections, RAM wipe on disconnect, count-only logging.
