class FakeHttpError(Exception):
    """Looks like the error of a common HTTP client: the status sits on .response.status_code."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")

        class _Response:
            status_code = status

        self.response = _Response()


class Script:
    """A fake paid function. Each call plays the next outcome; the last outcome repeats."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or ["ok"]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
