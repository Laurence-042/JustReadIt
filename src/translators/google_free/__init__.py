# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Free (no-API-key) Google Translate backend module."""
from __future__ import annotations

from .translator import GoogleFreeTranslator
from .translator import build_from_config

__all__ = ["GoogleFreeTranslator", "build_from_config"]
