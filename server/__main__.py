"""python -m server  ->  serves the API and the dashboard on GT_HOST:GT_PORT (0.0.0.0:8080)."""
import logging
import os

import uvicorn

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("GT_HOST", "0.0.0.0"), port=int(os.environ.get("GT_PORT", "8080")),
                log_level="info")
