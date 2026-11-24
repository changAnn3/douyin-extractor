"""画面文字识别：ffmpeg 抽帧、RapidOCR、行聚类、时间轴去重合并。"""
import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy as np

from config import Settings

ProgressCb = Callable[[float, str], None]

_NORMALIZE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_SIMILARITY_THRESHOLD = 0.88
_MAX_MISSING_FRAMES = 2

_ocr_engine = None


def _get_engine():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


@dataclass
class OcrRow:
    """单帧内聚类后的一行文字。"""
    text: str
    score: float
    cx: float
    cy: float
    h: float


@dataclass
class OcrBlock:
    """跨帧合并后的字幕块。"""
    start: float
    end: float
    text: str
    score: float
    cy: float
    h: float
    last_frame: int
    frame_count: int = 1


@dataclass
class OcrResult:
    text: str = ""
    lines: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    frame_count: int = 0
    interval: float = 0.0


def normalize_text(text: str) -> str:
    return _NORMALIZE_RE.sub("", text or "")


def _read_image(path: Path) -> Optional[np.ndarray]:
    """Windows 中文路径安全读图。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _recognize_frame(img: np.ndarray, min_score: float) -> List[OcrRow]:
    engine = _get_engine()
    result, _ = engine(img, use_cls=False)
    if not result:
        return []

    rows: List[OcrRow] = []
    for item in result:
        if len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        text = str(text).strip()
        if not text:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        rows.append(OcrRow(
            text=text,
            score=score,
            cx=(min(xs) + max(xs)) / 2.0,
            cy=(min(ys) + max(ys)) / 2.0,
            h=max(h, 1.0),
        ))
    return rows


def _join_line_texts(texts: List[str]) -> str:
    out = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if not out:
            out = text
            continue
        prev = out[-1]
        cur = text[0]
        if prev.isascii() and prev.isalnum() and cur.isascii() and cur.isalnum():
            out += " " + text
        else:
            out += text
    return out


def _cluster_rows(rows: List[OcrRow]) -> List[OcrRow]:
    """把同一帧内的文字片段按垂直位置聚成行，行内按水平位置拼接。"""
    if not rows:
        return []

    groups: List[Dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r.cy):
        placed = False
        for group in groups:
            if abs(row.cy - group["cy"]) <= 0.7 * max(row.h, group["h"]):
                group["rows"].append(row)
                group["cy"] = sum(r.cy for r in group["rows"]) / len(group["rows"])
                group["h"] = max(group["h"], row.h)
                placed = True
                break
        if not placed:
            groups.append({"rows": [row], "cy": row.cy, "h": row.h})

    clustered: List[OcrRow] = []
    for group in groups:
        members = sorted(group["rows"], key=lambda r: r.cx)
        text = _join_line_texts([m.text for m in members])
        if not text.strip():
            continue
        clustered.append(OcrRow(
            text=text,
            score=max(m.score for m in members),
            cx=sum(m.cx for m in members) / len(members),
            cy=group["cy"],
            h=group["h"],
        ))
    return clustered


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_same_text(norm_a: str, norm_b: str) -> bool:
    """判断两段归一化文本是否属于同一条字幕。

    - 包含关系视为同一块（打字机式逐字累积显示）。
    - 否则要求相似度达到阈值，避免把“第一句/第二句”这类相邻字幕误合并。
    """
    if not norm_a or not norm_b:
        return False
    short, long_ = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if len(short) >= 2 and short in long_:
        return True
    return _similar(norm_a, norm_b) >= _SIMILARITY_THRESHOLD


def _merge_rows_into_blocks(rows_by_frame: List[List[OcrRow]], interval: float,
                            duration: float) -> List[OcrBlock]:
    """跨帧合并为字幕块。"""
    active: List[OcrBlock] = []
    finished: List[OcrBlock] = []

    for frame_idx, rows in enumerate(rows_by_frame):
        start_t = frame_idx * interval
        end_t = min(duration, (frame_idx + 1) * interval) if duration > 0 else (frame_idx + 1) * interval

        matched_blocks: List[OcrBlock] = []
        for row in rows:
            norm = normalize_text(row.text)
            if not norm:
                continue
            best: Optional[OcrBlock] = None
            best_rank = 0.0
            for block in active:
                if block in matched_blocks:
                    continue
                if abs(row.cy - block.cy) > 0.75 * max(row.h, block.h):
                    continue
                block_norm = normalize_text(block.text)
                if not _is_same_text(norm, block_norm):
                    continue
                rank = _similar(norm, block_norm)
                if norm in block_norm or block_norm in norm:
                    rank = max(rank, 0.999)
                if rank > best_rank:
                    best = block
                    best_rank = rank
            if best is not None:
                matched_blocks.append(best)
                best.last_frame = frame_idx
                best.end = end_t
                best.frame_count += 1
                old_norm = normalize_text(best.text)
                # 保留更完整（更长）的文本；长度相同时取更高分
                if len(norm) > len(old_norm) or (
                        len(norm) == len(old_norm) and row.score > best.score):
                    best.text = row.text
                best.score = max(best.score, row.score)
                best.cy = row.cy
                best.h = max(best.h, row.h)
            else:
                block = OcrBlock(
                    start=start_t,
                    end=end_t,
                    text=row.text,
                    score=row.score,
                    cy=row.cy,
                    h=row.h,
                    last_frame=frame_idx,
                )
                active.append(block)
                matched_blocks.append(block)

        # 连续缺失超过阈值则结束块
        still_active: List[OcrBlock] = []
        for block in active:
            if frame_idx - block.last_frame > _MAX_MISSING_FRAMES:
                finished.append(block)
            else:
                still_active.append(block)
        active = still_active

    finished.extend(active)
    return finished


def _finalize_blocks(blocks: List[OcrBlock]) -> List[OcrBlock]:
    """过滤过短/重复的块。"""
    kept: List[OcrBlock] = []
    seen_short: List[tuple] = []
    for block in sorted(blocks, key=lambda b: (b.start, b.cy)):
        norm = normalize_text(block.text)
        if not norm:
            continue
        # 单帧且少于 3 字，视为噪声
        if block.frame_count <= 1 and len(norm) < 3:
            continue
        # 顶部区域的短拉丁/数字碎片多为旋转水印的 OCR 误识别，直接丢弃；
        # 稳定品牌水印会跨多帧出现，不会被此规则误删。
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", norm))
        if (not has_cjk and len(norm) <= 14 and block.cy <= 100
                and block.frame_count <= 2):
            continue
        # 短静态文本（≤12 字）全局只保留首次出现
        if len(norm) <= 12:
            duplicate = False
            for seen_norm, seen_cjk in seen_short:
                if norm == seen_norm or norm in seen_norm or seen_norm in norm:
                    duplicate = True
                    break
                # 纯拉丁/数字的短文本常是重复水印，允许近似去重
                if not has_cjk and not seen_cjk and _similar(norm, seen_norm) >= 0.7:
                    duplicate = True
                    break
            if duplicate:
                continue
            seen_short.append((norm, has_cjk))
        kept.append(block)
    return kept


def _frame_files(frames_dir: Path) -> List[Path]:
    return sorted(frames_dir.glob("f_*.jpg"))


def extract_frames(settings: Settings, video_path: str, frames_dir: Path,
                   duration: float, max_frames: int) -> float:
    """抽帧，返回帧间隔（秒）。"""
    frames_dir.mkdir(parents=True, exist_ok=True)
    interval = 1.0
    if duration and duration > 0:
        # 严格保证不超过 max_frames：超长视频自动增大采样间隔（不再固定 30 秒上限）
        interval = duration / max(1, max_frames)
    interval = max(1.0, interval)

    vf = f"fps=1/{interval},scale=min(1280\\,iw):-2"
    cmd = [
        settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-vf", vf,
        "-q:v", "3",
        "-start_number", "0",
        str(frames_dir / "f_%05d.jpg"),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return interval
    return interval


def process_images(image_paths: List[str], workdir: str, settings: Settings,
                   progress_cb: Optional[ProgressCb] = None) -> OcrResult:
    """图文作品：逐张图片 OCR，按图片顺序合并。"""
    result = OcrResult()
    result.frame_count = len(image_paths)
    if not image_paths:
        result.warnings.append("图文作品没有可用图片，画面文字为空。")
        return result

    total = len(image_paths)
    texts: List[str] = []
    for index, image_path in enumerate(image_paths):
        if progress_cb:
            progress_cb(index / total, f"正在识别图片文字（{index + 1}/{total}）…")
        img = _read_image(Path(image_path))
        if img is None:
            continue
        rows = _cluster_rows(_recognize_frame(img, settings.ocr_min_score))
        if not rows:
            continue
        text = "\n".join(r.text for r in rows if r.text.strip()).strip()
        if not text:
            continue
        texts.append(text)
        result.lines.append({
            "index": index,
            "start": index,
            "end": index + 1,
            "text": text,
            "score": round(max(r.score for r in rows), 3),
        })

    result.text = "\n".join(texts).strip()
    if not result.text:
        result.warnings.append("图片中未识别到文字。")
    if progress_cb:
        progress_cb(1.0, "图片文字识别完成")
    return result


def process_frames(video_path: str, workdir: str, duration: float,
                   settings: Settings,
                   progress_cb: Optional[ProgressCb] = None) -> OcrResult:
    """抽帧 + OCR + 时间轴合并。失败不抛异常，记 warnings。"""
    result = OcrResult()
    frames_dir = Path(workdir) / "frames"

    if progress_cb:
        progress_cb(0.05, "正在抽取视频帧…")
    interval = extract_frames(settings, video_path, frames_dir, duration,
                              settings.ocr_max_frames)
    result.interval = interval

    frames = _frame_files(frames_dir)
    if not frames:
        result.warnings.append("未能抽取视频帧，画面文字为空。")
        return result

    result.frame_count = len(frames)

    rows_by_frame: List[List[OcrRow]] = []
    total = len(frames)
    for i, frame_path in enumerate(frames):
        if progress_cb:
            progress_cb(0.1 + 0.8 * (i / total), f"正在识别画面文字（{i + 1}/{total}）…")
        img = _read_image(frame_path)
        if img is None:
            rows_by_frame.append([])
            continue
        rows = _recognize_frame(img, settings.ocr_min_score)
        rows_by_frame.append(_cluster_rows(rows))

    if progress_cb:
        progress_cb(0.95, "正在整理画面文字…")

    blocks = _finalize_blocks(_merge_rows_into_blocks(rows_by_frame, interval, duration))
    result.lines = [
        {"start": round(b.start, 2), "end": round(b.end, 2),
         "text": b.text, "score": round(b.score, 3)}
        for b in blocks
    ]
    result.text = "\n".join(b.text for b in blocks).strip()
    if not result.text:
        result.warnings.append("画面中未识别到文字。")
    if progress_cb:
        progress_cb(1.0, "画面文字识别完成")
    return result
