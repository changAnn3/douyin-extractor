"""Gradio 3.34 本地界面入口。"""
import json
import time
from pathlib import Path

import gradio as gr

import downloader
import extractor
import pipeline
import storage
from config import get_settings


def _patch_pydantic_optional_fields() -> None:
    """Gradio 3.34 的 PredictBody 未给 Optional 字段默认值，
    在 pydantic 2 下会被当作必填，导致前端队列提交校验失败。"""
    try:
        import pydantic_core
        from gradio.data_classes import PredictBody

        for name in ("session_hash", "event_id", "event_data", "fn_index"):
            field = PredictBody.model_fields.get(name)
            if field is not None and field.default is pydantic_core.PydanticUndefined:
                field.default = None
        PredictBody.model_rebuild(force=True)
    except Exception:
        pass


_patch_pydantic_optional_fields()


def _patch_gradio_compat() -> None:
    """Gradio 4.44 + 新版 pydantic 兼容补丁：
    - gradio_client 旧解析器遇到布尔型 additionalProperties 会报 TypeError
    - 本机可用性检查可能受系统代理影响，直接视为可用
    """
    try:
        import gradio_client.utils as gcu

        original = getattr(gcu, "_json_schema_to_python_type", None)
        if original is not None and not getattr(original, "_safe_patched", False):
            def _safe(schema, defs):
                if isinstance(schema, bool):
                    return "Any"
                try:
                    return original(schema, defs)
                except TypeError:
                    return "Any"

            _safe._safe_patched = True
            gcu._json_schema_to_python_type = _safe
    except Exception:
        pass
    try:
        import gradio.networking as gnet
        gnet.url_ok = lambda url: True
    except Exception:
        pass


_patch_gradio_compat()


SETTINGS = get_settings()

ERROR_HINT = (
    "**处理失败**\n\n{message}\n\n"
    "请确认已复制 `.env.example` 为 `.env` 并填写 `MIMO_API_KEY`，"
    "同时粘贴有效的抖音分享链接或分享口令。"
)


def _success_status(result: pipeline.PipelineResult) -> str:
    lines = [
        f"**处理完成** — 作品 ID：{result.aweme_id}",
        f"**标题**：{result.title}",
        f"**作者**：{result.author or '未知'}",
        f"**时长**：{storage.format_duration(result.duration)}",
        f"**保存目录**：`{result.output_dir}`",
    ]
    if result.warnings:
        lines.append("")
        lines.append("**提示**：")
        for warning in result.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def on_run(url_text: str, language: str, progress=gr.Progress()):
    print("[%s] 收到任务: %r 语种=%s" % (time.strftime("%H:%M:%S"), (url_text or "")[:120], language),
          flush=True)

    def bridge(frac: float, desc: str) -> None:
        progress(min(max(frac, 0.0), 0.99), desc=desc)

    try:
        result = pipeline.run(url_text, language, SETTINGS, progress_cb=bridge)
    except ValueError as exc:
        print("[%s] 任务失败(链接): %s" % (time.strftime("%H:%M:%S"), exc), flush=True)
        return [ERROR_HINT.format(message=str(exc)), "", "", "", "", None, None, None, None, None]
    except downloader.DownloaderError as exc:
        print("[%s] 任务失败(下载): %s" % (time.strftime("%H:%M:%S"), exc), flush=True)
        return [ERROR_HINT.format(message=str(exc)), "", "", "", "", None, None, None, None, None]
    except Exception as exc:  # 防御性兜底
        print("[%s] 任务失败(未知): %r" % (time.strftime("%H:%M:%S"), exc), flush=True)
        return [
            ERROR_HINT.format(message=f"发生未预期的错误：{exc}"),
            "", "", "", "", None, None, None, None, None,
        ]

    print("[%s] 任务完成: %s 警告=%s" % (time.strftime("%H:%M:%S"), result.aweme_id, result.warnings),
          flush=True)
    return [
        _success_status(result),
        result.title,
        result.summary or "（未生成摘要）",
        result.transcript or "（无口播文案）",
        result.ocr_text or "（未识别到画面文字）",
        result.image_paths or None,
        result.video_path or None,
        result.audio_path or None,
        result.json_path,
        result.md_path,
    ]


def _blank_outputs(status: str = "等待开始…"):
    return [status, "", "", "", "", None, None, None, None, None]


def _outputs_from_result(jpath: Path):
    try:
        data = json.loads(Path(jpath).read_text(encoding="utf-8"))
        folder = Path(jpath).parent
        media_type = data.get("media_type") or "video"
        images = [p for p in (data.get("images") or [])
                  if isinstance(p, str) and Path(p).exists()]
        video = folder / "video.mp4"
        audio = folder / "audio.mp3"
        md = folder / "result.md"
        status_lines = [
            f"**已加载结果** — 作品 ID：{data.get('aweme_id', '')}",
            f"**标题**：{data.get('title', '')}",
            f"**类型**：{'图文作品' if media_type == 'images' else '视频作品'}",
            f"**保存目录**：`{folder}`",
        ]
        warnings = data.get("warnings") or []
        if warnings:
            status_lines.extend(["", "**提示**："])
            for warning in warnings:
                status_lines.append(f"- {warning}")
        return [
            "\n".join(status_lines),
            data.get("title") or "",
            data.get("summary") or "（未生成摘要）",
            data.get("transcript") or "（无口播文案）",
            data.get("ocr_text") or "（未识别到画面文字）",
            images or None,
            str(video) if video.exists() else None,
            str(audio) if audio.exists() else None,
            str(jpath),
            str(md) if md.exists() else None,
        ]
    except Exception:
        return _blank_outputs("加载结果失败，请稍后重试。")


