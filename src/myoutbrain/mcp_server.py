from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import TextIO, cast

from myoutbrain.domain_protocol import execute_domain_request
from myoutbrain.protocol_contract import load_domain_schema


MCP_PROTOCOL_VERSION = "2025-11-25"
TOOL_NAME = "myoutbrain_gateway"


class StdioMcpServer:
    def __init__(self, root: Path) -> None:
        self._root = root

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> int:
        for line in input_stream:
            if not line.strip():
                continue
            response = self._handle_line(line)
            if response is not None:
                output_stream.write(
                    json.dumps(
                        response,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                output_stream.flush()
        return 0

    def _handle_line(self, line: str) -> dict[str, object] | None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return _rpc_error(None, -32700, "Parse error")
        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "Invalid Request")
        request = cast(dict[object, object], message)
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _rpc_error(request_id, -32600, "Invalid Request")
        if request_id is None:
            return None
        if method == "initialize":
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "myoutbrain",
                        "version": "0.1.0",
                        "description": "MyOutBrain transport-neutral memory gateway",
                    },
                    "instructions": (
                        "Send complete MyOutBrain domain requests through "
                        f"{TOOL_NAME}; do not access private-instance storage directly."
                    ),
                },
            )
        if method == "ping":
            return _rpc_result(request_id, {})
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": [_gateway_tool()]})
        if method == "tools/call":
            return self._call_tool(request_id, request.get("params"))
        return _rpc_error(request_id, -32601, "Method not found")

    def _call_tool(
        self,
        request_id: object,
        raw_params: object,
    ) -> dict[str, object]:
        if not isinstance(raw_params, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        params = cast(dict[object, object], raw_params)
        if params.get("name") != TOOL_NAME:
            return _rpc_error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        domain_request = cast(dict[object, object], arguments).get("request")
        domain_response, exit_code = execute_domain_request(
            self._root,
            domain_request,
        )
        return _rpc_result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            domain_response,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                ],
                "structuredContent": domain_response,
                "isError": exit_code != 0,
            },
        )


def _gateway_tool() -> dict[str, object]:
    return {
        "name": TOOL_NAME,
        "title": "MyOutBrain Memory Gateway",
        "description": (
            "Invoke a versioned MyOutBrain domain operation after declaring the "
            "client protocol range and capabilities."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": load_domain_schema("domain-request-v2.json")
            },
            "required": ["request"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def _rpc_result(request_id: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: object,
    code: int,
    message: str,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def run_stdio_mcp(root: Path) -> int:
    return StdioMcpServer(root).serve(sys.stdin, sys.stdout)
