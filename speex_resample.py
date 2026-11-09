"""基于系统 libspeexdsp 的重采样封装（ctypes），替代 scipy.signal.resample。

依赖：系统已安装 libspeexdsp 运行库（/usr/lib/riscv64-linux-gnu/libspeexdsp.so.1）。
"""
import ctypes

import numpy as np

_lib = ctypes.CDLL("libspeexdsp.so.1")

_lib.speex_resampler_init.restype = ctypes.c_void_p
_lib.speex_resampler_init.argtypes = [
    ctypes.c_uint32,  # nb_channels
    ctypes.c_uint32,  # in_rate
    ctypes.c_uint32,  # out_rate
    ctypes.c_int,     # quality (0-10)
    ctypes.POINTER(ctypes.c_int),
]
_lib.speex_resampler_process_float.restype = ctypes.c_int
_lib.speex_resampler_process_float.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,  # channel index
    ctypes.POINTER(ctypes.c_float),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_float),
    ctypes.POINTER(ctypes.c_uint32),
]
_lib.speex_resampler_destroy.argtypes = [ctypes.c_void_p]

_F32 = ctypes.POINTER(ctypes.c_float)


def resample(samples: np.ndarray, in_rate: int, out_rate: int, quality: int = 5) -> np.ndarray:
    """单声道音频重采样，返回 float32（幅度保持不变）。"""
    x = np.ascontiguousarray(samples, dtype=np.float32)
    if x.size == 0 or int(in_rate) == int(out_rate):
        return x
    err = ctypes.c_int(0)
    state = _lib.speex_resampler_init(1, int(in_rate), int(out_rate), int(quality), ctypes.byref(err))
    if not state:
        raise RuntimeError(f"speex_resampler_init failed, err={err.value}")
    try:
        y = np.empty(int(round(x.size * float(out_rate) / in_rate)) + 64, dtype=np.float32)
        in_len = ctypes.c_uint32(x.size)
        out_len = ctypes.c_uint32(y.size)
        ret = _lib.speex_resampler_process_float(
            state, 0,
            x.ctypes.data_as(_F32), ctypes.byref(in_len),
            y.ctypes.data_as(_F32), ctypes.byref(out_len),
        )
        if ret != 0:
            raise RuntimeError(f"speex_resampler_process_float failed, err={ret}")
        return y[: out_len.value]
    finally:
        _lib.speex_resampler_destroy(state)


class SpeexResampler:
    """持久态单声道重采样器（跨包保持状态，避免逐包 init/destroy 的边缘伪影）。

    供组播音源等连续流场景使用；一次性转换仍用上面的 resample()。
    """

    def __init__(self, in_rate: int, out_rate: int, quality: int = 5) -> None:
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        err = ctypes.c_int(0)
        self._state = _lib.speex_resampler_init(
            1, self.in_rate, self.out_rate, int(quality), ctypes.byref(err)
        )
        if not self._state:
            raise RuntimeError(f"speex_resampler_init failed, err={err.value}")

    def process_float(self, samples: np.ndarray) -> np.ndarray:
        """float32 单声道 → float32，幅度保持不变。"""
        x = np.ascontiguousarray(samples, dtype=np.float32)
        if x.size == 0:
            return x
        y = np.empty(int(round(x.size * float(self.out_rate) / self.in_rate)) + 64, dtype=np.float32)
        in_len = ctypes.c_uint32(x.size)
        out_len = ctypes.c_uint32(y.size)
        ret = _lib.speex_resampler_process_float(
            self._state, 0,
            x.ctypes.data_as(_F32), ctypes.byref(in_len),
            y.ctypes.data_as(_F32), ctypes.byref(out_len),
        )
        if ret != 0:
            raise RuntimeError(f"speex_resampler_process_float failed, err={ret}")
        return y[: out_len.value]

    def close(self) -> None:
        if getattr(self, "_state", None):
            _lib.speex_resampler_destroy(self._state)
            self._state = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
