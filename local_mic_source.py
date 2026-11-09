"""板端本地麦克风 PCM 源。

用法（在事件循环里）：
    src = LocalMicSource(device_index=0, rate=16000, chunk_samples=512)
    await src.start()
    async for frame in src.frames():
        ...  # frame 为 16kHz int16 mono PCM bytes
    await src.stop()

环境变量默认值：
  LOCAL_MIC_DEVICE_INDEX   输入设备索引（默认 0）
  LOCAL_MIC_RATE           采样率（默认 16000）
  LOCAL_MIC_CHUNK_SAMPLES  每次读取采样数（默认 512，对应 32ms@16k）
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

import numpy as np
import pyaudio
from speex_resample import SpeexResampler

def find_first_input_device(rate: int = 16000, channels: int = 1, chunk: int = 512) -> int:
    """扫描并返回能用指定参数打开的音频输入设备索引，找不到则返回 0。"""
    try:
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if int(info.get("maxInputChannels", 0)) <= 0:
                    continue
                try:
                    test_stream = pa.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        input=True,
                        input_device_index=i,
                        frames_per_buffer=chunk,
                    )
                    test_stream.close()
                    print(f"[local_mic] auto-selected input device {i}: {info.get('name')}")
                    return i
                except Exception:
                    continue
        finally:
            pa.terminate()
    except Exception as e:
        print(f"[local_mic] device scan failed: {e}")
    return 0


_DEFAULT_DEVICE_INDEX_STR = os.getenv("LOCAL_MIC_DEVICE_INDEX", "0")
if _DEFAULT_DEVICE_INDEX_STR.lower() in ("auto", ""):
    DEFAULT_DEVICE_INDEX = find_first_input_device()
else:
    DEFAULT_DEVICE_INDEX = int(_DEFAULT_DEVICE_INDEX_STR)
DEFAULT_RATE = int(os.getenv("LOCAL_MIC_RATE", "16000"))
DEFAULT_CHUNK_SAMPLES = int(os.getenv("LOCAL_MIC_CHUNK_SAMPLES", "512"))


class LocalMicSource:
    """pyaudio → 16k int16 mono PCM 帧流。"""

    def __init__(
        self,
        device_index: Optional[int] = None,
        rate: Optional[int] = None,
        chunk_samples: Optional[int] = None,
    ) -> None:
        self.device_index = int(
            device_index if device_index is not None else DEFAULT_DEVICE_INDEX
        )
        self.rate = int(rate if rate is not None else DEFAULT_RATE)
        self.chunk_samples = int(
            chunk_samples if chunk_samples is not None else DEFAULT_CHUNK_SAMPLES
        )
        self.channels = 1

        self._queue: Optional[asyncio.Queue] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None
        self._resampler: Optional[SpeexResampler] = None

    async def start(self) -> None:
        """在后台线程打开麦克风并开始采音。"""
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=100)
        self._stop_event.clear()

        # pyaudio 初始化可能阻塞，放到线程里执行
        await loop.run_in_executor(None, self._open_stream)

        if self.rate != 16000:
            self._resampler = SpeexResampler(self.rate, 16000)

        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _open_stream(self) -> None:
        """同步打开 pyaudio 输入流。"""
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk_samples,
                input_device_index=self.device_index,
            )
        except Exception as e:
            pa.terminate()
            raise RuntimeError(
                f"无法打开本地麦克风 device={self.device_index} rate={self.rate}: {e}"
            ) from e
        self._pa = pa
        self._stream = stream

    def _read_loop(self) -> None:
        """后台线程：持续读麦克风并往 asyncio.Queue 放帧。"""
        try:
            stream = self._stream
            assert stream is not None
            while not self._stop_event.is_set():
                try:
                    data = stream.read(self.chunk_samples, exception_on_overflow=False)
                except Exception as e:
                    print(f"[local_mic] 读取失败: {e}")
                    break
                if self._stop_event.is_set():
                    break
                if not data:
                    continue

                try:
                    pcm16 = np.frombuffer(data, dtype=np.int16)
                    if self._resampler is not None:
                        pcm16 = self._resampler.process_float(pcm16.astype(np.float32))
                        pcm16 = np.clip(pcm16, -32768, 32767).astype(np.int16)
                    frame_bytes = pcm16.tobytes()
                except Exception as e:
                    print(f"[local_mic] 重采样失败: {e}")
                    continue

                try:
                    self._queue.put_nowait(frame_bytes)
                except asyncio.QueueFull:
                    # 队满则丢最旧帧，保持低延迟
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._queue.put_nowait(frame_bytes)
                    except asyncio.QueueFull:
                        pass
        finally:
            try:
                if self._stream is not None:
                    self._stream.stop_stream()
                    self._stream.close()
            except Exception:
                pass
            if self._pa is not None:
                self._pa.terminate()

    async def frames(self):
        """async generator，yield 16k int16 mono PCM bytes。"""
        if self._queue is None:
            raise RuntimeError("LocalMicSource 未 start")
        while True:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if self._stop_event.is_set():
                    break
                continue
            if frame is None:
                break
            yield frame

    async def stop(self) -> None:
        """停止采音并释放资源。"""
        self._stop_event.set()
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._resampler is not None:
            self._resampler.close()
            self._resampler = None
