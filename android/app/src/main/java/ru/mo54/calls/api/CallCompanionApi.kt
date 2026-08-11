package ru.mo54.calls.api

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query

interface CallCompanionApi {
    @POST("api/call-companion/device-link")
    suspend fun deviceLink(@Body request: DeviceLinkRequest): Response<DeviceLinkResponse>

    @GET("api/call-companion/config")
    suspend fun config(): Response<ConfigResponse>

    @POST("api/call-companion/calls")
    suspend fun upsertCall(@Body request: CallRequest): Response<CallResponse>

    @Multipart
    @POST("api/call-companion/calls/{callId}/audio")
    suspend fun uploadAudio(
        @Path("callId") callId: String,
        @Part("localCallId") localCallId: RequestBody,
        @Part("audioSha256") audioSha256: RequestBody,
        @Part("recordingStatus") recordingStatus: RequestBody,
        @Part("mimeType") mimeType: RequestBody,
        @Part("durationSec") durationSec: RequestBody,
        @Part file: MultipartBody.Part
    ): Response<AudioResponse>

    @PATCH("api/call-companion/calls/{callId}/outcome")
    suspend fun saveOutcome(
        @Path("callId") callId: String,
        @Body request: OutcomeRequest
    ): Response<OutcomeResponse>

    @POST("api/call-companion/diagnostics")
    suspend fun diagnostics(@Body request: DiagnosticRequest): Response<DiagnosticResponse>

    @GET("api/call-companion/calls")
    suspend fun listCalls(
        @Query("workerId") workerId: String,
        @Query("status") status: String? = null
    ): Response<CallListResponse>
}
