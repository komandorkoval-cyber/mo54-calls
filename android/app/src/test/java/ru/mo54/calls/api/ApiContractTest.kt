package ru.mo54.calls.api

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

class ApiContractTest {
    private lateinit var server: MockWebServer
    private lateinit var api: CallCompanionApi

    @Before fun setup() {
        server = MockWebServer().apply { start() }
        api = Retrofit.Builder()
            .baseUrl(server.url("/"))
            .addConverterFactory(Json { explicitNulls = false }.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(CallCompanionApi::class.java)
    }

    @After fun teardown() = server.shutdown()

    @Test fun metadataUsesDocumentedPathAndNullablePhone() = runTest {
        server.enqueue(MockResponse().setHeader("Content-Type", "application/json").setBody(
            """{"callId":"server-id","localCallId":"local-id","syncStatus":"accepted","updatedAt":"2026-06-23T02:14:35Z"}"""
        ))
        val response = api.upsertCall(CallRequest(
            localCallId = "local-id",
            deviceId = "device-id",
            workerId = "worker-igor",
            direction = "incoming",
            phoneRaw = "unknown",
            phoneNormalized = null,
            startedAt = "2026-06-23T02:10:00Z",
            endedAt = "2026-06-23T02:14:30Z",
            durationSec = 270,
            recordingStatus = "recorded",
            noticeStatus = "unsupported",
            eventSource = "live",
            deviceSnapshot = DeviceSnapshot("10", "Xiaomi", "M1906G7G", "0.1.0")
        ))
        assertEquals("server-id", response.body()?.callId)
        val request = server.takeRequest()
        assertEquals("/api/call-companion/calls", request.path)
        assertFalse(request.body.readUtf8().contains("phoneNormalized"))
    }

    @Test fun outcomeUsesPatchAndDoesNotCreateClientSideTask() = runTest {
        server.enqueue(MockResponse().setHeader("Content-Type", "application/json").setBody(
            """{"callId":"server-id","outcomeSaved":true,"followupTaskId":"task-id","updatedAt":"2026-06-23T02:16:00Z"}"""
        ))
        val response = api.saveOutcome("server-id", OutcomeRequest(
            outcomeStatus = "continue_work",
            summary = "Итог",
            nextActionType = "call",
            nextActionAt = "2026-06-24T03:00:00Z",
            nextActionComment = "Перезвонить",
            noticeConfirmedByWorker = false
        ))
        assertEquals("task-id", response.body()?.followupTaskId)
        val request = server.takeRequest()
        assertEquals("PATCH", request.method)
        assertEquals("/api/call-companion/calls/server-id/outcome", request.path)
    }
}
