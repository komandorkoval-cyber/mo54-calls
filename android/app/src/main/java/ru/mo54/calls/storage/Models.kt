package ru.mo54.calls.storage

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

enum class CallDirection { INCOMING, OUTGOING, UNKNOWN }
enum class RecordingStatus { NOT_ATTEMPTED, RECORDING, RECORDED, UNSUPPORTED, PERMISSION_DENIED, FAILED }
enum class NoticeStatus { NOT_ATTEMPTED, PLAYED_TO_CALL, PLAYED_LOCALLY_ONLY, UNSUPPORTED, FAILED, CONFIRMED_MANUALLY }
enum class ClassificationStatus { PENDING_CLASSIFICATION, WORK, DELETING }
enum class EventSource { LIVE, RECOVERED_FROM_CALL_LOG }
enum class SyncStatus { LOCAL_ONLY, PENDING, SYNCING, SYNCED, FAILED, NEEDS_USER_ACTION }
enum class OutcomeStatus {
    NEW_LEAD, CONTINUE_WORK, PROPOSAL_NEEDED, MEASUREMENT_NEEDED,
    PREPAYMENT_OR_CONTRACT, NO_ANSWER, LOST, NO_ACTION_REQUIRED
}
enum class NextActionType { CALL, MESSAGE, PREPARE_PROPOSAL, SCHEDULE_MEASUREMENT, SEND_CONTRACT, CHECK_PAYMENT, OTHER }
enum class QueueKind { METADATA, AUDIO, OUTCOME }

@Entity(
    tableName = "call_records",
    indices = [Index(value = ["callLogId"], unique = true)]
)
data class CallRecordEntity(
    @PrimaryKey val localCallId: String,
    val serverCallId: String? = null,
    val callLogId: Long? = null,
    val deviceId: String? = null,
    val workerId: String = "worker-igor",
    val direction: CallDirection = CallDirection.UNKNOWN,
    val phoneRaw: String = "unknown",
    val phoneNormalized: String? = null,
    val startedAt: Long,
    val endedAt: Long? = null,
    val durationSec: Long = 0,
    val recordingStatus: RecordingStatus = RecordingStatus.NOT_ATTEMPTED,
    val noticeStatus: NoticeStatus = NoticeStatus.NOT_ATTEMPTED,
    val audioLocalPath: String? = null,
    val audioSha256: String? = null,
    val audioMimeType: String? = null,
    val classificationStatus: ClassificationStatus = ClassificationStatus.PENDING_CLASSIFICATION,
    val eventSource: EventSource = EventSource.LIVE,
    val outcomeStatus: OutcomeStatus? = null,
    val summary: String? = null,
    val objection: String? = null,
    val nextActionType: NextActionType? = null,
    val nextActionAt: Long? = null,
    val nextActionComment: String? = null,
    val syncStatus: SyncStatus = SyncStatus.LOCAL_ONLY,
    val lastSyncError: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)

@Entity(
    tableName = "sync_queue",
    foreignKeys = [ForeignKey(
        entity = CallRecordEntity::class,
        parentColumns = ["localCallId"],
        childColumns = ["callLocalId"],
        onDelete = ForeignKey.CASCADE
    )],
    indices = [
        Index("callLocalId"),
        Index(value = ["callLocalId", "kind"], unique = true)
    ]
)
data class SyncQueueItemEntity(
    @PrimaryKey val id: String,
    val callLocalId: String,
    val kind: QueueKind,
    val attempts: Int = 0,
    val nextAttemptAt: Long = System.currentTimeMillis(),
    val lastError: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)

