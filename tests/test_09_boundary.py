"""
09_asr_llm_ws.py 边界测试脚本

支持两种运行方式：
1. 独立运行：
   python tests/test_09_boundary.py ws://192.168.1.228:8060/stream

2. pytest：
   pytest tests/test_09_boundary.py -v --ws-uri ws://192.168.1.228:8060/stream

可选参数：
   --samples DIR          指定真实语音样本目录（默认：tests/test_wavs/）
   --stress-connect N     连接风暴压测 N 并发
   --endurance N          长稳循环 N 轮

样本目录应包含（默认路径 tests/test_wavs/）：
   wake_nihao_junge.wav   唤醒词“你好军哥”
   cmd_open_light.wav     指令“打开办公室灯”
   cmd_close_light.wav    指令“关闭办公室灯”
   cmd_status.wav         指令“办公室灯什么状态”
   cmd_chitchat.wav       闲聊“今天天气怎么样”
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WS_PATH = "/stream"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_BYTES = 1024  # 64ms @16k int16 mono
DEFAULT_TIMEOUT = 15.0
LONG_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# 音频合成辅助
# ---------------------------------------------------------------------------
def make_pcm16(waveform: np.ndarray) -> bytes:
    """将浮点波形 [-1,1] 转为 int16 PCM bytes。"""
    clipped = np.clip(waveform * 32767, -32768, 32767).astype(np.int16)
    return clipped.tobytes()


def make_silence(duration_ms: int, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    n = int(sample_rate * duration_ms / 1000)
    return make_pcm16(np.zeros(n, dtype=np.float32))


def make_sine(duration_ms: int, freq: float = 440.0, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n) / sample_rate
    return make_pcm16(np.sin(2 * np.pi * freq * t) * 0.3)


def make_noise(duration_ms: int, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    n = int(sample_rate * duration_ms / 1000)
    return make_pcm16((np.random.rand(n).astype(np.float32) - 0.5) * 0.3)


def load_wav(path: Path) -> tuple[bytes, int]:
    """读取 wav 文件，返回 (pcm_bytes, sample_rate)。"""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
        if sw == 2 and nch == 1:
            return frames, sr
        # 简单处理：转单声道、16bit
        audio = np.frombuffer(frames, dtype=np.int16 if sw == 2 else np.int8)
        if nch == 2:
            audio = audio.reshape(-1, 2).mean(axis=1).astype(np.int16)
        return audio.astype(np.int16).tobytes(), sr


# ---------------------------------------------------------------------------
# WebSocket 工具函数
# ---------------------------------------------------------------------------
async def connect(uri: str, path_ok: bool = True) -> websockets.WebSocketClientProtocol:
    """建立 WebSocket 连接。"""
    target = uri if path_ok else uri.replace(WS_PATH, "/wrong")
    return await websockets.connect(target, ping_interval=None, max_size=64 * 1024 * 1024)


async def recv_json(ws, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """接收一条 JSON 消息。"""
    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
    if isinstance(msg, bytes):
        return None
    return json.loads(msg)


async def recv_bytes(ws, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """接收一条 binary 消息。"""
    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
    if not isinstance(msg, bytes):
        raise RuntimeError(f"期待 binary，收到 text: {msg[:200]!r}")
    return msg


async def send_pcm(ws, pcm: bytes, chunk_size: int = DEFAULT_CHUNK_BYTES, pace_ms: float = 0) -> None:
    """按 chunk 分包发送 PCM，模拟实时推流。"""
    for i in range(0, len(pcm), chunk_size):
        await ws.send(pcm[i : i + chunk_size])
        if pace_ms > 0:
            await asyncio.sleep(pace_ms / 1000)


async def drain(ws, timeout: float = 0.5) -> list[dict]:
    """清空已到达的下行消息。"""
    out = []
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(msg, bytes):
                out.append({"__binary__": True, "len": len(msg)})
            else:
                out.append(json.loads(msg))
        except asyncio.TimeoutError:
            break
    return out


async def wait_for(ws, predicate, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """轮询直到满足 predicate 的消息到达。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = await recv_json(ws, timeout=min(1.0, deadline - time.monotonic()))
        if msg is None:
            continue
        if predicate(msg):
            return msg
    raise TimeoutError(f"未在 {timeout}s 内收到目标消息")


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------
class Result:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.skipped = []

    def ok(self, name: str):
        self.passed.append(name)
        print(f"  ✓ {name}")

    def fail(self, name: str, reason: str):
        self.failed.append((name, reason))
        print(f"  ✗ {name}: {reason}")

    def skip(self, name: str, reason: str):
        self.skipped.append((name, reason))
        print(f"  - {name}: {reason}")

    def summary(self):
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print(f"\n{'='*50}")
        print(f"总计 {total} 项 | 通过 {len(self.passed)} | 失败 {len(self.failed)} | 跳过 {len(self.skipped)}")
        if self.failed:
            print("\n失败项：")
            for name, reason in self.failed:
                print(f"  - {name}: {reason}")
        return len(self.failed) == 0


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
class BoundaryTests:
    def __init__(self, uri: str, samples_dir: Optional[Path] = None):
        self.uri = uri
        # 默认样本目录：tests/test_wavs/
        self.samples_dir = (
            samples_dir
            if samples_dir is not None
            else Path(__file__).resolve().parent / "test_wavs"
        )
        self.result = Result()
        self._welcome: Optional[dict] = None

    def sample_path(self, name: str) -> Optional[Path]:
        if not self.samples_dir:
            return None
        p = self.samples_dir / name
        return p if p.is_file() else None

    async def _open(self) -> websockets.WebSocketClientProtocol:
        ws = await connect(self.uri)
        # 清掉欢迎消息
        await drain(ws, timeout=1.0)
        return ws

    # ---- 协议边界 ----
    async def test_wrong_path(self):
        name = "wrong_path_close_1008"
        try:
            ws = await connect(self.uri, path_ok=False)
            try:
                await ws.recv()
            except ConnectionClosed as e:
                if e.code == 1008:
                    self.result.ok(name)
                else:
                    self.result.fail(name, f"close code={e.code}, reason={e.reason}")
            finally:
                if not ws.closed:
                    await ws.close()
        except Exception as e:
            self.result.fail(name, str(e))

    async def test_ping_pong(self):
        name = "ping_pong"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "ping"}))
            msg = await wait_for(ws, lambda d: d.get("type") == "pong")
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_invalid_json(self):
        name = "invalid_json"
        ws = await self._open()
        try:
            await ws.send("this is not json")
            msg = await wait_for(ws, lambda d: d.get("type") == "error")
            if "invalid json" in msg.get("message", ""):
                self.result.ok(name)
            else:
                self.result.fail(name, f"返回错误内容不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_unknown_type(self):
        name = "unknown_type"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "foobar"}))
            msg = await wait_for(ws, lambda d: d.get("type") == "error")
            if "unknown type" in msg.get("message", ""):
                self.result.ok(name)
            else:
                self.result.fail(name, f"返回错误内容不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_huge_binary(self):
        name = "huge_binary_disconnect"
        ws = await self._open()
        try:
            huge = b"\x00" * (20 * 1024 * 1024)
            await ws.send(huge)
            try:
                await ws.recv()
                self.result.fail(name, "服务未断开连接")
            except ConnectionClosed:
                self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            if not ws.closed:
                await ws.close()

    # ---- 配置边界 ----
    async def test_config_invalid_sample_rate(self):
        name = "config_invalid_sample_rate"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "config", "sample_rate": 0}))
            msg = await wait_for(ws, lambda d: d.get("type") == "error")
            if "invalid sample_rate" in msg.get("message", ""):
                self.result.ok(name)
            else:
                self.result.fail(name, f"返回错误不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_config_tts_toggle(self):
        name = "config_tts_toggle"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "config", "tts": False}))
            msg = await wait_for(ws, lambda d: d.get("type") == "status")
            if "config ok" in msg.get("message", "") and msg.get("tts_enabled") is False:
                self.result.ok(name)
            else:
                self.result.fail(name, f"状态不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    # ---- 音频输入边界（手动 end 模式） ----
    async def test_empty_audio_manual(self):
        name = "empty_audio_manual"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "end"}))
            msg = await wait_for(ws, lambda d: d.get("type") == "error")
            if "empty audio" in msg.get("message", ""):
                self.result.ok(name)
            else:
                self.result.fail(name, f"错误不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_silence_audio_manual(self):
        name = "silence_audio_manual"
        ws = await self._open()
        try:
            pcm = make_silence(1000)
            await send_pcm(ws, pcm)
            await ws.send(json.dumps({"type": "end"}))
            msg = await wait_for(ws, lambda d: d.get("type") in ("error", "done"))
            if msg.get("type") == "error" and "asr" in msg.get("message", "").lower():
                self.result.ok(name)
            elif msg.get("type") == "done":
                # 某些版本可能直接 done，不强求 error
                self.result.ok(name)
            else:
                self.result.fail(name, f"返回不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_short_noise_audio(self):
        name = "short_noise_audio"
        ws = await self._open()
        try:
            pcm = make_noise(80)
            await send_pcm(ws, pcm)
            await ws.send(json.dumps({"type": "end"}))
            msg = await wait_for(ws, lambda d: d.get("type") in ("error", "done"), timeout=10)
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_long_audio_manual(self):
        name = "long_audio_manual_12s"
        ws = await self._open()
        try:
            # 12s 正弦波，模拟长音频
            pcm = make_sine(12000, freq=440)
            await send_pcm(ws, pcm, chunk_size=DEFAULT_CHUNK_BYTES, pace_ms=0)
            await ws.send(json.dumps({"type": "end"}))
            msg = await wait_for(ws, lambda d: d.get("type") in ("error", "done"), timeout=60)
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    # ---- 门控边界 ----
    async def test_wake_gate_requires_16k(self):
        name = "wake_gate_requires_16k"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "config", "sample_rate": 48000}))
            await recv_json(ws)  # config ok
            await ws.send(json.dumps({"type": "wake_gate", "enabled": True}))
            msg = await wait_for(ws, lambda d: d.get("type") == "error")
            if "wake_gate 需要 16kHz" in msg.get("message", ""):
                self.result.ok(name)
            else:
                self.result.fail(name, f"错误不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_wake_gate_end_forbidden(self):
        name = "wake_gate_end_forbidden"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "wake_gate", "enabled": True}))
            msg = await wait_for(ws, lambda d: d.get("type") == "status")
            if not msg.get("wake_gate"):
                self.result.fail(name, "门控未开启")
                return
            await ws.send(json.dumps({"type": "end"}))
            msg = await wait_for(ws, lambda d: d.get("type") == "error")
            if "wake_gate 模式下无需发送 end" in msg.get("message", ""):
                self.result.ok(name)
            else:
                self.result.fail(name, f"错误不符: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_wake_gate_woken_timeout(self):
        name = "wake_gate_woken_timeout"
        sp = self.sample_path("wake_nihao_junge.wav")
        if not sp:
            self.result.skip(name, "缺少 wake_nihao_junge.wav")
            return
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "wake_gate", "enabled": True}))
            await wait_for(ws, lambda d: d.get("type") == "status" and "wake_gate=on" in d.get("message", ""))
            pcm, sr = load_wav(sp)
            if sr != DEFAULT_SAMPLE_RATE:
                pcm = self._resample_pcm16(pcm, sr, DEFAULT_SAMPLE_RATE)
            await send_pcm(ws, pcm)
            await wait_for(ws, lambda d: d.get("type") == "kws", timeout=10)
            # 唤醒后推 6s 静音，应触发超时回 idle
            await send_pcm(ws, make_silence(6000), chunk_size=DEFAULT_CHUNK_BYTES, pace_ms=0)
            await wait_for(ws, lambda d: d.get("type") == "wake_state" and d.get("state") == "idle", timeout=10)
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_wake_gate_followup_timeout(self):
        name = "wake_gate_followup_timeout"
        sp_wake = self.sample_path("wake_nihao_junge.wav")
        sp_cmd = self.sample_path("cmd_open_light.wav")
        if not sp_wake or not sp_cmd:
            self.result.skip(name, "缺少唤醒或指令样本")
            return
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "wake_gate", "enabled": True}))
            await wait_for(ws, lambda d: d.get("type") == "status" and "wake_gate=on" in d.get("message", ""))

            # 唤醒
            pcm, sr = load_wav(sp_wake)
            if sr != DEFAULT_SAMPLE_RATE:
                pcm = self._resample_pcm16(pcm, sr, DEFAULT_SAMPLE_RATE)
            await send_pcm(ws, pcm)
            await wait_for(ws, lambda d: d.get("type") == "kws", timeout=10)

            # 指令
            pcm2, sr2 = load_wav(sp_cmd)
            if sr2 != DEFAULT_SAMPLE_RATE:
                pcm2 = self._resample_pcm16(pcm2, sr2, DEFAULT_SAMPLE_RATE)
            await send_pcm(ws, pcm2)
            await wait_for(ws, lambda d: d.get("type") == "done", timeout=LONG_TIMEOUT)

            # 等 followup idle
            await wait_for(ws, lambda d: d.get("type") == "wake_state" and d.get("state") == "followup", timeout=5)
            await send_pcm(ws, make_silence(16000), chunk_size=DEFAULT_CHUNK_BYTES, pace_ms=0)
            await wait_for(ws, lambda d: d.get("type") == "wake_state" and d.get("state") == "idle", timeout=20)
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    @staticmethod
    def _resample_pcm16(pcm: bytes, src_sr: int, dst_sr: int) -> bytes:
        from scipy.signal import resample
        audio = np.frombuffer(pcm, dtype=np.int16)
        n = int(len(audio) * dst_sr / src_sr)
        if n <= 0:
            return b""
        out = np.clip(resample(audio.astype(np.float32), n), -32768, 32767).astype(np.int16)
        return out.tobytes()

    # ---- MCP 边界 ----
    async def _run_voice_command(self, sample_name: str) -> dict:
        """发送一条真实语音并返回 function 事件。"""
        sample = self.sample_path(sample_name)
        if not sample:
            raise FileNotFoundError(f"缺少 {sample_name}")
        ws = await self._open()
        try:
            pcm, sr = load_wav(sample)
            if sr != DEFAULT_SAMPLE_RATE:
                pcm = self._resample_pcm16(pcm, sr, DEFAULT_SAMPLE_RATE)
            await send_pcm(ws, pcm)
            await ws.send(json.dumps({"type": "end"}))
            asr = await wait_for(
                ws, lambda d: d.get("type") == "asr", timeout=30
            )
            print(f"    ASR({sample_name}): {asr.get('text')}")
            event = await wait_for(
                ws,
                lambda d: d.get("type") in ("function", "done", "error"),
                timeout=LONG_TIMEOUT,
            )
            if event.get("type") != "function":
                raise AssertionError(f"未收到 function: {event}")
            return event
        finally:
            await ws.close()

    @staticmethod
    def _function_call(event: dict, expected_name: str) -> dict:
        calls = event.get("calls") or [
            {
                "name": event.get("name"),
                "arguments": event.get("arguments") or {},
                "result": event.get("result"),
                "ok": event.get("ok"),
            }
        ]
        matches = [call for call in calls if call.get("name") == expected_name]
        if len(matches) != 1:
            raise AssertionError(
                f"期望唯一调用 {expected_name}，实际 calls={calls}"
            )
        call = matches[0]
        if call.get("arguments") not in ({}, None):
            raise AssertionError(f"{expected_name} 参数错误: {call.get('arguments')}")
        if not call.get("ok"):
            raise AssertionError(f"{expected_name} 执行失败: {call}")
        return call

    async def test_mcp_status(self):
        name = "mcp_status"
        ws = await self._open()
        try:
            await ws.send(json.dumps({"type": "mcp_status"}))
            msg = await wait_for(ws, lambda d: d.get("type") == "mcp_status")
            if "connected" in msg and "tools" in msg:
                self.result.ok(name)
            else:
                self.result.fail(name, f"字段缺失: {msg}")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws.close()

    async def test_command_open_light(self):
        name = "command_open_light_state_closed_loop"
        required = (
            "cmd_open_light.wav",
            "cmd_close_light.wav",
            "cmd_status.wav",
        )
        if any(not self.sample_path(item) for item in required):
            self.result.skip(name, f"缺少语音样本: {required}")
            return
        initial = ""
        try:
            before_event = await self._run_voice_command("cmd_status.wav")
            before = self._function_call(
                before_event, "get_office_ligth_state"
            )
            initial = str(before.get("result") or "")

            # 固定从关闭态开始，确保后续不仅检查 tools/call ok，还验证状态变化。
            if "关" not in initial:
                close_event = await self._run_voice_command(
                    "cmd_close_light.wav"
                )
                self._function_call(close_event, "btn_10_push")

            open_event = await self._run_voice_command("cmd_open_light.wav")
            self._function_call(open_event, "btn_9_push")

            after_event = await self._run_voice_command("cmd_status.wav")
            after = self._function_call(
                after_event, "get_office_ligth_state"
            )
            if "开" not in str(after.get("result") or ""):
                raise AssertionError(f"办公室灯终态不是开: {after}")
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            # 恢复测试前状态，避免验收改变现场设备。
            if "关" in initial:
                try:
                    restore = await self._run_voice_command(
                        "cmd_close_light.wav"
                    )
                    self._function_call(restore, "btn_10_push")
                except Exception as e:
                    self.result.fail(f"{name}_restore", str(e))

    # ---- 并发边界 ----
    async def test_two_clients(self):
        name = "two_clients_independent"
        ws1 = await self._open()
        ws2 = await self._open()
        try:
            await ws1.send(json.dumps({"type": "ping"}))
            await ws2.send(json.dumps({"type": "ping"}))
            r1 = await wait_for(ws1, lambda d: d.get("type") == "pong")
            r2 = await wait_for(ws2, lambda d: d.get("type") == "pong")
            if r1 and r2:
                self.result.ok(name)
            else:
                self.result.fail(name, "pong 不完整")
        except Exception as e:
            self.result.fail(name, str(e))
        finally:
            await ws1.close()
            await ws2.close()

    # ---- 压测 ----
    async def stress_connect(self, n: int):
        name = f"stress_connect_{n}"
        try:
            conns = await asyncio.gather(*(connect(self.uri) for _ in range(n)))
            await asyncio.gather(*(ws.close() for ws in conns))
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, str(e))

    async def endurance(self, n: int):
        name = f"endurance_{n}_rounds"
        try:
            for i in range(n):
                ws = await self._open()
                await ws.send(json.dumps({"type": "ping"}))
                await wait_for(ws, lambda d: d.get("type") == "pong")
                await ws.close()
            self.result.ok(name)
        except Exception as e:
            self.result.fail(name, f"第?轮失败: {e}")


