"""Public errors exclude upstream bodies and authentication credentials."""


class ServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        *,
        upstream_code: int | None = None,
        fallback_errors: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.upstream_code = upstream_code
        self.fallback_errors = fallback_errors
