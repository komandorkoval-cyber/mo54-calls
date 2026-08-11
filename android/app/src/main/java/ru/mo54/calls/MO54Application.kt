package ru.mo54.calls

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class MO54Application : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
        createChannels()
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            container.repository.finishInterruptedDeletions()
            container.repository.scheduleSync()
        }
    }

    private fun createChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(listOf(
            NotificationChannel(CHANNEL_CALLS, getString(R.string.channel_calls), NotificationManager.IMPORTANCE_LOW),
            NotificationChannel(CHANNEL_OUTCOMES, getString(R.string.channel_outcomes), NotificationManager.IMPORTANCE_HIGH),
            NotificationChannel(CHANNEL_SYNC, getString(R.string.channel_sync), NotificationManager.IMPORTANCE_LOW)
        ))
    }

    companion object {
        const val CHANNEL_CALLS = "calls"
        const val CHANNEL_OUTCOMES = "outcomes"
        const val CHANNEL_SYNC = "sync"
    }
}
