"""Minimal FastAPI proxy for a deployed A2A agent (Agent Runtime, agents-cli 1.1.0+).

The browser talks ONLY to this proxy (same origin, no CORS, no GCP creds in the
browser). The proxy authenticates with Application Default Credentials and
forwards chat to the deployed agent over the A2A protocol, returning replies as
structured parts the chat UI knows how to show:

  * {"kind": "text", "text": ...}  -> a normal chat bubble
  * {"kind": "a2ui", "data": ...}  -> one A2UI message (beginRendering /
    surfaceUpdate); static/index.html renders these as a card.
"""

import json
import os
import uuid

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Try loading from env var first, fallback to deployment_metadata.json
RESOURCE = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
if not RESOURCE:
    meta_path = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r") as f:
                data = json.load(f)
                RESOURCE = data.get("remote_agent_runtime_id")
        except Exception:
            pass

if not RESOURCE:
    raise RuntimeError("AGENT_ENGINE_RESOURCE_NAME is not set and deployment_metadata.json was not found.")

# The agent's app directory (matches agent_directory in agents-cli-manifest.yaml).
AGENT_DIRECTORY = os.environ.get("AGENT_DIRECTORY", "app")
# Location is embedded in the resource name: projects/<p>/locations/<loc>/reasoningEngines/<id>.
LOCATION = RESOURCE.split("/locations/")[1].split("/")[0]

# A2A endpoint for an Agent Runtime deployment, via the Agent Engine HTTP
# passthrough. The card lives at the well-known path under this base.
A2A_BASE = (
    f"https://{LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/"
    f"{RESOURCE}/api/a2a/{AGENT_DIRECTORY}"
)

# The agent tags its A2UI data parts with this mime type.
_A2UI_MIME = "application/json+a2ui"

# One set of ADC credentials, refreshed per request (access tokens expire ~1h).
_creds, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def _auth_headers() -> dict[str, str]:
    _creds.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {_creds.token}",
        "Content-Type": "application/json",
    }


app = FastAPI()


@app.exception_handler(Exception)
async def _json_errors(request: Request, exc: Exception):
    # Always return JSON so the browser never receives a plain-text 500 page
    return JSONResponse(
        status_code=200,
        content={
            "parts": [{"kind": "text", "text": f"Error: {type(exc).__name__}: {exc}"}]
        },
    )


# Reuse ONE A2A context per user so the agent remembers the conversation.
_contexts: dict[str, str] = {}
# Cache the agent card after the first fetch.
_card: AgentCard | None = None


async def _get_card(client: httpx.AsyncClient) -> AgentCard:
    global _card
    if _card is None:
        resolver = A2ACardResolver(httpx_client=client, base_url=A2A_BASE)
        _card = await resolver.get_agent_card()
    return _card


def _extract_parts(parts: list) -> list[dict]:
    """Turn A2A response parts into structured parts for the chat UI."""
    out: list[dict] = []
    for p in parts:
        root = getattr(p, "root", p)
        text = getattr(root, "text", None)
        if text:
            out.append({"kind": "text", "text": text})
            continue

        data = getattr(root, "data", None)
        if data is not None:
            meta = getattr(root, "metadata", None)
            mime = None
            if isinstance(meta, dict):
                mime = meta.get("mimeType")
            elif hasattr(meta, "mimeType"):
                mime = getattr(meta, "mimeType")
            elif hasattr(meta, "get"):
                mime = meta.get("mimeType")
            if mime == _A2UI_MIME:
                out.append({"kind": "a2ui", "data": data})
            continue

        url = getattr(root, "url", None)
        if url:
            out.append({"kind": "text", "text": url})
    return out


@app.post("/chat")
async def chat(req: Request):
    body = await req.json()
    message = body.get("message", "")
    user_id = body.get("user_id") or "web-user"
    parts: list[dict] = []

    async with httpx.AsyncClient(headers=_auth_headers(), timeout=120) as client:
        card = await _get_card(client)
        factory = ClientFactory(ClientConfig(httpx_client=client))
        a2a_client = factory.create(card)

        role_user = getattr(Role, "ROLE_USER", None)
        if role_user is None:
            role_user = getattr(Role, "user", 1)

        msg = Message(
            message_id=str(uuid.uuid4()),
            role=role_user,
            parts=[Part(text=message)],
            context_id=_contexts.get(user_id),
        )
        send_req = SendMessageRequest(message=msg)

        async for resp in a2a_client.send_message(send_req):
            if resp.HasField("status_update"):
                su = resp.status_update
                if su.context_id:
                    _contexts[user_id] = su.context_id
                if su.status and su.status.message:
                    parts.extend(_extract_parts(su.status.message.parts))
            elif resp.HasField("artifact_update"):
                au = resp.artifact_update
                if au.context_id:
                    _contexts[user_id] = au.context_id
                if au.artifact:
                    parts.extend(_extract_parts(au.artifact.parts))

    if not parts:
        parts = [{"kind": "text", "text": "(The agent didn't return a reply.)"}]
    return JSONResponse({"parts": parts})


# Serve the chat UI (keep this mount last so /chat wins).
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
