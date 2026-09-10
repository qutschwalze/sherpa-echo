# sherpa-server – LAN ASR+Diarization für Echo Show 5

Separater Server für Thin-Clients (z. B. Echo Show). Läuft als Docker-Container im LAN (Port 8010).

- Server: FastAPI WebSocket, sherpa-onnx 1.13.6 Python, i5-7400T (2 Kerne), Port 8010 LAN
- Client: separater Thin-Client (ohne ONNX, WebSocket)

Eigener docker-compose, keine Abhängigkeit zu anderen Services.
