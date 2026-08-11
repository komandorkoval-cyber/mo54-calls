package ru.mo54.calls.recording

import android.Manifest
import android.app.Notification
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import ru.mo54.calls.MO54Application
import ru.mo54.calls.storage.RecordingStatus
import ru.mo54.calls.storage.NoticeStatus
import java.io.File
import java.io.FileInputStream
import java.security.MessageDigest

class RecordingService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var recorder: MediaRecorder? = null
    private var activeCallId: String? = null
    private var outputFile: File? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording(intent.getStringExtra(EXTRA_CALL_ID) ?: return START_NOT_STICKY)
            ACTION_STOP -> stopRecording(intent.getStringExtra(EXTRA_CALL_ID))
        }
        return START_STICKY
    }

    private fun startRecording(callId: String) {
        if (recorder != null) return
        startForeground(NOTIFICATION_ID, activeNotification())
        activeCallId = callId
        scope.launch {
            // Публичный MediaRecorder не умеет доказанно вводить фразу в телефонную
            // линию. Запись продолжается, а пользователь может подтвердить устное уведомление.
            (application as MO54Application).container.database.calls()
                .updateNoticeIfNotAttempted(callId, NoticeStatus.UNSUPPORTED, System.currentTimeMillis())
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            update(callId, RecordingStatus.PERMISSION_DENIED, null)
            stopSelf()
            return
        }
        val settings = (application as MO54Application).container.settings
        val audioSource = settings.effectiveAudioSource()
        if (audioSource == MediaRecorder.AudioSource.VOICE_CALL && !settings.hasPrivilegedCallAudioAccess()) {
            update(callId, RecordingStatus.UNSUPPORTED, null)
            stopSelf()
            return
        }
        val directory = File(filesDir, "call-audio").apply { mkdirs() }
        val file = File(directory, "$callId.m4a")
        outputFile = file
        try {
            recorder = MediaRecorder().apply {
                setAudioSource(audioSource)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioEncodingBitRate(if (audioSource == MediaRecorder.AudioSource.VOICE_CALL) 64_000 else 128_000)
                setAudioSamplingRate(if (audioSource == MediaRecorder.AudioSource.VOICE_CALL) 16_000 else 44_100)
                setOutputFile(file.absolutePath)
                prepare()
                start()
            }
            update(callId, RecordingStatus.RECORDING, file)
        } catch (error: Exception) {
            recorder?.release()
            recorder = null
            file.delete()
            update(callId, RecordingStatus.FAILED, null)
            stopSelf()
        }
    }

    private fun stopRecording(requestedCallId: String?) {
        val callId = activeCallId ?: requestedCallId ?: return stopSelf()
        val file = outputFile
        val success = runCatching { recorder?.stop() }.isSuccess
        recorder?.release()
        recorder = null
        activeCallId = null
        outputFile = null
        if (success && file?.isFile == true && file.length() > 0) {
            update(callId, RecordingStatus.RECORDED, file, sha256(file))
        } else {
            file?.delete()
            update(callId, RecordingStatus.FAILED, null)
        }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun update(callId: String, status: RecordingStatus, file: File?, sha: String? = null) {
        scope.launch {
            (application as MO54Application).container.database.calls().updateRecording(
                callId, status, file?.absolutePath, sha, file?.let { "audio/mp4" }, System.currentTimeMillis()
            )
        }
    }

    private fun activeNotification(): Notification = NotificationCompat.Builder(this, MO54Application.CHANNEL_CALLS)
        .setSmallIcon(android.R.drawable.presence_audio_online)
        .setContentTitle("MO54 Calls")
        .setContentText("Диагностическая запись звонка")
        .setOngoing(true)
        .build()

    override fun onDestroy() {
        if (recorder != null) stopRecording(activeCallId)
        scope.cancel()
        super.onDestroy()
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        FileInputStream(file).use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    companion object {
        private const val ACTION_START = "ru.mo54.calls.START_RECORDING"
        private const val ACTION_STOP = "ru.mo54.calls.STOP_RECORDING"
        private const val EXTRA_CALL_ID = "call_id"
        private const val NOTIFICATION_ID = 5401

        fun start(context: Context, callId: String) {
            ContextCompat.startForegroundService(context, Intent(context, RecordingService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_CALL_ID, callId)
            })
        }

        fun stop(context: Context, callId: String) {
            context.startService(Intent(context, RecordingService::class.java).apply {
                action = ACTION_STOP
                putExtra(EXTRA_CALL_ID, callId)
            })
        }
    }
}
