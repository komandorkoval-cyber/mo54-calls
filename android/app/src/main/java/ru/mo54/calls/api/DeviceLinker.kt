package ru.mo54.calls.api

import android.os.Build
import ru.mo54.calls.BuildConfig
import ru.mo54.calls.storage.AppSettings
import ru.mo54.calls.storage.TokenVault

class DeviceLinker(
    private val factory: ApiFactory,
    private val settings: AppSettings,
    private val tokenVault: TokenVault
) {
    suspend fun link(baseUrl: String, pairingCode: String): Result<DeviceLinkResponse> = runCatching {
        require(baseUrl.startsWith("https://")) { "Нужен HTTPS URL" }
        require(pairingCode.isNotBlank()) { "Введите pairing code" }
        val response = factory.create(baseUrl).deviceLink(DeviceLinkRequest(
            pairingCode = pairingCode.trim(),
            deviceName = "${Build.MANUFACTURER} ${Build.MODEL}",
            androidVersion = Build.VERSION.RELEASE,
            manufacturer = Build.MANUFACTURER,
            model = Build.MODEL,
            appVersion = BuildConfig.VERSION_NAME
        ))
        if (!response.isSuccessful) error("Backend вернул HTTP ${response.code()}")
        val body = response.body() ?: error("Пустой ответ backend")
        tokenVault.save(body.deviceToken)
        settings.backendBaseUrl = baseUrl
        settings.deviceId = body.deviceId
        settings.workerId = body.workerId
        body
    }
}
