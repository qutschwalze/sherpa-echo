# sherpa-server – LAN ASR+Diarization für Echo Show 5

Separat vom Hauptprojekt /root/sherpa-app (offline, minSdk 26). Läuft auf BookStack-VM 172.16.120.218.

- Server: FastAPI WebSocket, sherpa-onnx 1.13.6 Python, i5-7400T (2 Kerne), Port 8010 LAN
- Client: /root/sherpa-fireos6-client (Thin, ohne ONNX, ws://...:8010)

Kein Touch an /root/bookstack/docker-compose.yml – eigener compose.
