"""音频处理：时长探测、抽取音轨、按大小/静音切块。"""
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from config import Settings

ProgressCb = Callable[[float, str], None]


@dataclass
class AudioChunk:
    index: int
    start: float
    end: float
    path: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class AudioResult:
    duration: float
    has_audio: bool
    audio_path: Optional[str]
    chunks: List[AudioChunk] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def b64_len(size: int) -> int:
    """原始字节数对应的 base64 字符长度。"""
    return 4 * ((size + 2) // 3)


def _run(cmd: List[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
    )


def probe_duration(ffprobe: str, video_path: str) -> float:
    """返回音视频时长（秒），失败返回 0。"""
    try:
        proc = _run([
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", video_path,
        ], timeout=60)
        data = json.loads(proc.stdout or "{}")
        duration = float(data.get("format", {}).get("duration") or 0.0)
        if duration > 0:
            return duration
    except (ValueError, OSError, subprocess.SubprocessError):
        pass

    try:
        proc = _run([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "json", video_path,
        ], timeout=60)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            return float(streams[0].get("duration") or 0.0)
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return 0.0


def has_audio_stream(ffprobe: str, video_path: str) -> bool:
    try:
        proc = _run([
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "json", video_path,
        ], timeout=60)
        data = json.loads(proc.stdout or "{}")
        return bool(data.get("streams"))
    except (ValueError, OSError, subprocess.SubprocessError):
        return False


def extract_audio(ffmpeg: str, video_path: str, out_path: str) -> bool:
    """抽取单声道 16kHz 64kbps MP3，成功返回 True。"""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-map", "0:a:0", "-vn",
        "-ac", "1", "-ar", "16000", "-b:a", "64k",
        "-codec:a", "libmp3lame", "-map_metadata", "-1",
        out_path,
    ]
    try:
        proc = _run(cmd, timeout=None)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 0


def slice_audio(ffmpeg: str, src_path: str, start: float, duration: float, out_path: str) -> bool:
    """无损剪切一段音频（-c:a copy）。"""
    if duration <= 0:
        return False
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{duration:.3f}",
        "-i", src_path,
        "-vn", "-c:a", "copy",
        out_path,
    ]
    try:
        proc = _run(cmd, timeout=None)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 0


_SILENCE_END_RE = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")


def _detect_silence_points(ffmpeg: str, audio_path: str) -> List[float]:
    """用 silencedetect 找出静音结束时间点，作为理想切点候选。"""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-nostdin",
        "-i", audio_path,
        "-af", "silencedetect=noise=-35dB:d=0.4",
        "-f", "null", "-",
    ]
    try:
        proc = _run(cmd, timeout=None)
    except (OSError, subprocess.SubprocessError):
        return []
    return [float(m) for m in _SILENCE_END_RE.findall(proc.stderr or "")]


def _snap_cut(cut: float, candidates: List[float], window: float = 60.0) -> float:
    """把切点吸附到窗口内最近的静音结束点。"""
    if not candidates:
        return cut
    nearby = [c for c in candidates if abs(c - cut) <= window]
    if not nearby:
        return cut
    return min(nearby, key=lambda c: abs(c - cut))


def _plan_ranges(duration: float, chunk_seconds: int,
                 silence_points: List[float]) -> List[Tuple[float, float]]:
    chunk_seconds = max(60, chunk_seconds)
    if duration <= chunk_seconds + 3:
        return [(0.0, duration)]

    ranges: List[Tuple[float, float]] = []
    start = 0.0
    step = float(chunk_seconds)
    while start + chunk_seconds < duration - 3:
        raw_cut = start + step
        cut = _snap_cut(raw_cut, silence_points)
        if cut <= start + 5 or cut >= duration - 1:
            cut = raw_cut
        if cut >= duration:
            break
        ranges.append((start, cut))
        start = cut
    ranges.append((start, duration))
    return ranges