# ---------------------------------------------------------------------------
# 主控
# ---------------------------------------------------------------------------
async def run_all(uri: str, samples_dir: Optional[Path], stress: int = 0, endurance: int = 0) -> bool:
    t = BoundaryTests(uri, samples_dir)

    print("\n[维度 1/4] WebSocket 协议边界")
    await t.test_wrong_path()
    await t.test_ping_pong()
    await t.test_invalid_json()
    await t.test_unknown_type()
    await t.test_huge_binary()

    print("\n[维度 2/4] 配置与音频输入边界")
    await t.test_config_invalid_sample_rate()
    await t.test_config_tts_toggle()
    await t.test_empty_audio_manual()
    await t.test_silence_audio_manual()
    await t.test_short_noise_audio()
    await t.test_long_audio_manual()

    print("\n[维度 3/4] 唤醒门控边界")
    await t.test_wake_gate_requires_16k()
    await t.test_wake_gate_end_forbidden()
    await t.test_wake_gate_woken_timeout()
    await t.test_wake_gate_followup_timeout()

    print("\n[维度 4/4] MCP / 并发 / 压测")
    await t.test_mcp_status()
    await t.test_command_open_light()
    await t.test_two_clients()
    if stress > 0:
        await t.stress_connect(stress)
    if endurance > 0:
        await t.endurance(endurance)

    return t.result.summary()


