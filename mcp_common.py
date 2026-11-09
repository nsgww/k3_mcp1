"""MCP 客户端共享数据结构（不依赖任何传输层，WS / SSE 均可复用）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class McpTool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=lambda: {"type": "object"})

    @classmethod
    def from_dict(cls, data: dict) -> "McpTool":
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "") or ""),
            input_schema=data.get("inputSchema") or data.get("input_schema") or {"type": "object"},
        )


@dataclass
class McpCallResult:
    ok: bool
    content: list[dict]
    text: str
    raw: Optional[dict] = None
    error: Optional[str] = None


def _content_to_text(content: list) -> str:
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(p for p in parts if p)
