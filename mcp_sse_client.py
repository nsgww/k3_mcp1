"""
MCP SSE Client：作为 MCP Client 通过 SSE transport 接入远端 MCP Server。

对应客户端配置:
    {
      "mcpServers": {
        "PGMControl SSE": {
          "type": "sse",
          "url": "http://192.168.1.163:8010/sse"
        }
      }
    }

实现要点:
  - 基于官方 mcp SDK: sse_client + ClientSession（自动完成 initialize）
  - 断线自动重连；重连成功后自动 tools/list（缓存供 Agent Host 使用）
  - 支持 mcp.json 多服务器配置：McpClientPool 聚合同类接口，
    09 主程序对单/多服务器无感
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from mcp import ClientSession, types
from mcp.client.sse import sse_client

from mcp_common import McpCallResult, McpTool, _content_to_text

# SDK 的 sse_reader 在断流时会打一条 ERROR；断线原因与重连由本模块日志给出，
# 静音 SDK 这条避免误导（信息不丢失）
logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)

DEFAULT_SSE_URL = "http://192.168.1.163:8010/sse"
DEFAULT_CONFIG_PATH = str(Path(__file__).resolve().parent / "mcp.json")


class McpSseClient:
    """MCP Client（SSE transport）。"""

    def __init__(
        self,
        *,
        url: str = DEFAULT_SSE_URL,
        server_name: str = "",
        client_name: str = "asr-llm-mcp-client",
        client_version: str = "1.0.0",
        request_timeout: float = 15.0,
        reconnect_delay: float = 3.0,
        sse_read_timeout: float = 300.0,
        auto_sync: bool = True,
    ):
        self.url = url
        self.server_name = server_name
        self.client_name = client_name
        self.client_version = client_version
        self.request_timeout = request_timeout
        self.reconnect_delay = reconnect_delay
        self.sse_read_timeout = sse_read_timeout
        # 单用时刷新后触发 on_change；交给 McpClientPool 时由 pool 聚合工具表
        self.auto_sync = auto_sync
        self.on_change: Optional[Callable[[], None]] = None
        # 状态资源变化回调：fn(state: dict, old, new)，state 含 uri/name/description/value
        self.on_state_change: Optional[Callable[[dict, Any, Any], None]] = None

        self._session: Optional[ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._tools: dict[str, McpTool] = {}
        # 状态资源订阅缓存：uri → {name, description, value, ts}
        self._states: dict[str, dict] = {}
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False
        self._log_tag = f"[MCP SSE:{server_name}]" if server_name else "[MCP SSE]"

    # ------------------------------------------------------------------ 状态
    @property
    def connected(self) -> bool:
        return self._session is not None and self._ready.is_set()

    @property
    def tool_names(self) -> set[str]:
        return set(self._tools.keys())

    def list_cached_tools(self) -> list[McpTool]:
        return list(self._tools.values())

    def tools_as_func_map(self) -> dict[str, Any]:
        return {name: None for name in self._tools}

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._closed = False
        self._task = asyncio.create_task(self._run())
        print(f"[MCP SSE] client mode → {self.url}")

    async def stop(self) -> None:
        self._closed = True
        self._ready.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._session = None

    async def wait_ready(self, timeout: Optional[float] = None) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------------------------------------------------------------- 主循环
    async def _run(self) -> None:
        while not self._closed:
            try:
                async with sse_client(
                    self.url,
                    timeout=self.request_timeout,
                    sse_read_timeout=self.sse_read_timeout,
                ) as (read_stream, write_stream):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        client_info={
                            "name": self.client_name,
                            "version": self.client_version,
                        },
                        message_handler=self._on_session_message,
                    ) as session:
                        self._session = session
                        init_result = await session.initialize()
                        server_info = getattr(init_result, "serverInfo", None)
                        print(
                            f"{self._log_tag} initialize ok: "
                            f"{getattr(server_info, 'name', '?')} "
                            f"protocol={getattr(init_result, 'protocolVersion', '?')}"
                        )
                        await self.refresh_tools()
                        await self._subscribe_states()
                        self._ready.set()
                        if self.on_change:
                            self.on_change()
                        # 挂住会话；SSE 断开时底层 anyio 任务组会抛出异常退出
                        await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closed:
                    return
                print(f"{self._log_tag} reconnect in {self.reconnect_delay:.0f}s: {e}")
            finally:
                if self._session is not None:
                    self._session = None
                    self._ready.clear()
                    self._tools.clear()
                    if self.on_change:
                        self.on_change()
                    print(f"{self._log_tag} MCP Server disconnected")
            if not self._closed:
                await asyncio.sleep(self.reconnect_delay)

    # ---------------------------------------------------------------- 状态资源订阅
    async def _subscribe_states(self) -> None:
        """握手后订阅全部状态资源（变量区 /// 暴露的全局变量）并读一次初值。
        服务器不支持 resources 时静默跳过，不影响工具调用。"""
        session = self._session
        if session is None:
            return
        try:
            async with self._lock:
                result = await asyncio.wait_for(
                    session.list_resources(), timeout=self.request_timeout
                )
        except Exception as e:
            print(f"{self._log_tag} list_resources 跳过: {e}")
            return
        states: dict[str, dict] = {}
        for res in (result.resources or []):
            uri = str(res.uri)
            states[uri] = {
                "uri": uri,
                "name": res.name or uri,
                "description": res.description or "",
                "value": None,
                "ts": 0,
            }
        self._states = states
        for uri in states:
            try:
                async with self._lock:
                    await asyncio.wait_for(
                        session.subscribe_resource(uri), timeout=self.request_timeout
                    )
                await self._refresh_state(uri)
            except Exception as e:
                print(f"{self._log_tag} subscribe {uri} failed: {e}")
        if states:
            print(f"{self._log_tag} states subscribed: {[s['name'] for s in states.values()]}")

    async def _refresh_state(self, uri: str) -> None:
        """读一次资源当前值写入缓存。"""
        session = self._session
        if session is None:
            return
        async with self._lock:
            res = await asyncio.wait_for(
                session.read_resource(uri), timeout=self.request_timeout
            )
        text = ""
        if res.contents:
            text = getattr(res.contents[0], "text", "") or ""
        value: Any = text
        try:
            value = json.loads(text).get("value", text)
        except (json.JSONDecodeError, AttributeError):
            pass
        st = self._states.get(uri)
        if st is not None:
            st["value"] = value
            st["ts"] = int(asyncio.get_running_loop().time() * 1000)

    async def _on_session_message(self, msg: Any) -> None:
        """会话消息回调：处理 resources/updated（状态）与 tools/list_changed
        （工具表变更）通知。注意此处运行在 SDK 接收循环里，不能再发请求
        （会等不到响应），用 create_task 另起协程去读最新值。"""
        root = getattr(msg, "root", msg)
        method = getattr(root, "method", "")
        if method == "notifications/tools/list_changed":
            # 服务端热更新工具（如 RELOAD_MCP_MANIFEST）且 SSE 未断时，
            # 重新拉取 tools/list 并触发 on_change 同步+预热
            if self._session is not None:
                asyncio.create_task(self.refresh_tools())
            return
        if method != "notifications/resources/updated":
            return
        params = getattr(root, "params", None)
        uri = str(getattr(params, "uri", "") or "")
        if uri and self._session is not None:
            asyncio.create_task(self._on_resource_updated(uri))

    async def _on_resource_updated(self, uri: str) -> None:
        old = (self._states.get(uri) or {}).get("value")
        try:
            await self._refresh_state(uri)
        except Exception as e:
            print(f"{self._log_tag} refresh {uri} failed: {e}")
            return
        st = self._states.get(uri) or {}
        new = st.get("value")
        print(f"{self._log_tag} 状态变化: {st.get('name', uri)} {old} → {new}")
        if self.on_state_change is not None:
            try:
                self.on_state_change(dict(st), old, new)
            except Exception as e:
                print(f"{self._log_tag} on_state_change 回调异常: {e}")

    def list_states(self) -> list[dict]:
        return list(self._states.values())

    # ---------------------------------------------------------------- 工具
    async def _force_reconnect(self) -> None:
        """会话失效时强制重连：取消当前 _run 任务并重新发起连接。"""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._session = None
        self._ready.clear()
        if not self._closed:
            self._task = asyncio.create_task(self._run())

    async def refresh_tools(self) -> list[McpTool]:
        if self._session is None:
            raise ConnectionError(f"MCP Server 未连接（SSE: {self.url}）")
        async with self._lock:
            result = await asyncio.wait_for(
                self._session.list_tools(), timeout=self.request_timeout
            )
        tools = [
            McpTool.from_dict(t.model_dump())
            for t in (result.tools or [])
            if getattr(t, "name", None)
        ]
        self._tools = {t.name: t for t in tools}
        print(f"{self._log_tag} tools/list → {[t.name for t in tools]}")
        # 握手阶段的 refresh 发生在 ready 置位之前，由 _run 在置位后统一通知；
        # 已连接后的显式 refresh 在此通知
        if self.on_change and self._ready.is_set():
            self.on_change()
        return tools

    async def call_tool(
        self, name: str, arguments: Optional[dict] = None
    ) -> McpCallResult:
        args = arguments or {}
        result = None
        for attempt in (1, 2):
            if not self.connected:
                # 会话可能在重连中,等一拍
                await self.wait_ready(timeout=self.reconnect_delay + 5)
                if not self.connected:
                    return McpCallResult(
                        ok=False,
                        content=[],
                        text="",
                        error=f"MCP Server 未连接（SSE: {self.url}）",
                    )
            try:
                async with self._lock:
                    result = await asyncio.wait_for(
                        self._session.call_tool(name, args),
                        timeout=self.request_timeout,
                    )
                break
            except Exception as e:
                err = str(e) or type(e).__name__
                if attempt == 1:
                    # 半开连接(显示 connected 实际已死): 强制重连后重试一次
                    print(f"{self._log_tag} call {name} 异常({err})，强制重连后重试")
                    await self._force_reconnect()
                    continue
                return McpCallResult(ok=False, content=[], text="", error=err)
        if result is None:
            return McpCallResult(ok=False, content=[], text="", error="call failed")

        content = [
            c.model_dump() if hasattr(c, "model_dump") else c
            for c in (result.content or [])
        ]
        if getattr(result, "isError", getattr(result, "is_error", False)):
            text = _content_to_text(content)
            return McpCallResult(
                ok=False,
                content=content,
                text=text,
                raw={"content": content},
                error=text or "tools/call isError",
            )
        return McpCallResult(
            ok=True,
            content=content,
            text=_content_to_text(content),
            raw={"content": content},
        )

    async def ping(self) -> bool:
        if not self.connected:
            return False
        try:
            async with self._lock:
                await asyncio.wait_for(
                    self._session.send_ping(), timeout=self.request_timeout
                )
            return True
        except Exception as e:
            print(f"[MCP SSE] ping failed: {e}")
            return False


def build_mcp_client_from_env() -> McpSseClient:
    return McpSseClient(
        url=os.getenv("MCP_SSE_URL", DEFAULT_SSE_URL),
        request_timeout=float(os.getenv("MCP_REQUEST_TIMEOUT", "15")),
        reconnect_delay=float(os.getenv("MCP_RECONNECT_DELAY", "3")),
    )


def load_mcp_json(path: str) -> dict[str, dict]:
    """解析 mcp.json 的 mcpServers 段，跳过 disabled 条目。"""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    servers = data.get("mcpServers") or {}
    return {
        name: cfg
        for name, cfg in servers.items()
        if isinstance(cfg, dict) and not cfg.get("disabled")
    }


class McpClientPool:
    """
    多 MCP Server 聚合：接口与 McpSseClient 一致（start/stop/connected/
    tool_names/list_cached_tools/refresh_tools/call_tool/ping）。
    工具表合并后供 Agent Host 使用；call_tool 按归属分发。
    空 pool 即为"MCP 禁用"：connected=False，start/stop 无操作。
    """

    def __init__(self, clients: dict[str, McpSseClient], refresh_interval: float = 0.0):
        self._clients = dict(clients)
        self._tool_owner: dict[str, str] = {}
        self._on_change: Optional[Callable[[], Any]] = None
        self._on_state_change: Optional[Callable[[dict, Any, Any], None]] = None
        for c in self._clients.values():
            c.on_change = self._resync

        # 定时刷新工具表（秒），<=0 表示关闭
        self._refresh_interval = refresh_interval
        self._refresh_task: Optional[asyncio.Task] = None

    @property
    def on_change(self) -> Optional[Callable[[], Any]]:
        return self._on_change

    @on_change.setter
    def on_change(self, fn: Optional[Callable[[], Any]]) -> None:
        """聚合工具表变化回调；支持普通函数和 async 函数。"""
        self._on_change = fn

    @property
    def on_state_change(self) -> Optional[Callable[[dict, Any, Any], None]]:
        return self._on_state_change

    @on_state_change.setter
    def on_state_change(self, fn: Optional[Callable[[dict, Any, Any], None]]) -> None:
        """状态变化回调（fn(state, old, new)），分发到池内所有 client。"""
        self._on_state_change = fn
        for c in self._clients.values():
            c.on_state_change = fn

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self._clients.values())

    @property
    def tool_names(self) -> set[str]:
        return set(self._tool_owner.keys())

    def list_cached_tools(self) -> list[McpTool]:
        tools = []
        for sname, c in self._clients.items():
            for t in c.list_cached_tools():
                if self._tool_owner.get(t.name) == sname:
                    tools.append(t)
        return tools

    def tools_as_func_map(self) -> dict[str, Any]:
        return {name: None for name in self._tool_owner}

    def list_states(self) -> list[dict]:
        """聚合各服务器订阅到的状态资源（含实时值）。"""
        states: list[dict] = []
        for sname, c in self._clients.items():
            for st in c.list_states():
                states.append({**st, "server": sname})
        return states

    def describe(self) -> str:
        return ", ".join(f"{name}({c.url})" for name, c in self._clients.items())

    async def start(self) -> None:
        for name, c in self._clients.items():
            await c.start()
        if self._clients:
            print(f"[MCP pool] {len(self._clients)} server(s): {self.describe()}")
        # 启动定时刷新任务
        if self._refresh_interval > 0 and self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        for c in self._clients.values():
            await c.stop()

    async def _periodic_refresh(self) -> None:
        """后台定时刷新 tools/list，动态感知服务器工具变更。"""
        while True:
            try:
                await asyncio.sleep(self._refresh_interval)
                if self.connected:
                    await self.refresh_tools()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[MCP pool] periodic refresh error: {e}")

    async def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """任一服务器就绪即返回 True。"""
        deadline = (
            asyncio.get_running_loop().time() + timeout if timeout else None
        )
        while True:
            if self.connected:
                return True
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def _resync(self) -> None:
        """聚合所有已连接服务器的工具归属表（供 list_cached_tools / call_tool）。"""
        owner: dict[str, str] = {}
        for sname, c in self._clients.items():
            if not c.connected:
                continue
            for t in c.list_cached_tools():
                if t.name in owner:
                    print(
                        f"[MCP pool] 工具名冲突: {t.name} "
                        f"({owner[t.name]} 与 {sname})，保留 {owner[t.name]}"
                    )
                    continue
                owner[t.name] = sname
        self._tool_owner = owner
        print(f"[MCP pool] tools synced: {sorted(owner.keys())}")
        if self._on_change is not None:
            try:
                result = self._on_change()
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception as e:
                print(f"[MCP pool] on_change callback error: {e}")

    async def refresh_tools(self) -> list[McpTool]:
        if not self.connected:
            raise ConnectionError("MCP Server 未连接")
        for name, c in self._clients.items():
            if not c.connected:
                continue
            try:
                await c.refresh_tools()
            except Exception as e:
                print(f"[MCP pool] refresh {name} failed: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                # 连接类错误（如服务器重启导致 SSE 断开）触发强制重连
                if "ClosedResource" in type(e).__name__ or "BrokenResource" in type(e).__name__:
                    print(f"[MCP pool] refresh {name} connection closed, force reconnect")
                    await c._force_reconnect()
        self._resync()
        return self.list_cached_tools()

    async def call_tool(
        self, name: str, arguments: Optional[dict] = None
    ) -> McpCallResult:
        sname = self._tool_owner.get(name)
        if sname is None:
            return McpCallResult(
                ok=False, content=[], text="", error=f"未知工具: {name}"
            )
        return await self._clients[sname].call_tool(name, arguments)

    async def ping(self) -> bool:
        oks = [await c.ping() for c in self._clients.values() if c.connected]
        return bool(oks) and all(oks)


def build_mcp_pool_from_env() -> "McpClientPool":
    """
    优先读 mcp.json（MCP_CONFIG 可改路径），没有则回退 MCP_SSE_URL 单服务器；
    都没有 → 空 pool（MCP 禁用）。
    """
    clients: dict[str, McpSseClient] = {}
    cfg_path = os.getenv("MCP_CONFIG", DEFAULT_CONFIG_PATH)
    if os.path.isfile(cfg_path):
        try:
            entries = load_mcp_json(cfg_path)
        except Exception as e:
            print(f"[MCP pool] 解析 {cfg_path} 失败: {e}")
            entries = {}
        for name, cfg in entries.items():
            typ = str(cfg.get("type") or "sse").lower()
            url = str(cfg.get("url") or "").strip()
            if typ in {"sse", "http+sse"} and url:
                clients[name] = McpSseClient(
                    url=url, server_name=name, auto_sync=False
                )
            else:
                print(
                    f"[MCP pool] 跳过 {name}: type={typ!r}（当前仅支持 sse 传输）"
                )
    else:
        url = os.getenv("MCP_SSE_URL", DEFAULT_SSE_URL).strip()
        if url:
            clients["default"] = McpSseClient(url=url, auto_sync=False)
    return McpClientPool(
        clients,
        refresh_interval=float(os.getenv("MCP_REFRESH_INTERVAL", "0")),
    )


if __name__ == "__main__":
    # 快速自测: python mcp_sse_client.py（走 mcp.json / MCP_SSE_URL）
    async def _demo():
        client = build_mcp_pool_from_env()
        await client.start()
        ok = await client.wait_ready(timeout=15)
        if not ok:
            print("connect timeout")
        else:
            for t in client.list_cached_tools():
                print(f"- {t.name}: {t.description}")
                print(f"  schema={json.dumps(t.input_schema, ensure_ascii=False)}")
        await client.stop()

    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        pass
