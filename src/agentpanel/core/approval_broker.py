"""Local approval broker — the live link between a spawned permission gate and the panel.

The MCP permission gate (``mcp_approver``) runs as a separate process (Claude spawns it). To
let the *user* answer a gated request live — instead of the policy auto-answering — the gate
connects to this broker over a unix socket inside the running AgentPanel process. The broker
hands the request to a resolver (the TUI's allow/allow-type/deny modal, or a headless stdin
prompt), then sends the decision back to the gate, which returns it to the agent.

Newline-delimited JSON, one request → one response, then the connection closes.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable, Dict

Resolver = Callable[[Dict], Awaitable[Dict]]  # async (request) -> {"behavior": "allow"|"deny", ...}


class ApprovalBroker:
    def __init__(self, resolver: Resolver, sock_path: str, timeout_s: float = 600.0) -> None:
        self.resolver = resolver
        self.path = sock_path
        self.timeout_s = timeout_s
        self._server = None

    async def start(self) -> str:
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        return self.path

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decision: Dict = {"behavior": "deny", "reason": "broker error"}
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_s)
            if line:
                request = json.loads(line.decode())
                decision = await self.resolver(request)
        except Exception:
            pass
        try:
            writer.write((json.dumps(decision) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
        finally:
            if self.path and os.path.exists(self.path):
                try:
                    os.unlink(self.path)
                except OSError:
                    pass


def socket_path_for(session_id: str) -> str:
    """A short unix-socket path (macOS caps sun_path at ~104 bytes, so avoid long tmp dirs)."""
    return f"/tmp/agentpanel-{os.getpid()}-{session_id}.sock"
