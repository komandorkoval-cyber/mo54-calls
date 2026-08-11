package ru.mo54.calls.api

import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import ru.mo54.calls.storage.AppSettings
import ru.mo54.calls.storage.TokenVault
import java.util.concurrent.TimeUnit

class ApiFactory(
    private val settings: AppSettings,
    private val tokenVault: TokenVault
) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    fun create(baseUrlOverride: String? = null): CallCompanionApi {
        val baseUrl = (baseUrlOverride ?: settings.backendBaseUrl)
            ?: error("Backend URL не настроен")
        require(baseUrl.startsWith("https://")) { "Backend URL должен использовать HTTPS" }
        val normalized = baseUrl.trimEnd('/') + "/"
        val client = OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val token = tokenVault.read()
                val request = chain.request().newBuilder().apply {
                    if (token != null) header("Authorization", "Bearer $token")
                }.build()
                chain.proceed(request)
            }
            .addInterceptor(HttpLoggingInterceptor().apply { level = HttpLoggingInterceptor.Level.BASIC })
            .build()
        return Retrofit.Builder()
            .baseUrl(normalized)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(CallCompanionApi::class.java)
    }
}

