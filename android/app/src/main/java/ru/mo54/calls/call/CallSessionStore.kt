package ru.mo54.calls.call

import android.content.Context

data class CallSession(val id: String, val startedAt: Long, val phoneRaw: String)

class CallSessionStore(context: Context) {
    private val preferences = context.getSharedPreferences("active-call", Context.MODE_PRIVATE)

    fun get(): CallSession? {
        val id = preferences.getString("id", null) ?: return null
        return CallSession(
            id = id,
            startedAt = preferences.getLong("started_at", System.currentTimeMillis()),
            phoneRaw = preferences.getString("phone", "unknown") ?: "unknown"
        )
    }

    fun save(session: CallSession) {
        preferences.edit()
            .putString("id", session.id)
            .putLong("started_at", session.startedAt)
            .putString("phone", session.phoneRaw)
            .commit()
    }

    fun clear() = preferences.edit().clear().commit()
}

