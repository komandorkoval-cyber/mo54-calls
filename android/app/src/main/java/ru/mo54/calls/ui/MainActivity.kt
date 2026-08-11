package ru.mo54.calls.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Build
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import ru.mo54.calls.storage.CallRecordEntity
import ru.mo54.calls.storage.NextActionType
import ru.mo54.calls.storage.OutcomeStatus
import java.text.DateFormat
import java.util.Date

class MainActivity : ComponentActivity() {
    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val initialCallId = intent.getStringExtra(EXTRA_CALL_ID)
        setContent { MaterialTheme { MO54App(viewModel, initialCallId) } }
        handleExternalIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleExternalIntent(intent)
    }

    @Suppress("DEPRECATION")
    private fun handleExternalIntent(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val uri = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            intent.getParcelableExtra(Intent.EXTRA_STREAM)
        }
        if (uri != null) {
            viewModel.importSharedRecording(uri, intent.type)
            setIntent(Intent(this, MainActivity::class.java))
        }
    }

    companion object { const val EXTRA_CALL_ID = "call_id" }
}

private enum class Screen(val title: String) {
    PENDING("На разбор"), HISTORY("История"), SETUP("Подключение"), DIAGNOSTICS("Диагностика")
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MO54App(viewModel: MainViewModel, initialCallId: String?) {
    val pending by viewModel.pending.collectAsState()
    val history by viewModel.history.collectAsState()
    val message by viewModel.message.collectAsState()
    var screen by remember { mutableStateOf(if (initialCallId != null) Screen.PENDING else Screen.SETUP) }
    var editingId by remember { mutableStateOf(initialCallId) }
    val editing = pending.firstOrNull { it.localCallId == editingId }

    Scaffold(
        topBar = { TopAppBar(title = { Text("MO54 Calls · ${screen.title}") }) },
        bottomBar = {
            NavigationBar {
                Screen.entries.forEach { item ->
                    NavigationBarItem(
                        selected = screen == item,
                        onClick = { screen = item; editingId = null },
                        icon = { Text(if (item == Screen.PENDING) pending.size.toString() else "•") },
                        label = { Text(item.title) }
                    )
                }
            }
        }
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            message?.let { Text(it, Modifier.padding(horizontal = 16.dp, vertical = 4.dp), color = MaterialTheme.colorScheme.primary) }
            when {
                editing != null -> OutcomeScreen(editing, viewModel) { editingId = null }
                screen == Screen.PENDING -> CallList(pending, true, onOpen = { editingId = it.localCallId })
                screen == Screen.HISTORY -> HistoryScreen(history, viewModel)
                screen == Screen.SETUP -> SetupScreen(viewModel)
                screen == Screen.DIAGNOSTICS -> DiagnosticsScreen(viewModel)
            }
        }
    }
}

@Composable
private fun CallList(calls: List<CallRecordEntity>, pending: Boolean, onOpen: (CallRecordEntity) -> Unit) {
    LazyColumn(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (calls.isEmpty()) item { Text(if (pending) "Нет звонков, ожидающих классификации" else "История пуста") }
        items(calls, key = { it.localCallId }) { call ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(14.dp)) {
                    Text(call.phoneRaw, style = MaterialTheme.typography.titleMedium)
                    Text("${call.direction.name.lowercase()} · ${call.durationSec} сек · ${formatTime(call.startedAt)}")
                    Text("Запись: ${call.recordingStatus.name.lowercase()} · ${call.syncStatus.name.lowercase()}")
                    if (pending) Button(onClick = { onOpen(call) }) { Text("Классифицировать") }
                }
            }
        }
    }
}

@Composable
private fun HistoryScreen(calls: List<CallRecordEntity>, viewModel: MainViewModel) {
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(12.dp), horizontalArrangement = Arrangement.End) {
            OutlinedButton(onClick = viewModel::retrySync) { Text("Повторить отправку") }
        }
        CallList(calls, false) {}
    }
}

