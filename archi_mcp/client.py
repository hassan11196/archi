"""
HTTP client for communicating with a running archi deployment.

Wraps the archi REST API so the MCP server can call archi endpoints
without embedding any of archi's internal Python dependencies.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

import requests


class ArchiClientError(RuntimeError):
    """Raised when the archi API returns an error or is unreachable."""


class ArchiClient:
    """Thin HTTP client for archi's REST API."""

    def __init__(
        self,
        base_url: str = "http://localhost:5000",
        timeout: int = 120,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client_id = f"mcp-{uuid.uuid4().hex[:12]}"
        self._session = requests.Session()
        self._session.headers["X-Client-ID"] = self._client_id
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        try:
            resp = self._session.get(self._url(path), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ArchiClientError(f"Cannot reach archi at {self.base_url}: {exc}") from exc
        if not resp.ok:
            raise ArchiClientError(f"archi returned {resp.status_code} for GET {path}: {resp.text}")
        return resp.json()

    def _post(self, path: str, payload: Dict) -> Any:
        try:
            resp = self._session.post(self._url(path), json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ArchiClientError(f"Cannot reach archi at {self.base_url}: {exc}") from exc
        if not resp.ok:
            raise ArchiClientError(f"archi returned {resp.status_code} for POST {path}: {resp.text}")
        return resp.json()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return health status from archi."""
        return self._get("/api/health")

    # ------------------------------------------------------------------
    # Query / Chat
    # ------------------------------------------------------------------

    def query(
        self,
        message: str,
        conversation_id: Optional[int] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ask archi a question using its active RAG pipeline.

        Returns a dict with at least:
            response (str): the answer text
            conversation_id (int): conversation ID for follow-up queries
        """
        payload: Dict[str, Any] = {
            "last_message": message,
            "client_id": self._client_id,
            "client_sent_msg_ts": int(time.time() * 1000),
            "client_timeout": self.timeout * 1000,
            "include_agent_steps": False,
            "include_tool_steps": False,
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model

        return self._post("/api/get_chat_response", payload)

    # ------------------------------------------------------------------
    # Documents / Data Viewer
    # ------------------------------------------------------------------

    def list_documents(
        self,
        page: int = 1,
        per_page: int = 50,
        search: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List indexed documents in the archi vectorstore."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if search:
            params["search"] = search
        if source_type:
            params["source_type"] = source_type
        return self._get("/api/data/documents", params=params)

    def get_document_content(self, document_hash: str) -> Dict[str, Any]:
        """Return the raw content of a specific indexed document."""
        return self._get(f"/api/data/documents/{document_hash}/content")

    def get_document_chunks(self, document_hash: str) -> Dict[str, Any]:
        """Return the vectorstore chunks for a specific document."""
        return self._get(f"/api/data/documents/{document_hash}/chunks")

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def get_static_config(self) -> Dict[str, Any]:
        """Return the static (deploy-time) archi configuration."""
        return self._get("/api/config/static")

    def get_dynamic_config(self) -> Dict[str, Any]:
        """Return the dynamic (runtime) archi configuration."""
        return self._get("/api/config/dynamic")

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def list_agents(self) -> Dict[str, Any]:
        """Return the list of available agent spec files."""
        return self._get("/api/agents/list")

    def get_agent_info(self) -> Dict[str, Any]:
        """Return info about the currently active agent."""
        return self._get("/api/agent/info")

    # ------------------------------------------------------------------
    # Providers / Models
    # ------------------------------------------------------------------

    def list_providers(self) -> Dict[str, Any]:
        """Return available model providers and their models."""
        return self._get("/api/providers")

    def get_api_info(self) -> Dict[str, Any]:
        """Return archi API version and feature information."""
        return self._get("/api/info")
