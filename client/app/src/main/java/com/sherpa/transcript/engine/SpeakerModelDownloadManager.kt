package com.sherpa.transcript.engine

import android.util.Log
import com.sherpa.transcript.SherpaTranscriptApp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL

import android.os.Build
import org.apache.commons.compress.compressors.bzip2.BZip2CompressorInputStream

/**
 * Lädt Speaker-Modelle herunter: pyannote segmentation + WESPEAKER.
 */
object SpeakerModelDownloadManager {
    private const val TAG = "SpeakerModelDownload"

    data class SpeakerModelSpec(
        val fileName: String,
        val url: String,
        val sizeMb: Int,
        val isTarBz2: Boolean = false,
    )

    val REQUIRED_MODELS = listOf(
        SpeakerModelSpec(
            fileName = "segmentation.onnx",
            url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-reverb-diarization-v1.tar.bz2",
            sizeMb = 11,
            isTarBz2 = true,
        ),
        SpeakerModelSpec(
            fileName = "embedding.onnx",
            url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx",
            sizeMb = 40,
        ),
    )

    /**
     * Lädt alle benötigten Modelle herunter.
     */
    suspend fun downloadModels(
        onProgress: (fileName: String, downloaded: Long, total: Long) -> Unit = { _, _, _ -> },
    ): Boolean = withContext(Dispatchers.IO) {
        val modelDir = SherpaTranscriptApp.instance.filesDir.resolve("models/speaker")
        modelDir.mkdirs()

        // Alte ungenutzte Modelle löschen
        listOf("segmentation.onnx", "segmentation.onnx.tar.bz2").forEach { name ->
            modelDir.resolve(name)?.let { if (it.exists()) { it.delete(); Log.i(TAG, "Cleaned up $name") } }
        }

        // Alte Modelle löschen (falsche Architektur)
        listOf("embedding.onnx").forEach { name ->
            modelDir.resolve(name)?.let { f ->
                if (f.exists()) {
                    Log.i(TAG, "Deleting old embedding model for replacement")
                    f.delete()
                }
            }
        }

        for (spec in REQUIRED_MODELS) {
            val targetFile = modelDir.resolve(spec.fileName)
            val archiveFile = modelDir.resolve(spec.fileName + ".tar.bz2")

            // Altes Archiv löschen falls Ziel fehlt
            if (!targetFile.exists() && archiveFile.exists()) {
                archiveFile.delete()
                Log.i(TAG, "Deleted stale archive for ${spec.fileName}")
            }

            if (targetFile.exists() && targetFile.length() > 0) {
                Log.i(TAG, "${spec.fileName} already exists (${targetFile.length() / 1024} KB)")
                continue
            }

            if (spec.isTarBz2) {
                if (!downloadFile(spec.url, archiveFile, spec.fileName, onProgress)) {
                    return@withContext false
                }
                if (!extractTarBz2(archiveFile, modelDir, spec.fileName)) {
                    return@withContext false
                }
                archiveFile.delete()
            } else {
                if (!downloadFile(spec.url, targetFile, spec.fileName, onProgress)) {
                    return@withContext false
                }
            }
        }

        Log.i(TAG, "All speaker models downloaded")
        true
    }

    private fun downloadFile(
        url: String,
        target: File,
        fileName: String,
        onProgress: (String, Long, Long) -> Unit,
    ): Boolean {
        return try {
            val connection = URL(url).openConnection() as HttpURLConnection
            connection.connectTimeout = 30_000
            connection.readTimeout = 120_000
            connection.setRequestProperty("User-Agent", "SherpaTranscript/0.1")
            connection.instanceFollowRedirects = true
            connection.connect()

            val totalBytes = connection.contentLengthLong
            connection.inputStream.use { input ->
                FileOutputStream(target).use { output ->
                    val buffer = ByteArray(8192)
                    var read: Int
                    var totalRead = 0L
                    while (input.read(buffer).also { read = it } != -1) {
                        output.write(buffer, 0, read)
                        totalRead += read
                        onProgress(fileName, totalRead, totalBytes)
                    }
                }
            }
            Log.i(TAG, "Downloaded $fileName (${target.length() / 1024} KB)")
            true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to download $fileName: ${e.message}")
            target.delete()
            false
        }
    }

    fun areModelsDownloaded(): Boolean {
        val modelDir = SherpaTranscriptApp.instance.filesDir.resolve("models/speaker")
        if (!modelDir.exists()) return false
        return REQUIRED_MODELS.all { modelDir.resolve(it.fileName).exists() }
    }

