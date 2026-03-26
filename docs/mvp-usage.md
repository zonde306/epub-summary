# EPUB2YAML MVP 使用说明

本文档说明当前版本的实际使用方式，包括环境准备、Textual 主控界面、CLI 辅助命令、输出目录结构与常见问题。

## 1. 功能概览

当前版本支持以下能力：

- 输入单个 EPUB
- 自动提取章节文本
- 自动按批次调用大模型生成 Delta YAML
- 自动将每批 Delta 合并到正式文档
- 自动输出最终 `actors.yaml` 与 `worldinfo.yaml`
- 支持暂停、恢复、失败重试
- 支持导出人工修订工作区、自动打开编辑器并等待退出
- 支持通过 Textual CUI 在运行中发送控制命令

当前推荐的**主入口**是 Textual 控制台，CLI 中的 `control-ui` 是它的启动命令。注意：UI 内部会自动加载模型工厂配置，因此只要环境变量已配置，初始化后即可直接点击 `Start / Continue`。另外，当你在运行中触发人工修订并关闭编辑器后，如果修订校验通过，后台任务会自动继续处理，无需再次点击 `Start / Continue`。现在 UI 还会记住你上次使用的 EPUB 路径与 `book_id`，显示最近运行任务与当前正式 `actors.yaml` 中的人物列表，并支持直接点击最近任务按钮回填。

## 2. 先理解交互模型

这一步非常重要。

### 2.1 哪个入口是主入口

当前推荐用法是：

- 用 `python -m epub2yaml.app.cli control-ui` 打开 Textual 主控界面
- 在界面里创建任务
- 在界面里启动任务
- 在界面里暂停、请求人工修订、恢复运行

### 2.2 为什么 `generate-yaml` 不能交互

`generate-yaml` 是一次性前台命令，它会阻塞当前终端直到任务结束。

因此：

- 它适合脚本式调用
- 它不适合作为“运行中持续控制”的主入口
- 如果你希望在运行时暂停、人工修订、恢复，应该使用 `control-ui`

### 2.3 暂停为什么不是“立刻强停”

当前暂停语义是**协作式中断**，不是进程级强杀。

也就是说：

- 你发送 `pause` 后，系统不会粗暴终止 Python 进程
- 它会在工作流安全边界生效
- 安全边界包括构造上下文、构造提示词、模型返回后、解析后、合并后、写审阅产物前后等阶段

因此“不是毫秒级立停”是符合设计的，但现在已经不是“等整本书跑完才停”了。

## 3. 前置条件

运行前需要满足：

1. 已安装 Python 运行环境
2. 已安装项目依赖
3. 已安装并配置可用的 LangChain OpenAI 适配依赖
4. 若需要 CUI，已安装 `textual`
5. 已准备可访问的模型 API Key
6. 若需要自动编辑器唤起，已配置可等待退出的编辑器命令

当前模型工厂实现位于 `src/epub2yaml/llm/model_factory.py`，默认按 OpenAI 兼容接口初始化聊天模型。

## 4. 环境变量配置

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
- `EPUB2YAML_EDITOR`
  - 人工修订编辑器命令模板，例如 `code --wait {file}` 或 `notepad {file}`
- `VISUAL`
  - 当未设置 `EPUB2YAML_EDITOR` 时的回退编辑器命令
- `EDITOR`
  - 当未设置 `EPUB2YAML_EDITOR` 与 `VISUAL` 时的回退编辑器命令
- `OPENAI_API_KEY`
  - 当未设置 `EPUB2YAML_API_KEY` 时可作为回退值
- `OPENAI_BASE_URL`
  - 当未设置 `EPUB2YAML_BASE_URL` 时可作为回退值

### Windows CMD 示例

```bat
set EPUB2YAML_MODEL=gpt-4o-mini
set EPUB2YAML_API_KEY=your_api_key
set EPUB2YAML_BASE_URL=https://api.openai.com/v1
set EPUB2YAML_EDITOR=code --wait {file}
```

## 5. 推荐主流程：使用 Textual 控制台

### 5.1 启动主控界面

```bat
python -m epub2yaml.app.cli control-ui
```

也可以直接带一个已有 `book_id` 启动：

