from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import dotenv
import typer

from epub2yaml.app.control_ui import run_control_ui
from epub2yaml.app.services import PipelineService
from epub2yaml.domain.enums import ControlAction, ReviewAction
from epub2yaml.llm.model_factory import create_document_update_chain_from_env

app = typer.Typer(help="EPUB 小说转 YAML 文档处理 CLI")


def get_workspace_dir() -> Path:
    return Path(__file__).resolve().parents[3]


def build_pipeline_service(*, provider: Optional[str] = None, model: Optional[str] = None) -> PipelineService:
    document_update_chain = create_document_update_chain_from_env(provider=provider, model=model)
    return PipelineService(get_workspace_dir(), document_update_chain=document_update_chain)


def _render_progress(event: dict[str, Any]) -> None:
    event_name = event.get("event")
    if event_name == "run_initialized":
        typer.echo(f"[进度] 已初始化运行: book_id={event['book_id']}, 总章节={event['total_chapters']}")
        return

    if event_name == "batch_started":
        typer.echo(
            f"[进度] 开始处理批次 {event['batch_id']} | 动作={event.get('recovery_action', 'continue_new_batch')} | 已完成批次={event['processed_batches']} | 下一章节索引={event['next_chapter_index']} / 总章节={event['total_chapters']}"
        )
        return

    if event_name == "batch_completed":
        typer.echo(
            f"[进度] 已完成批次 {event['batch_id']} | 动作={event.get('recovery_action', 'continue_new_batch')} | 累计完成批次={event['processed_batches']} | 下一个章节索引={event['next_chapter_index']} / 总章节={event['total_chapters']}"
        )
        return

    if event_name == "control_interrupted":
        typer.echo(
            f"[进度] 控制命令已在安全边界生效: action={event.get('control_action')} batch_id={event.get('batch_id')}"
        )



def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("init-run")
def init_run(
    epub_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    book_id: Optional[str] = typer.Option(None, "--book-id", help="运行书籍 ID，默认使用 EPUB 文件名"),
) -> None:
    service = PipelineService(get_workspace_dir())
    state = service.init_run(epub_path=epub_path, book_id=book_id)
    _echo_json(state.model_dump(mode="json"))


@app.command("generate-yaml")
def generate_yaml(
    epub_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True, help="待处理 EPUB 文件路径"),
    book_id: Optional[str] = typer.Option(None, "--book-id", help="输出运行目录 ID，默认使用 EPUB 文件名"),
    provider: Optional[str] = typer.Option(None, "--provider", help="模型提供方，默认读取环境变量 EPUB2YAML_MODEL_PROVIDER"),
    model: Optional[str] = typer.Option(None, "--model", help="模型名称，默认读取环境变量 EPUB2YAML_MODEL"),
) -> None:
    service = build_pipeline_service(provider=provider, model=model)
    result = service.generate_yaml(epub_path, book_id=book_id, progress_callback=_render_progress)
    _echo_json(result)


