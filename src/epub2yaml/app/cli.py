from __future__ import annotations

import json
from pathlib import Path

import typer

from epub2yaml.app.services import PipelineService
from epub2yaml.domain.enums import ReviewAction
from epub2yaml.llm.model_factory import create_document_update_chain_from_env

app = typer.Typer(help="EPUB 小说转 YAML 文档处理 CLI")


def get_workspace_dir() -> Path:
    return Path(__file__).resolve().parents[3]


def build_pipeline_service(*, provider: str | None = None, model: str | None = None) -> PipelineService:
    document_update_chain = None
    if provider is not None or model is not None:
        document_update_chain = create_document_update_chain_from_env(provider=provider, model=model)
    return PipelineService(get_workspace_dir(), document_update_chain=document_update_chain)


@app.command("init-run")
def init_run(
    epub_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    book_id: str | None = typer.Option(None, "--book-id", help="运行书籍 ID，默认使用 EPUB 文件名"),
) -> None:
    service = PipelineService(get_workspace_dir())
    state = service.init_run(epub_path=epub_path, book_id=book_id)
    typer.echo(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("generate-yaml")
def generate_yaml(
    epub_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True, help="待处理 EPUB 文件路径"),
    book_id: str | None = typer.Option(None, "--book-id", help="输出运行目录 ID，默认使用 EPUB 文件名"),
    provider: str | None = typer.Option(None, "--provider", help="模型提供方，默认读取环境变量 EPUB2YAML_MODEL_PROVIDER"),
    model: str | None = typer.Option(None, "--model", help="模型名称，默认读取环境变量 EPUB2YAML_MODEL"),
) -> None:
    service = build_pipeline_service(provider=provider, model=model)
    result = service.generate_yaml(epub_path, book_id=book_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("process-next-batch")
def process_next_batch(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    delta_file: Path = typer.Option(..., "--delta-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="LLM 生成的 Delta YAML 文件路径"),
) -> None:
    service = PipelineService(get_workspace_dir())
    record = service.process_next_batch(book_id=book_id, delta_yaml_text=delta_file.read_text(encoding="utf-8"))
    typer.echo(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("review-batch")
def review_batch(
    book_id: str = typer.Argument(..., help="书籍 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    action: ReviewAction = typer.Option(..., "--action", case_sensitive=False, help="审阅动作: accept/reject/edit"),
    reviewer: str | None = typer.Option(None, "--reviewer", help="审阅者"),
    comment: str | None = typer.Option(None, "--comment", help="审阅备注"),
    edited_actors_file: Path | None = typer.Option(None, "--edited-actors-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="人工修改后的 actors 预览文件"),
    edited_worldinfo_file: Path | None = typer.Option(None, "--edited-worldinfo-file", exists=True, file_okay=True, dir_okay=False, readable=True, help="人工修改后的 worldinfo 预览文件"),
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
    typer.echo(json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("show-status")
def show_status(book_id: str = typer.Argument(..., help="书籍 ID")) -> None:
    service = PipelineService(get_workspace_dir())
    status = service.show_status(book_id)
    typer.echo(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
