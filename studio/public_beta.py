"""Character Studio Public Beta 的精简产品界面。"""

from __future__ import annotations

import logging
from pathlib import Path
import threading

from bridge.public_codex_bridge import CODEX_INSTALL_URL, PublicBridgeHealth
from config.public_product import PRODUCT_DISPLAY_VERSION

from .public_service import PUBLIC_FAILURE_REASON, PublicStudioService
from .public_types import PublicStudioResult


PUBLIC_MODES = {
    "自动": "标准复刻（推荐）",
    "严格参考": "标准复刻（推荐）",
    "完整身体": "完整补全",
}

PUBLIC_BETA_CSS = """
.gradio-container {max-width: 1040px !important; margin: 0 auto !important;}
.beta-hero {padding: 24px 4px 6px;}
.beta-hero h1 {font-size: 2.05rem; margin: 0 0 4px;}
.beta-badge {color: var(--body-text-color-subdued); font-size: 1rem; margin: 0;}
.beta-card {border-radius: 16px !important; min-width: 0 !important;}
.beta-note {border: 1px solid #eadcae; background: #fffcf2; border-radius: 9px; padding: 10px 12px; margin: 8px 0 14px; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word;}
.beta-note-heading {display: flex; align-items: center; gap: 5px; margin-bottom: 2px;}
.beta-note-icon {font-size: 12px; line-height: 1;}
.beta-note strong {font-size: 13px; font-weight: 600;}
.dark .beta-note {border-color: #625735; background: #29261d; color: var(--body-text-color);}
.beta-result {border: 1px solid var(--border-color-primary); border-radius: 12px; padding: 10px 12px; overflow-wrap: anywhere;}
.codex-ready, .codex-action {border-radius: 12px; padding: 11px 13px; margin: 4px 0 8px; overflow-wrap: anywhere;}
.codex-ready {border: 1px solid #9bc7aa; background: #f4fbf6;}
.codex-action {border: 1px solid #d8c99b; background: #fffdf7;}
.dark .codex-ready {border-color: #3e6b4a; background: #1d2921;}
.dark .codex-action {border-color: #625735; background: #29261d;}
@media (max-width: 720px) {
  .gradio-container {padding-left: 10px !important; padding-right: 10px !important;}
  .beta-hero {padding-top: 14px;}
  .beta-hero h1 {font-size: 1.72rem;}
  .beta-card, .beta-result, .beta-note {min-width: 0 !important; max-width: 100% !important;}
}
"""


def _codex_status_markdown(health: PublicBridgeHealth) -> str:
    if health.available:
        return f"<div class='codex-ready'><strong>✅ Codex 已就绪</strong><br>{health.message}</div>"
    return f"<div class='codex-action'><strong>⚠️ Codex 需要准备</strong><br>{health.message}</div>"


def _progress_update(progress, value: str) -> None:
    if value.startswith("__studio_progress__|"):
        try:
            _prefix, fraction, stage, elapsed, remaining = value.split("|", 4)
            progress(
                min(0.995, max(0.0, float(fraction))),
                desc=f"{stage} · 已用 {_duration_text(int(elapsed))} · 预计剩余 {_duration_text(int(remaining))}",
            )
            return
        except (TypeError, ValueError):
            pass
    stages = {
        "已接收": 0.05,
        "快速生成中": 0.18,
        "正在调用用户自己的 Codex": 0.24,
        "快速检查中": 0.72,
        "Codex 任务已返回": 0.96,
    }
    fraction = next((amount for prefix, amount in stages.items() if value.startswith(prefix)), 0.08)
    progress(fraction, desc=value)


def _duration_text(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}分{seconds:02d}秒"


def _public_result_values(service: PublicStudioService, result: PublicStudioResult, mode: str):
    unsupported = result.failure_reason == PUBLIC_FAILURE_REASON
    if result.result_path and not result.failure_reason:
        completion_heading = "换装完成" if mode == "一键换装" else "姿势迁移完成"
        status = (
            f"### {completion_heading}\n\n"
            f"**当前模式**：{mode}\n\n"
            "✅ 角色一致性检查：通过  \n"
            "✅ 基础质量检查：通过"
        )
        image = str(result.result_path)
        diagnostic = None
    elif unsupported:
        status = (
            "### 当前 Beta 暂时无法可靠处理这个输入\n\n"
            "这个案例没有通过当前 Beta 的基础质量检查，因此没有直接交付可能存在明显错误的结果。\n\n"
            "- 调整角色图或参考图后重试\n"
            "- 可在下方导出不含图片的测试信息"
        )
        image = None
        diagnostic = str(service.export_beta_diagnostic(result))
    else:
        status = f"### {result.status}\n\n{result.message}"
        image = None
        diagnostic = str(service.export_beta_diagnostic(result)) if result.job_id else None
    return status, image, image, diagnostic


