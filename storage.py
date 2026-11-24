"""结果存储：写入 result.json 与 result.md，清理中间产物。"""
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from config import Settings


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds or 0)))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {sec} 秒"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def _build_markdown(payload: Dict[str, Any]) -> str:
    title = (payload.get("title") or "未命名作品").replace("\n", " ").strip()
    author = (payload.get("author") or "").strip() or "未知"
    url = payload.get("url") or ""
    duration = format_duration(payload.get("duration") or 0)
    media_type = payload.get("media_type") or "video"
    images = payload.get("images") or []

    summary = (payload.get("summary") or "").strip() or "（未生成摘要）"
    transcript = (payload.get("transcript") or "").strip() or "（无可用的口播文案）"
    ocr_text = (payload.get("ocr_text") or "").strip() or "（未识别到画面文字）"

    if media_type == "images":
        meta_lines = [
            f"- 作者：{author}",
            f"- 图片数量：{len(images)}",
            f"- 链接：{url}",
        ]
        ocr_heading = "## 图片画面文案（OCR）"
    else:
        meta_lines = [
            f"- 作者：{author}",
            f"- 时长：{duration}",
            f"- 链接：{url}",
        ]
        ocr_heading = "## 视频画面文案（OCR）"

    lines: List[str] = [f"# {title}", ""]
    lines.extend(meta_lines)
    lines.extend([
        "",
        "## 摘要",
        "",
        summary,
        "",
        "## 视频口播文案（ASR）",
        "",
        transcript,
        "",
        ocr_heading,
        "",
        ocr_text,
        "",
    ])

    if media_type == "images" and images:
        lines.extend(["## 图片文件", ""])
        for image_path in images:
            lines.append(f"- {Path(image_path).name}")
        lines.append("")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("## 处理提示")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines)


def save_result(aweme_id: str, workdir: str, payload: Dict[str, Any],
                settings: Settings, cleanup_intermediate: bool = True) -> Tuple[str, str]:
    """保存 result.json 与 result.md，返回 (json_path, md_path)。"""
    directory = Path(workdir)
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / "result.json"
    md_path = directory / "result.md"

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(_build_markdown(payload), encoding="utf-8")

    if cleanup_intermediate:
        for name in ("frames", "seg"):
            target = directory / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)

    return str(json_path), str(md_path)
