"""MiMo 云端 ASR：分块上传、重试、二分降级、合并转写结果。"""
import base64
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from audio import AudioChunk, slice_audio
from config import Settings

ProgressCb = Callable[[float, str], None]


class AsrError(Exception):
    """ASR 转写失败。"""


class AsrAuthError(AsrError):
    """API Key 无效或无权限。"""


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class AsrResult:
    text: str = ""
    segments: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    partial: bool = False


def _data_url(path: str) -> str:
    raw = Path(path).read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return "data:audio/mpeg;base64," + encoded


def _build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    # 忽略系统/环境代理；只有显式配置时才使用代理
    session.trust_env = False
    if settings.mimo_proxy:
        session.proxies = {"http": settings.mimo_proxy, "https": settings.mimo_proxy}
    return session


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _extract_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise AsrError("ASR 响应缺少 choices 字段。")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    if content is None:
        return ""
    return str(content).strip()


def _post_once(session: requests.Session, settings: Settings,
               data_url: str, language: str) -> str:
    url = settings.mimo_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "api-key": settings.mimo_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.mimo_asr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio",
                     "input_audio": {"data": data_url}},
                ],
            }
        ],
        "asr_options": {"language": language or "auto"},
        "max_tokens": 4096,
        "stream": False,
    }

    try:
        response = session.post(url, headers=headers, json=payload, timeout=(10, 600))
    except requests.exceptions.Timeout as exc:
        raise _Retryable("ASR 请求超时。") from exc
    except requests.exceptions.RequestException as exc:
        raise _Retryable(f"网络错误：{exc}") from exc

    status = response.status_code
    if status in (401, 403):
        raise AsrAuthError("MiMo API Key 无效或无权限，请检查 .env 中的 MIMO_API_KEY。")
    if status == 429 or 500 <= status < 600:
        raise _Retryable(
            f"ASR 服务返回 HTTP {status}",
            _parse_retry_after(response.headers.get("Retry-After")),
        )
    if status >= 400:
        raise AsrError(f"ASR 请求失败（HTTP {status}）：{response.text[:200]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise AsrError("ASR 响应不是合法 JSON：" + response.text[:200]) from exc

    choices = data.get("choices") or []
    finish_reason = choices[0].get("finish_reason") if choices else None
    text = _extract_content(data)
    if finish_reason == "length":
        # 输出被 token 上限截断，交给上层对该段做二分重试
        raise AsrError("ASR 输出被截断，音频分段过长。")
    return text


def _request_with_retry(session: requests.Session, settings: Settings,
                        data_url: str, language: str, max_retries: int = 4) -> str:
    for attempt in range(max_retries + 1):
        try:
            return _post_once(session, settings, data_url, language)
        except _Retryable as exc:
            if attempt >= max_retries:
                raise AsrError(f"ASR 多次重试后仍失败：{exc}") from exc
            base = (1.0, 2.0, 4.0, 8.0)[min(attempt, 3)]
            wait = exc.retry_after
            if wait is None:
                wait = base + random.uniform(0.0, base * 0.5)
            time.sleep(max(0.5, wait))
    raise AsrError("ASR 重试次数耗尽。")


def _split_for_retry(chunk: AudioChunk, ffmpeg: str) -> List[AudioChunk]:
    """把一块音频二分，用于失败后的降级重试。"""
    mid = chunk.start + chunk.duration / 2.0
    parent = Path(chunk.path).parent
    first = AudioChunk(index=chunk.index, start=chunk.start, end=mid,
                       path=str(parent / f"retry_{chunk.index}_a.mp3"))
    second = AudioChunk(index=chunk.index, start=mid, end=chunk.end,
                        path=str(parent / f"retry_{chunk.index}_b.mp3"))
    halves: List[AudioChunk] = []
    if slice_audio(ffmpeg, chunk.path, 0.0, first.duration, first.path):
        halves.append(first)
    if slice_audio(ffmpeg, chunk.path, mid - chunk.start, second.duration, second.path):
        halves.append(second)
    return halves


def _transcribe_chunk(chunk: AudioChunk, session: requests.Session, settings: Settings,
                      language: str, warnings: List[str]) -> List[Dict[str, Any]]:
    """转写单块；失败则二分重试一次。返回 segments 列表。"""
    try:
        text = _request_with_retry(session, settings, _data_url(chunk.path), language)
        if text:
            return [{"start": round(chunk.start, 2),
                     "end": round(chunk.end, 2),
                     "text": text}]
        warnings.append(f"第 {chunk.index + 1} 段未识别到语音内容。")
        return []
    except AsrAuthError:
        raise
    except AsrError as exc:
        halves = _split_for_retry(chunk, settings.ffmpeg_bin)
        if not halves:
            warnings.append(f"第 {chunk.index + 1} 段转写失败：{exc}")
            return []

        segments: List[Dict[str, Any]] = []
        failed = 0
        for half in halves:
            try:
                text = _request_with_retry(session, settings,
                                           _data_url(half.path), language)
                if text:
                    segments.append({"start": round(half.start, 2),
                                     "end": round(half.end, 2),
                                     "text": text})
            except AsrAuthError:
                raise
            except AsrError as half_exc:
                failed += 1
                warnings.append(
                    f"第 {chunk.index + 1} 段的一部分转写失败（{half.start:.0f}s-"
                    f"{half.end:.0f}s）：{half_exc}")
        if failed == len(halves):
            warnings.append(f"第 {chunk.index + 1} 段二分重试后仍然失败。")
        return segments


def transcribe(chunks: List[AudioChunk], settings: Settings, language: str = "auto",
               progress_cb: Optional[ProgressCb] = None) -> AsrResult:
    """逐块调用 MiMo ASR，按时间偏移合并。"""
    if not chunks:
        return AsrResult()

    if not settings.mimo_api_key:
        raise AsrAuthError(
            "未配置 MIMO_API_KEY，无法进行语音转写。请复制 .env.example 为 .env 并填写 API Key。")

    session = _build_session(settings)
    result = AsrResult()
    total = len(chunks)
    complete_count = 0

    try:
        for i, chunk in enumerate(chunks):
            if progress_cb:
                progress_cb(i / total, f"正在转写音频（{i + 1}/{total}）…")
            segments = _transcribe_chunk(chunk, session, settings, language, result.warnings)
            if segments:
                complete_count += 1
                result.segments.extend(segments)
            if progress_cb:
                progress_cb((i + 1) / total, f"已转写 {i + 1}/{total} 段")
    finally:
        session.close()

    texts = [seg["text"].strip() for seg in result.segments if seg.get("text")]
    result.text = "\n".join(texts).strip()
    result.partial = complete_count < total
    if result.partial and result.text:
        result.warnings.append("部分音频分段转写失败，口播文案可能不完整。")
    return result
