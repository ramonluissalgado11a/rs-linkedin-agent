"""Web process entrypoint: `python -m rs_agent.server`."""

from __future__ import annotations

import os

import uvicorn

from .logging_setup import configure_logging
from .web import create_app


def main() -> None:  # pragma: no cover - process entrypoint
    configure_logging()
    app = create_app()
    host = os.environ.get("HOST", "0.0.0.0")  # nosec B104 # noqa: S104 - container must listen on all interfaces
    uvicorn.run(app, host=host, port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*", log_config=None)


if __name__ == "__main__":  # pragma: no cover
    main()