```bat
python -m epub2yaml.app.cli control-ui my-book
```

### 5.2 进入界面后的正确操作顺序

进入 UI 后，按这个顺序操作：

1. 在第一个输入框填 EPUB 文件路径
2. 在第二个输入框填 `book_id`
   - 可以手填
   - 也可以先留空，初始化后自动回填
3. 点击 `Init Run`
4. 点击 `Start / Continue`
5. 运行中如需人工介入：
   - 点击 `Pause`
   - 或点击 `Prepare Manual Edit`
6. 若已进入人工修订：
   - 编辑器关闭后，若修订已成功应用，可继续点击 `Start / Continue`
   - 若编辑器未成功打开或需要再次修改，可点击 `Open Manual Edit Workspace`

### 5.3 键盘操作

当前 UI 支持以下快捷键：

- `Ctrl+I`
  - 初始化任务
- `Ctrl+S`
  - 启动或继续后台任务
- `Ctrl+P`
  - 请求暂停
- `Ctrl+M`
  - 请求人工修订
- `Ctrl+O`
  - 重新打开人工修订工作区
- `R`
  - 刷新状态
- `F5`
  - 刷新状态
- `Q`
  - 退出
- `Tab`
  - 在输入框、按钮之间切换焦点

### 5.4 UI 中各按钮的语义

- `Init Run`
  - 创建运行目录，提取章节，初始化状态文件
- `Start / Continue`
  - 在后台线程中执行 `PipelineService.run_to_completion()`
  - UI 本身不会被阻塞
- `Pause`
  - 只发送控制请求
  - 真正停下发生在工作流安全边界
- `Prepare Manual Edit`
  - 请求在当前批次安全边界切入人工修订流程
- `Resume`
  - 当前 UI 里实际继续执行仍推荐点 `Start / Continue`
  - 若你只想计算恢复决策，可用 CLI 的 `resume-run`
- `Open Manual Edit Workspace`
  - 重新打开人工修订文件

### 5.5 UI 里的状态含义

常见状态：

- `initialized`
  - 已初始化但还没开始
- `running`
  - 正在运行
- `paused`
  - 已在安全边界暂停
- `review_required`
  - 当前批次已生成，等待审阅/提交
- `awaiting_manual_edit`
  - 已进入人工修订流程，等待你修改并应用
- `completed`
  - 全部章节处理完成
- `failed`
  - 当前运行失败，可查看失败批次与阶段信息

## 6. CLI 命令的定位

### 6.1 交互主入口

- `control-ui`

这是推荐入口。

### 6.2 非交互或调试命令

以下命令仍然可用，但更适合调试或脚本调用：

- `init-run`
- `generate-yaml`
- `process-next-batch`
- `resume-run`
- `pause-run`
- `prepare-manual-edit`
- `open-manual-edit-workspace`
- `apply-manual-edit`
- `continue-after-manual-edit`
- `retry-last-failed`
- `retry-batch`
- `review-batch`
- `show-status`

### 6.3 什么时候还会用到 CLI 命令

典型场景：

- CI 或脚本里直接跑 `generate-yaml`
- 你只想发一个暂停请求，可单独调用 `pause-run`
- 你想在 UI 外手工处理人工修订，可用 `prepare-manual-edit` / `apply-manual-edit`
- 你想查看当前恢复决策，可用 `show-status`

## 7. 一次性自动命令

如果你不需要交互控制，也可以继续使用：

```bat
python -m epub2yaml.app.cli generate-yaml <EPUB路径> --book-id <书籍ID>
```

它适合：

- 小规模测试
- 脚本自动跑批
- 不需要暂停/人工修订时

它不适合：

- 运行中暂停
- 运行中切入人工修订
- 用同一个窗口做持续交互控制

## 8. 运行控制命令

### 暂停运行

```bat
python -m epub2yaml.app.cli pause-run my-book
```

### 恢复运行

```bat
python -m epub2yaml.app.cli resume-run my-book
```

### 准备人工修订

```bat
python -m epub2yaml.app.cli prepare-manual-edit my-book
```

只导出工作区而不自动打开编辑器：

```bat
python -m epub2yaml.app.cli prepare-manual-edit my-book --no-editor
```