@Composable
private fun OutcomeScreen(call: CallRecordEntity, viewModel: MainViewModel, close: () -> Unit) {
    var outcome by remember { mutableStateOf(OutcomeStatus.CONTINUE_WORK) }
    var summary by remember { mutableStateOf("") }
    var objection by remember { mutableStateOf("") }
    var nextType by remember { mutableStateOf<NextActionType?>(NextActionType.CALL) }
    var nextAt by remember { mutableStateOf("") }
    var nextComment by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }

    LazyColumn(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { Text("${call.phoneRaw} · ${call.durationSec} сек") }
        item { EnumPicker("Результат", outcome, OutcomeStatus.entries) { outcome = it } }
        item { OutlinedTextField(summary, { summary = it }, label = { Text("Итог разговора") }, modifier = Modifier.fillMaxWidth()) }
        item { OutlinedTextField(objection, { objection = it }, label = { Text("Возражение") }, modifier = Modifier.fillMaxWidth()) }
        if (outcome in ru.mo54.calls.storage.CallRepository.ACTIVE_OUTCOMES) {
            item { EnumPicker("Следующий шаг", nextType ?: NextActionType.CALL, NextActionType.entries) { nextType = it } }
            item { OutlinedTextField(nextAt, { nextAt = it }, label = { Text("Дата: 2026-06-24T10:00") }, modifier = Modifier.fillMaxWidth()) }
            item { OutlinedTextField(nextComment, { nextComment = it }, label = { Text("Комментарий к шагу") }, modifier = Modifier.fillMaxWidth()) }
        }
        item {
            Row { Checkbox(notice, { notice = it }); Text("Уведомил о записи вручную", Modifier.padding(top = 12.dp)) }
        }
        item {
            Button(onClick = {
                viewModel.saveWorkOutcome(call.localCallId, outcome, summary, objection, nextType, nextAt, nextComment, notice, close)
            }, modifier = Modifier.fillMaxWidth()) { Text("Рабочий — сохранить и отправить") }
        }
        item {
            OutlinedButton(onClick = { confirmDelete = true }, modifier = Modifier.fillMaxWidth()) { Text("Не рабочий — удалить полностью") }
        }
    }
    if (confirmDelete) AlertDialog(
        onDismissRequest = { confirmDelete = false },
        title = { Text("Удалить звонок?") },
        text = { Text("Metadata и аудиофайл будут удалены без отправки в backend.") },
        confirmButton = { TextButton(onClick = { confirmDelete = false; viewModel.discard(call.localCallId, close) }) { Text("Удалить") } },
        dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Отмена") } }
    )
}

@Composable
private fun <T : Enum<T>> EnumPicker(label: String, selected: T, values: List<T>, onSelect: (T) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Column {
        Text(label)
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth()) { Text(selected.name.lowercase()) }
        DropdownMenu(expanded, { expanded = false }) {
            values.forEach { value -> DropdownMenuItem(text = { Text(value.name.lowercase()) }, onClick = { onSelect(value); expanded = false }) }
        }
    }
}

