class DebateError(Exception):
    pass


class BudgetExceeded(DebateError):
    pass


class Cancelled(DebateError):
    pass


class EvidenceError(DebateError):
    pass


class NeedsReview(DebateError):
    pass


class ProviderFailure(DebateError):
    def __init__(self, message: str, *, retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain
