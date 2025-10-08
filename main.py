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

import asyncio
import audioop
import base64
import json
import logging
import os
import uuid

import google.auth
import websockets
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse
from google.auth.transport import requests as google_auth_requests
from starlette.websockets import WebSocketState
from twilio.request_validator import RequestValidator
from twilio.rest import Client

# Import the REST API Client
from twilio.twiml.voice_response import Connect, VoiceResponse
from websockets.protocol import State as WebsocketProtocolState

from message_handler import process_incoming_message
from phone_number_mapping import get_agent_for_phone_number_async
from secrets_utils import flush_token_cache, get_token_from_secret_manager_async
from twilio_utils import validate_twilio_signature

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# --- Constants for Virtual Agent Endpoint Construction ---
VA_ENDPOINT_TEMPLATES = {
    "wss": (
        "wss://{hostname}/ws/google.cloud.ces.v1.SessionService/"
        "BidiRunSession/locations/{agent_info}"
    ),
    "https": "https://{hostname}/{agent_info}/sessions/{session_id}:runSession",
}
VA_HOSTNAME_MAP = {
    "wss": {"dev": "autopush-ces.sandbox.googleapis.com", "prod": "ces.googleapis.com"},
    "https": {"dev": "ces.sandbox.googleapis.com", "prod": "ces.googleapis.com"},
}

# Twilio credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN").strip()

# The public hostname where this server is accessible
PUBLIC_SERVER_HOSTNAME = os.getenv("PUBLIC_SERVER_HOSTNAME")

# Twilio request validator
validator = RequestValidator(TWILIO_AUTH_TOKEN)

# Twilio REST API client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Audio configs
AGENT_AUDIO_SAMPLING_RATE = 16000
AGENT_AUDIO_ENCODING = "LINEAR16"
TWILIO_AUDIO_SAMPLING_RATE = 8000
TWILIO_AUDIO_ENCODING = "MULAW"

# Initialize the app
app = FastAPI()


def build_virtual_agent_endpoint(
    transport: str, environment: str, agent_info: str, session_id: str
) -> str:
    """Builds the WebSocket endpoint URL for the virtual agent."""
    hostname = VA_HOSTNAME_MAP.get(transport, {}).get(environment)
    if not hostname:
        raise ValueError(
            f"Invalid environment specified: '{environment}'."
            f"Must be one of {list(VA_HOSTNAME_MAP.keys())}"
        )
    if not agent_info:
        raise ValueError("Agent info cannot be empty.")
    return VA_ENDPOINT_TEMPLATES.get(transport).format(
        hostname=hostname, agent_info=agent_info, session_id=session_id
    )


def get_location_from_agent_id(agent_id: str) -> str | None:
    """Extracts the location from a full agent resource name."""
    if not agent_id:
        return None
    try:
        parts = agent_id.split("/")
        loc_index = parts.index("locations")
        if loc_index + 1 < len(parts):
            location = parts[loc_index + 1]
            logger.info(f"Extracted location '{location}' from agent ID '{agent_id}'")
            return location
    except (ValueError, IndexError):
        pass
    logger.warning(f"Could not extract location from agent ID: {agent_id}")
    return None


def get_project_id_from_session_id(session_id: str) -> str:
    if session_id:
        parts = session_id.split("/")
        if len(parts) > 1 and parts[0] == "projects":
            project_id = parts[1]
            logger.info(f"Extracted Project ID: {project_id}")
            return project_id
        else:
            logger.warning(
                f"Could not extract Project ID from Session ID: {session_id}"
            )


def get_config_message(session_id: str, deployment_id: str | None = None) -> dict:
    """Builds the configuration message for the virtual agent."""
    config_message = {
        "config": {
            "session": session_id,
            "inputAudioConfig": {
                "audioEncoding": AGENT_AUDIO_ENCODING,
                "sampleRateHertz": AGENT_AUDIO_SAMPLING_RATE,
            },
            "outputAudioConfig": {
                "audioEncoding": AGENT_AUDIO_ENCODING,
                "sampleRateHertz": AGENT_AUDIO_SAMPLING_RATE,
            },
        }
    }
    if deployment_id:
        config_message["config"]["deployment"] = deployment_id
    return config_message


