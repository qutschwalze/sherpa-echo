package com.sherpa.transcript.engine

import android.util.Log
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Thin client für Echo Show 5 (FireOS 6, API 25, MT8163).
 *
 * Statt lokaler ONNX-Inferenz (43s Init, 285MB, ANR) streamt er PCM 16kHz
 * zu ws://172.16.120.218:8010/ws (sherpa-server auf BookStack-VM, i5-7400T).
 *
 * Protokoll (server.py):
 *   client -> server: binary PCM int16 LE mono 16kHz (20-100ms Chunks)
 *   server -> client: JSON {type:"partial"|"final", text:"...", t_ms:int, is_final:bool}
 *   client -> server: JSON {type:"stop"} -> server {type:"done"}
 *
 * Keep-alive: ws:// im LAN umgeht kaputtes FireOS-TLS (WRONG_VERSION_NUMBER).
 */
class SherpaWsClient(
    private val serverUrl: String = com.sherpa.transcript.BuildConfig.SHERPA_SERVER_URL,
) {
    private val tag = "SherpaWsClient"
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var ws: WebSocket? = null
    private val events = Channel<WsEvent>(Channel.UNLIMITED)
    val eventsFlow: Flow<WsEvent> = events.receiveAsFlow()

    val isConnected: MutableStateFlow<Boolean> = MutableStateFlow(false)
    val lastError: MutableStateFlow<String?> = MutableStateFlow(null)

    data class DiarSegment(val startSec: Float, val endSec: Float, val speaker: Int)
    sealed class WsEvent {
        data class Partial(val text: String, val tMs: Long) : WsEvent()
        data class Final(val text: String, val tMs: Long) : WsEvent()
        data class Diarization(val segments: List<DiarSegment>) : WsEvent()
        data object Done : WsEvent()
        data class Error(val msg: String) : WsEvent()
        // Step 5 (DE/EN): erkannte Sprache + Modus-Bestaetigung (display-only)
        data class LangDetected(val lang: String) : WsEvent()
        data class LangReady(val mode: String) : WsEvent()
    }

    fun connect() {
        if (ws != null) return
        lastError.value = null
        // Phase 3 Step 1: Token-Auth (?token=), Token aus BuildConfig (Env zur Buildzeit, nie im Repo)
        val token = try { com.sherpa.transcript.BuildConfig.SHERPA_TOKEN } catch (_: Exception) { "" }
        val url = if (token.isNotBlank()) "$serverUrl?token=$token" else serverUrl
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(tag, "WS open ${response.code}")
                isConnected.value = true
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val j = JSONObject(text)
                    when (j.optString("type")) {
                        "partial" -> events.trySend(WsEvent.Partial(j.getString("text"), j.optLong("t_ms", 0)))
                        "final" -> events.trySend(WsEvent.Final(j.getString("text"), j.optLong("t_ms", 0)))
                        "diarization_started" -> {
                            Log.i(tag, "diarization started on server")
                        }
                        "diarization_live" -> {
                            try {
                                val arr = j.getJSONArray("segments")
                                val list = mutableListOf<DiarSegment>()
                                for (i in 0 until arr.length()) {
                                    val o = arr.getJSONObject(i)
                                    list.add(DiarSegment(o.getDouble("start").toFloat(), o.getDouble("end").toFloat(), o.getInt("speaker")))
                                }
                                events.trySend(WsEvent.Diarization(list))
                            } catch (e: Exception) { Log.w(tag, "diar_live parse $e") }
                        }
                        "diarization" -> {
                            try {
                                val arr = j.getJSONArray("segments")
                                val list = mutableListOf<DiarSegment>()
                                for (i in 0 until arr.length()) {
                                    val o = arr.getJSONObject(i)
                                    list.add(DiarSegment(o.getDouble("start").toFloat(), o.getDouble("end").toFloat(), o.getInt("speaker")))
                                }
                                pendingDiarization = list; events.trySend(WsEvent.Diarization(list))
                            } catch (e: Exception) { Log.w(tag, "diar parse $e") }
                        }
                        "done" -> events.trySend(WsEvent.Done)
                        "error" -> events.trySend(WsEvent.Error(j.optString("msg", "server error")))
                        "lang" -> events.trySend(WsEvent.LangDetected(j.optString("lang", "de")))
                        "lang_ready" -> events.trySend(WsEvent.LangReady(j.optString("mode", "de_only")))
                        else -> {
                            // compat official streaming_server.py: {text:"...", segment:int}
                            if (j.has("text")) {
                                val txt = j.getString("text")
                                if (txt.isNotBlank()) events.trySend(WsEvent.Final(txt, 0))
                            }
                        }
                    }
                } catch (e: Exception) {
                    Log.w(tag, "WS parse: $text -> $e")
                }
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(tag, "WS closed $code $reason")
                isConnected.value = false
                ws = null
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(tag, "WS failure ${t.message}", t)
                lastError.value = t.message
                isConnected.value = false
                events.trySend(WsEvent.Error(t.message ?: "WS failure"))
                ws = null
            }
        })
    }

    fun sendPcm(pcm: ShortArray) {
        val w = ws ?: return
        // ShortArray LE bytes
        val bytes = ByteArray(pcm.size * 2)
        var i = 0
        for (s in pcm) {
            bytes[i++] = (s.toInt() and 0xFF).toByte()
            bytes[i++] = ((s.toInt() shr 8) and 0xFF).toByte()
        }
        w.send(ByteString.of(*bytes))
    }

    fun sendStop() {
        ws?.send("""{"type":"stop"}""")
    }

    fun sendReset() {
        ws?.send("""{"type":"reset"}""")
    }

    // Step 5 (DE/EN): Opt-in direkt nach Connect (Server antwortet lang_ready)
    fun sendLangMode(deEnAuto: Boolean) {
        if (deEnAuto) ws?.send("""{"type":"lang_mode","mode":"de_en_auto"}""")
    }

    var pendingDiarization: List<DiarSegment>? = null
        private set

    fun disconnect() {
        try { ws?.close(1000, "stop") } catch (_: Exception) {}
        ws = null
        isConnected.value = false
    }
    fun clearDiarization() { pendingDiarization = null }
}
