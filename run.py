"""Startet den Pasta-Trockner-Webserver.

  python run.py
Konfig-Pfad optional über Umgebungsvariable PASTADRYER_CONFIG.
"""
import logging
import os

import uvicorn

from pastadryer.config import Config
from pastadryer.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

CONFIG_PATH = os.environ.get("PASTADRYER_CONFIG", "config.yaml")
app = create_app(CONFIG_PATH)

if __name__ == "__main__":
    cfg = Config.load(CONFIG_PATH)
    uvicorn.run(app, host=cfg.host, port=cfg.port)
