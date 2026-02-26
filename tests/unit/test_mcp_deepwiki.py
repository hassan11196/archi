"""
Unit tests for DeepWiki MCP integration.

Tests cover:
- Transport alias normalisation (_normalize_server_config, _normalize_servers)
- initialize_mcp_client behaviour with no servers, one server, multiple servers,
  and partial failures
- DeepWiki-specific configuration round-trip

These tests load the mcp module in isolation by mocking heavy runtime
dependencies (langchain, langchain_mcp_adapters, psycopg2) so they run in
the lightweight CI environment without a full application stack.
"""

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level stubs for heavy runtime deps that are unavailable in unit tests
# ---------------------------------------------------------------------------

def _make_stub(name: str) -> types.ModuleType:
    """Return an empty module stub with the given dotted *name*."""
    mod = types.ModuleType(name)
    for part in name.split(".")[1:]:
        parent_name = name[: name.rfind("." + part)]
        parent = sys.modules.get(parent_name)
        if parent:
            setattr(parent, part, mod)
    sys.modules[name] = mod
    return mod


def _install_stubs():
    """Install stubs for all heavy deps before the mcp module is imported."""
    stubs = [
        "langchain_core",
        "langchain_core.prompts",
        "langchain",
        "langchain.tools",
        "langchain_mcp_adapters",
        "langchain_mcp_adapters.client",
        "langchain_mcp_adapters.tools",
        # src.archi.pipelines imports cascade — stub the whole package
        "src.archi",
        "src.archi.pipelines",
        "src.archi.pipelines.classic_pipelines",
        "src.archi.pipelines.classic_pipelines.base",
        "src.archi.pipelines.agents",
        "src.archi.pipelines.agents.tools",
        "src.utils",
        "src.utils.config_access",
        "src.utils.logging",
    ]
    for name in stubs:
        if name not in sys.modules:
            _make_stub(name)

    # Attach the symbols that mcp.py actually imports from these stubs
    sys.modules["langchain_mcp_adapters.client"].MultiServerMCPClient = MagicMock
    sys.modules["langchain_mcp_adapters.tools"].load_mcp_tools = MagicMock()
    sys.modules["langchain.tools"].BaseTool = object
    sys.modules["src.utils.config_access"].get_archi_config = MagicMock(return_value={})
    sys.modules["src.utils.logging"].get_logger = lambda name: logging.getLogger(name)


_install_stubs()


# ---------------------------------------------------------------------------
# Helper: load mcp.py directly (bypassing package __init__.py)
# ---------------------------------------------------------------------------

_MCP_PATH = Path(__file__).parent.parent.parent / "src" / "archi" / "pipelines" / "agents" / "tools" / "mcp.py"


