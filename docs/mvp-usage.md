# EPUB2YAML MVP 使用说明

本文档说明当前最小可用版本的运行方式，包括环境准备、命令用法、输出目录结构与常见问题。

## 1. 功能概览

当前 MVP 版本支持以下能力：

- 输入单个 EPUB 文件
- 自动提取章节文本
- 自动按批次调用大模型生成 Delta YAML
- 自动将每批 Delta 合并到正式文档
- 自动输出最终 `actors.yaml` 与 `worldinfo.yaml`
- 在常见失败场景下返回可读错误信息

当前主入口命令定义在 `src/epub2yaml/app/cli.py` 中，核心自动化服务在 `src/epub2yaml/app/services.py` 中实现。

## 2. 前置条件

运行前需要满足：

1. 已安装 Python 运行环境
2. 已安装项目依赖
3. 已安装并配置可用的 LangChain OpenAI 适配依赖
4. 已准备可访问的模型 API Key

当前模型工厂实现位于 `src/epub2yaml/llm/model_factory.py`，默认按 OpenAI 兼容接口初始化聊天模型。

## 3. 环境变量配置

执行自动命令前，至少需要设置以下环境变量：

### 必填

- `EPUB2YAML_MODEL`
  - 模型名称，例如：`gpt-4o-mini`
- `EPUB2YAML_API_KEY`
  - 模型服务 API Key

### 选填

- `EPUB2YAML_MODEL_PROVIDER`
  - 模型提供方，默认值为 `openai`
- `EPUB2YAML_BASE_URL`
  - OpenAI 兼容接口地址
- `EPUB2YAML_TEMPERATURE`
  - 温度参数，默认值为 `0`
- `OPENAI_API_KEY`
  - 当未设置 `EPUB2YAML_API_KEY` 时可作为回退值
- `OPENAI_BASE_URL`
  - 当未设置 `EPUB2YAML_BASE_URL` 时可作为回退值

### Windows CMD 示例

```bat
set EPUB2YAML_MODEL=gpt-4o-mini
set EPUB2YAML_API_KEY=your_api_key
set EPUB2YAML_BASE_URL=https://api.openai.com/v1
```

## 4. 一键自动生成命令

最小可用版本推荐直接使用 `generate-yaml` 命令。

### 命令格式

```bat
python -m epub2yaml.app.cli generate-yaml <EPUB路径> --book-id <书籍ID>
```

### 示例

```bat
python -m epub2yaml.app.cli generate-yaml .\sample.epub --book-id my-book
```

### 可选参数

- `--book-id`
  - 指定运行目录名；不传时默认使用 EPUB 文件名
- `--provider`
  - 指定模型提供方；不传时读取环境变量
- `--model`
  - 直接指定模型名；不传时读取环境变量

### 成功输出

命令成功后会输出 JSON，例如：

```json
{
  "book_id": "my-book",
  "status": "completed",
  "processed_batches": [
    "0001",
    "0002"
  ],
  "actors_path": "f:/projects/epub2summary/runs/my-book/current/actors.yaml",
  "worldinfo_path": "f:/projects/epub2summary/runs/my-book/current/worldinfo.yaml",
  "total_chapters": 12
}
```

其中：

- `status=completed` 表示整本书已处理完成
- `processed_batches` 表示本次运行完成的批次列表
- `actors_path` 与 `worldinfo_path` 为最终正式 YAML 输出路径

## 5. 输出目录结构

每次运行会在工作区下生成：

```text
runs/
  <book_id>/
    source/
      original.epub
    extracted/
      chapters.jsonl
    current/
      actors.yaml
      worldinfo.yaml
    batches/
      0001/
        input.json
        prompt.txt
        raw_output.md
        delta.yaml
        merged_actors.preview.yaml
        merged_worldinfo.preview.yaml
        record.json
        review.json
    history/
      actors/
      worldinfo/
    state/
      run_state.json
      checkpoints.jsonl
      document_versions.jsonl
      review_queue.jsonl
```

