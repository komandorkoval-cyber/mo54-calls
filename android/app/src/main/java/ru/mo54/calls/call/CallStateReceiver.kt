package ru.mo54.calls.call

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import ru.mo54.calls.MO54Application
import ru.mo54.calls.recording.RecordingService
import java.util.UUID

class CallStateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) return
        val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
        val phone = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER)
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try { handle(context.applicationContext, state, phone) } finally { pending.finish() }
        }
    }

    private suspend fun handle(context: Context, state: String, phone: String?) {
        val application = context as MO54Application
        val sessions = CallSessionStore(context)
        when (state) {
            TelephonyManager.EXTRA_STATE_RINGING -> ensureSession(application, sessions, phone)
            TelephonyManager.EXTRA_STATE_OFFHOOK -> {
                val session = ensureSession(application, sessions, phone)
                RecordingService.start(context, session.id)
            }
            TelephonyManager.EXTRA_STATE_IDLE -> {
                val session = sessions.get()
                if (session != null) RecordingService.stop(context, session.id)
                CallLogReconcileWorker.schedule(
                    context,
                    session?.id,
                    session?.startedAt ?: System.currentTimeMillis() - 120_000,
                    System.currentTimeMillis()
                )
                sessions.clear()
            }
        }
    }

    private suspend fun ensureSession(
        application: MO54Application,
        sessions: CallSessionStore,
        phone: String?
    ): CallSession {
        sessions.get()?.let { return it }
        val session = CallSession(
            id = UUID.randomUUID().toString(),
            startedAt = System.currentTimeMillis(),
            phoneRaw = phone?.takeIf(String::isNotBlank) ?: "unknown"
        )
        sessions.save(session)
        application.container.repository.createLiveCandidate(session.id, session.phoneRaw, session.startedAt)
        return session
    }
}