def _scan_history():
    """扫描 output 下所有结果，按修改时间倒序，返回 Dropdown 用的 (label, id)。"""
    root = Path(SETTINGS.output_dir)
    entries = []
    for jpath in root.glob("*/result.json"):
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
            mtime = jpath.stat().st_mtime
        except Exception:
            continue
        aweme_id = str(data.get("aweme_id") or jpath.parent.name)
        media = "图文" if (data.get("media_type") or "video") == "images" else "视频"
        title = (data.get("title") or "未命名作品").replace("\n", " ").strip()
        if len(title) > 36:
            title = title[:36] + "…"
        label = "%s · %s · %s" % (
            time.strftime("%m-%d %H:%M", time.localtime(mtime)), media, title)
        entries.append((mtime, aweme_id, label))

    entries.sort(key=lambda item: item[0], reverse=True)
    seen = set()
    choices = []
    for _mtime, aweme_id, label in entries:
        if aweme_id in seen:
            continue
        seen.add(aweme_id)
        choices.append((label, aweme_id))
    return choices


def _latest_defaults():
    """启动时加载最近一次成功结果，避免页面刷新后空白。"""
    try:
        root = Path(SETTINGS.output_dir)
        candidates = sorted(
            root.glob("*/result.json"),
            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return _blank_outputs()
        return _outputs_from_result(candidates[0])
    except Exception:
        return _blank_outputs()


def _load_history(aweme_id):
    if not aweme_id:
        return _blank_outputs("请先在历史记录中选择一个作品。")
    jpath = Path(SETTINGS.output_dir) / str(aweme_id) / "result.json"
    if not jpath.exists():
        return _blank_outputs("未找到该历史记录的结果文件。")
    return _outputs_from_result(jpath)


def _refresh_history():
    """刷新历史列表，同时加载最新一条结果。返回 10 个结果 + 1 个下拉更新。"""
    choices = _scan_history()
    latest_id = choices[0][1] if choices else None
    if latest_id:
        outputs = _load_history(latest_id)
    else:
        outputs = _blank_outputs("还没有历史结果，先提交一个任务吧。")
    return outputs + [gr.update(choices=choices, value=latest_id)]


def build_ui() -> gr.Blocks:
    history_choices = _scan_history()
    latest_id = history_choices[0][1] if history_choices else None
    defaults = _latest_defaults()
    with gr.Blocks(title="抖音视频/图文文案提取工具") as demo:
        gr.Markdown(
            "# 抖音视频/图文文案提取工具\n\n"
            "粘贴抖音分享链接或分享口令，自动下载视频或图文作品并提取："
            "**标题、摘要文案、口播文案、画面文字**。图文作品会展示图片并识别图片文字。"
        )

        with gr.Row():
            with gr.Column(scale=1, min_width=320):
                url_input = gr.Textbox(
                    label="抖音分享链接 / 分享口令",
                    placeholder="例如：3.14 复制打开抖音，看看…… https://v.douyin.com/xxxxxxx/",
                    lines=3,
                )
                language = gr.Dropdown(
                    label="口播识别语种",
                    choices=["auto", "zh", "en"],
                    value="auto",
                )
                with gr.Row():
                    run_button = gr.Button("开始提取", variant="primary")
                    refresh_button = gr.Button("加载最近结果")
                status = gr.Markdown(value=defaults[0])
                history_dropdown = gr.Dropdown(
                    label="历史记录（选择后加载）",
                    choices=history_choices,
                    value=latest_id,
                )
                refresh_history_button = gr.Button("刷新历史列表")

            with gr.Column(scale=2):
                title_out = gr.Textbox(label="视频标题", lines=2, interactive=False, value=defaults[1])
                summary_out = gr.Textbox(label="摘要文案", lines=5, interactive=False, value=defaults[2])
                transcript_out = gr.Textbox(label="口播文案（ASR 转写）", lines=10, interactive=False,
                                            value=defaults[3])
                ocr_out = gr.Textbox(label="画面文字（OCR）", lines=8, interactive=False,
                                     value=defaults[4])
                gallery_out = gr.Gallery(label="图文作品图片", value=defaults[5])

                with gr.Accordion("视频预览与结果文件", open=False):
                    video_out = gr.Video(label="下载的视频", value=defaults[6])
                    audio_out = gr.Audio(label="抽取的音频", type="filepath", value=defaults[7])
                    with gr.Row():
                        json_out = gr.File(label="result.json", interactive=False, value=defaults[8])
                        md_out = gr.File(label="result.md", interactive=False, value=defaults[9])

        result_outputs = [
            status, title_out, summary_out, transcript_out, ocr_out, gallery_out,
            video_out, audio_out, json_out, md_out,
        ]
        run_button.click(
            fn=on_run,
            inputs=[url_input, language],
            outputs=result_outputs,
        )
        refresh_button.click(
            fn=_latest_defaults,
            inputs=None,
            outputs=result_outputs,
        )
        history_dropdown.change(
            fn=_load_history,
            inputs=[history_dropdown],
            outputs=result_outputs,
        )
        refresh_history_button.click(
            fn=_refresh_history,
            inputs=None,
            outputs=result_outputs + [history_dropdown],
        )
        # 页面每次加载/刷新时同步历史列表并加载最新结果
        demo.load(
            fn=_refresh_history,
            inputs=None,
            outputs=result_outputs + [history_dropdown],
        )
    return demo


def main() -> None:
    demo = build_ui()
    demo.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
    )


if __name__ == "__main__":
    main()
