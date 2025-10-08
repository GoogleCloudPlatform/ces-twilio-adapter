# CES Twilio Adapter

This repository contains a telephony adapter to connect Twilio with Google Cloud's conversational AI agents. It acts as a bridge, receiving voice calls from Twilio, dynamically routing them to the correct AI agent based on the dialed phone number, forwarding the audio stream to the agent via WebSocket, and streaming the agent's audio response back to the caller through Twilio.

The application is designed to be deployed as a Google Cloud Run service.

## Deployment to Cloud Run

### 1. Prerequisites

*   Google Cloud SDK (`gcloud`) installed and authenticated.
*   A Google Cloud project with the Cloud Run, Secret Manager, and Firestore APIs enabled.
*   A Firestore database created in your project.
*   A Twilio account and a provisioned phone number.
*   A deployed conversational AI agent (e.g., a CES agent).

### 2. Configuration

Before deploying, you must customize the configuration values in `script/values.sh`.

```bash
#!/bin/bash

# MUST BE EDITED
PROJECT_ID="your-gcp-project-id"
LOCATION="us-central1"

# USUALLY EDITED AFTER FIRST DEPLOYMENT
PUBLIC_SERVER_HOSTNAME="ces-twilio-adapter-xxxx.a.run.app" # e.g. ces-twilio-adapter-abcdef-uc.a.run.app

# TWILIO CONFIGURATION
TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# SECRET CONFIGURATION (see Secrets and Authentication section)
# The full path to the secret containing the OAuth2 token for the agent.
AUTH_TOKEN_SECRET_PATH="projects/your-gcp-project-id/secrets/ces-twilio-adapter-token"
# The full path to the secret containing your Twilio Auth Token.
TWILIO_AUTH_TOKEN_PATH="projects/your-gcp-project-id/secrets/ces-twilio-auth-token"

# PHONE NUMBER MAPPING CONFIGURATION (see "Phone Number to Agent Mapping" section)
# Choose ONE of the following methods.
# 1. Firestore (recommended for production)
NUMBERS_COLLECTION_ID="ces-twilio-adapter-mappings"
# 2. Local JSON file (for development/testing)
# NUMBERS_CONFIG_FILE="number_mappings.json"

# GENERALLY UNCHANGED
SERVICE_NAME=ces-twilio-adapter
SERVICE_ACCOUNT=ces-twilio-adapter@${PROJECT_ID}.iam.gserviceaccount.com
TIMEOUT=60m
```

**Required changes:**
*   `PROJECT_ID`: Your Google Cloud Project ID.
*   `LOCATION`: The Google Cloud region where you want to deploy the service.
*   `TWILIO_ACCOUNT_SID`: Your Twilio Account SID.
*   `AUTH_TOKEN_SECRET_PATH`: Update the project ID in this path. The secret name can be changed, but it must match the secret you create.
*   `TWILIO_AUTH_TOKEN_PATH`: Update the project ID in this path. The secret name can be changed, but it must match the secret you create.
*   `NUMBERS_COLLECTION_ID` / `NUMBERS_CONFIG_FILE`: Set one of these to define your phone number to agent mapping source. Comment out the one you are not using.

### 3. Phone Number to Agent Mapping

This adapter dynamically maps an incoming phone number to a specific conversational agent. This allows you to change which agent answers a call without redeploying the service. You can configure this mapping using either Firestore (for dynamic, remote configuration) or a local JSON file (for simple, static configuration).

Choose **one** of the following methods and configure the corresponding environment variable in `script/values.sh`.

#### Method 1: Firestore (Recommended for Production)

Using Firestore allows you to update phone number mappings without redeploying the adapter.

**Configuration:** Set the `NUMBERS_COLLECTION_ID` environment variable in `script/values.sh` to the name of your Firestore collection.

