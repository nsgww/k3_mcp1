"""
sherpa-onnx 关键词检测（KWS）。

与 ASR→MCP/LLM 并行：对同一路上行 PCM 实时喂帧，命中即返回关键词，
不拦截、不替代后续 ASR 流程。

模型目录默认:
  /var/lib/sm-sdk/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import numpy as np

_DEFAULT_DIR = Path(
    "/var/lib/sm-sdk/models/kws/"
    "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
)

# 板端优先 int8；不存在则回退 fp32
_ENC = "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEC = "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_JOIN = "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_ENC_FP32 = "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
_DEC_FP32 = "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
_JOIN_FP32 = "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"


def _pick_model_files(model_dir: Path) -> tuple[Path, Path, Path, Path, Path]:
    tokens = model_dir / "tokens.txt"
    keywords = model_dir / "keywords.txt"
    enc = model_dir / _ENC
    dec = model_dir / _DEC
    joiner = model_dir / _JOIN
    if not enc.is_file():
        enc = model_dir / _ENC_FP32
        dec = model_dir / _DEC_FP32
        joiner = model_dir / _JOIN_FP32
    for p in (tokens, keywords, enc, dec, joiner):
        if not p.is_file():
            raise FileNotFoundError(f"KWS 缺少模型文件: {p}")
    return tokens, keywords, enc, dec, joiner


def _normalize_keyword(raw: str) -> str:
    """sherpa 结果可能是 '小爱同学' 或带多余空白。"""
    text = (raw or "").strip()
    if not text:
        return ""
    # 有的版本带时间戳/前缀，取最后一段非空
    if "@" in text:
        text = text.split("@")[-1].strip()
    return text


class KwsSession:
    """单路连接的 KWS 流；与共享 KeywordSpotter 配合使用。"""

    def __init__(self, engine: "KwsEngine"):
        self._engine = engine
        self._stream = engine._kws.create_stream()

    def accept_pcm16(self, pcm_bytes: bytes, sample_rate: int = 16000) -> List[str]:
        """
        喂入 int16 PCM，返回本批新检测到的关键词列表（可能为空）。
        命中后会 reset_stream，便于连续检测。
        """
        if not pcm_bytes or self._engine._kws is None:
            return []
        audio = np.frombuffer(pcm_bytes, dtype=np.int16)
        if audio.size == 0:
            return []
        samples = audio.astype(np.float32) / 32768.0
        # KWS 模型固定 16 kHz
        if sample_rate != 16000 and samples.size > 0:
            try:
                from speex_resample import resample

                samples = resample(samples, sample_rate, 16000)
                sample_rate = 16000
            except Exception:
                sample_rate = 16000
        return self._engine._decode(self._stream, samples, sample_rate)

    def reset(self) -> None:
        if self._engine._kws is not None:
            self._engine._kws.reset_stream(self._stream)


class KwsEngine:
    """进程级 KWS 引擎（多连接共享模型，每连接一个 Session）。"""

    def __init__(
        self,
        model_dir: Optional[str] = None,
        *,
        num_threads: int = 1,
        keywords_score: float = 1.0,
        keywords_threshold: float = 0.25,
        provider: str = "cpu",
    ):
        self.enabled = False
        self._kws = None
        self.model_dir = Path(
            model_dir or os.getenv("KWS_MODEL_DIR", str(_DEFAULT_DIR))
        )
        try:
            import sherpa_onnx
        except Exception as e:
            # 包已安装但 .so / onnxruntime 版本不匹配时也常抛 ImportError
            print(f"[KWS] import sherpa_onnx 失败，关键词检测已禁用: {type(e).__name__}: {e}")
            err = str(e)
            if "VERS_" in err or "onnxruntime" in err.lower():
                print(
                    "[KWS] 原因多为 onnxruntime 与 sherpa_onnx 版本不匹配 "
                    "(sherpa 需要 VERS_1.24.2；系统 spacemit ORT 常为 1.24.0)。\n"
                    "  正确顺序: 先 KWS/VITS(sherpa)，再 ASR（见 09_asr_llm_ws.py）。\n"
                    "  排查: python -c \"import onnxruntime as ort; print(ort.__version__, ort.__file__)\""
                )
            else:
                print(
                    '[KWS] 排查: python -c "import sherpa_onnx; print(sherpa_onnx.__file__)"'
                )
            return

        try:
            tokens, keywords, enc, dec, joiner = _pick_model_files(self.model_dir)
            print(f"[KWS] model_dir={self.model_dir}")
            print(f"[KWS] encoder={enc.name}")
            self._kws = sherpa_onnx.KeywordSpotter(
                tokens=str(tokens),
                encoder=str(enc),
                decoder=str(dec),
                joiner=str(joiner),
                keywords_file=str(keywords),
                num_threads=num_threads,
                keywords_score=keywords_score,
                keywords_threshold=keywords_threshold,
                provider=provider,
            )
            self.enabled = True
            print(f"[KWS] ready: {self.model_dir.name}")
        except Exception as e:
            print(f"[KWS] 初始化失败，已禁用: {type(e).__name__}: {e}")
            self._kws = None
            self.enabled = False

    def create_session(self) -> Optional[KwsSession]:
        if not self.enabled or self._kws is None:
            return None
        return KwsSession(self)

    def _decode(self, stream, samples: np.ndarray, sample_rate: int) -> List[str]:
        assert self._kws is not None
        hit: List[str] = []
        stream.accept_waveform(sample_rate, samples)
        while self._kws.is_ready(stream):
            self._kws.decode_stream(stream)
            result = self._kws.get_result(stream)
            if not result:
                continue
            # get_result 可能是 str 或带 .keyword 的对象
            if hasattr(result, "keyword"):
                raw = str(getattr(result, "keyword") or "")
            else:
                raw = str(result)
            kw = _normalize_keyword(raw)
            if kw:
                hit.append(kw)
                # 官方要求：检出后立即 reset
                self._kws.reset_stream(stream)
        return hit


def format_kws_message(keyword: str) -> str:
    return f"检测到关键词:{keyword}"


def load_keyword_phrases(model_dir: Optional[Path] = None) -> list[str]:
    """从 keywords_raw.txt 读取中文唤醒词列表。"""
    d = Path(model_dir or os.getenv("KWS_MODEL_DIR", str(_DEFAULT_DIR)))
    raw = d / "keywords_raw.txt"
    if not raw.is_file():
        return []
    out = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        w = line.strip()
        if w and not w.startswith("#"):
            out.append(w)
    return out


def _word_to_tokens(word: str) -> str:
    """中文词 → sherpa keywords.txt 拼音 token 行（x iǎo 形式）。"""
    from pypinyin import Style, pinyin

    parts: list[str] = []
    for ch in word:
        if "一" <= ch <= "鿿":
            ini = pinyin(ch, style=Style.INITIALS, strict=False)[0][0]
            fin = pinyin(ch, style=Style.FINALS_TONE, strict=False)[0][0]
            parts.extend([p for p in (ini, fin) if p])
        elif ch.strip():
            parts.append(ch)
    return " ".join(parts)


def apply_keywords(
    model_dir: Optional[Path] = None,
    *,
    content: Optional[str] = None,
    raw_content: Optional[str] = None,
) -> int:
    """替换 keywords.txt（联动 keywords_raw.txt），返回词条数。

    - content: sherpa 原生格式（每行 ``p y @词``），原样写入
    - raw_content: 纯中文词一行一个，自动生成拼音行
    """
    d = Path(model_dir or os.getenv("KWS_MODEL_DIR", str(_DEFAULT_DIR)))
    kw_file = d / "keywords.txt"
    raw_file = d / "keywords_raw.txt"
    if content is not None:
        lines = [l for l in content.splitlines() if l.strip()]
        bad = [l for l in lines if "@" not in l]
        if bad:
            raise ValueError(f"keywords.txt 格式错误（缺少 @）: {bad[0]!r}")
        if not lines:
            raise ValueError("content 为空")
        kw_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        phrases = [l.split("@", 1)[1].strip() for l in lines]
        raw_file.write_text("\n".join(phrases) + "\n", encoding="utf-8")
        return len(lines)
    if raw_content is not None:
        words = [
            l.strip()
            for l in raw_content.splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        if not words:
            raise ValueError("raw_content 为空")
        lines = [f"{_word_to_tokens(w)} @{w}" for w in words]
        kw_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        raw_file.write_text("\n".join(words) + "\n", encoding="utf-8")
        return len(words)
    raise ValueError("content 与 raw_content 至少给一个")


def normalize_utterance(text: str) -> str:
    """去掉 ASR 杂质，便于和唤醒词比对。"""
    if not text:
        return ""
    t = text.strip()
    for ch in ("😔", "。", "！", "？", "!", "?", " ", "\t", "\n"):
        t = t.replace(ch, "")
    return t


def match_wake_keyword(
    asr_text: str,
    kws_hits: Optional[list[str]] = None,
    phrases: Optional[list[str]] = None,
) -> Optional[str]:
    """
    判断本轮是否「只说了唤醒词」。
    优先用 KWS 命中；KWS 漏检时用 ASR 全文精确匹配 keywords_raw。
    """
    phrases = phrases if phrases is not None else load_keyword_phrases()
    phrase_set = set(phrases)
    norm = normalize_utterance(asr_text)

    if norm in phrase_set:
        return norm

    for h in kws_hits or []:
        hn = normalize_utterance(h)
        if not hn:
            continue
        if norm == hn or norm == hn + hn:  # 偶发叠词
            return hn
        # ASR 几乎只有唤醒词（允许前后极短噪声字）
        if hn in norm and len(norm) <= len(hn) + 2:
            return hn
    return None


def strip_wake_prefix(
    text: str,
    phrases: Optional[list[str]] = None,
) -> Optional[tuple[str, str]]:
    """
    ASR 文本以唤醒词开头时返回 (keyword, 剩余文本)，否则 None。
    用于端侧唤醒（方案 B）连说场景：「你好军哥打开客厅灯」→「打开客厅灯」。
    长词优先，避免「你好军哥」被「你好」类短词截断。
    """
    phrases = phrases if phrases is not None else load_keyword_phrases()
    norm = normalize_utterance(text)
    if not norm:
        return None
    for p in sorted(phrases, key=len, reverse=True):
        pn = normalize_utterance(p)
        if pn and norm.startswith(pn):
            return pn, norm[len(pn):]
    return None


# 09 启动时按需创建
def build_kws_engine_from_env() -> Optional[KwsEngine]:
    flag = os.getenv("KWS_ENABLED", "1").strip().lower()
    if flag in {"0", "false", "off", "no"}:
        print("[KWS] KWS_ENABLED=0，跳过加载")
        return None
    eng = KwsEngine(
        num_threads=int(os.getenv("KWS_NUM_THREADS", "1")),
        keywords_score=float(os.getenv("KWS_SCORE", "1.0")),
        keywords_threshold=float(os.getenv("KWS_THRESHOLD", "0.25")),
    )
    return eng if eng.enabled else None
