from __future__ import annotations

from typing import List, Any, Dict, Tuple, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain.tools import BaseTool

from src.utils.config_access import get_archi_config
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Aliases that map to the canonical transport names accepted by MultiServerMCPClient.
# langchain-mcp-adapters accepts "stdio", "sse", "streamable_http", and "http".
# "streamable_http" and "http" both connect to a Streamable HTTP endpoint — the
# DeepWiki MCP server (https://mcp.deepwiki.com/mcp) uses this transport.
_TRANSPORT_ALIASES: Dict[str, str] = {
    "streamable-http": "streamable_http",   # hyphen variant (common typo)
    "streamablehttp": "streamable_http",    # no-separator variant
    "http": "streamable_http",              # short alias supported by adapters library
    "https": "streamable_http",             # convenience alias for HTTPS endpoints
}


def _normalize_server_config(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a single MCP server config entry.

    - Canonicalises the ``transport`` value so that common aliases
      (``streamablehttp``, ``streamable-http``, ``http``, ``https``) all map to
      the ``streamable_http`` string expected by langchain-mcp-adapters.
    - Emits a warning when an alias is used so operators can update their YAML.
    """
    cfg = dict(cfg)
    transport = cfg.get("transport", "")
    canonical = _TRANSPORT_ALIASES.get(transport.lower())
    if canonical:
        logger.warning(
            "MCP server '%s': transport '%s' is an alias — using '%s'. "
            "Update your config to use '%s' to suppress this warning.",
            name, transport, canonical, canonical,
        )
        cfg["transport"] = canonical
    return cfg


def _normalize_servers(mcp_servers: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *mcp_servers* with every entry transport-normalised."""
    return {name: _normalize_server_config(name, cfg) for name, cfg in mcp_servers.items()}


async def initialize_mcp_client() -> Tuple[Optional[MultiServerMCPClient], List[BaseTool]]:
    """
    Initialise the MCP client and fetch tool definitions from all configured servers.

    MCP servers are declared under ``archi.mcp_servers`` in the deployment config.
    Both legacy SSE and modern Streamable HTTP transports are supported, including
    the public DeepWiki MCP server (``https://mcp.deepwiki.com/mcp``).

    Returns:
        client: The active ``MultiServerMCPClient`` instance.  The caller is
            responsible for keeping it alive for the duration of the service.
        tools: The combined list of LangChain-compatible tools from all servers
            that connected successfully.
    """
    archi_cfg = get_archi_config()
    raw_servers: Dict[str, Any] = archi_cfg.get("mcp_servers") or {}

    if not raw_servers:
        logger.info("No MCP servers configured (archi.mcp_servers is empty).")
        return None, []

    mcp_servers = _normalize_servers(raw_servers)
    client = MultiServerMCPClient(mcp_servers)

    all_tools: List[BaseTool] = []
    failed_servers: Dict[str, str] = {}

    for name in mcp_servers:
        try:
            tools = await client.get_tools(server_name=name)
            all_tools.extend(tools)
            logger.info(
                "MCP server '%s' connected — %d tool(s) loaded: %s",
                name,
                len(tools),
                [t.name for t in tools],
            )
        except Exception as exc:
            failed_servers[name] = str(exc)
            logger.warning("MCP server '%s' failed to connect: %s", name, exc)

    active = [n for n in mcp_servers if n not in failed_servers]
    if active:
        logger.info("Active MCP servers: %s (%d tool(s) total)", active, len(all_tools))
    if failed_servers:
        logger.error(
            "The following MCP servers could not be reached and will be unavailable: %s",
            list(failed_servers.keys()),
        )

    return client, all_tools
