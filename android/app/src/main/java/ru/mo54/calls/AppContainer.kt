package ru.mo54.calls

import android.content.Context
import ru.mo54.calls.api.ApiFactory
import ru.mo54.calls.api.DeviceLinker
import ru.mo54.calls.storage.AppDatabase
import ru.mo54.calls.storage.AppSettings
import ru.mo54.calls.storage.CallRepository
import ru.mo54.calls.storage.TokenVault

class AppContainer(context: Context) {
    val database = AppDatabase.create(context)
    val settings = AppSettings(context)
    val tokenVault = TokenVault(context)
    val apiFactory = ApiFactory(settings, tokenVault)
    val deviceLinker = DeviceLinker(apiFactory, settings, tokenVault)
    val repository = CallRepository(context, database, settings)
}