def build_public_beta_demo(
    app_root: Path,
    logger: logging.Logger | None = None,
    *,
    service: PublicStudioService | None = None,
    preview_result: PublicStudioResult | None = None,
    preview_mode: str = "自动",
    favicon_path: Path | None = None,
):
    import gradio as gr

    service = service or PublicStudioService(app_root, logger)

    def stop_task_ui():
        if service.cancel_active():
            gr.Info("正在停止当前任务，已生成的本地诊断会保留。")
            return "### 正在停止\n\n已发送停止请求，请稍候。"
        return "### 当前没有运行中的任务"

    def run_pose_ui(character, reference, mode, user_prompt, progress=gr.Progress()):
        result = service.run(
            character,
            reference,
            mode,
            user_prompt=user_prompt or "",
            on_status=lambda value: _progress_update(progress, value),
        )
        progress(1.0, desc=result.status)
        return _public_result_values(service, result, mode)

    def run_outfit_ui(character, outfit, progress=gr.Progress()):
        result = service.run_outfit(
            character,
            outfit,
            on_status=lambda value: _progress_update(progress, value),
            preserve_pose=True,
        )
        progress(1.0, desc=result.status)
        return _public_result_values(service, result, "一键换装")

    def recheck_codex_ui():
        health = service.codex_health()
        if health.available:
            gr.Info("Codex 已就绪，可以开始使用 Character Studio。")
        return _codex_status_markdown(health)

    def exit_studio_ui():
        threading.Timer(0.6, demo.close).start()
        return "### 正在退出工作室"

    codex_status = _codex_status_markdown(service.codex_health())
    theme = gr.themes.Soft(primary_hue="indigo", secondary_hue="violet")
    with gr.Blocks(title=f"Character Studio {PRODUCT_DISPLAY_VERSION}", analytics_enabled=False) as demo:
        gr.HTML(
            "<div class='beta-hero'><h1>Character Studio</h1>"
            "<p class='beta-badge'>Beta · 免费测试</p></div>"
        )
        codex_panel = gr.HTML(codex_status)
        with gr.Row():
            recheck_codex = gr.Button("重新检测 Codex", variant="secondary")
            gr.Button("打开 Codex 官方安装说明", link=CODEX_INSTALL_URL)
            exit_studio = gr.Button("退出工作室", variant="secondary")
        recheck_codex.click(recheck_codex_ui, None, codex_panel, queue=False)
        exit_studio.click(exit_studio_ui, None, None, queue=False)
        gr.HTML(
            "<div class='beta-note'><div class='beta-note-heading'><span class='beta-note-icon' aria-hidden='true'>⚠️</span>"
            "<strong>迁移姿势与换装使用你自己的 Codex</strong></div>"
            "开始迁移姿势或换装会消耗该账号的 Codex 使用额度；如果账号按 Token / Credits 计量，"
            "也可能产生相应消耗。实际使用量取决于模型、任务复杂度和运行时长。</div>"
        )

        with gr.Tabs():
            with gr.Tab("迁移姿势"):
                with gr.Row(equal_height=True):
                    character = gr.Image(
                        label="角色图",
                        type="filepath",
                        sources=["upload", "clipboard"],
                        image_mode="RGB",
                        elem_classes=["beta-card"],
                    )
                    reference = gr.Image(
                        label="姿势参考图",
                        type="filepath",
                        sources=["upload", "clipboard"],
                        image_mode="RGB",
                        elem_classes=["beta-card"],
                    )
                mode = gr.Radio(
                    choices=list(PUBLIC_MODES),
                    value="自动",
                    label="迁移模式",
                )
                user_prompt = gr.Textbox(
                    label="补充提示词（可选）",
                    placeholder="例如：保持原角色服装，镜头稍微拉远，背景简单一些……",
                    lines=3,
                )
                with gr.Row():
                    start = gr.Button("迁移姿势", variant="primary", scale=4)
                    stop = gr.Button("停止", variant="stop", scale=1)
                gr.Markdown("预计约 1–3 分钟。仅为估算时间，会根据实际任务有所差异。")
                status = gr.Markdown("### 准备就绪\n\n请上传角色图和姿势参考图。", elem_classes=["beta-result"])
                result_image = gr.Image(label="最终结果图", interactive=False, elem_classes=["beta-card"])
                with gr.Row():
                    save_result = gr.File(label="保存结果", interactive=False)
                    diagnostic = gr.File(label="导出测试信息（不含图片）", interactive=False)
                start.click(
                    run_pose_ui,
                    [character, reference, mode, user_prompt],
                    [status, result_image, save_result, diagnostic],
                )
                stop.click(stop_task_ui, None, status, queue=False)

            with gr.Tab("一键换装"):
                with gr.Row(equal_height=True):
                    outfit_character = gr.Image(
                        label="角色图",
                        type="filepath",
                        sources=["upload", "clipboard"],
                        image_mode="RGB",
                        elem_classes=["beta-card"],
                    )
                    outfit_reference = gr.Image(
                        label="服装参考图",
                        type="filepath",
                        sources=["upload", "clipboard"],
                        image_mode="RGB",
                        elem_classes=["beta-card"],
                    )
                with gr.Row():
                    outfit_start = gr.Button("开始换装", variant="primary", scale=4)
                    outfit_stop = gr.Button("停止", variant="stop", scale=1)
                gr.Markdown("预计约 1–3 分钟。仅为估算时间，会根据实际任务有所差异。")
                outfit_status = gr.Markdown("### 准备就绪\n\n请上传角色图和服装参考图。", elem_classes=["beta-result"])
                outfit_result = gr.Image(label="最终结果图", interactive=False, elem_classes=["beta-card"])
                with gr.Row():
                    outfit_save = gr.File(label="保存结果", interactive=False)
                    outfit_diagnostic = gr.File(label="导出测试信息（不含图片）", interactive=False)
                outfit_start.click(
                    run_outfit_ui,
                    [outfit_character, outfit_reference],
                    [outfit_status, outfit_result, outfit_save, outfit_diagnostic],
                )
                outfit_stop.click(stop_task_ui, None, outfit_status, queue=False)

        gr.Markdown("Character Studio Beta 0.1")

        if preview_result is not None:
            demo.load(
                lambda: _public_result_values(service, preview_result, preview_mode),
                None,
                [status, result_image, save_result, diagnostic],
            )

    demo._studio_theme = theme
    demo._studio_css = PUBLIC_BETA_CSS
    demo._studio_favicon = str(favicon_path.resolve()) if favicon_path and favicon_path.is_file() else None
    return demo
