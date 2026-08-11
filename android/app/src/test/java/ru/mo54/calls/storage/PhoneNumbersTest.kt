package ru.mo54.calls.storage

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PhoneNumbersTest {
    @Test fun normalizesRussianNumbers() {
        assertEquals("+79130000000", PhoneNumbers.normalize("+7 913 000-00-00"))
        assertEquals("+79130000000", PhoneNumbers.normalize("8 (913) 000-00-00"))
        assertEquals("+79130000000", PhoneNumbers.normalize("9130000000"))
    }

    @Test fun leavesUnavailableNumberNull() {
        assertNull(PhoneNumbers.normalize("unknown"))
        assertNull(PhoneNumbers.normalize("private"))
        assertNull(PhoneNumbers.normalize("123"))
    }
}

