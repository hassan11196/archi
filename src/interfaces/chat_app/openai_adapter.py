"""
OpenAI-compatible API adapter for Archi.

Exposes Archi pipelines/agents as an OpenAI-compatible /v1/chat/completions
endpoint so that OpenWebUI (or any OpenAI-compatible client) can use Archi
as a backend.

Streaming uses Server-Sent Events (SSE) in the standard OpenAI format.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from functools import wraps

from flask import Blueprint, Response, jsonify, request, stream_with_context

from src.utils.logging import get_logger
from src.utils.config_access import get_full_config
from src.utils.env import read_secret

logger = get_logger(__name__)

openai_api = Blueprint("openai_api", __name__, url_prefix="/v1")

# Internal token used by the OpenWebUI container to authenticate to Archi.
# When Archi auth is disabled this token is accepted unconditionally.
_INTERNAL_API_KEY = "archi-internal"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_openai_auth(f):
    """Validate Bearer token for the OpenAI-compatible endpoints.

    Accepts:
    * The internal API key (``archi-internal``) – always valid so that the
      co-deployed OpenWebUI container can reach Archi without extra setup.
    * A valid Archi session token – when Archi auth is enabled, tokens issued
      by the SSO / basic-auth flow are accepted.

    When Archi auth is **disabled** any token (or none) is accepted.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import current_app

        auth_enabled = getattr(current_app, "_archi_auth_enabled", False)
        token = _extract_auth_user(request.headers)

        if token == _INTERNAL_API_KEY:
            # Always allow the internal service token
            return f(*args, **kwargs)

        if not auth_enabled:
            # Auth disabled – allow everything
            return f(*args, **kwargs)

        if not token:
            return jsonify({
                "error": {
                    "message": "Authentication required",
                    "type": "invalid_request_error",
                }
            }), 401

        # When auth is enabled, accept any non-empty Bearer token.
        # Full session validation happens inside the ChatWrapper
        # (the client_id derived from the token gates conversation access).
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_chat_wrapper():
    """Retrieve the ChatWrapper singleton from the app context."""
    from flask import current_app

    wrapper = getattr(current_app, "chat_wrapper", None)
    if wrapper is None:
        raise RuntimeError("ChatWrapper not initialised on the Flask app")
    return wrapper


def _get_archi_models() -> List[Dict[str, Any]]:
    """Build model list from Archi config (agents + providers)."""
    config = get_full_config()
    services_cfg = config.get("services", {}) or {}
    chat_cfg = services_cfg.get("chat_app", {}) or {}
    providers_cfg = chat_cfg.get("providers", {}) or {}

    models: List[Dict[str, Any]] = []
    created = int(time.time())

    # Each provider/model combination is exposed as a selectable "model"
    for provider_name, provider_cfg in providers_cfg.items():
        if not isinstance(provider_cfg, dict):
            continue
        if not provider_cfg.get("enabled", True):
            continue
        for model_id in provider_cfg.get("models", []):
            full_id = f"{provider_name}/{model_id}"
            models.append({
                "id": full_id,
                "object": "model",
                "created": created,
                "owned_by": f"archi-{provider_name}",
                "permission": [],
                "root": full_id,
                "parent": None,
            })

    # Always include a default "archi" model that uses the deployment default
    models.insert(0, {
        "id": "archi",
        "object": "model",
        "created": created,
        "owned_by": "archi",
        "permission": [],
        "root": "archi",
        "parent": None,
    })

    return models


def _extract_auth_user(headers) -> Optional[str]:
    """Extract user identity from the Authorization header.

    OpenWebUI sends ``Authorization: Bearer <token>``.  When Archi auth is
    enabled the token is expected to be a valid session/API token.  When auth
    is disabled we fall back to an anonymous client id.
    """
    auth_header = headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip() or None
    return None


def _messages_to_history(messages: List[Dict[str, str]]) -> List[tuple]:
    """Convert OpenAI-style messages list to Archi's history format.

    Archi history is a list of (sender, content) tuples where sender is
    either the username or 'archi'.
    """
    history: List[tuple] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            # System prompts are prepended as context but not stored as history
            continue
        elif role == "assistant":
            history.append(("archi", content))
        else:
            history.append(("user", content))
    return history


