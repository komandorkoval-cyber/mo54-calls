package ru.mo54.calls.storage

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow

@Dao
interface CallDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(record: CallRecordEntity)

    @Query("SELECT * FROM call_records WHERE localCallId = :id")
    suspend fun get(id: String): CallRecordEntity?

    @Query("SELECT * FROM call_records WHERE callLogId = :callLogId LIMIT 1")
    suspend fun getByCallLogId(callLogId: Long): CallRecordEntity?

    @Query("SELECT * FROM call_records WHERE classificationStatus = 'PENDING_CLASSIFICATION' ORDER BY startedAt DESC")
    fun observePending(): Flow<List<CallRecordEntity>>

    @Query("SELECT * FROM call_records WHERE classificationStatus = 'WORK' ORDER BY startedAt DESC")
    fun observeWorkHistory(): Flow<List<CallRecordEntity>>

    @Query("SELECT * FROM call_records WHERE classificationStatus = 'DELETING'")
    suspend fun deleting(): List<CallRecordEntity>

    @Query("SELECT * FROM call_records WHERE callLogId IS NULL AND startedAt >= :notBefore ORDER BY startedAt DESC LIMIT 1")
    suspend fun recentUnreconciled(notBefore: Long): CallRecordEntity?

    @Query("DELETE FROM call_records WHERE localCallId = :id")
    suspend fun delete(id: String)

    @Query("UPDATE call_records SET classificationStatus = 'DELETING', updatedAt = :now WHERE localCallId = :id")
    suspend fun markDeleting(id: String, now: Long)

    @Query("UPDATE call_records SET recordingStatus = :status, audioLocalPath = :path, audioSha256 = :sha, audioMimeType = :mime, updatedAt = :now WHERE localCallId = :id")
    suspend fun updateRecording(id: String, status: RecordingStatus, path: String?, sha: String?, mime: String?, now: Long)

    @Query("UPDATE call_records SET noticeStatus = :status, updatedAt = :now WHERE localCallId = :id AND noticeStatus = 'NOT_ATTEMPTED'")
    suspend fun updateNoticeIfNotAttempted(id: String, status: NoticeStatus, now: Long)

    @Query("UPDATE call_records SET serverCallId = :serverId, updatedAt = :now WHERE localCallId = :id")
    suspend fun setServerId(id: String, serverId: String, now: Long)

    @Query("UPDATE call_records SET audioLocalPath = NULL, updatedAt = :now WHERE localCallId = :id")
    suspend fun clearLocalAudio(id: String, now: Long)

    @Query("UPDATE call_records SET syncStatus = :status, lastSyncError = :error, updatedAt = :now WHERE localCallId = :id")
    suspend fun setSyncStatus(id: String, status: SyncStatus, error: String?, now: Long)

    @Query("UPDATE call_records SET callLogId = :callLogId, direction = :direction, phoneRaw = :phoneRaw, phoneNormalized = :phoneNormalized, startedAt = :startedAt, endedAt = :endedAt, durationSec = :durationSec, eventSource = :source, updatedAt = :now WHERE localCallId = :id")
    suspend fun reconcile(
        id: String,
        callLogId: Long,
        direction: CallDirection,
        phoneRaw: String,
        phoneNormalized: String?,
        startedAt: Long,
        endedAt: Long,
        durationSec: Long,
        source: EventSource,
        now: Long
    )
}

@Dao
interface SyncQueueDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertAll(items: List<SyncQueueItemEntity>)

    @Query("""
        SELECT q.* FROM sync_queue q
        WHERE q.nextAttemptAt <= :now
          AND (q.kind = 'METADATA' OR NOT EXISTS (
              SELECT 1 FROM sync_queue dependency
              WHERE dependency.callLocalId = q.callLocalId AND dependency.kind = 'METADATA'
          ))
          AND (q.kind != 'OUTCOME' OR NOT EXISTS (
              SELECT 1 FROM sync_queue dependency
              WHERE dependency.callLocalId = q.callLocalId AND dependency.kind = 'AUDIO'
          ))
        ORDER BY q.createdAt, CASE q.kind WHEN 'METADATA' THEN 0 WHEN 'AUDIO' THEN 1 ELSE 2 END
        LIMIT 1
    """)
    suspend fun next(now: Long): SyncQueueItemEntity?

    @Query("SELECT COUNT(*) FROM sync_queue WHERE callLocalId = :callId")
    suspend fun countForCall(callId: String): Int

    @Query("DELETE FROM sync_queue WHERE id = :id")
    suspend fun delete(id: String)

    @Query("UPDATE sync_queue SET attempts = attempts + 1, nextAttemptAt = :nextAttemptAt, lastError = :error, updatedAt = :now WHERE id = :id")
    suspend fun retry(id: String, nextAttemptAt: Long, error: String, now: Long)

    @Query("UPDATE sync_queue SET nextAttemptAt = :now, lastError = NULL, updatedAt = :now")
    suspend fun resetAll(now: Long)
}
