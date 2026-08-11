package ru.mo54.calls.call

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.provider.CallLog
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import ru.mo54.calls.MO54Application
import ru.mo54.calls.R
import ru.mo54.calls.storage.CallDirection
import ru.mo54.calls.storage.CallRecordEntity
import ru.mo54.calls.storage.EventSource
import ru.mo54.calls.storage.PhoneNumbers
import ru.mo54.calls.ui.MainActivity
import ru.mo54.calls.recording.SystemRecordingImporter
import java.util.UUID
import java.util.concurrent.TimeUnit

class CallLogReconcileWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        if (ContextCompat.checkSelfPermission(applicationContext, Manifest.permission.READ_CALL_LOG) != PackageManager.PERMISSION_GRANTED) {
            return Result.failure()
        }
        val application = applicationContext as MO54Application
        val dao = application.container.database.calls()
        val row = latestCall() ?: return Result.retry()
        if (dao.getByCallLogId(row.id) != null) return Result.success()

        val requestedId = inputData.getString(KEY_CALL_ID)
        val startHint = inputData.getLong(KEY_STARTED_AT, row.startedAt)
        val existing = requestedId?.let { dao.get(it) } ?: dao.recentUnreconciled(startHint - 120_000)
        val localId = existing?.localCallId ?: UUID.randomUUID().toString()
        if (existing == null) {
            dao.upsert(CallRecordEntity(
                localCallId = localId,
                callLogId = row.id,
                deviceId = application.container.settings.deviceId,
                workerId = application.container.settings.workerId,
                direction = row.direction,
                phoneRaw = row.phoneRaw,
                phoneNormalized = PhoneNumbers.normalize(row.phoneRaw),
                startedAt = row.startedAt,
                endedAt = row.endedAt,
                durationSec = row.durationSec,
                eventSource = EventSource.RECOVERED_FROM_CALL_LOG
            ))
        } else {
            dao.reconcile(
                id = localId,
                callLogId = row.id,
                direction = row.direction,
                phoneRaw = row.phoneRaw,
                phoneNormalized = PhoneNumbers.normalize(row.phoneRaw),
                startedAt = row.startedAt,
                endedAt = inputData.getLong(KEY_ENDED_AT, row.endedAt),
                durationSec = row.durationSec,
                source = existing.eventSource,
                now = System.currentTimeMillis()
            )
        }
        SystemRecordingImporter(applicationContext, dao).importNewestFor(
            localId,
            row.startedAt,
            inputData.getLong(KEY_ENDED_AT, row.endedAt)
        )
        showOutcomeNotification(localId, row.phoneRaw)
        return Result.success()
    }

    private fun latestCall(): Row? {
        val projection = arrayOf(
            CallLog.Calls._ID, CallLog.Calls.NUMBER, CallLog.Calls.TYPE,
            CallLog.Calls.DATE, CallLog.Calls.DURATION
        )
        return applicationContext.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            "${CallLog.Calls.DATE} >= ?",
            arrayOf((System.currentTimeMillis() - TimeUnit.MINUTES.toMillis(10)).toString()),
            "${CallLog.Calls.DATE} DESC LIMIT 1"
        )?.use { cursor ->
            if (!cursor.moveToFirst()) return@use null
            val started = cursor.getLong(3)
            val duration = cursor.getLong(4)
            Row(
                id = cursor.getLong(0),
                phoneRaw = cursor.getString(1)?.takeIf(String::isNotBlank) ?: "unknown",
                direction = when (cursor.getInt(2)) {
                    CallLog.Calls.OUTGOING_TYPE -> CallDirection.OUTGOING
                    CallLog.Calls.INCOMING_TYPE, CallLog.Calls.MISSED_TYPE, CallLog.Calls.REJECTED_TYPE -> CallDirection.INCOMING
                    else -> CallDirection.UNKNOWN
                },
                startedAt = started,
                endedAt = started + TimeUnit.SECONDS.toMillis(duration),
                durationSec = duration
            )
        }
    }

    private fun showOutcomeNotification(callId: String, phone: String) {
        val intent = Intent(applicationContext, MainActivity::class.java).putExtra(MainActivity.EXTRA_CALL_ID, callId)
        val pending = PendingIntent.getActivity(
            applicationContext,
            callId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(applicationContext, MO54Application.CHANNEL_OUTCOMES)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("Заполните итог звонка")
            .setContentText(phone)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        if (android.os.Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(applicationContext, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            NotificationManagerCompat.from(applicationContext).notify(callId.hashCode(), notification)
        }
    }

    private data class Row(
        val id: Long,
        val phoneRaw: String,
        val direction: CallDirection,
        val startedAt: Long,
        val endedAt: Long,
        val durationSec: Long
    )

    companion object {
        private const val KEY_CALL_ID = "call_id"
        private const val KEY_STARTED_AT = "started_at"
        private const val KEY_ENDED_AT = "ended_at"

        fun schedule(context: Context, callId: String?, startedAt: Long, endedAt: Long) {
            val data = Data.Builder()
                .putString(KEY_CALL_ID, callId)
                .putLong(KEY_STARTED_AT, startedAt)
                .putLong(KEY_ENDED_AT, endedAt)
                .build()
            WorkManager.getInstance(context).enqueue(
                OneTimeWorkRequestBuilder<CallLogReconcileWorker>()
                    .setInputData(data)
                    .setInitialDelay(2, TimeUnit.SECONDS)
                    .build()
            )
        }
    }
}