重点说明：

- `current/actors.yaml`
  - 当前正式角色文档
- `current/worldinfo.yaml`
  - 当前正式世界设定文档
- `batches/<batch_id>/delta.yaml`
  - 模型返回的增量结果
- `batches/<batch_id>/raw_output.md`
  - 模型原始输出，排错时优先查看
- `batches/<batch_id>/merged_*.preview.yaml`
  - 每批合并预览结果
- `state/run_state.json`
  - 当前运行状态
- `state/checkpoints.jsonl`
  - 关键流程检查点日志

## 6. 保留的调试命令

除自动命令外，当前仍保留原有分步命令，便于调试：

### 初始化运行

```bat
python -m epub2yaml.app.cli init-run .\sample.epub --book-id my-book
```

### 处理下一批（手动传入 Delta YAML）

```bat
python -m epub2yaml.app.cli process-next-batch my-book --delta-file .\delta.yaml
```

### 审阅并提交某一批

```bat
python -m epub2yaml.app.cli review-batch my-book 0001 --action accept --reviewer tester
```

### 查看运行状态

```bat
python -m epub2yaml.app.cli show-status my-book
```

这些命令更适合开发调试，不是 MVP 的推荐主路径。

## 7. 常见失败场景

### 1）未提取到可处理章节

可能报错：

```text
未从 EPUB 中提取到可处理章节
```

排查建议：

- 确认 EPUB 文件本身可正常打开
- 确认章节正文不是空内容
- 确认章节抽取逻辑可处理该 EPUB 结构

### 2）缺少模型名称

可能报错：

```text
缺少模型名称，请设置环境变量 EPUB2YAML_MODEL 或通过参数传入 --model
```

处理方式：

- 设置 `EPUB2YAML_MODEL`
- 或在命令行显式传入 `--model`

### 3）缺少 API Key

可能报错：

```text
缺少 API Key，请设置环境变量 EPUB2YAML_API_KEY 或 OPENAI_API_KEY
```

处理方式：

- 设置 `EPUB2YAML_API_KEY`
- 或设置 `OPENAI_API_KEY`

### 4）模型调用失败

可能报错：

```text
模型调用失败: ...
```

处理方式：

- 检查 API Key 是否正确
- 检查 Base URL 是否可访问
- 检查模型名是否存在
- 检查网络连接与服务配额

### 5）模型输出非法 YAML

可能报错：

```text
Delta YAML 解析失败: ...
```

或：

```text
delta.actors 必须是映射
```

处理方式：

- 查看对应批次目录中的 `raw_output.md`
- 查看 `delta.yaml` 是否为合法 YAML
- 检查模型是否输出了额外说明文字或 Markdown 代码围栏

## 8. 推荐排错顺序

当自动生成失败时，建议按以下顺序排查：

1. 查看命令行输出的错误信息
2. 打开 `runs/<book_id>/state/run_state.json`
3. 打开 `runs/<book_id>/state/checkpoints.jsonl`
4. 查看最后一个批次目录下的 `raw_output.md`
5. 查看对应的 `delta.yaml` 与 `merged_*.preview.yaml`

## 9. 当前限制

当前文档对应的是最小可用版本，仍存在以下限制：

- 只处理单本 EPUB
- 不支持 Web UI
- 不支持人工审阅工作台
- 不支持拒绝后自动重试
- 不支持断点恢复
- 当前模型工厂仅实现 OpenAI 兼容提供方

## 10. 建议使用方式

生产前建议先用较短章节样本验证一轮：

1. 准备一份较小的 EPUB 文件
2. 配置模型环境变量
3. 执行 `generate-yaml`
4. 检查 `runs/<book_id>/current/actors.yaml`
5. 检查 `runs/<book_id>/current/worldinfo.yaml`
6. 如结果异常，再回看批次级中间产物进行排错
