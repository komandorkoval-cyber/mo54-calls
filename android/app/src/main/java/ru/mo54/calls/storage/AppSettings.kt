package ru.mo54.calls.storage

import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaRecorder

class AppSettings(private val context: Context) {
    private val preferences = context.getSharedPreferences("mo54-settings", Context.MODE_PRIVATE)

    var backendBaseUrl: String?
        get() = preferences.getString("backend_url", null)
        set(value) { preferences.edit().putString("backend_url", value?.trimEnd('/')?.plus('/')).apply() }
    var deviceId: String?
        get() = preferences.getString("device_id", null)
        set(value) { preferences.edit().putString("device_id", value).apply() }
    var workerId: String
        get() = preferences.getString("worker_id", "worker-igor") ?: "worker-igor"
        set(value) { preferences.edit().putString("worker_id", value).apply() }
    var audioSource: Int
        get() = preferences.getInt("audio_source", MediaRecorder.AudioSource.VOICE_RECOGNITION)
        set(value) { preferences.edit().putInt("audio_source", value).apply() }

    fun effectiveAudioSource(): Int =
        if (hasPrivilegedCallAudioAccess()) MediaRecorder.AudioSource.VOICE_CALL else audioSource

    fun hasPrivilegedCallAudioAccess(): Boolean =
        context.checkSelfPermission(PERMISSION_CAPTURE_AUDIO_OUTPUT) == PackageManager.PERMISSION_GRANTED ||
            context.checkSelfPermission(PERMISSION_CAPTURE_VOICE_COMMUNICATION_OUTPUT) == PackageManager.PERMISSION_GRANTED

    companion object {
        private const val PERMISSION_CAPTURE_AUDIO_OUTPUT = "android.permission.CAPTURE_AUDIO_OUTPUT"
        private const val PERMISSION_CAPTURE_VOICE_COMMUNICATION_OUTPUT = "android.permission.CAPTURE_VOICE_COMMUNICATION_OUTPUT"
    }
}
