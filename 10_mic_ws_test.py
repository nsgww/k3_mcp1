import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pyaudio
import websockets


RATE = 16000
CHUNK = 1024  # 64ms @ 16kHz
DEFAULT_URI = "ws://192.168.1.228:8060/stream"
# 板端 MeloTTS 单句 decode 约 20s×多片，整段常要 2～4 分钟；用「空闲超时」而非总时长硬切
IDLE_TIMEOUT = float(os.getenv("REPLY_IDLE_TIMEOUT", "300"))  # 多久没新消息才判超时
OVERALL_TIMEOUT = float(os.getenv("REPLY_TIMEOUT", "600"))  # 单轮总上限


def list_devices() -> None:
    pa = pyaudio.PyAudio()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"  [{i}] {info['name']} (默认采样率 {int(info['defaultSampleRate'])})")
    pa.terminate()


def _play_wav_bytes(data: bytes) -> None:
    """临时落盘并播放。Windows 上 playsound 常秒退无声，优先 winsound / PyAudio。"""
    import wave

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    path = str(Path(path).resolve())
    # 默认保存到项目下 tts_out/；设 TTS_SAVE_DIR=0 可关闭
    save_dir = os.getenv("TTS_SAVE_DIR", "tts_out").strip()
    if save_dir and save_dir not in {"0", "false", "off", "no"}:
        try:
            out_dir = Path(save_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            keep = out_dir / f"tts_{stamp}.wav"
            keep.write_bytes(data)
            print(f"[TTS] saved {keep}")
        except Exception as e:
            print(f"[TTS] save failed: {e}")

    try:
        Path(path).write_bytes(data)
        with wave.open(path, "rb") as wf:
            nch, sw, sr, nframes = (
                wf.getnchannels(),
                wf.getsampwidth(),
                wf.getframerate(),
                wf.getnframes(),
            )
        dur = (nframes / float(sr)) if sr else 0.0
        print(f"[TTS] wav {sr}Hz {nch}ch {sw*8}bit ~{dur:.1f}s → playing...")

        errors: list[str] = []

        # 1) Windows 系统 API（同步，可靠）
        if sys.platform == "win32":
            try:
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME)
                print("[TTS] played via winsound")
                return
            except Exception as e:
                errors.append(f"winsound: {e}")

        # 2) PyAudio 直接推流
        try:
            with wave.open(path, "rb") as wf:
                pa = pyaudio.PyAudio()
                stream = pa.open(
                    format=pa.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True,
                )
                chunk = 1024
                while True:
                    frames = wf.readframes(chunk)
                    if not frames:
                        break
                    stream.write(frames)
                stream.stop_stream()
                stream.close()
                pa.terminate()
            print("[TTS] played via pyaudio")
            return
        except Exception as e:
            errors.append(f"pyaudio: {e}")

        # 3) playsound（部分环境会秒退且不报错，放最后）
        try:
            from playsound import playsound

            t0 = time.monotonic()
            playsound(path)
            elapsed = time.monotonic() - t0
            if dur > 1.0 and elapsed < 0.5:
                raise RuntimeError(
                    f"playsound returned too fast ({elapsed:.2f}s << {dur:.1f}s)"
                )
            print(f"[TTS] played via playsound ({elapsed:.1f}s)")
            return
        except Exception as e:
            errors.append(f"playsound: {e}")

        raise RuntimeError("; ".join(errors) or "no playback backend")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class _TurnPrinter:
    """打印一轮 WS 回复；仅以 type=done 结束本轮（且本轮已有实质结果）。"""

    def __init__(self, round_no: int) -> None:
        self.round_no = round_no
        self._llm_started = False
        # 本轮是否已收到 asr/function/llm/error/kws；用于丢弃上一轮残留的 done
        self._got_result = False
        # 期待下一条 binary 为 TTS wav
        self.expect_tts_bytes = 0
        self._tts_meta: dict = {}

    def feed(self, d: dict) -> bool:
        """返回 True 表示本轮结束，可开始下一轮。"""
        mtype = d.get("type")

        if mtype == "status":
            msg = d.get("message", "")
            extra = ""
            if "mcp_connected" in d:
                extra = f" | mcp={d.get('mcp_connected')} tools={d.get('mcp_tools')}"
            if "tts_enabled" in d:
                extra += f" | tts={d.get('tts_enabled')}"
            print(f"[status] {msg}{extra}")
            return False

        if mtype == "mcp_status":
            print(f"[mcp] connected={d.get('connected')} tools={d.get('tools')}")
            return False

        if mtype == "kws":
            self._got_result = True
            print(
                f"\n[KWS] {d.get('message') or ('检测到关键词:' + str(d.get('keyword', '')))}"
            )
            return False

        if mtype == "asr":
            self._got_result = True
            print(f"\n[R{self.round_no}|ASR] {d.get('text', '')}")
            return False

        if mtype == "llm":
            text = d.get("text") or ""
            done = bool(d.get("done"))
            if done and not text:
                if self._llm_started:
                    print()
                    print(
                        f"[R{self.round_no}] 文本已齐，等待 TTS"
                        f"（板端合成较慢，空闲超时 {IDLE_TIMEOUT:.0f}s）...",
                        flush=True,
                    )
                return False
            if not text and not done:
                return False
            self._got_result = True
            if not self._llm_started:
                print(f"\n[R{self.round_no}|LLM] ", end="", flush=True)
                self._llm_started = True
            print(text, end="", flush=True)
            return False

        if mtype == "function":
            self._got_result = True
            source = d.get("source") or ""
            name = d.get("name") or ""
            if source == "mcp" or str(name).startswith("mcp__"):
                kind = "MCP"
            else:
                kind = "FUNC"
            print(
                f"\n[R{self.round_no}|{kind}] {name} "
                f"ok={d.get('ok')} args={d.get('arguments')} "
                f"result={d.get('result') or d.get('error') or ''}"
            )
            return False

        if mtype == "tts":
            # 元数据：下一条 binary 为 wav
            self.expect_tts_bytes = int(d.get("byte_length") or 0)
            self._tts_meta = d
            print(
                f"\n[R{self.round_no}|TTS] sr={d.get('sample_rate')} "
                f"bytes={self.expect_tts_bytes} text={d.get('text', '')[:60]!r}"
            )
            return False

        if mtype == "error":
            self._got_result = True
            print(f"\n[R{self.round_no}|ERROR] {d.get('message', '')}")
            return False

        if mtype == "pong":
            print("[pong]")
            return False

        if mtype == "done":
            if not self._got_result:
                return False
            print(f"\n[R{self.round_no}|done]\n")
            return True

        print(f"\n[?] {d}")
        return False

    def feed_binary(self, data: bytes) -> None:
        if self.expect_tts_bytes <= 0:
            print(f"\n[bin?] unexpected {len(data)} bytes")
            return
        print(f"[TTS] playing {len(data)} bytes ...")
        try:
            _play_wav_bytes(data)
        except Exception as e:
            print(f"[TTS] play failed: {e}")
        finally:
            self.expect_tts_bytes = 0
            self._tts_meta = {}