def _build_chunk(model: str, chat_id: str, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> str:
    """Build a single SSE chunk in OpenAI streaming format."""
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _build_tool_annotation(tool_name: str, tool_args: Any, tool_result: str = "") -> str:
    """Format a tool call as a visible annotation within the response text.

    OpenWebUI does not natively render OpenAI tool_calls the same way
    Archi's custom UI does.  We render them as collapsible details blocks
    in Markdown so users still see agent trace information.
    """
    args_str = json.dumps(tool_args, indent=2) if isinstance(tool_args, dict) else str(tool_args)
    annotation = f"\n<details><summary>🔧 {tool_name}</summary>\n\n```json\n{args_str}\n```\n"
    if tool_result:
        truncated = tool_result[:500] + ("..." if len(tool_result) > 500 else "")
        annotation += f"\n**Result:**\n```\n{truncated}\n```\n"
    annotation += "</details>\n"
    return annotation


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------

@openai_api.route("/models", methods=["GET"])
@_require_openai_auth
def list_models():
    """OpenAI-compatible model listing."""
    models = _get_archi_models()
    return jsonify({"object": "list", "data": models})


@openai_api.route("/models/<path:model_id>", methods=["GET"])
@_require_openai_auth
def get_model(model_id: str):
    """Retrieve a single model."""
    for m in _get_archi_models():
        if m["id"] == model_id:
            return jsonify(m)
    return jsonify({"error": {"message": f"Model '{model_id}' not found", "type": "invalid_request_error"}}), 404


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------

@openai_api.route("/chat/completions", methods=["POST"])
@_require_openai_auth
def chat_completions():
    """OpenAI-compatible chat completions endpoint.

    Supports both streaming (``stream: true``) and non-streaming modes.
    """
    body = request.get_json(force=True)
    if not body:
        return jsonify({"error": {"message": "Request body required", "type": "invalid_request_error"}}), 400

    messages = body.get("messages", [])
    if not messages:
        return jsonify({"error": {"message": "'messages' is required", "type": "invalid_request_error"}}), 400

    model_requested = body.get("model", "archi")
    stream_mode = body.get("stream", False)
    temperature = body.get("temperature")
    max_tokens = body.get("max_tokens")

    # Parse provider/model from the model ID
    provider = None
    model = None
    if "/" in model_requested and model_requested != "archi":
        parts = model_requested.split("/", 1)
        provider = parts[0]
        model = parts[1]

    # Auth: extract user identity
    user_token = _extract_auth_user(request.headers)
    client_id = user_token or f"openwebui_{uuid.uuid4().hex[:16]}"

    # Extract the last user message as the query
    user_message = ""
    system_prompt = ""
    for msg in messages:
        if msg.get("role") == "user":
            user_message = msg.get("content", "")
        elif msg.get("role") == "system":
            system_prompt = msg.get("content", "")

    if not user_message:
        return jsonify({"error": {"message": "No user message found", "type": "invalid_request_error"}}), 400

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    try:
        chat_wrapper = _get_chat_wrapper()
    except RuntimeError as e:
        return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500

    if stream_mode:
        return _handle_streaming(chat_wrapper, messages, user_message, model_requested, provider, model, client_id, chat_id)
    else:
        return _handle_non_streaming(chat_wrapper, messages, user_message, model_requested, provider, model, client_id, chat_id)


def _handle_non_streaming(
    chat_wrapper,
    messages: List[Dict],
    user_message: str,
    model_requested: str,
    provider: Optional[str],
    model: Optional[str],
    client_id: str,
    chat_id: str,
) -> Response:
    """Handle a non-streaming chat completion request."""
    now = datetime.now(timezone.utc)

    output, conversation_id, message_ids, timestamps, error_code = chat_wrapper(
        message=[user_message],
        conversation_id=None,
        client_id=client_id,
        is_refresh=False,
        server_received_msg_ts=now,
        client_sent_msg_ts=time.time(),
        client_timeout=600,
        config_name=None,
        user_id=client_id,
    )

    if error_code:
        return jsonify({
            "error": {
                "message": f"Pipeline error (code {error_code})",
                "type": "server_error",
            }
        }), 500

    response = {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_requested,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": output or "",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    return jsonify(response)


def _handle_streaming(
    chat_wrapper,
    messages: List[Dict],
    user_message: str,
    model_requested: str,
    provider: Optional[str],
    model: Optional[str],
    client_id: str,
    chat_id: str,
) -> Response:
    """Handle a streaming chat completion request using SSE."""
    now = datetime.now(timezone.utc)

    def generate() -> Iterator[str]:
        # Send initial role chunk
        yield _build_chunk(model_requested, chat_id, {"role": "assistant"})

        accumulated_text = ""
        try:
            for event in chat_wrapper.stream(
                message=[user_message],
                conversation_id=None,
                client_id=client_id,
                is_refresh=False,
                server_received_msg_ts=now,
                client_sent_msg_ts=time.time(),
                client_timeout=600,
                config_name=None,
                include_agent_steps=True,
                include_tool_steps=True,
                provider=provider,
                model=model,
                user_id=client_id,
            ):
                event_type = event.get("type", "")

                # --- Error events ---
                if event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    yield _build_chunk(model_requested, chat_id, {"content": f"\n\n**Error:** {error_msg}"})
                    break

                # --- Tool trace events (rendered as Markdown details blocks) ---
                if event_type == "tool_start":
                    annotation = _build_tool_annotation(
                        event.get("tool_name", "unknown"),
                        event.get("tool_args", {}),
                    )
                    yield _build_chunk(model_requested, chat_id, {"content": annotation})
                    continue

                if event_type == "tool_output":
                    output_text = event.get("output", "")
                    if output_text:
                        annotation = f"\n<details><summary>Tool Result</summary>\n\n```\n{output_text[:800]}\n```\n</details>\n"
                        yield _build_chunk(model_requested, chat_id, {"content": annotation})
                    continue

                if event_type == "tool_end":
                    continue

                if event_type in ("thinking_start", "thinking_end"):
                    continue

                # --- Legacy step events (from _stream_events_from_output) ---
                if event_type == "step":
                    step_type = event.get("step_type", "")
                    if step_type == "tool_call":
                        annotation = _build_tool_annotation(
                            event.get("tool_name", "unknown"),
                            event.get("tool_args", {}),
                        )
                        yield _build_chunk(model_requested, chat_id, {"content": annotation})
                    elif step_type == "tool_result":
                        content = event.get("content", "")
                        if content:
                            annotation = f"\n<details><summary>Tool Result</summary>\n\n```\n{content[:500]}\n```\n</details>\n"
                            yield _build_chunk(model_requested, chat_id, {"content": annotation})
                    continue

                # --- Text chunk events ---
                if event_type == "chunk":
                    content = event.get("content", "")
                    if event.get("accumulated"):
                        # Content is the full accumulated text so far
                        if content and content != accumulated_text:
                            delta = content[len(accumulated_text):]
                            if delta:
                                yield _build_chunk(model_requested, chat_id, {"content": delta})
                            accumulated_text = content
                    elif content:
                        # Content is an incremental delta
                        yield _build_chunk(model_requested, chat_id, {"content": content})
                        accumulated_text += content
                    continue

                # --- Final event (carries the complete response) ---
                if event_type == "final":
                    final_response = event.get("response", "")
                    if final_response and final_response != accumulated_text:
                        delta = final_response[len(accumulated_text):] if final_response.startswith(accumulated_text) else final_response
                        if delta and delta != accumulated_text:
                            yield _build_chunk(model_requested, chat_id, {"content": delta})
                    continue

                # --- Meta events (stream_started, etc.) – skip ---
                if event_type == "meta":
                    continue

        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            yield _build_chunk(model_requested, chat_id, {"content": f"\n\n**Error:** {e}"})

        # Send final chunk
        yield _build_chunk(model_requested, chat_id, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_openai_api(app):
    """Register the OpenAI-compatible API blueprint with a Flask app.

    Usage:
        from src.interfaces.chat_app.openai_adapter import register_openai_api
        register_openai_api(app)
    """
    app.register_blueprint(openai_api)
    logger.info("Registered OpenAI-compatible API blueprint at /v1")