**Data Structure:**
*   **Collection ID:** The name you specified for `NUMBERS_COLLECTION_ID` (e.g., `ces-twilio-adapter-mappings`).
*   **Document ID:** The Twilio phone number in E.164 format (e.g., `+18005551212`).
*   **Fields:**
    *   `deployment_id` (string): **(Preferred)** The full resource name of a specific agent deployment. This is the recommended way to specify an agent, as it pins the adapter to a specific version of your agent.
        *   Example: `projects/your-gcp-project-id/locations/us-east1/apps/app-id/deployments/deployment-id`
    *   `environment` (string): Optional. The agent's environment (`dev` or `prod`). Defaults to `prod` if not specified.
        *   Example: `dev`
    *   `agent_id` (string): (Fallback) The full resource name of the agent. This is only used if `deployment_id` is not provided. Using `deployment_id` is preferred.
        *   Example: `projects/your-gcp-project-id/locations/us-east1/apps/app-id`

**How to find your Deployment ID**

You can obtain the `deployment_id` from the CES console after creating a channel for your agent deployment.

1.  In the CES console, navigate to your agent.
2.  Go to the **Deployments** section.
3.  Click **New Channel > Connect platform**. This action creates a new deployment specifically for this channel.
4.  Enter a name, choose **Twilio** as the platform, and save the channel.
5.  After saving, the console will display the full **Deployment ID** for the newly created deployment. Copy this value to use in your Firestore or JSON configuration. It will look similar to `projects/your-project/locations/your-location/apps/your-app-id/deployments/your-deployment-id`.

**Note:** It is strongly recommended to use `deployment_id`. If `deployment_id` is present, any `agent_id` field in the same configuration will be ignored.

