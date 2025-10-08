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
Handles fetching and caching secrets from Google Secret Manager.
"""
import json
import logging
import os
from datetime import datetime, timedelta

from google.cloud import secretmanager_v1 as secretmanager

logger = logging.getLogger(__name__)

# --- Token Caching and Secret Manager ---
_token_cache = {
    "token": None,
    "fetched_at": None,
}
_secret_manager_async_client = None


def flush_token_cache():
    """Flushes the in-memory auth token cache."""
    logger.info("Flushing auth token cache.")
    _token_cache["token"] = None
    _token_cache["fetched_at"] = None


def get_secret_manager_async_client():
    """Initializes and returns a singleton async Secret Manager client."""
    global _secret_manager_async_client
    if _secret_manager_async_client is None:
        logger.info("Initializing async Secret Manager client.")
        _secret_manager_async_client = secretmanager.SecretManagerServiceAsyncClient()
    return _secret_manager_async_client


async def get_token_from_secret_manager_async():
    """
    Asynchronously fetches a token from Google Secret Manager, with in-memory caching.
    The token is fetched from the path specified in the
    AUTH_TOKEN_SECRET_PATH environment variable.
    """
    now = datetime.utcnow()
    # We don't know how recently the token was updated so we'll cache for 5 min.
    if (
        _token_cache["token"]
        and _token_cache["fetched_at"]
        and (now - _token_cache["fetched_at"] < timedelta(minutes=5))
    ):
        logger.info("Using cached auth token from Secret Manager.")
        return _token_cache["token"]

    logger.info("Fetching fresh auth token from Secret Manager.")
    secret_path = os.getenv("AUTH_TOKEN_SECRET_PATH")
    if not secret_path:
        logger.error("AUTH_TOKEN_SECRET_PATH environment variable is not set.")
        raise ValueError(
            "AUTH_TOKEN_SECRET_PATH is not set for service account token retrieval."
        )

    client = get_secret_manager_async_client()

    # Ensure we are fetching the latest version.
    if "/versions/" not in secret_path:
        secret_path = f"{secret_path}/versions/latest"

    try:
        response = await client.access_secret_version(name=secret_path)
        secret_payload = response.payload.data.decode("UTF-8")
        try:
            secret_json = json.loads(secret_payload)
            token = secret_json.get("access_token")
        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON from secret payload at {secret_path}")
            raise ValueError("Secret payload is not valid JSON.")

        if not token:
            logger.error(
                f"'access_token' key not found in secret payload from {secret_path}"
            )
            raise ValueError("'access_token' key not found in secret payload.")

        # Update cache
        _token_cache["token"] = token.strip()
        _token_cache["fetched_at"] = datetime.utcnow()

        logger.info("Successfully fetched and cached auth token from Secret Manager.")
        return token
    except Exception as e:
        logger.error(
            f"Failed to fetch token from Secret Manager at path {secret_path}: {e}",
            exc_info=True,
        )
        # Re-raise to ensure the connection fails if we can't get a token.
        raise
