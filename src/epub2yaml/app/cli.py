from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer
import dotenv

from epub2yaml.app.services import PipelineService
from epub2yaml.domain.enums import ReviewAction
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
    if decision.action == "review_structure_loss" and decision.batch_id is not None:
        summary = service.get_review_batch_summary(book_id, batch_id=decision.batch_id)
        typer.echo(f"[待审批] 批次 {decision.batch_id} 存在结构缺失，缺失路径数={summary['missing_paths_count']}")
    _echo_json(decision.model_dump(mode="json"))


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
    summary = service.get_review_batch_summary(book_id, batch_id=batch_id)
    typer.echo(f"Batch: {summary['batch_id']}")
    typer.echo(f"Chapters: {summary['chapter_start'] + 1}-{summary['chapter_end'] + 1}")
    typer.echo(f"Status: {summary['status']}")
    typer.echo(f"Retry Count: {summary['retry_count']}")
    typer.echo(f"Structure loss detected: {'yes' if summary['requires_loss_approval'] else 'no'}")
    typer.echo(f"Missing paths: {summary['missing_paths_count']}")
    typer.echo(f"Actors missing: {summary['actors_missing_count']}")
    typer.echo(f"Worldinfo missing: {summary['worldinfo_missing_count']}")
    if summary['requires_loss_approval']:
        typer.echo("WARNING: 当前结果相较上一版正式文档存在字段缺失。")
        typer.echo("WARNING: accept 表示人工确认允许当前缺失结果继续提交。")
        for path in summary["actors_missing_paths"][:10]:
            typer.echo(f"- ACTORS {path}")
        for path in summary["worldinfo_missing_paths"][:10]:
            typer.echo(f"- WORLDINFO {path}")
        if summary["missing_paths_count"] > 20:
            typer.echo("更多缺失路径请查看 structure_check.json 或 missing_paths.txt")
    typer.echo("Files:")
    typer.echo(f"- {summary['files']['delta']}")
    typer.echo(f"- {summary['files']['merged_actors_preview']}")
    typer.echo(f"- {summary['files']['merged_worldinfo_preview']}")
    typer.echo(f"- {summary['files']['structure_check']}")
    typer.echo(f"- {summary['files']['missing_paths']}")
    typer.echo("Actions:")
    typer.echo("- accept: accept and continue")
    typer.echo("- reject: reject and retry later")
    typer.echo("- edit: edit preview before commit")

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


if __name__ == "__main__":
    dotenv.load_dotenv()
    app()
