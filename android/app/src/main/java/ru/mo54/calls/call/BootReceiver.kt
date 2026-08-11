package ru.mo54.calls.call

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import ru.mo54.calls.MO54Application

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val sessionStore = CallSessionStore(context)
        val session = sessionStore.get()
        CallLogReconcileWorker.schedule(
            context,
            session?.id,
            session?.startedAt ?: System.currentTimeMillis() - 600_000,
            System.currentTimeMillis()
        )
        sessionStore.clear()
        (context.applicationContext as MO54Application).container.repository.scheduleSync()
    }
}