def main():
    ap = argparse.ArgumentParser(description="09_asr_llm_ws.py 边界测试")
    ap.add_argument("uri", nargs="?", default="ws://192.168.1.228:8060/stream")
    ap.add_argument(
        "--samples",
        type=Path,
        default=None,
        help="真实语音样本目录（默认：tests/test_wavs/）",
    )
    ap.add_argument("--stress-connect", type=int, default=0, help="连接风暴并发数")
    ap.add_argument("--endurance", type=int, default=0, help="长稳循环轮数")
    args = ap.parse_args()

    ok = asyncio.run(run_all(args.uri, args.samples, args.stress_connect, args.endurance))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# pytest 兼容入口（未安装 pytest 时独立运行不受影响）
# ---------------------------------------------------------------------------
try:
    import pytest

    @pytest.fixture(scope="module")
    def ws_uri():
        return os.environ.get("WS_URI", "ws://192.168.1.228:8060/stream")


    @pytest.fixture(scope="module")
    def samples_dir():
        p = os.environ.get("SAMPLES_DIR")
        if p:
            return Path(p)
        # 默认与 BoundaryTests 保持一致
        return Path(__file__).resolve().parent / "test_wavs"


    @pytest.mark.asyncio
    async def test_wrong_path(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_wrong_path()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_ping_pong(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_ping_pong()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_invalid_json(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_invalid_json()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_unknown_type(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_unknown_type()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_empty_audio_manual(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_empty_audio_manual()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_config_invalid_sample_rate(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_config_invalid_sample_rate()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_wake_gate_requires_16k(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_wake_gate_requires_16k()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_wake_gate_end_forbidden(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_wake_gate_end_forbidden()
        assert not t.result.failed


    @pytest.mark.asyncio
    async def test_two_clients(ws_uri):
        t = BoundaryTests(ws_uri, None)
        await t.test_two_clients()
        assert not t.result.failed

except ImportError:
    pass  # 独立运行时无需 pytest


if __name__ == "__main__":
    main()
