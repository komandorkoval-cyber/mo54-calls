package ru.mo54.calls.storage

import org.junit.Test

class OutcomeRulesTest {
    @Test(expected = IllegalStateException::class)
    fun summaryIsRequiredExceptNoAnswer() {
        OutcomeRules.validate(OutcomeInput(
            OutcomeStatus.NEW_LEAD, null, null, null, null, null, false
        ))
    }

    @Test fun noAnswerMayHaveNoSummary() {
        OutcomeRules.validate(OutcomeInput(
            OutcomeStatus.NO_ANSWER, null, null, null, null, null, false
        ))
    }

    @Test(expected = IllegalArgumentException::class)
    fun activeOutcomeRequiresCompleteNextAction() {
        OutcomeRules.validate(OutcomeInput(
            OutcomeStatus.CONTINUE_WORK, "Итог", null, NextActionType.CALL, null, null, false
        ))
    }

    @Test fun activeOutcomeWithNextActionIsValid() {
        OutcomeRules.validate(OutcomeInput(
            OutcomeStatus.CONTINUE_WORK, "Итог", null, NextActionType.CALL,
            System.currentTimeMillis(), "Перезвонить", false
        ))
    }
}

