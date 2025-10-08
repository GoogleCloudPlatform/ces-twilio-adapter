# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Utilities for interacting with Twilio.
"""
import logging
from typing import Mapping

from twilio.request_validator import RequestValidator

logger = logging.getLogger(__name__)


def validate_twilio_signature(
    url: str, params: Mapping, signature: str | None, validator: RequestValidator
) -> bool:
    """Validates a Twilio request signature."""
    # force url to appear as https or wss because Cloud Run sees http / ws
    url = url.replace("http:", "https:").replace("ws:", "wss:")
    is_valid = validator.validate(url, params, signature)
    if not is_valid:
        logger.warning(f"Twilio signature validation failed for url: {url}")
    logger.info(f"Twilio signature validation result: {is_valid}")
    return is_valid
