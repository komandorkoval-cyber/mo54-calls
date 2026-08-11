package ru.mo54.calls.api

import kotlinx.serialization.Serializable

@Serializable data class DeviceLinkRequest(
    val pairingCode: String,
    val deviceName: String,
    val androidVersion: String,
    val manufacturer: String,
    val model: String,
    val appVersion: String
)
@Serializable data class DeviceLinkResponse(
    val deviceId: String,
    val workerId: String,
    val workerName: String? = null,
    val deviceToken: String,
    val expiresAt: String? = null
)
@Serializable data class DeviceSnapshot(
    val androidVersion: String,
    val manufacturer: String,
    val model: String,
    val appVersion: String
)
@Serializable data class CallRequest(
    val localCallId: String,
    val deviceId: String,
    val workerId: String,
    val direction: String,
    val phoneRaw: String,
    val phoneNormalized: String? = null,
    val startedAt: String,
    val endedAt: String,
    val durationSec: Long,
    val recordingStatus: String,
    val noticeStatus: String,
    val source: String = "android-companion",
    val eventSource: String,
    val deviceSnapshot: DeviceSnapshot
)
@Serializable data class CallResponse(
    val callId: String,
    val localCallId: String,
    val syncStatus: String,
    val updatedAt: String
)
@Serializable data class AudioResponse(
    val audioAssetId: String,
    val callId: String,
    val recordingStatus: String,
    val receivedSha256: String,
    val sizeBytes: Long,
    val createdAt: String
)
@Serializable data class OutcomeRequest(
    val outcomeStatus: String,
    val summary: String? = null,
    val objection: String? = null,
    val nextActionType: String? = null,
    val nextActionAt: String? = null,
    val nextActionComment: String? = null,
    val noticeConfirmedByWorker: Boolean
)
@Serializable data class OutcomeResponse(
    val callId: String,
    val outcomeSaved: Boolean,
    val followupTaskId: String? = null,
    val updatedAt: String
)
@Serializable data class ConfigResponse(
    val workerId: String,
    val requiredOutcomeForWorkCalls: Boolean,
    val recordingNoticeText: String,
    val maxAudioSizeMb: Int,
    val allowedAudioMimeTypes: List<String>,
    val nextActionTypes: List<String>
)
@Serializable data class DiagnosticRequest(
    val deviceId: String,
    val workerId: String,
    val androidVersion: String,
    val manufacturer: String,
    val model: String,
    val appVersion: String,
    val incomingCallDetected: Boolean,
    val outgoingCallDetected: Boolean,
    val incomingAudioRecorded: Boolean,
    val outgoingAudioRecorded: Boolean,
    val bothSidesAudibleIncoming: Boolean,
    val bothSidesAudibleOutgoing: Boolean,
    val noticePlayedToCall: Boolean,
    val worksOnLockedScreen: Boolean,
    val worksAfterReboot: Boolean,
    val conclusion: String,
    val notes: String
)
@Serializable data class DiagnosticResponse(val diagnosticId: String, val saved: Boolean)
@Serializable data class CallListItem(
    val callId: String,
    val workerId: String,
    val direction: String,
    val phoneNormalized: String? = null,
    val startedAt: String,
    val durationSec: Long,
    val recordingStatus: String,
    val noticeStatus: String,
    val outcomeStatus: String? = null,
    val hasAudio: Boolean,
    val hasFollowupTask: Boolean
)
@Serializable data class CallListResponse(val items: List<CallListItem>)
