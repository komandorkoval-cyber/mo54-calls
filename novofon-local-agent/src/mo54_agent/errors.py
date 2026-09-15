class AgentError(RuntimeError):
    """A safe, user-visible agent error with no recording data in its text."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class BrowserInteractionError(AgentError):
    pass


class StableIdentifierMissing(BrowserInteractionError):
    def __init__(self):
        super().__init__("stable_identifier_missing", "Novofon row has no stable call_session_id; automation stopped", retryable=False)
