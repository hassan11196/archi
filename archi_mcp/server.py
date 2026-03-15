"""
archi MCP Server

Exposes archi's RAG capabilities as MCP tools so AI assistants in
VS Code (GitHub Copilot), Cursor, and other MCP-compatible clients
can query your archi knowledge base directly.

Usage
-----
Run as a standalone process (stdio transport, which VS Code / Cursor use):

    python -m archi_mcp

Or via the installed CLI entry-point:

    archi-mcp

Configuration via environment variables:

    ARCHI_URL        URL of a running archi deployment (default: http://localhost:5000)
    ARCHI_API_KEY    Optional bearer token if archi authentication is enabled
    ARCHI_TIMEOUT    HTTP timeout in seconds (default: 120)
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Dependency check – give a clear error if mcp is not installed.
# ---------------------------------------------------------------------------
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types
except ImportError as _err:  # noqa: F841
    print(
        "ERROR: The 'mcp' package is required to run the archi MCP server.\n"
        "Install it with:  pip install mcp\n"
        "Or:               pip install 'archi[mcp]'",
        file=sys.stderr,
    )
    sys.exit(1)

from archi_mcp.client import ArchiClient, ArchiClientError  # noqa: E402  (local import)

# ---------------------------------------------------------------------------
# Server configuration
# ---------------------------------------------------------------------------
_DEFAULT_URL = "http://localhost:5000"
_DEFAULT_TIMEOUT = 120

ARCHI_URL: str = os.environ.get("ARCHI_URL", _DEFAULT_URL)
ARCHI_API_KEY: Optional[str] = os.environ.get("ARCHI_API_KEY")
ARCHI_TIMEOUT: int = int(os.environ.get("ARCHI_TIMEOUT", str(_DEFAULT_TIMEOUT)))

# ---------------------------------------------------------------------------
# MCP Server setup
# ---------------------------------------------------------------------------
server = Server("archi")
_client: Optional[ArchiClient] = None


def _get_client() -> ArchiClient:
    global _client
    if _client is None:
        _client = ArchiClient(
            base_url=ARCHI_URL,
            timeout=ARCHI_TIMEOUT,
            api_key=ARCHI_API_KEY,
        )
    return _client


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="archi_query",
            description=textwrap.dedent("""\
                Ask a question to the archi RAG (Retrieval-Augmented Generation) system.

                archi retrieves relevant documents from its knowledge base and uses an LLM
                to compose a grounded answer.  Use this tool when you need information that
                is stored in the connected archi deployment (documentation, tickets, wiki
                pages, research papers, course material, etc.).

                You may continue a conversation by passing the conversation_id returned by
                a previous call.
            """),
            inputSchema={
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
                        "description": (
                            "Optional. Override the LLM provider for this query "
                            "(e.g. 'openai', 'anthropic', 'gemini', 'openrouter', 'local')."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional. Override the specific model for this query "
                            "(e.g. 'gpt-4o', 'claude-3-5-sonnet', 'gemini-1.5-pro')."
                        ),
                    },
                },
                "required": ["question"],
            },
        ),
        types.Tool(
            name="archi_list_documents",
            description=textwrap.dedent("""\
                List the documents that have been indexed into archi's knowledge base.

                Returns a paginated list of document metadata (filename, source type,
                URL, last updated, etc.).  Use this tool to discover what information
                archi has access to before querying it, or to find a specific document's
                hash for use with archi_get_document_content.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Optional keyword to filter documents by name or URL.",
                    },
                    "source_type": {
                        "type": "string",
                        "description": (
                            "Optional. Filter by source type: 'web', 'git', 'local', "
                            "'jira', 'redmine', etc."
                        ),
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (1-based, default 1).",
                        "default": 1,
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Number of results per page (default 50, max 200).",
                        "default": 50,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="archi_get_document_content",
            description=textwrap.dedent("""\
                Retrieve the full text content of a document that is indexed in archi.

                Use archi_list_documents first to obtain a document's hash, then pass it
                here to read the raw source text that archi ingested.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_hash": {
                        "type": "string",
                        "description": "The document hash returned by archi_list_documents.",
                    },
                },
                "required": ["document_hash"],
            },
        ),
        types.Tool(
            name="archi_get_deployment_info",
            description=textwrap.dedent("""\
                Return configuration and status information about the connected archi
                deployment.

                Includes the active LLM pipeline and model, retrieval settings (number of
                documents retrieved, hybrid search weights), embedding model, and the list
                of available pipelines and LLM providers.  Useful for understanding how
                archi is configured before issuing queries.
            """),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="archi_list_agents",
            description=textwrap.dedent("""\
                Return the agent configurations (agent specs) available in the connected
                archi deployment.

                Each agent spec defines a name, a system prompt, and the set of tools
                (retriever, MCP servers, local file search, etc.) that agent can use.
                Use this to understand which specialised agents are available before
                selecting one for archi_query.
            """),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="archi_health",
            description=textwrap.dedent("""\
                Check whether the archi deployment is reachable and healthy.

                Returns the service status and database connectivity.  Call this first
                if other archi tools are failing, to confirm that the deployment is up.
            """),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _ok(data: Any) -> List[types.TextContent]:
    """Wrap any Python value as a JSON TextContent block."""
    if isinstance(data, str):
        return [types.TextContent(type="text", text=data)]
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(message: str) -> List[types.TextContent]:
    return [types.TextContent(type="text", text=f"ERROR: {message}")]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    client = _get_client()

    # ------------------------------------------------------------------
    # archi_query
    # ------------------------------------------------------------------
    if name == "archi_query":
        question = arguments.get("question", "").strip()
        if not question:
            return _err("'question' is required and must not be empty.")
        try:
            result = client.query(
                message=question,
                conversation_id=arguments.get("conversation_id"),
                provider=arguments.get("provider"),
                model=arguments.get("model"),
            )
        except ArchiClientError as exc:
            return _err(str(exc))

        answer = result.get("response", "")
        conv_id = result.get("conversation_id")

        parts = [answer]
        if conv_id is not None:
            parts.append(
                f"\n\n---\n_conversation_id: {conv_id} "
                "(pass this to archi_query to continue the conversation)_"
            )
        return [types.TextContent(type="text", text="".join(parts))]

    # ------------------------------------------------------------------
    # archi_list_documents
    # ------------------------------------------------------------------
    elif name == "archi_list_documents":
        try:
            result = client.list_documents(
                page=arguments.get("page", 1),
                per_page=min(arguments.get("per_page", 50), 200),
                search=arguments.get("search"),
                source_type=arguments.get("source_type"),
            )
        except ArchiClientError as exc:
            return _err(str(exc))

        docs = result.get("documents", result.get("items", []))
        total = result.get("total", len(docs))
        page = result.get("page", 1)
        per_page = result.get("per_page", len(docs))

        lines = [f"Found {total} document(s) (page {page}, {per_page} per page):\n"]
        for doc in docs:
            name_field = (
                doc.get("filename")
                or doc.get("name")
                or doc.get("url")
                or doc.get("hash", "unknown")
            )
            source = doc.get("source_type", doc.get("type", ""))
            doc_hash = doc.get("hash", doc.get("id", ""))
            lines.append(f"  • {name_field}  [{source}]  hash={doc_hash}")

        lines.append(
            "\nUse archi_get_document_content(document_hash=<hash>) to read a document's text."
        )
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ------------------------------------------------------------------
    # archi_get_document_content
    # ------------------------------------------------------------------
    elif name == "archi_get_document_content":
        doc_hash = arguments.get("document_hash", "").strip()
        if not doc_hash:
            return _err("'document_hash' is required.")
        try:
            result = client.get_document_content(doc_hash)
        except ArchiClientError as exc:
            return _err(str(exc))
        content = result.get("content", result.get("text", json.dumps(result, indent=2)))
        return [types.TextContent(type="text", text=content)]

    # ------------------------------------------------------------------
    # archi_get_deployment_info
    # ------------------------------------------------------------------
    elif name == "archi_get_deployment_info":
        try:
            static = client.get_static_config()
            dynamic = client.get_dynamic_config()
        except ArchiClientError as exc:
            return _err(str(exc))

        lines = [
            f"# archi Deployment: {static.get('deployment_name', 'unknown')}",
            "",
            "## Active configuration",
            f"  Pipeline:              {dynamic.get('active_pipeline', 'n/a')}",
            f"  Model:                 {dynamic.get('active_model', 'n/a')}",
            f"  Temperature:           {dynamic.get('temperature', 'n/a')}",
            f"  Max tokens:            {dynamic.get('max_tokens', 'n/a')}",
            f"  Docs retrieved (k):    {dynamic.get('num_documents_to_retrieve', 'n/a')}",
            f"  Hybrid search:         {dynamic.get('use_hybrid_search', 'n/a')}",
            f"    BM25 weight:         {dynamic.get('bm25_weight', 'n/a')}",
            f"    Semantic weight:     {dynamic.get('semantic_weight', 'n/a')}",
            "",
            "## Embedding",
            f"  Model:                 {static.get('embedding_model', 'n/a')}",
            f"  Dimensions:            {static.get('embedding_dimensions', 'n/a')}",
            f"  Chunk size:            {static.get('chunk_size', 'n/a')}",
            f"  Chunk overlap:         {static.get('chunk_overlap', 'n/a')}",
            f"  Distance metric:       {static.get('distance_metric', 'n/a')}",
            "",
            "## Available pipelines",
        ]
        for p in static.get("available_pipelines", []):
            lines.append(f"  • {p}")
        lines.append("")
        lines.append("## Available models / providers")
        for provider in static.get("available_providers", []):
            lines.append(f"  • {provider}")

        return [types.TextContent(type="text", text="\n".join(lines))]

    # ------------------------------------------------------------------
    # archi_list_agents
    # ------------------------------------------------------------------
    elif name == "archi_list_agents":
        try:
            result = client.list_agents()
        except ArchiClientError as exc:
            return _err(str(exc))

        agents = result.get("agents", result if isinstance(result, list) else [])
        if not agents:
            return [types.TextContent(type="text", text="No agent specs found in this deployment.")]

        lines = [f"Available agent specs ({len(agents)}):\n"]
        for agent in agents:
            agent_name = agent.get("name", agent.get("filename", "unknown"))
            tools = agent.get("tools", [])
            tools_str = ", ".join(tools) if tools else "none"
            lines.append(f"  • {agent_name}")
            lines.append(f"    Tools: {tools_str}")

        return [types.TextContent(type="text", text="\n".join(lines))]

    # ------------------------------------------------------------------
    # archi_health
    # ------------------------------------------------------------------
    elif name == "archi_health":
        try:
            result = client.health()
        except ArchiClientError as exc:
            return _err(str(exc))
        status = result.get("status", "unknown")
        db = result.get("database", "unknown")
        ts = result.get("timestamp", "")
        msg = f"archi status: {status}\nDatabase: {db}\nTimestamp: {ts}"
        return [types.TextContent(type="text", text=msg)]

    else:
        return _err(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """CLI entry point: archi-mcp"""
    import asyncio

    print(
        f"Starting archi MCP server (archi URL: {ARCHI_URL})",
        file=sys.stderr,
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
