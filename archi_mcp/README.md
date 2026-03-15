# archi MCP Server

Expose your [archi](https://github.com/archi-physics/archi) knowledge base as a set of
**Model Context Protocol (MCP) tools** so that AI assistants in
[VS Code](https://code.visualstudio.com/), [Cursor](https://cursor.sh/), and any other
MCP-compatible client can query it directly.

---

## What this provides

| Tool | Description |
|---|---|
| `archi_query` | Ask archi a question. Uses the active RAG pipeline to retrieve relevant documents and compose a grounded answer. Supports multi-turn conversation. |
| `archi_list_documents` | List documents indexed in archi's knowledge base (filterable by keyword or source type). |
| `archi_get_document_content` | Read the full text of a specific indexed document. |
| `archi_get_deployment_info` | Show the active pipeline, model, retrieval settings, and available providers. |
| `archi_list_agents` | List available agent configurations (agent specs). |
| `archi_health` | Check that the archi deployment is reachable and its database is connected. |

---

## Prerequisites

1. A running archi deployment (the chat app service must be reachable).
2. Python 3.9+ with the `mcp` package installed.

```bash
pip install "mcp>=1.0.0"
```

Or install directly from the archi repo:

```bash
pip install -e ".[mcp]"
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ARCHI_URL` | `http://localhost:5000` | Base URL of your archi chat app service. |
| `ARCHI_API_KEY` | _(none)_ | Bearer token, if archi auth is enabled. |
| `ARCHI_TIMEOUT` | `120` | HTTP request timeout in seconds. |

---

## Running the server manually

```bash
ARCHI_URL=http://your-archi-host:5000 python -m archi_mcp
# or
ARCHI_URL=http://your-archi-host:5000 archi-mcp
```

The server uses **stdio transport** (stdin/stdout), which is the standard transport
used by VS Code and Cursor.

---

## VS Code setup

### Option A — `.vscode/mcp.json` (workspace-scoped, recommended)

Create `.vscode/mcp.json` in your project root:

```json
{
  "servers": {
    "archi": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "archi_mcp"],
      "env": {
        "ARCHI_URL": "http://localhost:5000",
        "ARCHI_API_KEY": "",
        "ARCHI_TIMEOUT": "120"
      }
    }
  }
}
```

> **Tip:** Set `ARCHI_URL` to the address of your archi chat app service.
> If archi runs in a container, use `http://localhost:<host-port>` where
> `<host-port>` is the port mapped to the container's chat service.

### Option B — User settings (`settings.json`)

Open your VS Code user `settings.json` and add:

```json
"github.copilot.chat.mcp.servers": {
  "archi": {
    "type": "stdio",
    "command": "python",
    "args": ["-m", "archi_mcp"],
    "env": {
      "ARCHI_URL": "http://localhost:5000"
    }
  }
}
```

### Verifying in VS Code

1. Open the GitHub Copilot Chat panel.
2. Click the **Tools** button (plug icon).
3. You should see the six `archi_*` tools listed and enabled.

---

## Cursor setup

Open **Cursor Settings → MCP** (or edit `~/.cursor/mcp.json`) and add:

```json
{
  "mcpServers": {
    "archi": {
      "command": "python",
      "args": ["-m", "archi_mcp"],
      "env": {
        "ARCHI_URL": "http://localhost:5000",
        "ARCHI_API_KEY": "",
        "ARCHI_TIMEOUT": "120"
      }
    }
  }
}
```

Then restart Cursor. The archi tools appear in the **Composer** tool list.

---

## Other MCP clients

The server uses the standard **stdio** transport, so it works with any MCP-compatible
client that can launch a subprocess. Point the client at:

```
command: python -m archi_mcp
env:     ARCHI_URL=http://<your-archi-host>:<port>
```

---

## Example usage

Once configured, you can ask your AI assistant:

> _"Use archi to find documentation about the GPU cluster submission process."_

> _"Query archi: what are the memory limits for batch jobs?"_

> _"List the documents archi has indexed, then show me the content of the SLURM guide."_

The assistant will invoke the appropriate `archi_*` tool and incorporate the retrieved
information into its response.

---

## Multi-turn conversations

`archi_query` returns a `conversation_id`. Pass it back to continue the thread:

```
First call:  archi_query(question="What is SubMIT?")
             → answer + conversation_id: 42

Follow-up:   archi_query(question="What hardware does it use?", conversation_id=42)
             → answer with context from the previous exchange
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach archi at http://localhost:5000` | Check `ARCHI_URL`, ensure the chat service is running, and that the port is reachable from where the MCP server runs. |
| `archi returned 401` | Set `ARCHI_API_KEY` to a valid token if archi authentication is enabled. |
| Tools not visible in VS Code | Reload the VS Code window after editing `mcp.json`. Confirm that `python -m archi_mcp` exits cleanly without errors. |
| Empty document list | The archi data-manager service may not have finished ingestion yet, or no sources are configured. |
