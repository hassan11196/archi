"""
MCP SSE endpoint – exposes archi's RAG capabilities as MCP tools over HTTP+SSE.

AI assistants in VS Code (GitHub Copilot), Cursor, Claude Desktop, Claude Code,
and any other MCP-compatible client can connect with just a URL:

    http://<host>:<port>/mcp/sse

No local installation required on the client side.

VS Code  (.vscode/mcp.json):
    {
      "servers": {
        "archi": { "type": "sse", "url": "http://localhost:7861/mcp/sse" }
      }
    }

Cursor  (~/.cursor/mcp.json):
    {
      "mcpServers": {
        "archi": { "url": "http://localhost:7861/mcp/sse" }
      }
    }

Claude Desktop  (~/Library/Application Support/Claude/claude_desktop_config.json):
    {
      "mcpServers": {
                "archi": {
                    "command": "npx",
                    "args": [
                        "mcp-remote",
                        "http://localhost:7861/mcp/sse",
                        "--header",
                        "Authorization: Bearer <TOKEN>"
                    ]
                }
      }
    }

Claude Code  (run once in terminal):
    claude mcp add --transport sse archi http://localhost:7861/mcp/sse

    Or add to .mcp.json in your project root:
    {
      "mcpServers": {
        "archi": { "type": "sse", "url": "http://localhost:7861/mcp/sse" }
      }
    }

Protocol
--------
This implements the MCP SSE transport (JSON-RPC 2.0 over Server-Sent Events):

  1.  Client GETs /mcp/sse  →  receives an SSE stream.
  2.  Server immediately sends an "endpoint" event with the POST URL:
          event: endpoint
          data: /mcp/messages?session_id=<uuid>
  3.  Client POSTs JSON-RPC messages to /mcp/messages?session_id=<uuid>.
  4.  Server pushes JSON-RPC responses back via the SSE stream.
  5.  Keepalive comments (": keepalive") are sent every 30 s to prevent
      proxies from closing idle connections.

No external ``mcp`` package is required for the SSE transport – the protocol
is implemented directly in Flask using thread-safe queues.
"""

from __future__ import annotations

import json
import queue
import textwrap
import uuid
from datetime import datetime, timezone
from threading import Lock, Thread
from typing import Any, Dict, Optional

import psycopg2
from flask import Blueprint, Response, jsonify, request, stream_with_context

from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MCP_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "archi", "version": "1.0.0"}
_KEEPALIVE_TIMEOUT = 30  # seconds between keepalive pings

# ---------------------------------------------------------------------------
# Session registry  (session_id → {"queue": Queue, "user_id": str|None})
# ---------------------------------------------------------------------------

