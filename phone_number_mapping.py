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

import json
import logging
import os
import re
from abc import ABC, abstractmethod

from google.cloud import firestore

logger = logging.getLogger(__name__)

# --- Abstract Base Class ---


def _process_agent_config(config: dict) -> dict | None:
    """Helper to process agent config, prioritizing deployment_id."""
    if not config:
        return None
    if "deployment_id" in config:
        # Extract agent_id from deployment_id
        match = re.match(
            r"^(projects/[^/]+/locations/[^/]+/apps/[^/]+)/deployments/.*",
            config["deployment_id"],
        )
        if match:
            config["agent_id"] = match.group(1)
    return config if "agent_id" in config else None


class PhoneNumberMappingProvider(ABC):
    """Abstract base class for providing phone number to agent mappings."""

    @abstractmethod
    async def get_agent_config(self, phone_number: str) -> dict | None:
        """
        Retrieves agent configuration for a given phone number.

        Args:
            phone_number: The E.164 formatted phone number to look up.

        Returns:
            A dictionary containing 'agent_id' and 'endpoint', or None if not found.
        """
        pass


# --- Firestore Implementation ---


class FirestoreMappingProvider(PhoneNumberMappingProvider):
    """Retrieves agent configuration from Firestore."""

    def __init__(self, collection_id: str):
        if not collection_id:
            raise ValueError("Firestore collection_id cannot be empty.")
        self.collection_id = collection_id
        self.db = firestore.AsyncClient()

    async def get_agent_config(self, phone_number: str) -> dict | None:
        """
        Queries Firestore for a document matching the phone number.
        """
        logger.info(
            f"Querying Firestore collection '{self.collection_id}' "
            f"for document '{phone_number}'"
        )
        try:
            doc_ref = self.db.collection(self.collection_id).document(phone_number)
            doc = await doc_ref.get()

            if doc.exists:
                agent_config = doc.to_dict()
                processed_config = _process_agent_config(agent_config)
                if processed_config:
                    logger.info(
                        f"Found agent config for {phone_number}: {agent_config}"
                    )
                    return processed_config
                else:
                    logger.warning(
                        f"Document for {phone_number} found but is missing 'agent_id' "
                        f"and a valid 'deployment_id'."
                    )
            else:
                logger.warning(
                    f"No document found in Firestore for phone number: {phone_number}"
                )

        except Exception as e:
            logger.error(
                f"Error querying Firestore for phone number {phone_number}: {e}",
                exc_info=True,
            )
            raise

        return None


# --- File-based Implementation ---


class FileMappingProvider(PhoneNumberMappingProvider):
    """Retrieves agent configuration from a local JSON file."""

    def __init__(self, file_path: str):
        if not file_path:
            raise ValueError("File path for mapping provider cannot be empty.")
        self.file_path = file_path
        self._mappings = self._load_mappings()

    def _load_mappings(self) -> dict:
        logger.info(f"Loading phone number mappings from file: {self.file_path}")
        try:
            with open(self.file_path, "r") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError(
                        "JSON root of mapping file must be an object/dictionary."
                    )
                logger.info(
                    f"Successfully loaded {len(data)} mappings from {self.file_path}"
                )
                return data
        except FileNotFoundError:
            logger.error(f"Mapping file not found at path: {self.file_path}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from {self.file_path}: {e}")
            raise
        except Exception as e:
            logger.error(
                f"Error loading mappings from {self.file_path}: {e}", exc_info=True
            )
            raise

    async def get_agent_config(self, phone_number: str) -> dict | None:
        """
        Looks up the phone number in the loaded file mappings.
        """
        logger.info(f"Looking up phone number '{phone_number}' in file-based mappings.")
        agent_config = self._mappings.get(phone_number, {})
        processed_config = _process_agent_config(
            agent_config.copy()
        )  # Use copy to avoid modifying loaded dict
        if processed_config:
            logger.info(f"Found agent config for {phone_number}: {processed_config}")
            return processed_config

        logger.warning(
            f"No valid agent configuration found in file for phone number: "
            f"{phone_number}"
        )
        return None


# --- Factory ---

_mapping_provider_instance: PhoneNumberMappingProvider | None = None


def get_mapping_provider() -> PhoneNumberMappingProvider:
    """
    Factory function to get the correct phone number mapping provider
    based on environment variables. This function caches the provider instance.
    """
    global _mapping_provider_instance
    if _mapping_provider_instance:
        return _mapping_provider_instance

    firestore_collection_id = os.getenv("NUMBERS_COLLECTION_ID")
    file_config_path = os.getenv("NUMBERS_CONFIG_FILE")

    if firestore_collection_id and file_config_path:
        raise ValueError(
            "Both NUMBERS_COLLECTION_ID and NUMBERS_CONFIG_FILE are set. "
            "Please choose one."
        )

    if firestore_collection_id:
        logger.info("Using Firestore for phone number mapping.")
        _mapping_provider_instance = FirestoreMappingProvider(firestore_collection_id)
    elif file_config_path:
        logger.info("Using local file for phone number mapping.")
        _mapping_provider_instance = FileMappingProvider(file_config_path)
    else:
        raise ValueError(
            "No phone number mapping provider configured. "
            "Set either NUMBERS_COLLECTION_ID or NUMBERS_CONFIG_FILE."
        )

    return _mapping_provider_instance


# --- Public API ---


async def get_agent_for_phone_number_async(phone_number: str) -> dict | None:
    """
    Retrieves agent configuration from the configured source based on a phone number.
    The data source (Firestore or local file) is determined by environment variables.
    """
    try:
        provider = get_mapping_provider()
        return await provider.get_agent_config(phone_number)
    except ValueError as e:
        logger.error(f"Configuration error for mapping provider: {e}")
        raise