# @app.on_event("startup")
# async def startup_event():
#     # placeholder: use this for any application startup logic
#     pass


# This endpoint handles incoming calls from Twilio
@app.post("/incoming-call")
async def handle_incoming_call(request: Request):
    """
    Twilio will hit this endpoint when a call comes in.
    It returns TwiML to instruct Twilio to connect to our WebSocket server.
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

    to_number = form_params.get("To")
    if not to_number:
        logger.error("'To' phone number not found in Twilio request.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="'To' parameter is missing."
        )

    try:
        agent_config = await get_agent_for_phone_number_async(to_number)
    except Exception as e:
        logger.error(
            f"Failed to retrieve agent configuration for {to_number}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve agent configuration.",
        )

    if not agent_config:
        logger.error(f"No agent configuration found for phone number {to_number}.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No agent configured for phone number {to_number}.",
        )

    logger.info(f"Retrieved agent config for {to_number}: {agent_config}")
    agent_id = agent_config["agent_id"]
    # Default to 'prod' if the environment field is not specified in the config.
    deployment_id = agent_config.get("deployment_id")
    environment = agent_config.get("environment", "prod")
    logger.info(f"Using environment '{environment}' for agent {agent_id}")

    agent_location = get_location_from_agent_id(agent_id)
    if not agent_location:
        logger.error(f"Could not determine agent location from agent_id: {agent_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not determine agent location from agent ID.",
        )

    # The session_id is generated here. It's passed to the websocket handler
    # via TwiML parameters. The WSS URL for voice doesn't use it, but we pass
    # it to satisfy the function signature.
    session_id = f"{agent_id}/sessions/{uuid.uuid4()}"
    try:
        virtual_agent_endpoint = build_virtual_agent_endpoint(
            "wss", environment, agent_location, session_id
        )
    except ValueError as e:
        logger.error(f"Error building virtual agent endpoint: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )

    logger.info(f"Generated session ID for call from {to_number}: {session_id}")

    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"wss://{PUBLIC_SERVER_HOSTNAME}/media-stream")
    stream.parameter(name="session_id", value=session_id)
    if deployment_id:
        stream.parameter(name="deployment_id", value=deployment_id)
    stream.parameter(name="virtual_agent_endpoint", value=virtual_agent_endpoint)
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")


# This endpoint handles incoming messages from Twilio
@app.post("/incoming-message")
async def handle_incoming_message(request: Request):
    """
    Twilio will hit this endpoint when a message comes in.
    It validates the request, processes the message, and sends a reply.
    """
    return await process_incoming_message(request, validator, client)


# This is your WebSocket endpoint where Twilio will stream audio
@app.websocket("/media-stream")
async def websocket_endpoint(websocket: WebSocket):
    """
    Handles the bidirectional WebSocket connection with Twilio.
    Audio streams are sent and received here.
    """

    # Make sure this is really Twilio
    twilio_signature = websocket.headers.get("x-twilio-signature") or ""
    if not validate_twilio_signature(
        str(websocket.url), websocket.query_params, twilio_signature, validator
    ):
        logger.warning(
            "Twilio signature validation failed for WebSocket. Rejecting connection."
        )
        # Returning before accept() rejects the connection with a 403,
        return

    await websocket.accept()
    logger.info("Twilio WebSocket connection accepted.")

    va_ws_ready = asyncio.Event()
    va_ws = None
    session_id = None
    project_id = None
    ratecv_state_to_va = None
    ratecv_state_to_twilio = None
    stream_sid = None
    try:
        # --- Authentication ---
        # Determine the authentication method. The default is to use Application
        # Default Credentials (ADC). This can be overridden by setting the
        # AUTH_TOKEN_SECRET_PATH environment variable to fetch a token from
        # Secret Manager. If this override is used, you are responsible for
        # ensuring the token in Secret Manager is valid and refreshed periodically.
        auth_token_secret_path = os.getenv("AUTH_TOKEN_SECRET_PATH")
        virtual_agent_token = ""

        if auth_token_secret_path:
            # Method 2: Explicit override via Secret Manager
            logger.info(
                "AUTH_TOKEN_SECRET_PATH is set. "
                "Fetching OAuth2 token from Secret Manager as an override."
            )
            try:
                virtual_agent_token = await get_token_from_secret_manager_async()
            except Exception as e:
                logger.error(
                    f"Failed to get auth token from Secret Manager: {e}", exc_info=True
                )
                await websocket.close(
                    code=1011,
                    reason="Internal server error: could not retrieve auth token.",
                )
                return
        else:
            # Method 1: Default to Application Default Credentials (ADC)
            logger.info("Using Application Default Credentials to get access token.")
            try:
                creds, _ = google.auth.default()
                auth_req = google_auth_requests.Request()
                creds.refresh(auth_req)  # Ensure the token is valid
                virtual_agent_token = creds.token
            except Exception as e:
                logger.error(
                    f"Failed to get access token from Application Default Credentials: "
                    f"{e}",
                    exc_info=True,
                )
                await websocket.close(
                    code=1011,
                    reason="Internal server error: could not authenticate with ADC.",
                )
                return

        async def forward_twilio_to_va():
            """Receives messages from Twilio and forwards them to the Virtual Agent."""
            nonlocal stream_sid
            nonlocal ratecv_state_to_va
            nonlocal session_id
            nonlocal project_id
            nonlocal va_ws
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)
                event_type = data.get("event")

                if event_type == "connected":
                    logger.info(f"Twilio Connected: {data}")

                elif event_type == "start":
                    if "start" in data and "streamSid" in data["start"]:
                        stream_sid = data["start"]["streamSid"]
                        session_id = data["start"]["customParameters"].get("session_id")
                        deployment_id = data["start"]["customParameters"].get(
                            "deployment_id"
                        )
                        virtual_agent_url = data["start"]["customParameters"].get(
                            "virtual_agent_endpoint"
                        )
                        project_id = get_project_id_from_session_id(session_id)
                        logger.info(
                            f"Twilio Start. Stream SID: {stream_sid}, Call SID: "
                            f"{data['start']['callSid']}, Session ID: {session_id}, "
                            f"Project ID: {project_id}"
                        )

                        # Establish a connection to the virtual agent
                        auth_headers = {
                            "Authorization": f"Bearer {virtual_agent_token}",
                            "Content-Type": "application/json",
                            "X-Goog-User-Project": project_id,
                        }
                        # Create a redacted copy of headers for safe logging
                        log_headers = auth_headers.copy()
                        if virtual_agent_token and len(virtual_agent_token) > 12:
                            redacted_token = (
                                f"{virtual_agent_token[:6]}..."
                                f"{virtual_agent_token[-6:]}"
                            )
                            log_headers["Authorization"] = f"Bearer {redacted_token}"
                        elif "Authorization" in log_headers:
                            log_headers["Authorization"] = "Bearer <REDACTED>"

                        logger.debug(f"VA connection headers: {log_headers}")
                        logger.info(
                            f"Connecting to virtual agent at {virtual_agent_url} "
                            f"on session {session_id}"
                        )
                        va_ws = await websockets.connect(
                            virtual_agent_url,
                            max_size=2**22,
                            additional_headers=auth_headers,
                        )
                        logger.info(
                            f"Connected to virtual agent at {virtual_agent_url} "
                            f"on session {session_id}"
                        )
                        va_ws_ready.set()

                        # Send config message
                        config_message = get_config_message(session_id, deployment_id)
                        logger.info(
                            f"Sending config to virtual agent: {config_message}"
                        )
                        try:
                            await va_ws.send(json.dumps(config_message))
                            logger.info(
                                f"-> Sent config to virtual agent: {config_message}"
                            )
                        except websockets.exceptions.ConnectionClosedError as e:
                            if "Token expired" in str(
                                e
                            ) or "Credential sent is invalid" in str(e):
                                logger.warning(
                                    "Connection closed due to invalid/expired token. "
                                    "Flushing token cache."
                                )
                                flush_token_cache()
                            # Re-raise to let the main loop handle connection closing
                            raise

                        # Send initial message
                        kickstart_message = {"realtimeInput": {"text": "Hi!"}}
                        logger.info(
                            f"Sending initial message to virtual agent: "
                            f"{kickstart_message}"
                        )
                        await va_ws.send(json.dumps(kickstart_message))
                        logger.info(
                            f"-> Sent initial message to virtual agent: "
                            f"{kickstart_message}"
                        )
                    else:
                        logger.warning(f"Malformed start event from Twilio: {data}")

                elif event_type == "media":
                    if "media" in data and "payload" in data["media"]:
                        payload = data["media"]["payload"]
                        audio_chunk = base64.b64decode(payload)
                        logger.debug(
                            f"Received {len(audio_chunk)} bytes of audio from Twilio. "
                            f"Sending to VA."
                        )

                        linear_audio = audioop.ulaw2lin(audio_chunk, 2)
                        resampled_linear_audio, ratecv_state_to_va = audioop.ratecv(
                            linear_audio,
                            2,
                            1,
                            TWILIO_AUDIO_SAMPLING_RATE,
                            AGENT_AUDIO_SAMPLING_RATE,
                            ratecv_state_to_va,
                        )

                        base64_pcm_payload = base64.b64encode(
                            resampled_linear_audio
                        ).decode("utf-8")
                        va_input = {"realtimeInput": {"audio": base64_pcm_payload}}
                        await va_ws.send(json.dumps(va_input))
                    else:
                        logger.warning(f"Malformed media event from Twilio: {data}")

                elif event_type == "stop":
                    logger.info(f"Twilio Stop message: {data}. Closing connections.")
                    break

                elif event_type == "mark":
                    logger.info(f"Twilio Mark message: {data}")

                elif event_type == "dtmf":
                    digit = data["dtmf"]["digit"]
                    logger.info(f"Received DTMF digit: {digit}")

                else:
                    logger.warning(
                        f"Unknown event type from Twilio: {event_type}, data: {data}"
                    )

        async def forward_va_to_twilio():
            nonlocal ratecv_state_to_twilio
            """Receives messages from the Virtual Agent and forwards them to Twilio."""
            await va_ws_ready.wait()
            logger.info(
                "VA WebSocket is ready, starting to forward messages from VA to Twilio."
            )
            while True:
                va_response = await va_ws.recv()
                logger.debug(f"<- Received from virtual agent: {va_response}")
                va_data = json.loads(va_response)
                if "sessionOutput" in va_data:
                    if "audio" in va_data["sessionOutput"]:
                        va_audio = base64.b64decode(va_data["sessionOutput"]["audio"])
                        if not stream_sid:
                            logger.warning(
                                "No stream_sid available yet. "
                                "Cannot send media to Twilio."
                            )
                            continue

                        pcm_8khz_data, ratecv_state_to_twilio = audioop.ratecv(
                            va_audio,
                            2,
                            1,
                            AGENT_AUDIO_SAMPLING_RATE,
                            TWILIO_AUDIO_SAMPLING_RATE,
                            ratecv_state_to_twilio,
                        )
                        mulaw_audio = audioop.lin2ulaw(pcm_8khz_data, 2)

                        encoded_va_audio = base64.b64encode(mulaw_audio).decode("utf-8")
                        response_media_message = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": encoded_va_audio, "track": "outbound"},
                        }
                        await websocket.send_text(json.dumps(response_media_message))
                    else:
                        logger.debug(
                            f"Invalid or unknown sessionOutput from VA: {va_data}"
                        )
                elif "endSession" in va_data:
                    logger.info("VA has ended the session. Closing connections.")
                    await va_ws.close()
                    await websocket.close()
                    break
                else:
                    logger.debug(f"Invalid or unknown message from VA: {va_data}")

        # Run both tasks concurrently
        twilio_task = asyncio.create_task(forward_twilio_to_va())
        va_task = asyncio.create_task(forward_va_to_twilio())

        done, pending = await asyncio.wait(
            {twilio_task, va_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        for task in done:
            if task.exception():
                # Log the full traceback of the exception from the task.
                logger.error(
                    "Task finished with an exception", exc_info=task.exception()
                )

    except WebSocketDisconnect:
        logger.warning("WebSocket disconnected by the remote end (Twilio).")
    except websockets.exceptions.ConnectionClosed as e:
        # Log the full traceback for connection closed errors.
        logger.error(f"Connection to virtual agent closed: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected WebSocket error occurred: {e}", exc_info=True)
    finally:
        if va_ws and va_ws.state != WebsocketProtocolState.CLOSED:
            await va_ws.close()
        if websocket.client_state != WebSocketState.DISCONNECTED:
            await websocket.close()
        logger.info("All WebSocket connections closed.")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
