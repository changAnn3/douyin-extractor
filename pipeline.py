"""流程编排：链接 → 下载 → 音频 → ASR → OCR → 摘要 → 保存。"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import asr
import audio
import downloader
import extractor
import ocr
import storage
import summarizer
from config import Settings

ProgressCb = Callable[[float, str], None]


@dataclass
class PipelineResult:
    aweme_id: str
    url: str
    title: str
    caption: str
    author: str
    duration: float
    summary: str
    transcript: str
    transcript_segments: List[Dict[str, Any]]
    ocr_text: str
    ocr_lines: List[Dict[str, Any]]
    warnings: List[str]
    output_dir: str
    video_path: str
    audio_path: Optional[str]
    json_path: str
    md_path: str
    models: Dict[str, str]
    media_type: str = "video"
    image_paths: List[str] = field(default_factory=list)


def _wrap(progress_cb: Optional[ProgressCb], low: float, high: float) -> Optional[ProgressCb]:
    if progress_cb is None:
        return None

    def inner(frac: float, desc: str) -> None:
        bounded = min(max(frac, 0.0), 1.0)
        progress_cb(low + (high - low) * bounded, desc)

    return inner


def run(url_text: str, language: str, settings: Settings,
        progress_cb: Optional[ProgressCb] = None,
        cleanup_intermediate: bool = True) -> PipelineResult:
    """执行完整流程。致命错误（链接无效、下载失败）抛出对应异常。"""
    warnings: List[str] = []

    def emit(frac: float, desc: str) -> None:
        if progress_cb:
            progress_cb(min(max(frac, 0.0), 1.0), desc)

    # 1. 解析链接
    emit(0.01, "正在解析分享链接…")
    url = extractor.extract_url(url_text)
    url = extractor.canonical_video_url(url)
    url = extractor.resolve_url(url)
    url = extractor.canonical_video_url(url)

    # 2. 下载视频/图片
    media_hint = "视频" if "/note/" not in url else "图文"
    emit(0.03, f"正在准备下载{media_hint}…")
    dl = downloader.download_video(url, settings, _wrap(progress_cb, 0.03, 0.30))

    workdir = str(Path(settings.output_dir) / dl.aweme_id)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    media_type = getattr(dl, "media_type", "video") or "video"
    warnings.extend(getattr(dl, "warnings", []) or [])

    # 3. 抽取音频（图文作品无音频）
    if media_type == "images":
        emit(0.30, "图文作品没有音频，跳过了口播转写")
        audio_result = audio.AudioResult(
            duration=0.0, has_audio=False, audio_path=None, chunks=[])
        warnings.append("图文作品没有音频，口播文案为空。")
        duration = 0.0
    else:
        emit(0.30, "正在处理音频…")
        audio_result = audio.process_audio(
            dl.video_path, workdir, settings, _wrap(progress_cb, 0.30, 0.35))
        warnings.extend(audio_result.warnings)
        duration = audio_result.duration or dl.duration

    has_key = bool(settings.mimo_api_key)
    if not has_key:
        warnings.append("未配置 MIMO_API_KEY：已跳过语音转写与摘要生成。")

    # 4. ASR 语音转写
    transcript = ""
    transcript_segments: List[Dict[str, Any]] = []
    if has_key and audio_result.has_audio and audio_result.chunks:
        emit(0.35, "正在转写口播文案…")
        try:
            asr_result = asr.transcribe(
                audio_result.chunks, settings, language,
                _wrap(progress_cb, 0.35, 0.65))
            transcript = asr_result.text
            transcript_segments = asr_result.segments
            warnings.extend(asr_result.warnings)
        except asr.AsrError as exc:
            warnings.append(f"语音转写失败：{exc}")
        except Exception as exc:  # 防御性兜底，避免整条流程崩溃
            warnings.append(f"语音转写异常：{exc}")

    # 5. 抽帧/图片 + OCR
    emit(0.65, "正在识别画面文字…")
    ocr_result = ocr.OcrResult()
    try:
        if media_type == "images":
            ocr_result = ocr.process_images(
                dl.image_paths, workdir, settings,
                _wrap(progress_cb, 0.65, 0.90))
        else:
            ocr_result = ocr.process_frames(
                dl.video_path, workdir, duration, settings,
                _wrap(progress_cb, 0.65, 0.90))
        warnings.extend(ocr_result.warnings)
    except Exception as exc:
        warnings.append(f"画面文字识别失败：{exc}")

    # 6. 生成摘要
    summary = ""
    if has_key:
        emit(0.90, "正在生成摘要文案…")
        try:
            summary = summarizer.summarize(
                dl.title, dl.caption, transcript, ocr_result.text, settings,
                _wrap(progress_cb, 0.90, 0.97))
        except summarizer.SummarizerError as exc:
            warnings.append(f"摘要生成失败：{exc}")
        except Exception as exc:
            warnings.append(f"摘要生成异常：{exc}")

    # 7. 保存结果
    emit(0.97, "正在保存结果…")
    models = {
        "asr": settings.mimo_asr_model,
        "text": settings.mimo_text_model,
    }
    payload: Dict[str, Any] = {
        "aweme_id": dl.aweme_id,
        "url": dl.url,
        "media_type": media_type,
        "images": dl.image_paths,
        "title": dl.title,
        "caption": dl.caption,
        "author": dl.author,
        "duration": round(float(duration or 0), 2),
        "summary": summary,
        "transcript": transcript,
        "transcript_segments": transcript_segments,
        "ocr_text": ocr_result.text,
        "ocr_lines": ocr_result.lines,
        "models": models,
        "warnings": warnings,
    }
    json_path, md_path = storage.save_result(
        dl.aweme_id, workdir, payload, settings,
        cleanup_intermediate=cleanup_intermediate)

    emit(1.0, "处理完成")
    return PipelineResult(
        aweme_id=dl.aweme_id,
        url=dl.url,
        title=dl.title,
        caption=dl.caption,
        author=dl.author,
        duration=float(duration or 0),
        summary=summary,
        transcript=transcript,
        transcript_segments=transcript_segments,
        ocr_text=ocr_result.text,
        ocr_lines=ocr_result.lines,
        warnings=warnings,
        output_dir=workdir,
        video_path=dl.video_path,
        audio_path=audio_result.audio_path,
        json_path=json_path,
        md_path=md_path,
        models=models,
        media_type=media_type,
        image_paths=dl.image_paths,
    )
