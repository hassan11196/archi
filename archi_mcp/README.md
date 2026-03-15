# archi MCP Server

Expose your [archi](https://github.com/archi-physics/archi) knowledge base as
**Model Context Protocol (MCP) tools** so that AI assistants in VS Code, Cursor,
and any other MCP-compatible client can query it directly.

Two transport options are available:

| Transport | How to connect | Requires local install? |
|---|---|---|
| **HTTP+SSE** *(built-in)* | Point client at `http://<host>:<port>/mcp/sse` | **No** |
| **stdio** | Run `archi-mcp` locally | Yes (`pip install "archi[mcp]"`) |

> **Recommended:** use the built-in HTTP+SSE endpoint — no installation needed.

---

## What this provides

| Tool | Description |
|---|---|
| `archi_query` | Ask a question via archi's active RAG pipeline |
| `archi_list_documents` | Browse the indexed knowledge base |
| `archi_get_document_content` | Read the full text of an indexed document |
| `archi_get_deployment_info` | Show active pipeline, model, and retrieval config |
| `archi_list_agents` | List available agent specs |
| `archi_health` | Verify the deployment is reachable |

---

## Option A – Built-in HTTP+SSE endpoint (recommended)

The archi chat service exposes MCP tools directly at `/mcp/sse`.
No separate process to install or start.

### VS Code (.vscode/mcp.json)

```json
{
  "servers": {
    "archi": {
      "type": "sse",
      "url": "http://localhost:7861/mcp/sse"
    }
  }
}
```

### Cursor (~/.cursor/mcp.json)

```json
{
  "mcpServers": {
    "archi": {
      "url": "http://localhost:7861/mcp/sse"
    }
  }
}
```

Replace `localhost:7861` with the public hostname and port of your archi
deployment when connecting remotely.

Reload the window / restart the editor and the archi tools appear automatically.

---

## Option B – stdio server (archi-mcp CLI)

Use this when you cannot reach the archi service directly over HTTP (e.g. the
service is behind a firewall and you tunnel to it separately).

### Server setup

### 1. Install

```bash
pip install "archi[mcp]"
```

Or, in development (from the repo root):

```bash
pip install -e ".[mcp]"
```

### 2. Configure archi

Add an `mcp_server` block to your archi deployment config YAML.  The defaults
work for a local deployment on the standard port:

```yaml
services:
  chat_app:
    port: 7861
    external_port: 7861
    hostname: localhost        # or your public hostname / domain

  mcp_server:
    enabled: true
    # Public URL that MCP clients will connect to.
    # Defaults to http://<chat_app.hostname>:<chat_app.external_port>
    url: "http://localhost:7861"
    # Set this if chat app auth is enabled (services.chat_app.auth.enabled: true).
    api_key: ""
    # HTTP request timeout in seconds.
    timeout: 120
```

Redeploy so the rendered config picks up the new block:

```bash
archi restart --name <deployment-name> --service chatbot
```

The rendered config lands at:

```
~/.archi/archi-<name>/configs/chat-config.yaml
```

### 3. Start the MCP server

Point `archi-mcp` at the rendered config so it reads `services.mcp_server.*`
automatically:

```bash
archi-mcp --config ~/.archi/archi-<name>/configs/chat-config.yaml
```

Without `--config`, settings fall back to environment variables:

| Variable | Default | Description |
|---|---|---|
| `ARCHI_URL` | `http://localhost:7861` | Base URL of the archi chat service |
| `ARCHI_API_KEY` | *(none)* | Bearer token when auth is enabled |
| `ARCHI_TIMEOUT` | `120` | HTTP timeout in seconds |

---

## stdio Client setup

### VS Code (GitHub Copilot)

Create or edit `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "archi": {
      "type": "stdio",
      "command": "archi-mcp",
      "args": ["--config", "${env:HOME}/.archi/archi-mydeployment/configs/chat-config.yaml"]
    }
  }
}
```

To use environment variables instead:

```json
{
  "servers": {
    "archi": {
      "type": "stdio",
      "command": "archi-mcp",
      "env": {
        "ARCHI_URL": "http://localhost:7861",
        "ARCHI_API_KEY": "optional-token"
      }
    }
  }
}
```

Reload the window (`Ctrl+Shift+P` → **Developer: Reload Window**) and the
archi tools appear in GitHub Copilot's tool picker.

### Cursor

Edit `~/.cursor/mcp.json` (create it if it doesn't exist):

```json
{
  "mcpServers": {
    "archi": {
      "command": "archi-mcp",
      "args": ["--config", "/home/you/.archi/archi-mydeployment/configs/chat-config.yaml"]
    }
  }
}
```

Or with environment variables:

```json
{
  "mcpServers": {
    "archi": {
      "command": "archi-mcp",
      "env": {
        "ARCHI_URL": "http://localhost:7861"
      }
    }
  }
}
```

Restart Cursor. The archi tools appear under **MCP Tools** in the Composer panel.

---

---

## Troubleshooting

| Error | Fix |
|---|---|
| SSE URL not reachable | Confirm the archi chat service is running and the URL is correct |
| `mcp package not found` (stdio) | Run `pip install "archi[mcp]"` |
| `Cannot reach archi at http://localhost:7861` | Check `ARCHI_URL` or `services.mcp_server.url`; ensure the chat service is running |
| `401 Unauthorized` | Set `ARCHI_API_KEY` or `services.mcp_server.api_key` to a valid token |
| `WARNING: could not read archi config` | Check the path passed to `--config` |