    private fun extractTarBz2(
        archive: File,
        outputDir: File,
        renameTo: String,
    ): Boolean {
        // FireOS 6 (API 25): TarArchiveInputStream/IOUtils nutzt java.nio.file.LinkOption (API 26) -> NoClassDefFoundError
        // Fallback für API <26: BZip2 + manuelles Tar-Parsing (512-Byte-Header), braucht keine NIO-Klassen
        return if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            extractTarBz2Legacy(archive, outputDir, renameTo)
        } else {
            extractTarBz2Modern(archive, outputDir, renameTo)
        }
    }

    private fun extractTarBz2Legacy(
        archive: File,
        outputDir: File,
        renameTo: String,
    ): Boolean {
        return try {
            var extracted = false
            BZip2CompressorInputStream(archive.inputStream()).use { bz2 ->
                val buf = ByteArray(512)
                while (true) {
                    // Header lesen (512 Bytes, padded)
                    var read = 0
                    while (read < 512) {
                        val n = bz2.read(buf, read, 512 - read)
                        if (n == -1) break
                        read += n
                    }
                    if (read == 0) break // EOF
                    if (read < 512) {
                        Log.e(TAG, "Legacy tar: truncated header")
                        break
                    }
                    // Leerer Block = Ende des Archivs (zwei Nullblöcke)
                    if (buf.all { it == 0.toByte() }) break
                    val name = String(buf, 0, 100).trim { it == '\u0000' || it == ' ' }
                    // Size ist Oktal bei Offset 124, Länge 12
                    val sizeStr = String(buf, 124, 12).trim { it == '\u0000' || it == ' ' }.trim()
                    val size = if (sizeStr.isEmpty()) 0L else sizeStr.toLong(8)
                    val typeFlag = buf[156].toInt().toChar()
                    val isDir = typeFlag == '5' || name.endsWith('/')
                    val fileName = name.substringAfterLast('/')
                    if (!isDir && fileName == "model.onnx" && size > 0) {
                        Log.i(TAG, "Legacy tar: found $name ($size bytes)")
                        val out = outputDir.resolve(renameTo)
                        FileOutputStream(out).use { outStream ->
                            var remaining = size
                            val chunk = ByteArray(8192)
                            while (remaining > 0) {
                                val toRead = minOf(chunk.size.toLong(), remaining).toInt()
                                val n = bz2.read(chunk, 0, toRead)
                                if (n == -1) break
                                outStream.write(chunk, 0, n)
                                remaining -= n
                            }
                            // Padding auf 512-Byte-Grenze überspringen
                            val padding = (512 - (size % 512)) % 512
                            if (padding > 0) {
                                var skip = padding
                                val skipBuf = ByteArray(512)
                                while (skip > 0) {
                                    val n = bz2.read(skipBuf, 0, minOf(skip.toInt(), skipBuf.size))
                                    if (n == -1) break
                                    skip -= n
                                }
                            }
                        }
                        extracted = true
                        Log.i(TAG, "Legacy tar: extracted $name → $renameTo")
                        break // model.onnx gefunden, fertig
                    } else {
                        // Entry überspringen (Inhalt + Padding)
                        var toSkip = size + (512 - (size % 512)) % 512
                        val skipBuf = ByteArray(8192)
                        while (toSkip > 0) {
                            val n = bz2.read(skipBuf, 0, minOf(toSkip, skipBuf.size.toLong()).toInt())
                            if (n == -1) break
                            toSkip -= n
                        }
                    }
                }
            }
            if (!extracted) Log.e(TAG, "Legacy tar: model.onnx not found in archive")
            extracted
        } catch (e: Exception) {
            Log.e(TAG, "Legacy extraction failed: ${e.message}", e)
            false
        } catch (e: NoClassDefFoundError) {
            Log.e(TAG, "Legacy extraction NoClassDefFoundError: ${e.message}")
            false
        }
    }

    private fun extractTarBz2Modern(
        archive: File,
        outputDir: File,
        renameTo: String,
    ): Boolean {
        return try {
            // Dynamisch via Reflection laden damit auf API 25 keine Verifizierung scheitert
            val tarCls = Class.forName("org.apache.commons.compress.archivers.tar.TarArchiveInputStream")
            var extracted = false
            BZip2CompressorInputStream(archive.inputStream()).use { bz2 ->
                val tarCtor = tarCls.getConstructor(java.io.InputStream::class.java)
                val tar = tarCtor.newInstance(bz2) as java.io.Closeable
                @Suppress("UNCHECKED_CAST")
                val getNextEntry = tarCls.getMethod("getNextEntry")
                val getName = Class.forName("org.apache.commons.compress.archivers.tar.TarArchiveEntry").getMethod("getName")
                val isDirM = Class.forName("org.apache.commons.compress.archivers.tar.TarArchiveEntry").getMethod("isDirectory")
                val getSize = Class.forName("org.apache.commons.compress.archivers.tar.TarArchiveEntry").getMethod("getSize")
                tar.use {
                    while (true) {
                        val entry = getNextEntry.invoke(tar) ?: break
                        val isDir = isDirM.invoke(entry) as Boolean
                        if (!isDir) {
                            val entryName = getName.invoke(entry) as String
                            val entryFileName = entryName.substringAfterLast('/')
                            if (entryFileName == "model.onnx") {
                                val sz = getSize.invoke(entry) as Long
                                Log.i(TAG, "Found $entryName in archive ($sz bytes)")
                                val out = outputDir.resolve(renameTo)
                                FileOutputStream(out).use { output -> (tar as java.io.InputStream).copyTo(output) }
                                extracted = true
                                Log.i(TAG, "Extracted $entryName → $renameTo (${out.length() / 1024} KB)")
                            }
                        }
                    }
                }
            }
            if (!extracted) Log.e(TAG, "model.onnx not found in archive")
            extracted
        } catch (e: Exception) {
            Log.e(TAG, "Modern extraction failed: ${e.message}", e)
            false
        }
    }
}
