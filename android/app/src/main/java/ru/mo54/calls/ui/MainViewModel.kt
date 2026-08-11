package ru.mo54.calls.ui

import android.app.Application
import android.net.Uri
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import ru.mo54.calls.BuildConfig
import ru.mo54.calls.MO54Application
import ru.mo54.calls.api.DiagnosticRequest
import ru.mo54.calls.storage.CallRecordEntity
import ru.mo54.calls.storage.NextActionType
import ru.mo54.calls.storage.OutcomeInput
import ru.mo54.calls.storage.OutcomeStatus
import java.time.LocalDateTime
import java.time.ZoneId

data class DiagnosticForm(
    val incomingCallDetected: Boolean = false,
    val outgoingCallDetected: Boolean = false,
    val incomingAudioRecorded: Boolean = false,
    val outgoingAudioRecorded: Boolean = false,
    val bothSidesAudibleIncoming: Boolean = false,
    val bothSidesAudibleOutgoing: Boolean = false,
    val noticePlayedToCall: Boolean = false,
    val worksOnLockedScreen: Boolean = false,
    val worksAfterReboot: Boolean = false,
    val notes: String = ""
)

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val container = (application as MO54Application).container
    private val repository = container.repository

    val pending: StateFlow<List<CallRecordEntity>> = repository.pending.stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList()
    )
    val history: StateFlow<List<CallRecordEntity>> = repository.history.stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList()
    )
    val message = MutableStateFlow<String?>(null)
    val linked = MutableStateFlow(container.tokenVault.read() != null)

    fun link(url: String, pairingCode: String) = viewModelScope.launch {
        message.value = "Подключение…"
        container.deviceLinker.link(url, pairingCode)
            .onSuccess { linked.value = true; message.value = "Устройство привязано к ${it.workerId}" }
            .onFailure { message.value = it.message ?: "Ошибка подключения" }
    }

    fun saveWorkOutcome(
        callId: String,
        outcomeStatus: OutcomeStatus,
        summary: String,
        objection: String,
        nextActionType: NextActionType?,
        nextActionAtText: String,
        nextActionComment: String,
        noticeConfirmed: Boolean,
        onSaved: () -> Unit
    ) = viewModelScope.launch {
        runCatching {
            val nextAt = nextActionAtText.trim().takeIf(String::isNotEmpty)?.let {
                LocalDateTime.parse(it).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
            }
            repository.saveOutcome(callId, OutcomeInput(
                outcomeStatus, summary, objection, nextActionType, nextAt,
                nextActionComment, noticeConfirmed
            ))
        }.onSuccess {
            message.value = "Рабочий звонок сохранен и поставлен в очередь"
            onSaved()
        }.onFailure { message.value = it.message ?: "Не удалось сохранить итог" }
    }

    fun discard(callId: String, onDeleted: () -> Unit) = viewModelScope.launch {
        runCatching { repository.discardNonWork(callId) }
            .onSuccess { message.value = "Нерабочий звонок полностью удален"; onDeleted() }
            .onFailure { message.value = it.message ?: "Ошибка удаления" }
    }

    fun retrySync() {
        viewModelScope.launch {
            repository.forceRetry()
            message.value = "Повторная синхронизация запланирована"
        }
    }

    fun importSharedRecording(uri: Uri, mimeType: String?) {
        viewModelScope.launch {
            message.value = "Импорт записи из Google Phone…"
            runCatching { repository.importSharedRecording(uri, mimeType) }
                .onSuccess {
                    message.value = "Запись импортирована и привязана к последнему звонку"
                }
                .onFailure { message.value = it.message ?: "Не удалось импортировать аудио" }
        }
    }

    fun setAudioSource(source: Int) {
        container.settings.audioSource = source
        message.value = "Аудиоисточник изменен; проверьте реальным звонком"
    }

    fun currentAudioSource() = container.settings.audioSource

    fun effectiveAudioSource() = container.settings.effectiveAudioSource()

    fun hasPrivilegedCallAudioAccess() = container.settings.hasPrivilegedCallAudioAccess()

    fun sendDiagnostics(form: DiagnosticForm) = viewModelScope.launch {
        val settings = container.settings
        val deviceId = settings.deviceId
        if (deviceId == null) {
            message.value = "Сначала привяжите устройство"
            return@launch
        }
        runCatching {
            val pass = form.incomingCallDetected && form.outgoingCallDetected &&
                form.incomingAudioRecorded && form.outgoingAudioRecorded &&
                form.bothSidesAudibleIncoming && form.bothSidesAudibleOutgoing
            val response = container.apiFactory.create().diagnostics(DiagnosticRequest(
                deviceId = deviceId,
                workerId = settings.workerId,
                androidVersion = Build.VERSION.RELEASE,
                manufacturer = Build.MANUFACTURER,
                model = Build.MODEL,
                appVersion = BuildConfig.VERSION_NAME,
                incomingCallDetected = form.incomingCallDetected,
                outgoingCallDetected = form.outgoingCallDetected,
                incomingAudioRecorded = form.incomingAudioRecorded,
                outgoingAudioRecorded = form.outgoingAudioRecorded,
                bothSidesAudibleIncoming = form.bothSidesAudibleIncoming,
                bothSidesAudibleOutgoing = form.bothSidesAudibleOutgoing,
                noticePlayedToCall = form.noticePlayedToCall,
                worksOnLockedScreen = form.worksOnLockedScreen,
                worksAfterReboot = form.worksAfterReboot,
                conclusion = if (pass) "pass" else "fail",
                notes = form.notes
            ))
            if (!response.isSuccessful || response.body()?.saved != true) error("HTTP ${response.code()}")
        }.onSuccess { message.value = "Диагностика отправлена" }
            .onFailure { message.value = it.message ?: "Ошибка отправки диагностики" }
    }
}