**Adding a Mapping via the Google Cloud Console:**
1.  Go to the Firestore console.
2.  Click **+ Start collection**.
3.  Enter your `NUMBERS_COLLECTION_ID` and click **Next**.
4.  For the **Document ID**, enter the Twilio phone number in E.164 format (e.g., `+18005551212`).
5.  Add the required field for the agent mapping:
    *   **Field name:** `deployment_id`, **Type:** `string`, **Value:** (Your agent deployment's full resource name)
6.  Optionally, add an `environment` field if you need to target a non-production agent environment.
6.  Click **Save**. You can now add more documents for other phone numbers.

#### Method 2: Local JSON File (for Development/Testing)

For simpler setups or local testing, you can use a JSON file to define the mappings.

**Configuration:**
1.  Create a JSON file (e.g., `number_mappings.json`) in the root of the repository. You can copy `number_mappings.json.example`.
2.  Set the `NUMBERS_CONFIG_FILE` environment variable in `script/values.sh` to the path of this file (e.g., `number_mappings.json`).

**Data Structure:**
The JSON file should be an object where keys are the Twilio phone numbers in E.164 format and values are objects containing the `agent_id` and an optional `environment` field.
The preferred method is to provide a `deployment_id`, which will always take precedence over `agent_id`.

**Example `number_mappings.json`:**
```json
{
  "+18005551212": {
    "deployment_id": "projects/your-gcp-project-id/locations/us-east1/apps/app-id/deployments/deployment-id",
    "environment": "dev"
  },
  "+18005551213": {
    "deployment_id": "projects/your-gcp-project-id/locations/us-east1/apps/another-app-id/deployments/another-deployment-id"
    // The 'environment' field is optional and defaults to "prod" if omitted.
  }
}
```
**Note:** The path to the JSON file is relative to the root of the repository. When deploying to Cloud Run, ensure this file is included in the container image. It should be included by default unless you have a custom `.gcloudignore` file.


### 4. Secrets and Authentication Setup

The adapter requires two secrets to be stored in Google Cloud Secret Manager:

1.  **Twilio Auth Token**: Used to validate that incoming webhook requests are genuinely from Twilio.
2.  **Agent Auth Token**: An OAuth2 token used to authenticate with the backend conversational AI agent. This token is stored in Secret Manager.

#### Twilio Auth Token Secret

1.  Create a secret to hold your Twilio Auth Token. The name should match the one in `script/values.sh` (e.g., `ces-twilio-auth-token`).
    ```bash
    gcloud secrets create ces-twilio-auth-token --project=$(gcloud config get-value project)
    ```

2.  Add your Twilio Auth Token as a secret version.
    ```bash
    echo -n "YOUR_TWILIO_AUTH_TOKEN" | gcloud secrets versions add ces-twilio-auth-token --data-from-file=- --project=$(gcloud config get-value project)
    ```
    (Replace `YOUR_TWILIO_AUTH_TOKEN` with your actual token).

The `deploy.sh` script uses the `--set-secrets` flag to securely mount this secret's value into the Cloud Run service as an environment variable. For this to work, the service account needs permission to access the secret, which you will grant in the next section.

#### Authentication to the Conversational AI Agent

The adapter supports two methods for authenticating to the backend conversational AI agent.

##### Method 1: Application Default Credentials (Default)

By default, the application uses Application Default Credentials (ADC). This is the simplest method and works in two main scenarios:
*   **On Cloud Run:** The application uses the identity of the attached service account. For this to work, you must grant the service account an IAM role that allows it to access the agent (e.g., `roles/ces.client`).
*   **For Local Development:** The application uses the credentials you generate by running `gcloud auth application-default login`.

To use this method, simply leave the `AUTH_TOKEN_SECRET_PATH` environment variable unset in `script/values.sh`.

##### Method 2: Token from Secret Manager (Override)

This method provides an override for situations where the runtime identity (like the Cloud Run service account) cannot be used to authenticate with the target API.

To enable this method, you set the `AUTH_TOKEN_SECRET_PATH` environment variable. The application will then fetch a pre-existing OAuth2 token from the specified path in Secret Manager.

**Important:** If you use this method, you are responsible for ensuring the token in Secret Manager is valid and refreshed periodically. The token typically has a lifetime of 1 hour.

**Steps to configure the token override:**

1.  Create the secret in Secret Manager. The name should match `AUTH_TOKEN_SECRET_PATH` in `script/values.sh`.
    ```bash
    gcloud secrets create ces-twilio-adapter-token --project=$(gcloud config get-value project)
    ```

2.  Generate an access token from a principal that has access to the agent (e.g., your user account) and store it in the secret. The secret **must** be a JSON object with a key named `access_token`.
    ```bash
    gcloud auth application-default login # Ensure you are logged in as the correct principal
    TOKEN=$(gcloud auth print-access-token)
    echo -n "{\"access_token\": \"$TOKEN\"}" | gcloud secrets versions add ces-twilio-adapter-token --data-from-file=- --project=$(gcloud config get-value project)
    ```
    The content of the secret version will be a JSON string, for example:
    ```json
    {"access_token": "ya29.c.b0..."}
    ```

**Important:** Because the application code fetches this secret directly using the Secret Manager API, you **must manually grant** the Cloud Run service account the `Secret Manager Secret Accessor` role for this specific secret. This is handled in the next step.

### 5. Grant IAM Permissions

Before the first deployment, you must manually grant the service account the necessary IAM roles to access project resources. The `deploy.sh` script does **not** handle these permissions, so granting them explicitly ensures the service has the required access from the start.

1.  **Grant Firestore Access:** Allows the service to read the phone number-to-agent mappings.
    ```bash
    gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
        --member="serviceAccount:$(bash -c 'source script/values.sh && echo $SERVICE_ACCOUNT')" \
        --role="roles/datastore.user"
    ```

2.  **Grant Agent Token Secret Access:** Allows the service to read the agent's OAuth2 token from Secret Manager at runtime.
    ```bash
    gcloud secrets add-iam-policy-binding ces-twilio-adapter-token \
        --member="serviceAccount:$(bash -c 'source script/values.sh && echo $SERVICE_ACCOUNT')" \
        --role="roles/secretmanager.secretAccessor" \
        --project=$(gcloud config get-value project)
    ```

3.  **Grant Twilio Token Secret Access:** Allows the Cloud Run service to mount the Twilio Auth Token as an environment variable.
    ```bash
    gcloud secrets add-iam-policy-binding ces-twilio-auth-token \
        --member="serviceAccount:$(bash -c 'source script/values.sh && echo $SERVICE_ACCOUNT')" \
        --role="roles/secretmanager.secretAccessor" \
        --project=$(gcloud config get-value project)
    ```

### 6. Deploy the Service

Run the deployment script:

```bash
bash script/deploy.sh
```

This command will build the container image from the source, push it to Artifact Registry, and deploy it to Cloud Run.

### 7. Final Configuration

1.  **Update Service Hostname:** After the first deployment, Cloud Run will assign a public URL to your service. Copy this URL.
    *   Open `script/values.sh` and update the `PUBLIC_SERVER_HOSTNAME` variable with your service's hostname (e.g., `ces-twilio-adapter-xxxx.a.run.app`).
    *   Redeploy the service by running `bash script/deploy.sh` again so the application has its own public address.
    *   Open `script/values.sh` and update the `PUBLIC_SERVER_HOSTNAME` variable with your service's hostname (e.g., `ces-twilio-adapter-xxxx.a.run.app`).
    *   Redeploy the service by running `bash script/deploy.sh` again so the application has its own public address.

2.  **Configure Twilio Webhook:**
    *   Go to your Twilio console and navigate to the settings for your phone number.
    *   Under "Voice & Fax", configure the "A CALL COMES IN" webhook to point to your service's endpoint. The URL will be: `https://<YOUR_PUBLIC_SERVER_HOSTNAME>/twilio/voice`
    *   Set the HTTP method to `HTTP POST`.

Your adapter is now live.

## 8. Local Development & Testing

For faster development cycles, you can run the server locally and expose it to the internet using `ngrok`. This is easy to do from a local machine or a Cloud Shell instance.

### 1. Prerequisites

*   Python 3.12 and `pip`.
*   Install dependencies: `pip install -r requirements.txt`.
*   `ngrok` installed and configured.
*   Authenticated with Google Cloud for local development:
    ```bash
    gcloud auth application-default login
    ```

### 2. Run Locally

**Important Note for Googlers:** Do not install and run `ngrok` on your workstation within the corporate network. Instead, use Cloud Shell within your Google Cloud project. The `script/setup-cloud-shell.sh` script is provided for easy setup within Cloud Shell.

1.  **Start `ngrok`:** In a terminal, start `ngrok` to create a public tunnel to your local port 8080.
    ```bash
    ngrok http 8080
    ```
    `ngrok` will display a forwarding URL (e.g., `https://random-string.ngrok.io`). Copy the **hostname** part (e.g., `random-string.ngrok.io`).

2.  **Create `.env` file:** In the root of the repository, create a file named `.env`. This file will hold your local configuration and is loaded automatically by the application at startup.

3.  **Configure `.env`:** Populate the `.env` file with the necessary values. You can use `script/values.sh` as a reference for most of them.
    *   Get a fresh access token by running `gcloud auth print-access-token`.
    *   Use your `ngrok` hostname for `PUBLIC_SERVER_HOSTNAME`.

    Your `.env` file should look like this:

    ```dotenv
    # From ngrok
    PUBLIC_SERVER_HOSTNAME="<your-ngrok-hostname>"

    # From 'gcloud auth print-access-token'
    AUTH_TOKEN="<your-gcloud-access-token>"
    ```

4.  **Run the application:** In a separate terminal, run the application directly using Python.
    ```bash
    python main.py
    ```
    The server will start on `http://localhost:8080`.

5.  **Configure Twilio Webhook:** In your Twilio console, configure your phone number's voice webhook to point to your `ngrok` URL: `https://<YOUR_NGROK_HOSTNAME>/incoming-call`.

You can now call your Twilio number, and the request will be forwarded to your local machine for debugging.

## 9. Important Notes

*   **Token Expiration (Override Method):** If you are using the Secret Manager override for authentication, remember that the access token expires (typically after 1 hour). For a production deployment, you will need to periodically run the `gcloud secrets versions add ...` command to update the secret with a fresh token. A more robust, long-term solution would involve a mechanism to programmatically refresh and update the token.
*   **Service Hostname:** The application needs to know its own public hostname to correctly configure Twilio TwiML responses. This creates a small circular dependency during the first deployment, which is why you must deploy, update the hostname in `script/values.sh`, and then redeploy.
*   **Permissions:** The service account used by Cloud Run (`ces-twilio-adapter@${PROJECT_ID}.iam.gserviceaccount.com` by default) requires specific IAM roles to function correctly:
    *   **Firestore Access:** It needs the `Cloud Datastore User` role (`roles/datastore.user`) to read the phone number to agent mappings.
    *   **Agent Token Secret Access:** It needs the `Secret Manager Secret Accessor` role (`roles/secretmanager.secretAccessor`) for the Agent Auth Token secret.
    *   **Twilio Token Secret Access:** It needs the `Secret Manager Secret Accessor` role (`roles/secretmanager.secretAccessor`) for the Twilio Auth Token secret.
    
