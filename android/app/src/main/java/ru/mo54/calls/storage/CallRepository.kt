package ru.mo54.calls.storage

import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.CallLog
import android.provider.OpenableColumns
import android.webkit.MimeTypeMap
import androidx.core.content.ContextCompat
import androidx.room.withTransaction
import androidx.work.ExistingWorkPolicy
import androidx.work.Constraints
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import kotlinx.coroutines.flow.Flow
import ru.mo54.calls.sync.SyncWorker
import java.io.File
import java.io.FileInputStream
import java.security.MessageDigest
import java.util.UUID

data class OutcomeInput(
    val outcomeStatus: OutcomeStatus,
    val summary: String?,
    val objection: String?,
    val nextActionType: NextActionType?,
    val nextActionAt: Long?,
    val nextActionComment: String?,
    val noticeConfirmedManually: Boolean
)

class CallRepository(
    private val context: Context,
    private val database: AppDatabase,
    private val settings: AppSettings
) {
    val pending: Flow<List<CallRecordEntity>> = database.calls().observePending()
    val history: Flow<List<CallRecordEntity>> = database.calls().observeWorkHistory()

    suspend fun get(id: String) = database.calls().get(id)

    suspend fun createLiveCandidate(id: String, phoneRaw: String?, startedAt: Long) {
        if (database.calls().get(id) != null) return
        val raw = phoneRaw?.takeIf { it.isNotBlank() } ?: "unknown"
        database.calls().upsert(CallRecordEntity(
            localCallId = id,
            deviceId = settings.deviceId,
            workerId = settings.workerId,
            phoneRaw = raw,
            phoneNormalized = PhoneNumbers.normalize(raw),
            startedAt = startedAt
        ))
    }

    suspend fun saveOutcome(id: String, input: OutcomeInput) {
        OutcomeRules.validate(input)
        val current = requireNotNull(database.calls().get(id)) { "Звонок не найден" }
        val now = System.currentTimeMillis()
        val active = input.outcomeStatus in ACTIVE_OUTCOMES
        val updated = current.copy(
            deviceId = settings.deviceId,
            workerId = settings.workerId,
            classificationStatus = ClassificationStatus.WORK,
            outcomeStatus = input.outcomeStatus,
            summary = input.summary?.trim()?.ifBlank { null },
            objection = input.objection?.trim()?.ifBlank { null },
            nextActionType = input.nextActionType.takeIf { active },
            nextActionAt = input.nextActionAt.takeIf { active },
            nextActionComment = input.nextActionComment?.trim()?.ifBlank { null }.takeIf { active },
            noticeStatus = if (input.noticeConfirmedManually) NoticeStatus.CONFIRMED_MANUALLY else current.noticeStatus,
            syncStatus = SyncStatus.PENDING,
            updatedAt = now
        )
        val queue = buildList {
            add(item(id, QueueKind.METADATA, now))
            if (updated.recordingStatus == RecordingStatus.RECORDED && updated.audioLocalPath != null) {
                add(item(id, QueueKind.AUDIO, now + 1))
            }
            add(item(id, QueueKind.OUTCOME, now + 2))
        }
        database.withTransaction {
            database.calls().upsert(updated)
            database.queue().insertAll(queue)
        }
        scheduleSync()
    }

    suspend fun discardNonWork(id: String) {
        val current = database.calls().get(id) ?: return
        database.calls().markDeleting(id, System.currentTimeMillis())
        current.audioLocalPath?.let { runCatching { File(it).delete() } }
        database.calls().delete(id)
    }

    suspend fun finishInterruptedDeletions() {
        database.calls().deleting().forEach { record ->
            record.audioLocalPath?.let { runCatching { File(it).delete() } }
            database.calls().delete(record.localCallId)
        }
    }

    fun scheduleSync() {
        WorkManager.getInstance(context).enqueueUniqueWork(
            SyncWorker.UNIQUE_NAME,
            ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
        )
    }

    suspend fun forceRetry() {
        database.queue().resetAll(System.currentTimeMillis())
        scheduleSync()
    }

    suspend fun importSharedRecording(uri: Uri, mimeHint: String?): CallRecordEntity {
        val now = System.currentTimeMillis()
        val row = latestCallLogRow(now - IMPORT_CALL_LOG_WINDOW_MS)
        val call = database.withTransaction {
            val existing = row?.let { database.calls().getByCallLogId(it.id) }
            if (existing != null) {
                existing
            } else {
                val created = row?.toEntity(settings.deviceId, settings.workerId)
                    ?: CallRecordEntity(
                        localCallId = UUID.randomUUID().toString(),
                        deviceId = settings.deviceId,
                        workerId = settings.workerId,
                        startedAt = now,
                        endedAt = now,
                        durationSec = 0,
                        eventSource = EventSource.RECOVERED_FROM_CALL_LOG
                    )
                database.calls().upsert(created)
                created
            }
        }

        val displayName = displayName(uri)
        val extension = extensionFor(displayName, mimeHint)
        val output = File(File(context.filesDir, "call-audio").apply { mkdirs() }, "${call.localCallId}-shared.$extension")
        val temp = File(output.parentFile, "${output.name}.tmp")
        context.contentResolver.openInputStream(uri)?.use { input ->
            temp.outputStream().use { outputStream -> input.copyTo(outputStream) }
        } ?: error("Не удалось открыть переданный аудиофайл")
        if (temp.length() == 0L) {
            temp.delete()
            error("Переданный аудиофайл пустой")
        }
        if (output.exists()) output.delete()
        check(temp.renameTo(output)) { "Не удалось сохранить аудиофайл MO54" }
        val sha = sha256(output)
        call.audioLocalPath?.takeIf { it != output.absolutePath }?.let { runCatching { File(it).delete() } }
        database.calls().updateRecording(
            call.localCallId,
            RecordingStatus.RECORDED,
            output.absolutePath,
            sha,
            mimeHint ?: context.contentResolver.getType(uri) ?: "audio/mp4",
            System.currentTimeMillis()
        )
        database.calls().updateNoticeIfNotAttempted(call.localCallId, NoticeStatus.PLAYED_TO_CALL, System.currentTimeMillis())
        return requireNotNull(database.calls().get(call.localCallId))
    }

    private fun item(callId: String, kind: QueueKind, at: Long) = SyncQueueItemEntity(
        id = UUID.randomUUID().toString(), callLocalId = callId, kind = kind,
        nextAttemptAt = at, createdAt = at, updatedAt = at
    )

    private fun latestCallLogRow(notBefore: Long): CallLogRow? {
        if (ContextCompat.checkSelfPermission(context, android.Manifest.permission.READ_CALL_LOG) != PackageManager.PERMISSION_GRANTED) return null
        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.TYPE,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION
        )
        return context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            "${CallLog.Calls.DATE} >= ?",
            arrayOf(notBefore.toString()),
            "${CallLog.Calls.DATE} DESC LIMIT 1"
        )?.use { cursor ->
            if (!cursor.moveToFirst()) null else {
                val number = cursor.getString(1)?.takeIf { it.isNotBlank() } ?: "unknown"
                val startedAt = cursor.getLong(3)
                val durationSec = cursor.getLong(4)
                CallLogRow(
                    id = cursor.getLong(0),
                    phoneRaw = number,
                    direction = when (cursor.getInt(2)) {
                        CallLog.Calls.OUTGOING_TYPE -> CallDirection.OUTGOING
                        CallLog.Calls.INCOMING_TYPE, CallLog.Calls.MISSED_TYPE, CallLog.Calls.REJECTED_TYPE -> CallDirection.INCOMING
                        else -> CallDirection.UNKNOWN
                    },
                    startedAt = startedAt,
                    endedAt = startedAt + durationSec * 1000,
                    durationSec = durationSec
                )
            }
        }
    }

    private fun CallLogRow.toEntity(deviceId: String?, workerId: String) = CallRecordEntity(
        localCallId = UUID.randomUUID().toString(),
        callLogId = id,
        deviceId = deviceId,
        workerId = workerId,
        direction = direction,
        phoneRaw = phoneRaw,
        phoneNormalized = PhoneNumbers.normalize(phoneRaw),
        startedAt = startedAt,
        endedAt = endedAt,
        durationSec = durationSec,
        eventSource = EventSource.RECOVERED_FROM_CALL_LOG
    )

    private fun displayName(uri: Uri): String? =
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) cursor.getString(0) else null
        }

    private fun extensionFor(displayName: String?, mimeHint: String?): String {
        val fromName = displayName
            ?.substringAfterLast('.', missingDelimiterValue = "")
            ?.lowercase()
            ?.takeIf { it.length in 2..5 && it.all { char -> char.isLetterOrDigit() } }
        if (fromName != null) return fromName
        return MimeTypeMap.getSingleton().getExtensionFromMimeType(mimeHint) ?: "m4a"
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

    private data class CallLogRow(
        val id: Long,
        val phoneRaw: String,
        val direction: CallDirection,
        val startedAt: Long,
        val endedAt: Long,
        val durationSec: Long
    )

    companion object {
        private const val IMPORT_CALL_LOG_WINDOW_MS = 2 * 60 * 60 * 1000L

        val ACTIVE_OUTCOMES = setOf(
            OutcomeStatus.CONTINUE_WORK,
            OutcomeStatus.PROPOSAL_NEEDED,
            OutcomeStatus.MEASUREMENT_NEEDED,
            OutcomeStatus.PREPAYMENT_OR_CONTRACT
        )
    }
}

object OutcomeRules {
    fun validate(input: OutcomeInput) {
        if (input.outcomeStatus != OutcomeStatus.NO_ANSWER && input.summary.isNullOrBlank()) {
            error("Для рабочего звонка нужен итог")
        }
        if (input.outcomeStatus in CallRepository.ACTIVE_OUTCOMES) {
            require(input.nextActionType != null && input.nextActionAt != null && !input.nextActionComment.isNullOrBlank()) {
                "Для активного результата нужен следующий шаг, дата и комментарий"
            }
        }
    }
}