### 重新打开人工修订工作区

```bat
python -m epub2yaml.app.cli open-manual-edit-workspace my-book
```

### 应用人工修订

```bat
python -m epub2yaml.app.cli apply-manual-edit my-book
```

### 人工修订后继续同批次

```bat
python -m epub2yaml.app.cli continue-after-manual-edit my-book --delta-file .\delta.yaml
```

## 9. 输出目录结构

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
    manual_edit/
      active_session.json
      actors.editable.yaml
      worldinfo.editable.yaml
      note.txt
    state/
      run_state.json
      checkpoints.jsonl
      document_versions.jsonl
      review_queue.json
      review_history.jsonl
```

重点说明：

- `current/actors.yaml`
  - 当前正式角色文档
- `current/worldinfo.yaml`
  - 当前正式世界设定文档
- `batches/<batch_id>/delta.yaml`
  - 模型返回的增量结果
- `batches/<batch_id>/merged_*.preview.yaml`
  - 每批合并预览结果
- `manual_edit/actors.editable.yaml`
  - 人工修订基线角色文档
- `manual_edit/worldinfo.editable.yaml`
  - 人工修订基线世界设定文档
- `manual_edit/active_session.json`
  - 当前人工修订会话元数据
- `state/run_state.json`
  - 当前运行状态
- `state/checkpoints.jsonl`
  - 关键流程检查点日志

## 10. 常见失败场景

### 1）UI 能打开，但没法操作

请先确认：

- 终端支持交互式 TUI
- `textual` 已正确安装
- 焦点在输入框或按钮上，可用 `Tab` 切换
- 不要用 `generate-yaml` 代替 `control-ui`

### 2）发送了暂停，但没有“瞬间停下”

这是正常的。

当前是协作式控制，不是强杀：

- 会在安全边界停下
- 不会在写文件中途硬切
- 现在已经支持在单批工作流内部多个边界点生效

### 3）模型输出非法 YAML

可能报错：

```text
Delta YAML 解析失败: ...
```

或：

```text
delta.actors 必须是映射
```

### 4）人工修订文件非法

可能报错：

```text
actors.yaml 根节点必须是映射
```

处理方式：

- 检查 `runs/<book_id>/manual_edit/actors.editable.yaml`
- 检查 `runs/<book_id>/manual_edit/worldinfo.editable.yaml`
- 修正后重新执行 `apply-manual-edit` 或 `resume-run`

### 5）编辑器无法自动打开

处理方式：

- 检查 `EPUB2YAML_EDITOR`
- 确保命令支持等待退出语义
- 改用 `open-manual-edit-workspace`
- 手工编辑 `runs/<book_id>/manual_edit/` 后再恢复

## 11. 推荐排错顺序

当自动生成失败时，建议按以下顺序排查：

1. 查看 UI 日志区或命令行输出
2. 打开 `runs/<book_id>/state/run_state.json`
3. 打开 `runs/<book_id>/state/checkpoints.jsonl`
4. 查看最后一个批次目录下的 `raw_output.md`
5. 查看对应的 `delta.yaml` 与 `merged_*.preview.yaml`
6. 若处于人工修订流程，再检查 `manual_edit/active_session.json`

## 12. 当前限制

当前实现仍存在以下限制：

- 只处理单本 EPUB
- 不支持 Web UI
- Textual 为本地单进程控制台，不是远程任务面板
- 工作流仍是单批执行 + 服务层恢复，不是图内长期挂起
- 编辑器自动打开当前以单文件入口为主
- 当前模型工厂仅实现 OpenAI 兼容提供方

## 13. 建议使用方式

建议按以下方式工作：

1. 配置模型环境变量与编辑器环境变量
2. 启动 `control-ui`
3. 在 UI 中输入 EPUB 路径与 `book_id`
4. 点击 `Init Run`
5. 点击 `Start / Continue`
6. 如需人工介入，在 UI 中点击 `Pause` 或 `Prepare Manual Edit`
7. 查看 `runs/<book_id>/current/actors.yaml`
8. 查看 `runs/<book_id>/current/worldinfo.yaml`
9. 如结果异常，再回看批次级中间产物与人工修订工作区进行排错
