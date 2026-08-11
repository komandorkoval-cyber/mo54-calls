package ru.mo54.calls.storage

object PhoneNumbers {
    fun normalize(raw: String?): String? {
        if (raw.isNullOrBlank() || raw == "unknown" || raw.equals("private", true)) return null
        val digits = raw.filter(Char::isDigit)
        if (digits.length < 7) return null
        return when {
            raw.trim().startsWith("+") -> "+$digits"
            digits.length == 11 && digits.startsWith("8") -> "+7${digits.drop(1)}"
            digits.length == 11 && digits.startsWith("7") -> "+$digits"
            digits.length == 10 -> "+7$digits"
            else -> "+$digits"
        }
    }
}
