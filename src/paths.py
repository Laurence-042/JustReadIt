# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Centralised persistent-data path helpers.

All JustReadIt data files land under ``%APPDATA%\\JustReadIt\\``.
Importing this module never requires PySide6, so it can be used by the MCP
server and any headless script as well as the debug UI.
"""
from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    """Return ``%APPDATA%\\JustReadIt``, creating it if absent."""
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    path = appdata / "JustReadIt"
    path.mkdir(parents=True, exist_ok=True)
    return path


def knowledge_db_path() -> Path:
    """Default path for the game knowledge base SQLite file."""
    return app_data_dir() / "knowledge.db"


def translations_db_path() -> Path:
    """Default path for the persistent translation cache SQLite file."""
    return app_data_dir() / "translations.db"


def dataset_db_path() -> Path:
    """Default path for the pipeline dataset SQLite file."""
    return app_data_dir() / "pipeline_dataset.db"


def config_path() -> Path:
    """Path for the JSON application configuration file."""
    return app_data_dir() / "config.json"
