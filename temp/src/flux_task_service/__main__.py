"""Run the service with its configured address."""

import uvicorn

from flux_task_service.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "flux_task_service.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
