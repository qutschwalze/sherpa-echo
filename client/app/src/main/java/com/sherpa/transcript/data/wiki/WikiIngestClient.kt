package com.sherpa.transcript.data.wiki

import android.util.Log
import com.sherpa.transcript.data.local.SettingsStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL

/**
 * Step 6: Manueller Wiki-Ingest – POST an das BookStack Theme-Plugin
 * (POST Pfad + X-MirMir-Token, Antwort 202 accepted), ohne MirMir-App.
 *
 * URL + Token kommen aus den Einstellungen (SettingsStore, vom User
 * eingetragen). Kein Hardcode, keine Defaults. Alles auf Dispatchers.IO,
 * FireOS-TLS wird umgangen indem der User eine http-LAN-URL einträgt
 * (gleiche Entscheidung wie SHERPA_SERVER_URL per ws://).
 */
object WikiIngestClient {

    private const val TAG = "WikiIngestClient"
    private const val CONNECT_TIMEOUT_MS = 10_000
    private const val READ_TIMEOUT_MS = 60_000
    private const val MAX_CHARS = 300_000 // Plugin-Limit (längere Texte kürzt der Server)

    /** Konfiguriert? (URL + Token gesetzt) */
    fun isConfigured(): Boolean {
        val s = SettingsStore.current
        return s.wikiIngestUrl.value.isNotBlank() && s.wikiIngestToken.value.isNotBlank()
    }

    /**
     * Sendet Markdown als Meeting-Seite ans Wiki.
     * @return Result mit Server-Antwort ("accepted") oder Fehlermeldung.
     */
    suspend fun sendMeeting(title: String, markdown: String): Result<String> =
        withContext(Dispatchers.IO) {
            try {
                val store = SettingsStore.current
                val base = store.wikiIngestUrl.value.trimEnd('/')
                val token = store.wikiIngestToken.value.trim()
                if (base.isBlank() || token.isBlank()) {
                    return@withContext Result.failure(
                        IllegalStateException("Wiki-Ingest nicht konfiguriert (URL + Token in Einstellungen)")
                    )
                }
                val text = if (markdown.length > MAX_CHARS) markdown.take(MAX_CHARS) else markdown
                val body = JSONObject()
                    .put("text", text)
                    .put("template", "meeting")
                    .put("title", title.take(200))
                    .toString()

                val conn = (URL(base).openConnection() as HttpURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = CONNECT_TIMEOUT_MS
                    readTimeout = READ_TIMEOUT_MS
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                    setRequestProperty("Accept", "application/json")
                    setRequestProperty("X-MirMir-Token", token)
                }
                OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(body) }
                val code = conn.responseCode
                val respBody = try {
                    BufferedReader(InputStreamReader(if (code in 200..299) conn.inputStream else conn.errorStream, Charsets.UTF_8)).use { it.readText() }
                } catch (_: Exception) { "" }
                conn.disconnect()
                if (code == 202 || code == 200) {
                    Log.i(TAG, "Wiki-Ingest accepted (${text.length} Zeichen)")
                    Result.success("accepted")
                } else {
                    Log.w(TAG, "Wiki-Ingest Fehler $code: ${respBody.take(200)}")
                    Result.failure(IllegalStateException("Wiki antwortet $code"))
                }
            } catch (t: Throwable) {
                Log.w(TAG, "Wiki-Ingest fehlgeschlagen: ${t.message}", t)
                Result.failure(t as? Exception ?: IllegalStateException(t.message ?: "Unbekannter Fehler"))
            }
        }
}
