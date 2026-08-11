package ru.mo54.calls.sync

import android.content.Context
import android.os.Build
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import ru.mo54.calls.BuildConfig
import ru.mo54.calls.MO54Application
import ru.mo54.calls.api.CallRequest
import ru.mo54.calls.api.DeviceSnapshot
import ru.mo54.calls.api.OutcomeRequest
import ru.mo54.calls.storage.CallRecordEntity
import ru.mo54.calls.storage.QueueKind
import ru.mo54.calls.storage.SyncQueueItemEntity
import ru.mo54.calls.storage.SyncStatus
import java.io.File
import java.time.Instant
import java.util.concurrent.TimeUnit

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private val container = (context.applicationContext as MO54Application).container
    private val calls = container.database.calls()
    private val queue = container.database.queue()

    override suspend fun doWork(): Result {
        if (container.settings.backendBaseUrl == null || container.tokenVault.read() == null) return Result.failure()
        val api = runCatching { container.apiFactory.create() }.getOrElse { return Result.failure() }
        repeat(25) {
            val item = queue.next(System.currentTimeMillis()) ?: return Result.success()
            val call = calls.get(item.callLocalId) ?: run { queue.delete(item.id); return@repeat }
            calls.setSyncStatus(call.localCallId, SyncStatus.SYNCING, null, System.currentTimeMillis())
            try {
                when (item.kind) {
                    QueueKind.METADATA -> {
                        val response = api.upsertCall(call.toRequest())
                        checkResponse(response.code(), response.isSuccessful)
                        val body = response.body() ?: throw RetryableFailure("Пустой ответ metadata")
                        calls.setServerId(call.localCallId, body.callId, System.currentTimeMillis())
                    }
                    QueueKind.AUDIO -> {
                        val current = calls.get(call.localCallId) ?: throw PermanentFailure("Звонок удален")
                        val serverId = current.serverCallId ?: throw RetryableFailure("Metadata еще не приняты")
                        val path = current.audioLocalPath ?: throw PermanentFailure("Локальный аудиофайл отсутствует")
                        val sha = current.audioSha256 ?: throw PermanentFailure("Checksum аудио отсутствует")
                        val file = File(path)
                        if (!file.isFile) throw PermanentFailure("Аудиофайл потерян")
                        val mime = current.audioMimeType ?: "audio/mp4"
                        val response = api.uploadAudio(
                            serverId,
                            current.localCallId.text(),
                            sha.text(),
                            current.recordingStatus.name.lowercase().text(),
                            mime.text(),
                            current.durationSec.toString().text(),
                            MultipartBody.Part.createFormData("file", file.name, file.asRequestBody(mime.toMediaType()))
                        )
                        checkResponse(response.code(), response.isSuccessful)
                        val received = response.body()?.receivedSha256 ?: throw RetryableFailure("Пустой ответ audio")
                        if (!received.equals(sha, ignoreCase = true)) throw PermanentFailure("audio_checksum_mismatch")
                        if (!file.delete()) throw RetryableFailure("Backend принял аудио, но локальный файл не удален")
                        calls.clearLocalAudio(current.localCallId, System.currentTimeMillis())
                    }
                    QueueKind.OUTCOME -> {
                        val current = calls.get(call.localCallId) ?: throw PermanentFailure("Звонок удален")
                        val serverId = current.serverCallId ?: throw RetryableFailure("Metadata еще не приняты")
                        val response = api.saveOutcome(serverId, current.toOutcome())
                        checkResponse(response.code(), response.isSuccessful)
                        if (response.body()?.outcomeSaved != true) throw RetryableFailure("Backend не подтвердил итог")
                    }
                }
                queue.delete(item.id)
                if (queue.countForCall(item.callLocalId) == 0) {
                    calls.setSyncStatus(item.callLocalId, SyncStatus.SYNCED, null, System.currentTimeMillis())
                } else {
                    calls.setSyncStatus(item.callLocalId, SyncStatus.PENDING, null, System.currentTimeMillis())
                }
            } catch (error: PermanentFailure) {
                calls.setSyncStatus(call.localCallId, SyncStatus.NEEDS_USER_ACTION, error.message, System.currentTimeMillis())
                queue.retry(item.id, Long.MAX_VALUE, error.message ?: "Неустранимая ошибка", System.currentTimeMillis())
                return Result.failure()
            } catch (error: Exception) {
                val delayMinutes = (1L shl item.attempts.coerceAtMost(6)).coerceAtMost(60)
                queue.retry(
                    item.id,
                    System.currentTimeMillis() + TimeUnit.MINUTES.toMillis(delayMinutes),
                    error.message ?: error.javaClass.simpleName,
                    System.currentTimeMillis()
                )
                calls.setSyncStatus(call.localCallId, SyncStatus.FAILED, error.message, System.currentTimeMillis())
                return Result.retry()
            }
        }
        return Result.retry()
    }

    private fun checkResponse(code: Int, successful: Boolean) {
        if (successful) return
        if (code == 408 || code == 429 || code >= 500) throw RetryableFailure("HTTP $code")
        throw PermanentFailure("HTTP $code")
    }

    private fun CallRecordEntity.toRequest() = CallRequest(
        localCallId = localCallId,
        deviceId = deviceId ?: throw PermanentFailure("Устройство не привязано"),
        workerId = workerId,
        direction = direction.name.lowercase(),
        phoneRaw = phoneRaw,
        phoneNormalized = phoneNormalized,
        startedAt = Instant.ofEpochMilli(startedAt).toString(),
        endedAt = Instant.ofEpochMilli(endedAt ?: throw PermanentFailure("Звонок не завершен")).toString(),
        durationSec = durationSec,
        recordingStatus = recordingStatus.name.lowercase(),
        noticeStatus = noticeStatus.name.lowercase(),
        eventSource = eventSource.name.lowercase(),
        deviceSnapshot = DeviceSnapshot(
            androidVersion = Build.VERSION.RELEASE,
            manufacturer = Build.MANUFACTURER,
            model = Build.MODEL,
            appVersion = BuildConfig.VERSION_NAME
        )
    )

    private fun CallRecordEntity.toOutcome() = OutcomeRequest(
        outcomeStatus = outcomeStatus?.name?.lowercase() ?: throw PermanentFailure("Итог не заполнен"),
        summary = summary,
        objection = objection,
        nextActionType = nextActionType?.name?.lowercase(),
        nextActionAt = nextActionAt?.let { Instant.ofEpochMilli(it).toString() },
        nextActionComment = nextActionComment,
        noticeConfirmedByWorker = noticeStatus.name == "CONFIRMED_MANUALLY"
    )

    private fun String.text() = toRequestBody("text/plain".toMediaType())

    private class RetryableFailure(message: String) : Exception(message)
    private class PermanentFailure(message: String) : Exception(message)

    companion object { const val UNIQUE_NAME = "call-companion-sync" }
}