@Composable
private fun SetupScreen(viewModel: MainViewModel) {
    var url by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    val context = LocalContext.current
    var permissionRefresh by remember { mutableIntStateOf(0) }
    val permissions = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { permissionRefresh++ }
    val requiredPermissions = remember(permissionRefresh) {
        buildList {
            add(Manifest.permission.READ_PHONE_STATE)
            add(Manifest.permission.READ_CALL_LOG)
            add(Manifest.permission.READ_PHONE_NUMBERS)
            add(Manifest.permission.RECORD_AUDIO)
            add(Manifest.permission.READ_EXTERNAL_STORAGE)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
    LazyColumn(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { OutlinedTextField(url, { url = it }, label = { Text("Backend HTTPS URL") }, modifier = Modifier.fillMaxWidth()) }
        item { OutlinedTextField(code, { code = it }, label = { Text("Pairing code") }, modifier = Modifier.fillMaxWidth()) }
        item { Button(onClick = { viewModel.link(url, code) }, modifier = Modifier.fillMaxWidth()) { Text("Подключить") } }
        item {
            Button(onClick = {
                permissions.launch(requiredPermissions.toTypedArray())
            }, modifier = Modifier.fillMaxWidth()) { Text("Выдать разрешения") }
        }
        items(requiredPermissions) { permission ->
            val granted = androidx.core.content.ContextCompat.checkSelfPermission(context, permission) == android.content.pm.PackageManager.PERMISSION_GRANTED
            Text("${if (granted) "✓" else "✗"} ${permission.substringAfterLast('.')}")
        }
        item {
            OutlinedButton(onClick = {
                context.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}")))
            }, modifier = Modifier.fillMaxWidth()) { Text("Отключить оптимизацию батареи") }
        }
        item { Text("В MIUI также включите Автозапуск и режим батареи «Нет ограничений» для MO54 Calls.") }
        item { Text("Для этого Redmi рабочий обход без root: звонить через Google Phone, нажать «Запись», после завершения открыть запись и нажать «Поделиться» → MO54 Calls. Приложение импортирует аудио и привяжет его к последнему звонку.") }
    }
}

@Composable
private fun DiagnosticsScreen(viewModel: MainViewModel) {
    var source by remember { mutableIntStateOf(viewModel.currentAudioSource()) }
    var form by remember { mutableStateOf(DiagnosticForm()) }
    val privileged = remember { viewModel.hasPrivilegedCallAudioAccess() }
    val effectiveSource = remember(source, privileged) { viewModel.effectiveAudioSource() }
    val sources = listOf(
        android.media.MediaRecorder.AudioSource.MIC to "MIC",
        android.media.MediaRecorder.AudioSource.VOICE_RECOGNITION to "VOICE_RECOGNITION",
        android.media.MediaRecorder.AudioSource.VOICE_COMMUNICATION to "VOICE_COMMUNICATION",
        android.media.MediaRecorder.AudioSource.VOICE_CALL to "VOICE_CALL (может требовать root)"
    )
    LazyColumn(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { Text("${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL} · Android ${android.os.Build.VERSION.RELEASE}") }
        item { Text("Privileged call-audio: ${if (privileged) "да, будет использован VOICE_CALL автоматически" else "нет, обычный APK не может писать обе стороны"}") }
        item { Text("Фактический источник записи: ${if (effectiveSource == android.media.MediaRecorder.AudioSource.VOICE_CALL) "VOICE_CALL" else source.toString()}") }
        item { Text("Выберите аудиоисточник и выполните три входящих и три исходящих теста.") }
        item { Text("Если публичные источники дают тишину, используйте импорт: Google Phone → запись звонка → Поделиться → MO54 Calls.") }
        items(sources) { (id, name) ->
            OutlinedButton(onClick = { source = id; viewModel.setAudioSource(id) }, modifier = Modifier.fillMaxWidth()) {
                Text(if (source == id) "✓ $name" else name)
            }
        }
        item { DiagnosticCheck("Входящие обнаружены", form.incomingCallDetected) { form = form.copy(incomingCallDetected = it) } }
        item { DiagnosticCheck("Исходящие обнаружены", form.outgoingCallDetected) { form = form.copy(outgoingCallDetected = it) } }
        item { DiagnosticCheck("Аудио входящих создано", form.incomingAudioRecorded) { form = form.copy(incomingAudioRecorded = it) } }
        item { DiagnosticCheck("Аудио исходящих создано", form.outgoingAudioRecorded) { form = form.copy(outgoingAudioRecorded = it) } }
        item { DiagnosticCheck("Обе стороны: входящие 3/3", form.bothSidesAudibleIncoming) { form = form.copy(bothSidesAudibleIncoming = it) } }
        item { DiagnosticCheck("Обе стороны: исходящие 3/3", form.bothSidesAudibleOutgoing) { form = form.copy(bothSidesAudibleOutgoing = it) } }
        item { DiagnosticCheck("Автофраза слышна в линии", form.noticePlayedToCall) { form = form.copy(noticePlayedToCall = it) } }
        item { DiagnosticCheck("Работает при блокировке", form.worksOnLockedScreen) { form = form.copy(worksOnLockedScreen = it) } }
        item { DiagnosticCheck("Работает после перезагрузки", form.worksAfterReboot) { form = form.copy(worksAfterReboot = it) } }
        item { OutlinedTextField(form.notes, { form = form.copy(notes = it) }, label = { Text("Примечания") }, modifier = Modifier.fillMaxWidth()) }
        item { Button(onClick = { viewModel.sendDiagnostics(form) }, modifier = Modifier.fillMaxWidth()) { Text("Отправить диагностику") } }
        item { Spacer(Modifier.height(12.dp)) }
    }
}

@Composable
private fun DiagnosticCheck(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row { Checkbox(checked, onChange); Text(label, Modifier.padding(top = 12.dp)) }
}

private fun formatTime(value: Long): String = DateFormat.getDateTimeInstance().format(Date(value))
