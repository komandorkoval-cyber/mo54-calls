package ru.mo54.calls.recording

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import ru.mo54.calls.storage.CallDao
import ru.mo54.calls.storage.RecordingStatus
import java.io.File
import java.io.FileInputStream
import java.security.MessageDigest

/** Imports MIUI/system call recordings indexed by MediaStore into private app storage. */
class SystemRecordingImporter(private val context: Context, private val calls: CallDao) {
    suspend fun importNewestFor(callId: String, startedAt: Long, endedAt: Long): Boolean {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) return false
        val projection = arrayOf(
            MediaStore.Audio.Media._ID,
            MediaStore.Audio.Media.DISPLAY_NAME,
            MediaStore.Audio.Media.RELATIVE_PATH,
            MediaStore.Audio.Media.DATE_ADDED,
            MediaStore.Audio.Media.SIZE
        )
        val fromSeconds = (startedAt - 30_000) / 1000
        val toSeconds = (endedAt + 120_000) / 1000
        val candidate = context.contentResolver.query(
            MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
            projection,
            "${MediaStore.Audio.Media.DATE_ADDED} BETWEEN ? AND ?",
            arrayOf(fromSeconds.toString(), toSeconds.toString()),
            "${MediaStore.Audio.Media.DATE_ADDED} DESC"
        )?.use { cursor ->
            var match: Candidate? = null
            while (cursor.moveToNext() && match == null) {
                val path = cursor.getString(2).orEmpty().lowercase()
                val name = cursor.getString(1).orEmpty().lowercase()
                if ((path.contains("call") || path.contains("sound_recorder") || name.contains("call")) && cursor.getLong(4) > 0) {
                    match = Candidate(cursor.getLong(0), cursor.getString(1) ?: "system-recording.m4a")
                }
            }
            match
        } ?: return false

        val uri = ContentUris.withAppendedId(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, candidate.id)
        val extension = candidate.name.substringAfterLast('.', "m4a")
        val output = File(File(context.filesDir, "call-audio").apply { mkdirs() }, "$callId-system.$extension")
        context.contentResolver.openInputStream(uri)?.use { input -> output.outputStream().use(input::copyTo) } ?: return false
        if (output.length() == 0L) { output.delete(); return false }
        val current = calls.get(callId)
        current?.audioLocalPath?.takeIf { it != output.absolutePath }?.let { File(it).delete() }
        calls.updateRecording(
            callId,
            RecordingStatus.RECORDED,
            output.absolutePath,
            sha256(output),
            context.contentResolver.getType(uri) ?: "audio/mp4",
            System.currentTimeMillis()
        )
        return true
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        FileInputStream(file).use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private data class Candidate(val id: Long, val name: String)
}