def _slice_with_size_limit(ffmpeg: str, src_path: str, start: float, end: float,
                           seg_dir: Path, settings: Settings,
                           counter: List[int], depth: int = 0) -> List[AudioChunk]:
    """切出一段；若 base64 超限则二分（最多两层）。"""
    safe_end = max(start + 0.01, end)
    out_path = seg_dir / f"seg_{counter[0]:03d}.mp3"
    counter[0] += 1
    if not slice_audio(ffmpeg, src_path, start, safe_end - start, str(out_path)):
        return []

    size = out_path.stat().st_size if out_path.exists() else 0
    if b64_len(size) <= settings.asr_max_base64_bytes or depth >= 2:
        return [AudioChunk(index=counter[0] - 1, start=start, end=safe_end,
                           path=str(out_path))]

    # 超限：删除并二分重切
    try:
        out_path.unlink()
    except OSError:
        pass
    mid = start + (safe_end - start) / 2
    left = _slice_with_size_limit(ffmpeg, src_path, start, mid, seg_dir, settings, counter, depth + 1)
    right = _slice_with_size_limit(ffmpeg, src_path, mid, safe_end, seg_dir, settings, counter, depth + 1)
    return left + right


def process_audio(video_path: str, workdir: str, settings: Settings,
                  progress_cb: Optional[ProgressCb] = None) -> AudioResult:
    """探测音轨、抽音频、分块。无音轨时返回 has_audio=False。"""
    warnings: List[str] = []
    Path(workdir).mkdir(parents=True, exist_ok=True)
    duration = probe_duration(settings.ffprobe_bin, video_path)
    if progress_cb:
        progress_cb(0.2, "读取视频时长…")

    if not has_audio_stream(settings.ffprobe_bin, video_path):
        return AudioResult(
            duration=duration,
            has_audio=False,
            audio_path=None,
            chunks=[],
            warnings=["视频没有音轨，口播文案将为空。"],
        )

    audio_path = str(Path(workdir) / "audio.mp3")
    if progress_cb:
        progress_cb(0.4, "正在抽取音频…")
    if not extract_audio(settings.ffmpeg_bin, video_path, audio_path):
        return AudioResult(
            duration=duration,
            has_audio=False,
            audio_path=None,
            chunks=[],
            warnings=["音频抽取失败，口播文案将为空。"],
        )

    if duration <= 0:
        duration = probe_duration(settings.ffprobe_bin, audio_path)

    # 时长 ≤ 分块上限：直接使用整个文件
    if duration <= settings.asr_chunk_seconds + 3:
        size = Path(audio_path).stat().st_size
        if b64_len(size) <= settings.asr_max_base64_bytes:
            return AudioResult(
                duration=duration,
                has_audio=True,
                audio_path=audio_path,
                chunks=[AudioChunk(index=0, start=0.0, end=duration, path=audio_path)],
                warnings=warnings,
            )

    if progress_cb:
        progress_cb(0.6, "正在分析静音切点…")
    silence_points = _detect_silence_points(settings.ffmpeg_bin, audio_path)
    ranges = _plan_ranges(duration, settings.asr_chunk_seconds, silence_points)

    if progress_cb:
        progress_cb(0.8, "正在切分音频…")
    seg_dir = Path(workdir) / "seg"
    seg_dir.mkdir(parents=True, exist_ok=True)
    counter = [0]
    chunks: List[AudioChunk] = []
    for start, end in ranges:
        chunks.extend(_slice_with_size_limit(
            settings.ffmpeg_bin, audio_path, start, end, seg_dir, settings, counter))

    if not chunks:
        warnings.append("音频切块失败，口播文案将为空。")
        return AudioResult(duration, True, audio_path, [], warnings)

    # 重新编号，保证按时间顺序
    for i, chunk in enumerate(chunks):
        chunk.index = i

    if progress_cb:
        progress_cb(1.0, f"音频切分为 {len(chunks)} 块")
    return AudioResult(duration, True, audio_path, chunks, warnings)
