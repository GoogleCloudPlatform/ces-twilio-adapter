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
Handles incoming Twilio messages by validating the request, processing the
message, and sending a reply.
"""
import asyncio
import logging
import uuid

import aiohttp
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from phone_number_mapping import get_agent_for_phone_number_async
from secrets_utils import get_token_from_secret_manager_async
from twilio_utils import validate_twilio_signature

logger = logging.getLogger(__name__)

# A constant namespace for generating session IDs from phone numbers.
# This ensures that the same phone number always generates the same UUID.
PHONE_NUMBER_NAMESPACE = uuid.UUID("a9c1512f-4b62-4a01-808c-29d0f8b8f1e1")

# --- Virtual Agent REST API ---
VA_HOSTNAME_MAP = {
    "dev": "autopush-ces.sandbox.googleapis.com",
    "prod": "ces.googleapis.com",
}


async def _forward_message_to_va(
    session_id: str, agent_id: str, message_body: str, auth_token: str, environment: str
) -> dict | None:
    """
    Forwards a user's message to the virtual agent via REST API and returns
    the response.
    """
    hostname = VA_HOSTNAME_MAP.get(environment, VA_HOSTNAME_MAP["prod"])
    url = f"https://{hostname}/v1beta/{agent_id}/sessions/{session_id}:runSession"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    body = {"inputs": [{"text": message_body}]}

    logger.info(f"Forwarding message for session {session_id} to VA at {url}")
    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(url, headers=headers, json=body) as response:
                if response.status == 200:
                    response_json = await response.json()
                    logger.info(
                        f"Successfully forwarded message for session {session_id}. "
                        f"VA response: {response_json}"
                    )
                    return response_json
                else:
                    error_text = await response.text()
                    logger.error(
                        f"Error forwarding message for session {session_id}. "
                        f"Status: {response.status}, Response: {error_text}"
                    )
                    return None
    except Exception as e:
        logger.error(
            f"Exception while forwarding message for session {session_id}: {e}",
            exc_info=True,
        )
        return None


async def process_incoming_message(
    request: Request,
    validator: RequestValidator,
    client: Client,
) -> HTMLResponse:
    """
    Validates the request, forwards it to the virtual agent, and sends the agent's
    response back to the user via SMS.
    """
    # Make sure this is really Twilio
    twilio_signature = request.headers.get("X-Twilio-Signature") or ""
    form_params = await request.form()
    if not validate_twilio_signature(
        str(request.url), form_params, twilio_signature, validator
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature"
        )

    # Extract message details from the incoming request
    from_number = form_params.get("From")
    to_number = form_params.get("To")  # This is your Twilio number
    message_body = form_params.get("Body")
    if not from_number or not to_number:
        logger.error("'From' or 'To' not found in Twilio request.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'From' or 'To' parameter is missing.",
        )

    if not message_body:
        logger.error("'Body' not found in Twilio request.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'Body' parameter is missing.",
        )

    # Generate a deterministic session ID from the sender's phone number.
    # This ensures the same phone number always produces the same session ID.
    session_id = str(uuid.uuid5(PHONE_NUMBER_NAMESPACE, from_number))

    logger.info(f"Message received for session ID: {session_id}. Forwarding to agent.")

    # Asynchronously forward the message to the virtual agent and send a reply.
    # We do this in a background task so we can immediately return a 200 OK to Twilio.
    try:
        agent_config = await get_agent_for_phone_number_async(to_number)
        if not agent_config:
            # If no agent is found, we can't forward the message. Log an error.
            # We still return a 200 to Twilio to acknowledge receipt of the message.
            logger.error(
                f"No agent configuration found for phone number {to_number}. "
                f"Cannot forward message."
            )
        else:
            agent_id = agent_config["agent_id"]
            environment = agent_config.get("environment", "prod")
            auth_token = await get_token_from_secret_manager_async()

            async def forward_and_reply():
                """Forwards message to VA and sends reply back to user."""
                va_response = await _forward_message_to_va(
                    session_id=session_id,
                    agent_id=agent_id,
                    message_body=message_body,
                    auth_token=auth_token,
                    environment=environment,
                )

                if (
                    va_response
                    and "sessionOutput" in va_response
                    and va_response["sessionOutput"]
                ):
                    reply_text = va_response["sessionOutput"][0].get("text")
                    if reply_text:
                        logger.info(
                            f"Sending reply from VA to {from_number}: '{reply_text}'"
                        )
                        try:
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(
                                None,
                                lambda: client.messages.create(
                                    to=from_number, from_=to_number, body=reply_text
                                ),
                            )
                            logger.info(f"Successfully sent reply to {from_number}")
                        except Exception as e:
                            logger.error(
                                f"Failed to send Twilio SMS to {from_number}: {e}",
                                exc_info=True,
                            )
                    else:
                        logger.warning(
                            f"No text found in VA response for session {session_id}: "
                            f"{va_response}"
                        )
                else:
                    logger.warning(
                        f"No valid response from VA for session {session_id}"
                    )

            asyncio.create_task(forward_and_reply())
    except Exception as e:
        logger.error(
            f"Failed to create task to forward message for session {session_id}: {e}",
            exc_info=True,
        )

    # Respond to Twilio immediately to acknowledge receipt of the message.
    response = MessagingResponse()
    return HTMLResponse(content=str(response), media_type="application/xml")