_sessions: Dict[str, Dict] = {}
_sessions_lock = Lock()


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def _validate_mcp_token(token: str, pg_config: dict) -> Optional[str]:
    """Validate an MCP bearer token and return the user_id, or None if invalid."""
    if not token or not pg_config:
        return None
    try:
        conn = psycopg2.connect(**pg_config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT user_id FROM mcp_tokens
                       WHERE token = %s
                         AND (expires_at IS NULL OR expires_at > NOW())""",
                    (token,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE mcp_tokens SET last_used_at = NOW() WHERE token = %s",
                        (token,),
                    )
                    conn.commit()
                    return row[0]
        finally:
            conn.close()
    except Exception:
        logger.exception("Error validating MCP token")
    return None


def _extract_bearer_token(req) -> Optional[str]:
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None

# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "archi_query",
        "description": textwrap.dedent("""\
            Ask a question to the archi RAG (Retrieval-Augmented Generation) system.

            archi retrieves relevant documents from its knowledge base and uses an LLM
            to compose a grounded answer.  Use this tool when you need information that
            is stored in the connected archi deployment (documentation, tickets, wiki
            pages, research papers, course material, etc.).

            You may continue a conversation by passing the conversation_id returned by
            a previous call.
        """),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or request to send to archi.",
                },
                "conversation_id": {
                    "type": "integer",
                    "description": (
                        "Optional. Pass the conversation_id from a previous archi_query "
                        "call to continue the same conversation thread."
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": "Optional. Override the LLM provider (e.g. 'openai', 'anthropic').",
                },
                "model": {
                    "type": "string",
                    "description": "Optional. Override the specific model (e.g. 'gpt-4o').",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "archi_list_documents",
        "description": textwrap.dedent("""\
            List the documents that have been indexed into archi's knowledge base.

            Returns a paginated list of document metadata (filename, source type,
            URL, last updated, etc.).  Use this tool to discover what information
            archi has access to before querying it, or to find a specific document's
            hash for use with archi_get_document_content.
        """),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional keyword to filter documents by name or URL.",
                },
                "source_type": {
                    "type": "string",
                    "description": "Optional. Filter by source type: 'web', 'git', 'local', 'jira', etc.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 50, max 200).",
                    "default": 50,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "default": 0,
                },
            },
            "required": [],
        },
    },
    {
        "name": "archi_get_document_content",
        "description": textwrap.dedent("""\
            Retrieve the full text content of a document indexed in archi.

            Use archi_list_documents first to obtain a document's hash, then pass
            it here to read the raw source text that archi ingested.
        """),
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_hash": {
                    "type": "string",
                    "description": "The document hash returned by archi_list_documents.",
                },
            },
            "required": ["document_hash"],
        },
    },
    {
        "name": "archi_get_deployment_info",
        "description": textwrap.dedent("""\
            Return configuration and status information about the connected archi
            deployment.

            Includes the active LLM pipeline and model, retrieval settings (number of
            documents retrieved, hybrid search weights), embedding model, and the list
            of available pipelines.  Useful for understanding how archi is configured
            before issuing queries.
        """),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "archi_list_agents",
        "description": textwrap.dedent("""\
            Return the agent configurations (agent specs) available in this archi
            deployment.

            Each agent spec defines a name, a system prompt, and the set of tools
            (retriever, MCP servers, local file search, etc.) that agent can use.
        """),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "archi_health",
        "description": (
            "Check whether the archi deployment is reachable and its database is healthy."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _ok(result: Any, rpc_id: Any) -> Dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(code: int, message: str, rpc_id: Any) -> Dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _text(text: str) -> Dict:
    """Wrap a string as an MCP tool result."""
    return {"content": [{"type": "text", "text": str(text)}]}


# ---------------------------------------------------------------------------
# Tool handlers (run inside the Flask process – no HTTP round-trip)
# ---------------------------------------------------------------------------


def _call_tool(
    name: str,
    arguments: Dict[str, Any],
    wrapper,
    user_id: Optional[str] = None,
    notify=None,
) -> Dict:
    """Dispatch a tools/call request to the appropriate archi internals.

    ``notify`` is an optional callable(message, progress, total) that sends a
    ``notifications/progress`` event back to the MCP client over the SSE stream.
    It is only provided when the client included ``_meta.progressToken`` in the
    tools/call request.
    """
    try:
        if name == "archi_query":
            return _tool_query(arguments, wrapper, user_id, notify=notify)
        elif name == "archi_list_documents":
            return _tool_list_documents(arguments, wrapper)
        elif name == "archi_get_document_content":
            return _tool_get_document_content(arguments, wrapper)
        elif name == "archi_get_deployment_info":
            return _tool_deployment_info(wrapper)
        elif name == "archi_list_agents":
            return _tool_list_agents(wrapper)
        elif name == "archi_health":
            return _text("status: OK\ndatabase: OK")
        else:
            return _text(f"ERROR: Unknown tool '{name}'.")
    except Exception as exc:
        logger.exception("MCP tool %s raised an exception", name)
        return _text(f"ERROR: {exc}")


def _tool_query(
    arguments: Dict[str, Any],
    wrapper,
    user_id: Optional[str] = None,
    notify=None,
) -> Dict:
    question = (arguments.get("question") or "").strip()
    if not question:
        return _text("ERROR: 'question' is required.")

    conversation_id = arguments.get("conversation_id")
    provider = arguments.get("provider") or None
    model = arguments.get("model") or None
    client_id = f"mcp-sse-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)

    # Always use the streaming pipeline so behaviour is identical to the web
    # app regardless of whether the client supplied a progressToken.
    # When notify is None (no progressToken) we simply skip the notifications.
    #
    # NOTE: chunk events carry *accumulated* text (the full answer so far), not
    # deltas.  We must NOT join them — only the last one (or final.response.answer)
    # is the complete answer.
    answer: str = ""
    new_conv_id = None

    for event in wrapper.chat.stream(
        [["User", question]],
        conversation_id,
        client_id,
        False,           # is_refresh
        now,             # server_received_msg_ts
        now.timestamp(), # client_sent_msg_ts
        120.0,           # client_timeout
        None,            # config_name (use active)
        provider=provider,
        model=model,
        user_id=user_id,
    ):
        etype = event.get("type", "")

        if etype == "error":
            return _text(f"ERROR: {event.get('message', 'unknown error')}")

        elif etype == "thinking_start":
            if notify:
                notify("Thinking…")

        elif etype == "thinking_end":
            if notify:
                thinking = event.get("thinking_content", "")
                if thinking:
                    preview = thinking[:120].replace("\n", " ")
                    notify(f"Thought: {preview}{'…' if len(thinking) > 120 else ''}")

        elif etype == "tool_start":
            if notify:
                tool_name = event.get("tool_name", "tool")
                tool_args = event.get("tool_args") or {}
                if tool_args:
                    args_preview = ", ".join(
                        f"{k}={str(v)[:40]}" for k, v in (tool_args if isinstance(tool_args, dict) else {}).items()
                    )
                    notify(f"Calling {tool_name}({args_preview})")
                else:
                    notify(f"Calling {tool_name}()")

        elif etype == "tool_output":
            if notify:
                notify(f"Got result from {event.get('tool_name', 'tool')}")

        elif etype == "chunk":
            # content is accumulated (full text so far) — overwrite, never append
            content = event.get("content", "")
            if content:
                answer = content
                if notify:
                    notify("Generating answer…")

        elif etype == "final":
            conv_id = event.get("conversation_id")
            if conv_id is not None:
                new_conv_id = conv_id
            # Prefer the clean answer from PipelineOutput over the last chunk
            response = event.get("response")
            final_answer = getattr(response, "answer", None) if response is not None else None
            if final_answer:
                answer = final_answer
    parts = [answer]
    if new_conv_id is not None:
        parts.append(
            f"\n\n---\n_conversation_id: {new_conv_id} "
            "(pass this to archi_query to continue the conversation)_"
        )
    return _text("".join(parts))


def _tool_list_documents(arguments: Dict[str, Any], wrapper) -> Dict:
    limit = min(int(arguments.get("limit", 50)), 200)
    offset = int(arguments.get("offset", 0))
    search: Optional[str] = arguments.get("search") or None
    source_type: Optional[str] = arguments.get("source_type") or None

    result = wrapper.chat.data_viewer.list_documents(
        conversation_id=None,
        source_type=source_type,
        search=search,
        enabled_filter=None,
        limit=limit,
        offset=offset,
    )
    docs = result.get("documents", result.get("items", []))
    total = result.get("total", len(docs))

    lines = [f"Found {total} document(s) (offset={offset}, limit={limit}):\n"]
    for doc in docs:
        display = (
            doc.get("display_name")
            or doc.get("filename")
            or doc.get("url")
            or doc.get("hash", "unknown")
        )
        source = doc.get("source_type", doc.get("type", ""))
        doc_hash = doc.get("hash", doc.get("id", ""))
        lines.append(f"  • {display}  [{source}]  hash={doc_hash}")

    lines.append(
        "\nUse archi_get_document_content(document_hash=<hash>) to read a document."
    )
    return _text("\n".join(lines))


def _tool_get_document_content(arguments: Dict[str, Any], wrapper) -> Dict:
    doc_hash = (arguments.get("document_hash") or "").strip()
    if not doc_hash:
        return _text("ERROR: 'document_hash' is required.")

    result = wrapper.chat.data_viewer.get_document_content(doc_hash)
    if result is None:
        return _text(f"ERROR: Document not found: {doc_hash}")

    content = result.get("content", result.get("text", json.dumps(result, indent=2)))
    return _text(content)


def _tool_deployment_info(wrapper) -> Dict:
    from src.utils.config_access import get_full_config, get_dynamic_config

    config = get_full_config() or {}
    services = config.get("services", {})
    chat_cfg = services.get("chat_app", {})
    dm_cfg = services.get("data_manager", {})

    try:
        dynamic = get_dynamic_config()
    except Exception:
        dynamic = None

    lines = [
        f"# archi Deployment: {config.get('name', 'unknown')}",
        "",
        "## Active configuration",
        f"  Pipeline:              {chat_cfg.get('pipeline', 'n/a')}",
    ]
    if dynamic:
        lines += [
            f"  Model:                 {dynamic.active_model}",
            f"  Temperature:           {dynamic.temperature}",
            f"  Max tokens:            {dynamic.max_tokens}",
            f"  Docs retrieved (k):    {dynamic.num_documents_to_retrieve}",
            f"  Hybrid search:         {dynamic.use_hybrid_search}",
            f"    BM25 weight:         {dynamic.bm25_weight}",
            f"    Semantic weight:     {dynamic.semantic_weight}",
        ]

    embedding_cfg = dm_cfg.get("embedding", {})
    lines += [
        "",
        "## Embedding",
        f"  Model:                 {embedding_cfg.get('model', 'n/a')}",
        f"  Chunk size:            {embedding_cfg.get('chunk_size', 'n/a')}",
        f"  Chunk overlap:         {embedding_cfg.get('chunk_overlap', 'n/a')}",
    ]
    return _text("\n".join(lines))


def _tool_list_agents(wrapper) -> Dict:
    from src.archi.pipelines.agents.agent_spec import (
        AgentSpecError,
        list_agent_files,
        load_agent_spec,
    )

    agents_dir = wrapper._get_agents_dir()
    lines = []
    for path in list_agent_files(agents_dir):
        try:
            spec = load_agent_spec(path)
            tools_str = ", ".join(getattr(spec, "tools", []) or []) or "none"
            lines.append(f"  • {spec.name}  ({path.name})")
            lines.append(f"    Tools: {tools_str}")
        except AgentSpecError:
            pass

    if not lines:
        return _text("No agent specs found in this deployment.")
    return _text("Available agents:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# JSON-RPC dispatcher
# ---------------------------------------------------------------------------


def _dispatch(body: Dict, session_queue: queue.Queue, wrapper, user_id: Optional[str] = None) -> None:
    """Process one incoming JSON-RPC message and enqueue the response if needed."""
    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    # Notifications have no id – no response expected.
    if rpc_id is None:
        return

    if method == "initialize":
        response = _ok(
            {
                "protocolVersion": _MCP_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": _SERVER_INFO,
            },
            rpc_id,
        )
    elif method == "tools/list":
        response = _ok({"tools": _TOOLS}, rpc_id)
    elif method == "tools/call":
        # Extract optional progress token from _meta so we can stream status
        # events back to the client while archi works.
        meta = params.get("_meta") or {}
        progress_token = meta.get("progressToken")

        logger.info(
            "tools/call %s – progressToken=%s",
            params.get("name", "?"),
            progress_token if progress_token is not None else "<none: non-streaming>",
        )

        notify = None
        if progress_token is not None:
            _progress_counter = [0]

            def notify(message: str, progress: int = None, total: int = None) -> None:
                _progress_counter[0] += 1
                p = progress if progress is not None else _progress_counter[0]
                notification: Dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": progress_token,
                        "progress": p,
                        "message": message,
                    },
                }
                if total is not None:
                    notification["params"]["total"] = total
                session_queue.put(notification)

        result = _call_tool(
            params.get("name", ""),
            params.get("arguments") or {},
            wrapper,
            user_id,
            notify=notify,
        )
        response = _ok(result, rpc_id)
    elif method == "ping":
        response = _ok({}, rpc_id)
    else:
        response = _err(-32601, f"Method not found: {method}", rpc_id)

    session_queue.put(response)


# ---------------------------------------------------------------------------
# Blueprint factory
# ---------------------------------------------------------------------------


def register_mcp_sse(app, wrapper, pg_config: dict = None, auth_enabled: bool = False) -> None:
    """Register the MCP SSE endpoints on a Flask app.

    Adds routes:
      GET  /mcp/sse        – SSE stream (MCP clients connect here)
      POST /mcp/messages   – JSON-RPC message receiver

    When ``auth_enabled`` is True, both endpoints require an
    ``Authorization: Bearer <mcp-token>`` header.  Tokens are issued via
    the ``/mcp/auth`` page after the user logs in through SSO.
    """
    mcp = Blueprint("mcp_sse", __name__)

    def _auth_check():
        """Return (user_id, error_response) tuple.  error_response is None on success."""
        if not auth_enabled:
            return None, None
        token = _extract_bearer_token(request)
        if not token:
            resp = jsonify({
                "error": "unauthorized",
                "message": "MCP access requires a bearer token. "
                           "Visit /mcp/auth to generate one after logging in.",
                "login_url": "/mcp/auth",
            })
            resp.status_code = 401
            return None, resp
        user_id = _validate_mcp_token(token, pg_config)
        if not user_id:
            resp = jsonify({
                "error": "invalid_token",
                "message": "The bearer token is invalid or has expired. "
                           "Visit /mcp/auth to generate a new token.",
                "login_url": "/mcp/auth",
            })
            resp.status_code = 401
            return None, resp
        return user_id, None

    @mcp.route("/mcp/sse")
    def sse():
        """Open an SSE stream for one MCP client session."""
        user_id, err = _auth_check()
        if err is not None:
            return err

        session_id = uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        with _sessions_lock:
            _sessions[session_id] = {"queue": q, "user_id": user_id}

        def generate():
            # Advertise the POST endpoint to the client.
            yield f"event: endpoint\ndata: /mcp/messages?session_id={session_id}\n\n"
            try:
                while True:
                    try:
                        msg = q.get(timeout=_KEEPALIVE_TIMEOUT)
                        if msg is None:
                            break
                        yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                with _sessions_lock:
                    _sessions.pop(session_id, None)

        return Response(
            stream_with_context(generate()),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @mcp.route("/mcp/messages", methods=["POST"])
    def messages():
        """Receive a JSON-RPC message from an MCP client."""
        _, err = _auth_check()
        if err is not None:
            return err

        session_id = request.args.get("session_id", "")
        with _sessions_lock:
            session_entry = _sessions.get(session_id)
        if session_entry is None:
            return {"error": "unknown or expired session_id"}, 404

        q = session_entry["queue"]
        user_id = session_entry.get("user_id")

        body = request.get_json(silent=True)
        if not body:
            return {"error": "request body must be valid JSON"}, 400

        # Run dispatch in a background thread so the 202 is returned immediately.
        # This is required for progress notifications to reach the client over SSE
        # while the tool call is still executing.
        Thread(target=_dispatch, args=(body, q, wrapper, user_id), daemon=True).start()
        return "", 202

    app.register_blueprint(mcp)
    if auth_enabled:
        logger.info("Registered MCP SSE endpoint at /mcp/sse (auth required – Bearer token)")
    else:
        logger.info("Registered MCP SSE endpoint at /mcp/sse (no auth)")