@app.command("process-next-batch")
def process_next_batch(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    delta_file: Path = typer.Option(..., "--delta-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="LLM 生成的 Delta YAML 文件路径"),
) -> None:
    service = PipelineService(get_workspace_dir())
    record = service.process_next_batch(book_id=book_id, delta_yaml_text=delta_file.read_text(encoding="utf-8"))
    _echo_json(record.model_dump(mode="json"))


@app.command("resume-run")
def resume_run(book_id: str = typer.Argument(..., help="书籍 ID")) -> None:
    service = PipelineService(get_workspace_dir())
    decision = service.resume_run(book_id)
    _echo_json(decision.model_dump(mode="json"))


@app.command("pause-run")
def pause_run(book_id: str = typer.Argument(..., help="书籍 ID")) -> None:
    service = PipelineService(get_workspace_dir())
    state = service.request_control_action(book_id, ControlAction.PAUSE)
    _echo_json(state.model_dump(mode="json"))


@app.command("prepare-manual-edit")
def prepare_manual_edit(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    batch_id: Optional[str] = typer.Option(None, "--batch-id", help="指定进入人工修订的批次 ID"),
    no_editor: bool = typer.Option(False, "--no-editor", help="只导出工作区，不自动打开编辑器"),
) -> None:
    service = PipelineService(get_workspace_dir())
    session = service.prepare_manual_edit(book_id, batch_id=batch_id, open_editor=not no_editor)
    _echo_json(session.model_dump(mode="json"))


@app.command("open-manual-edit-workspace")
def open_manual_edit_workspace(book_id: str = typer.Argument(..., help="书籍 ID")) -> None:
    service = PipelineService(get_workspace_dir())
    session = service.open_manual_edit_workspace(book_id)
    _echo_json(session.model_dump(mode="json"))


@app.command("apply-manual-edit")
def apply_manual_edit(book_id: str = typer.Argument(..., help="书籍 ID")) -> None:
    service = PipelineService(get_workspace_dir())
    session = service.apply_manual_edit_session(book_id)
    _echo_json(session.model_dump(mode="json"))


@app.command("continue-after-manual-edit")
def continue_after_manual_edit(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    delta_file: Optional[Path] = typer.Option(None, "--delta-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="重跑同批次时覆盖使用的 Delta YAML 文件"),
) -> None:
    service = PipelineService(get_workspace_dir())
    record = service.continue_after_manual_edit(
        book_id,
        delta_yaml_text=delta_file.read_text(encoding="utf-8") if delta_file else None,
    )
    _echo_json(record.model_dump(mode="json"))


@app.command("retry-last-failed")
def retry_last_failed(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    delta_file: Optional[Path] = typer.Option(None, "--delta-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="重试时覆盖使用的 Delta YAML 文件"),
) -> None:
    service = PipelineService(get_workspace_dir())
    record = service.retry_last_failed(
        book_id,
        delta_yaml_text=delta_file.read_text(encoding="utf-8") if delta_file else None,
    )
    _echo_json(record.model_dump(mode="json"))


@app.command("retry-batch")
def retry_batch(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    delta_file: Optional[Path] = typer.Option(None, "--delta-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="重试时覆盖使用的 Delta YAML 文件"),
) -> None:
    service = PipelineService(get_workspace_dir())
    record = service.retry_batch(
        book_id,
        batch_id=batch_id,
        delta_yaml_text=delta_file.read_text(encoding="utf-8") if delta_file else None,
    )
    _echo_json(record.model_dump(mode="json"))


@app.command("review-batch")
def review_batch(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    action: ReviewAction = typer.Option(..., "--action", case_sensitive=False, help="审阅动作: accept/reject/edit"),
    reviewer: Optional[str] = typer.Option(None, "--reviewer", help="审阅者"),
    comment: Optional[str] = typer.Option(None, "--comment", help="审阅备注"),
    edited_actors_file: Optional[Path] = typer.Option(None, "--edited-actors-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="人工修改后的 actors 预览文件"),
    edited_worldinfo_file: Optional[Path] = typer.Option(None, "--edited-worldinfo-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="人工修改后的 worldinfo 预览文件"),
) -> None:
    service = PipelineService(get_workspace_dir())
    decision = service.review_batch(
        book_id=book_id,
        batch_id=batch_id,
        action=action,
        reviewer=reviewer,
        comment=comment,
        edited_actors_text=edited_actors_file.read_text(encoding="utf-8") if edited_actors_file else None,
        edited_worldinfo_text=edited_worldinfo_file.read_text(encoding="utf-8") if edited_worldinfo_file else None,
    )
    _echo_json(decision.model_dump(mode="json"))


@app.command("show-status")
def show_status(book_id: str = typer.Argument(..., help="书籍 ID")) -> None:
    service = PipelineService(get_workspace_dir())
    status = service.show_status(book_id)
    _echo_json(status)


@app.command("control-ui")
def control_ui(book_id: Optional[str] = typer.Argument(None, help="可选：已有书籍 ID")) -> None:
    run_control_ui(get_workspace_dir(), book_id=book_id)


if __name__ == "__main__":
    dotenv.load_dotenv()
    app()