async def drain_pending(ws, timeout: float = 0.3) -> None:
    """清掉连接后积压的欢迎 status，避免掺进第一轮。"""
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        if isinstance(msg, bytes):
            continue
        try:
            d = json.loads(msg)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "status":
            print(
                f"[status] {d.get('message', '')} "
                f"| mcp={d.get('mcp_connected')} tools={d.get('mcp_tools')} "
                f"| tts={d.get('tts_enabled')}"
            )
        else:
            print(f"[recv] {d}")


async def record_until_enter(ws, pa: pyaudio.PyAudio) -> None:
    """按回车开始→录音推流→再按回车结束并发送 end。"""
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    stop = asyncio.Event()

    async def wait_stop() -> None:
        await asyncio.to_thread(input)
        stop.set()

    async def pump() -> None:
        while not stop.is_set():
            data = await asyncio.to_thread(stream.read, CHUNK, False)
            await ws.send(data)

    print("  [录音中] 说完再按回车结束本轮...")
    stopper = asyncio.create_task(wait_stop())
    pumper = asyncio.create_task(pump())
    try:
        await stop.wait()
        await pumper
    finally:
        stopper.cancel()
        stream.stop_stream()
        stream.close()

    await ws.send(json.dumps({"type": "end"}))
    print("  [已发送 end，等待回复]")


async def wait_reply(ws, round_no: int) -> None:
    """等待本轮直到 done；每收到一包刷新空闲计时（适配慢速 TTS）。"""
    printer = _TurnPrinter(round_no)
    deadline = time.monotonic() + OVERALL_TIMEOUT
    idle_deadline = time.monotonic() + IDLE_TIMEOUT

    while True:
        now = time.monotonic()
        if now >= deadline:
            print(
                f"\n[R{round_no}|TIMEOUT] 总时长超过 {OVERALL_TIMEOUT:.0f}s，进入下一轮\n"
            )
            await drain_pending(ws, timeout=1.0)
            return
        wait = min(idle_deadline - now, deadline - now)
        if wait <= 0:
            print(
                f"\n[R{round_no}|TIMEOUT] {IDLE_TIMEOUT:.0f}s 内无新消息"
                f"（多半还在板端 TTS），进入下一轮\n"
            )
            await drain_pending(ws, timeout=1.0)
            return
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=wait)
        except asyncio.TimeoutError:
            print(
                f"\n[R{round_no}|TIMEOUT] {IDLE_TIMEOUT:.0f}s 内无新消息"
                f"（多半还在板端 TTS），进入下一轮\n"
            )
            await drain_pending(ws, timeout=1.0)
            return

        idle_deadline = time.monotonic() + IDLE_TIMEOUT
        if isinstance(msg, bytes):
            await asyncio.to_thread(printer.feed_binary, msg)
            continue
        try:
            d = json.loads(msg)
        except json.JSONDecodeError:
            print(f"\n[RAW] {msg!r}")
            continue
        if printer.feed(d):
            return


async def one_turn(ws, pa: pyaudio.PyAudio, round_no: int) -> None:
    """一轮完整对话。"""
    print(f"===== 第 {round_no} 轮 =====")
    await record_until_enter(ws, pa)
    await wait_reply(ws, round_no)


async def main(uri: str) -> None:
    pa = pyaudio.PyAudio()
    round_no = 0
    try:
        async with websockets.connect(uri, ping_interval=None, max_size=16 * 1024 * 1024) as ws:
            print(f"已连接 {uri}")
            await drain_pending(ws)
            print("多轮对话: 回车开始说话 → 说完再回车结束 → 等回复 → 再回车开下一轮")
            print("输入 q 回车退出\n")

            while True:
                cmd = await asyncio.to_thread(input, ">>> 回车开始下一轮 (q 退出): ")
                if cmd.strip().lower() in {"q", "quit", "exit"}:
                    print("再见")
                    break
                round_no += 1
                try:
                    await one_turn(ws, pa, round_no)
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"[连接断开: {e}] 请重启服务后重试")
                    break
    finally:
        pa.terminate()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if "--devices" in args:
        list_devices()
        sys.exit(0)
    uri = args[0] if args else DEFAULT_URI
    try:
        asyncio.run(main(uri))
    except KeyboardInterrupt:
        print("\n已退出")
