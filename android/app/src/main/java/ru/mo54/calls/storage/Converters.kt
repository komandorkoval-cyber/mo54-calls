package ru.mo54.calls.storage

import androidx.room.TypeConverter

class Converters {
    @TypeConverter fun fromDirection(value: CallDirection) = value.name
    @TypeConverter fun toDirection(value: String) = CallDirection.valueOf(value)
    @TypeConverter fun fromRecording(value: RecordingStatus) = value.name
    @TypeConverter fun toRecording(value: String) = RecordingStatus.valueOf(value)
    @TypeConverter fun fromNotice(value: NoticeStatus) = value.name
    @TypeConverter fun toNotice(value: String) = NoticeStatus.valueOf(value)
    @TypeConverter fun fromClassification(value: ClassificationStatus) = value.name
    @TypeConverter fun toClassification(value: String) = ClassificationStatus.valueOf(value)
    @TypeConverter fun fromEventSource(value: EventSource) = value.name
    @TypeConverter fun toEventSource(value: String) = EventSource.valueOf(value)
    @TypeConverter fun fromSyncStatus(value: SyncStatus) = value.name
    @TypeConverter fun toSyncStatus(value: String) = SyncStatus.valueOf(value)
    @TypeConverter fun fromOutcome(value: OutcomeStatus?) = value?.name
    @TypeConverter fun toOutcome(value: String?) = value?.let(OutcomeStatus::valueOf)
    @TypeConverter fun fromNextAction(value: NextActionType?) = value?.name
    @TypeConverter fun toNextAction(value: String?) = value?.let(NextActionType::valueOf)
    @TypeConverter fun fromQueueKind(value: QueueKind) = value.name
    @TypeConverter fun toQueueKind(value: String) = QueueKind.valueOf(value)
}

