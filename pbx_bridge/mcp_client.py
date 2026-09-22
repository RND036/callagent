"""Client for the PBX's MCP server, used for call transfer and hangup.

Transfer/hangup are NOT sent over the AudioSocket TCP connection (which has
no transfer mechanism of its own) -- they go through a separate MCP server
(JSON-RPC 2.0 over HTTP, "tools/list" + "tools/call"), authenticated via
MCP_SERVER_URL + MCP_API_KEY with call_id/account_id carried as headers.

The live server's tool schema has been observed to drift from published docs
(e.g. transfer wants a "destination number" field with a literal space, plus
a separate "tier", not the documented destination_number). Rather than
hardcode field names, this discovers the real schema at runtime via
tools/list() and maps our known arguments onto whatever field names the
schema actually declares. Destination numbers are tried in multiple
candidate shapes since the dialplan is picky about format.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class McpError(Exception):
    pass


def get_destination_candidates(number: str, domain: str | None = None) -> list[str]:
    """Different PBX dialplans expect the destination in different shapes;
    callers should try each in order until one is accepted."""
    digits_plus = "".join(ch for ch in number if ch.isdigit() or ch == "+")
    digits = digits_plus.lstrip("+")

    candidates = [number, digits_plus, f"tel:{digits}", digits]
    if not digits_plus.startswith("+"):
        candidates.append(f"+{digits}")
    if domain:
        candidates.append(f"sip:{digits}@{domain}")
    if len(digits) >= 3:
        candidates.append(digits[-4:])  # bare extension, 4-digit
        candidates.append(digits[-3:])  # bare extension, 3-digit

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


class McpClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._tools_cache: dict[str, dict] | None = None
        self._request_id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, call_id: str | None, account_id: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if call_id:
            headers["call_id"] = call_id
        if account_id:
            headers["account_id"] = account_id
        return headers

    async def _rpc(self, method: str, params: dict, call_id: str | None, account_id: str | None) -> Any:
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        response = await self._client.post(self._base_url, json=body, headers=self._headers(call_id, account_id))
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise McpError(str(data["error"]))
        return data.get("result")

    async def list_tools(
        self, call_id: str | None = None, account_id: str | None = None, force_refresh: bool = False
    ) -> dict[str, dict]:
        if self._tools_cache is not None and not force_refresh:
            return self._tools_cache
        result = await self._rpc("tools/list", {}, call_id, account_id)
        self._tools_cache = {tool["name"]: tool for tool in (result or {}).get("tools", [])}
        return self._tools_cache

    async def _find_tool(self, *name_hints: str, call_id: str | None, account_id: str | None) -> str:
        tools = await self.list_tools(call_id=call_id, account_id=account_id)
        for hint in name_hints:
            if hint in tools:
                return hint
        for hint in name_hints:
            for name in tools:
                if hint in name.lower():
                    return name
        raise McpError(f"No MCP tool found matching {name_hints}; available tools: {list(tools)}")

    def _tool_properties(self, tool_name: str) -> dict:
        tool = (self._tools_cache or {}).get(tool_name, {})
        return tool.get("inputSchema", {}).get("properties", {})

    async def transfer_call(
        self,
        call_id: str | None,
        other_leg_call_id: str | None,
        account_id: str | None,
        target_number: str,
        reason: str,
        domain: str | None = None,
        tier: str = "internal",
    ) -> dict:
        tool_name = await self._find_tool("transfer_call", "transfer", call_id=call_id, account_id=account_id)
        props = self._tool_properties(tool_name)

        last_error: Exception | None = None
        for destination in get_destination_candidates(target_number, domain=domain):
            args = self._build_transfer_args(props, destination, tier, reason, call_id, other_leg_call_id, account_id)
            try:
                return await self._rpc("tools/call", {"name": tool_name, "arguments": args}, call_id, account_id)
            except (McpError, httpx.HTTPStatusError) as exc:
                logger.warning("MCP transfer candidate %r rejected: %s", destination, exc)
                last_error = exc
        raise McpError(f"All destination candidates failed for {target_number!r}: {last_error}")

    def _build_transfer_args(
        self,
        props: dict,
        destination: str,
        tier: str,
        reason: str,
        call_id: str | None,
        other_leg_call_id: str | None,
        account_id: str | None,
    ) -> dict:
        args: dict[str, Any] = {}
        destination_aliases = ["destination number", "destination_number", "destination", "target_number", "number", "to"]
        for alias in destination_aliases:
            if alias in props:
                args[alias] = destination
                break
        else:
            args["destination_number"] = destination

        for key, value in (
            ("tier", tier),
            ("call_id", call_id),
            ("callId", call_id),
            ("other_leg_call_id", other_leg_call_id),
            ("otherLegCallId", other_leg_call_id),
            ("account_id", account_id),
            ("accountId", account_id),
            ("reason", reason),
        ):
            if key in props and value is not None:
                args[key] = value
        return args

    async def hangup_call(
        self, call_id: str | None, other_leg_call_id: str | None, account_id: str | None, reason: str
    ) -> dict:
        tool_name = await self._find_tool("hangup_call", "hangup", call_id=call_id, account_id=account_id)
        props = self._tool_properties(tool_name)

        args: dict[str, Any] = {}
        for key, value in (
            ("call_id", call_id),
            ("callId", call_id),
            ("other_leg_call_id", other_leg_call_id),
            ("otherLegCallId", other_leg_call_id),
            ("account_id", account_id),
            ("accountId", account_id),
            ("reason", reason),
        ):
            if key in props and value is not None:
                args[key] = value
        return await self._rpc("tools/call", {"name": tool_name, "arguments": args}, call_id, account_id)