def _load_mcp_module():
    """Return a fresh import of the mcp module, isolated from the pkg __init__."""
    spec = importlib.util.spec_from_file_location("_mcp_isolated", _MCP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load once for the whole test session
_mcp = _load_mcp_module()


# ---------------------------------------------------------------------------
# Transport normalisation tests
# ---------------------------------------------------------------------------

class TestNormalizeServerConfig:
    """Tests for _normalize_server_config."""

    def test_streamable_http_unchanged(self):
        cfg = {"transport": "streamable_http", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["transport"] == "streamable_http"

    def test_http_alias_normalised(self):
        cfg = {"transport": "http", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["transport"] == "streamable_http"

    def test_https_alias_normalised(self):
        cfg = {"transport": "https", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["transport"] == "streamable_http"

    def test_hyphen_alias_normalised(self):
        cfg = {"transport": "streamable-http", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["transport"] == "streamable_http"

    def test_no_separator_alias_normalised(self):
        cfg = {"transport": "streamablehttp", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["transport"] == "streamable_http"

    def test_stdio_unchanged(self):
        cfg = {"transport": "stdio", "command": "uvx", "args": ["mcp-server-example"]}
        result = _mcp._normalize_server_config("local", cfg)
        assert result["transport"] == "stdio"

    def test_sse_unchanged(self):
        cfg = {"transport": "sse", "url": "http://localhost:8080/sse"}
        result = _mcp._normalize_server_config("legacy", cfg)
        assert result["transport"] == "sse"

    def test_original_dict_not_mutated(self):
        """_normalize_server_config must not mutate the caller's dict."""
        cfg = {"transport": "http", "url": "https://mcp.deepwiki.com/mcp"}
        _mcp._normalize_server_config("deepwiki", cfg)
        assert cfg["transport"] == "http"   # unchanged

    def test_normalisation_is_case_insensitive(self):
        cfg = {"transport": "Streamable-HTTP", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["transport"] == "streamable_http"

    def test_extra_keys_preserved(self):
        cfg = {
            "transport": "http",
            "url": "https://mcp.deepwiki.com/mcp",
            "headers": {"X-Custom": "value"},
        }
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["headers"] == {"X-Custom": "value"}
        assert result["url"] == "https://mcp.deepwiki.com/mcp"


class TestNormalizeServers:
    """Tests for _normalize_servers."""

    def test_empty_dict(self):
        assert _mcp._normalize_servers({}) == {}

    def test_single_deepwiki_server(self):
        servers = {
            "deepwiki": {"transport": "http", "url": "https://mcp.deepwiki.com/mcp"}
        }
        result = _mcp._normalize_servers(servers)
        assert result["deepwiki"]["transport"] == "streamable_http"

    def test_mixed_transports(self):
        servers = {
            "deepwiki": {"transport": "http",  "url": "https://mcp.deepwiki.com/mcp"},
            "local":    {"transport": "stdio", "command": "uvx"},
            "legacy":   {"transport": "sse",   "url": "http://localhost:8080/sse"},
        }
        result = _mcp._normalize_servers(servers)
        assert result["deepwiki"]["transport"] == "streamable_http"
        assert result["local"]["transport"] == "stdio"
        assert result["legacy"]["transport"] == "sse"

    def test_original_dict_not_mutated(self):
        servers = {
            "deepwiki": {"transport": "http", "url": "https://mcp.deepwiki.com/mcp"}
        }
        _mcp._normalize_servers(servers)
        assert servers["deepwiki"]["transport"] == "http"   # unchanged


# ---------------------------------------------------------------------------
# initialize_mcp_client tests
# ---------------------------------------------------------------------------

def _make_mock_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


class TestInitializeMcpClient:
    """Tests for initialize_mcp_client async function."""

    @pytest.mark.asyncio
    async def test_no_servers_returns_none_and_empty_list(self):
        _mcp.get_archi_config = MagicMock(return_value={})
        client, tools = await _mcp.initialize_mcp_client()
        assert client is None
        assert tools == []

    @pytest.mark.asyncio
    async def test_empty_mcp_servers_returns_none_and_empty_list(self):
        _mcp.get_archi_config = MagicMock(return_value={"mcp_servers": {}})
        client, tools = await _mcp.initialize_mcp_client()
        assert client is None
        assert tools == []

    @pytest.mark.asyncio
    async def test_deepwiki_server_tools_loaded(self):
        deepwiki_tools = [
            _make_mock_tool("ask_question"),
            _make_mock_tool("read_wiki_structure"),
            _make_mock_tool("read_wiki_contents"),
        ]
        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(return_value=deepwiki_tools)

        _mcp.get_archi_config = MagicMock(return_value={
            "mcp_servers": {
                "deepwiki": {
                    "transport": "streamable_http",
                    "url": "https://mcp.deepwiki.com/mcp",
                }
            }
        })
        _mcp.MultiServerMCPClient = MagicMock(return_value=mock_client)

        client, tools = await _mcp.initialize_mcp_client()

        assert client is mock_client
        assert len(tools) == 3
        assert {t.name for t in tools} == {
            "ask_question", "read_wiki_structure", "read_wiki_contents"
        }

    @pytest.mark.asyncio
    async def test_http_alias_normalised_before_client_construction(self):
        """Transport alias 'http' must be normalised to 'streamable_http'."""
        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(return_value=[_make_mock_tool("ask_question")])

        captured: dict = {}

        def capture_client(servers):
            captured.update(servers)
            return mock_client

        _mcp.get_archi_config = MagicMock(return_value={
            "mcp_servers": {
                "deepwiki": {"transport": "http", "url": "https://mcp.deepwiki.com/mcp"}
            }
        })
        _mcp.MultiServerMCPClient = MagicMock(side_effect=capture_client)

        await _mcp.initialize_mcp_client()

        assert captured["deepwiki"]["transport"] == "streamable_http"

    @pytest.mark.asyncio
    async def test_failed_server_skipped_other_servers_still_loaded(self):
        """When one server fails, tools from remaining servers are still returned."""
        good_tool = _make_mock_tool("ask_question")

        async def get_tools_side_effect(server_name):
            if server_name == "bad_server":
                raise ConnectionError("timeout")
            return [good_tool]

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=get_tools_side_effect)

        _mcp.get_archi_config = MagicMock(return_value={
            "mcp_servers": {
                "bad_server": {"transport": "sse",   "url": "http://unreachable:9999/sse"},
                "deepwiki":   {"transport": "streamable_http", "url": "https://mcp.deepwiki.com/mcp"},
            }
        })
        _mcp.MultiServerMCPClient = MagicMock(return_value=mock_client)

        client, tools = await _mcp.initialize_mcp_client()

        assert client is mock_client
        assert len(tools) == 1
        assert tools[0].name == "ask_question"

    @pytest.mark.asyncio
    async def test_all_servers_fail_returns_client_with_empty_tools(self):
        """When every server fails, client is still returned but tools is empty."""
        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=ConnectionError("refused"))

        _mcp.get_archi_config = MagicMock(return_value={
            "mcp_servers": {
                "deepwiki": {
                    "transport": "streamable_http",
                    "url": "https://mcp.deepwiki.com/mcp",
                }
            }
        })
        _mcp.MultiServerMCPClient = MagicMock(return_value=mock_client)

        client, tools = await _mcp.initialize_mcp_client()

        assert client is mock_client
        assert tools == []

    @pytest.mark.asyncio
    async def test_multiple_servers_tools_combined(self):
        """Tools from all successfully connected servers are combined."""
        deepwiki_tools = [
            _make_mock_tool("ask_question"),
            _make_mock_tool("read_wiki_structure"),
        ]
        local_tools = [_make_mock_tool("run_code")]

        async def get_tools_side_effect(server_name):
            return deepwiki_tools if server_name == "deepwiki" else local_tools

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=get_tools_side_effect)

        _mcp.get_archi_config = MagicMock(return_value={
            "mcp_servers": {
                "deepwiki": {"transport": "streamable_http", "url": "https://mcp.deepwiki.com/mcp"},
                "sandbox":  {"transport": "stdio",           "command": "sandbox-mcp"},
            }
        })
        _mcp.MultiServerMCPClient = MagicMock(return_value=mock_client)

        client, tools = await _mcp.initialize_mcp_client()

        assert len(tools) == 3
        tool_names = {t.name for t in tools}
        assert tool_names == {"ask_question", "read_wiki_structure", "run_code"}


# ---------------------------------------------------------------------------
# DeepWiki config round-trip tests
# ---------------------------------------------------------------------------

class TestDeepWikiConfigRoundTrip:
    """Verify that the canonical DeepWiki config is accepted without warnings."""

    def test_canonical_config_no_alias_warning(self, caplog):
        cfg = {"transport": "streamable_http", "url": "https://mcp.deepwiki.com/mcp"}
        with caplog.at_level(logging.WARNING):
            result = _mcp._normalize_server_config("deepwiki", cfg)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, f"Unexpected warnings: {[r.message for r in warnings]}"
        assert result["transport"] == "streamable_http"
        assert result["url"] == "https://mcp.deepwiki.com/mcp"

    def test_http_alias_emits_warning(self, caplog):
        cfg = {"transport": "http", "url": "https://mcp.deepwiki.com/mcp"}
        with caplog.at_level(logging.WARNING):
            _mcp._normalize_server_config("deepwiki", cfg)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "Expected a warning for transport alias 'http'"
        assert "streamable_http" in warnings[0].message

    def test_deepwiki_url_preserved(self):
        cfg = {"transport": "streamable_http", "url": "https://mcp.deepwiki.com/mcp"}
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["url"] == "https://mcp.deepwiki.com/mcp"

    def test_deepwiki_with_headers_preserved(self):
        """Headers (e.g. for private repo access) pass through unchanged."""
        cfg = {
            "transport": "streamable_http",
            "url": "https://mcp.deepwiki.com/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        result = _mcp._normalize_server_config("deepwiki", cfg)
        assert result["headers"] == {"Authorization": "Bearer tok"}
