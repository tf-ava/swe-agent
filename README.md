# swe-agent

## 新版本开发进度

项目正在重构。安装后的 `swe-agent` 已切换到 `cli_new.py`，当前提供 `init` 和 session 管理命令；新版 `ask` 和交互式 `chat` 将逐步接入。旧入口和编排代码保留供参考，本文后面的旧版功能说明不代表新入口当前可用的命令。

更新安装后初始化：

```powershell
uv sync
swe-agent init
```

也可以直接调用新入口：

```powershell
python -m swe_agent.cli_new init
```

默认在用户目录的 `.sweagent` 下创建 `config.toml`、`.env` 和 `sessions.sqlite3`。如需指定位置，在启动前设置环境变量：

```powershell
$env:SWE_AGENT_HOME = "D:\AgentData"
swe-agent init
```

初始化后，编辑 `config.toml` 中的模型名称和服务地址，在数据目录的 `.env` 中填写配置所引用的密钥。初始化不要求已有密钥，不请求模型，也不创建 session。

重复运行会保留已有配置、密钥文件和会话数据，只补建缺失资源。配置无效或数据库结构不兼容时会报错；中途失败不会整体回滚已经创建的文件，修正问题后可重新运行。初始化不会加载当前项目的 `.env`，集中目录中的密钥读取将在模型执行接入时实现。

### Session 管理

```powershell
# 创建空会话；省略 --path 时使用当前工作目录。
swe-agent session create demo --path "D:\projects\example"

# 仅修改明确指定的配置项，其余字段保留。
swe-agent session config demo --model "provider/model" --max-steps 20

# 查看保存的会话属性和配置，以 JSON 显示，不加载对话历史。
swe-agent session show demo

# 按最近更新时间倒序查询；先显示前 20 条，再跳过前 20 条查询。
swe-agent session list --limit 20 --offset 0
swe-agent session list --limit 20 --offset 20
```

配置修改支持 `--model`、`--base-url`、`--api-key-env`、`--compress-model`、`--context-token-limit`、`--keep-recent-messages`、`--max-steps`。`--api-key-env` 是密钥环境变量名，不是密钥值。多字段修改整体校验通过才保存；不传修改项会提示使用 `session show`。

创建会话时从全局配置复制默认值，之后全局配置变化不影响已有会话。工作目录在创建时确定。重复 ID、无效工作目录或不存在的会话都会报错。所有会话命令都要求已有数据库，不自动初始化；查询和修改已有会话不依赖全局配置文件或 `.env`，也不请求模型。

列表仅显示 ID、状态、工作目录、最近更新时间；时间包含本地时区偏移，不生成会话标题或对话总结。`offset` 表示跳过多少条记录，持续有更新时分页结果可能移动。对话 history 查询和删除命令尚未实现。

## 原版本说明（供参考）

一个最小可用的 SWE Agent CLI。它可以在指定工作目录中读取代码、搜索代码、查看 git 状态、运行命令、生成文件 diff，并在用户确认后执行高风险操作。
。

## 功能

- `ask`：向 agent 提问，或让 agent 执行代码任务。
- `trace`：查看某个 session 的历史消息、状态和 pending tool call。
- SQLite session 记忆：对话历史存储在工作目录下的 `.sweagent/sessions.sqlite3`。
- 上下文压缩：当上下文过长时，使用压缩模型总结历史。
- 路径安全：工具只能在指定工作目录内操作路径。
- 高风险工具确认：`write_file`、`delete_file`、`run_command` 需要用户确认后才会执行。

## 安装

本项目需要 Python 3.12+。

```bash
uv sync
```

安装后可以使用：

```bash
swe-agent --help
```

## 环境变量

复制 `.env.example` 为 `.env`，并填写配置：

```env
BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=your_api_key
MODEL=openrouter/deepseek/deepseek-v4-flash
COMPRESS_MODEL=openrouter/deepseek/deepseek-v4-flash
CONTEXT_TOKEN_LIMIT=120000
KEEP_RECENT_MESSAGES=12
```

说明：

- `BASE_URL`：LiteLLM 请求使用的模型服务地址。
- `OPENROUTER_API_KEY`：模型服务 API key。
- `MODEL`：主 agent 使用的模型。
- `COMPRESS_MODEL`：上下文压缩使用的模型。
- `CONTEXT_TOKEN_LIMIT`：触发上下文压缩的 token 阈值。
- `KEEP_RECENT_MESSAGES`：压缩时保留的最近消息数量。

## 使用方法

### ask

`ask` 是核心命令。必须指定 `session_id`，可选指定工作目录和模型。

```bash
swe-agent ask "帮我解释这个项目的结构" --session-id demo
```

指定工作目录：

```bash
swe-agent ask "运行测试并告诉我失败原因" --session-id demo --path /path/to/project
```

指定模型：

```bash
swe-agent ask "查找登录逻辑在哪里" --session-id demo --model openrouter/deepseek/deepseek-v4-flash
```

当 agent 请求执行需要确认的工具时，CLI 会返回 pending 信息。确认执行可以继续输入：

```bash
swe-agent ask "确认" --session-id demo
```

拒绝执行则输入其他内容：

```bash
swe-agent ask "不要执行" --session-id demo
```

### trace

查看某个 session 的历史上下文：

```bash
swe-agent trace --session-id demo
```

指定工作目录：

```bash
swe-agent trace --session-id demo --path /path/to/project
```

## 工作目录与路径安全

如果传入 `--path`，agent 会在该目录中工作；如果不传，默认使用当前命令执行目录。

所有文件工具都会把路径限制在工作目录内部。类似 `../` 或绝对路径逃出工作目录的访问会被拒绝。

注意：`run_command` 会固定在工作目录内执行，并且属于需要用户确认的工具。MVP 版本只做基础命令拦截和工作目录限制，不提供完整沙箱。

## 项目结构

```text
src/swe_agent/
  main.py                    CLI 入口
  cli.py                     Typer 命令定义
  llm.py                     Agent 编排循环
  prompt.py                  Agent 和上下文压缩提示词
  sqlite_session_manager.py  SQLite session 记忆和上下文压缩
  workspace.py               工作目录路径安全
  tools/
    __init__.py              工具注册和 tool schemas
    file_tools.py            文件读取、搜索、diff、写入、删除
    command_tools.py         命令执行
    git_tools.py             git 状态和 diff
    project_tools.py         项目探查
```

## 当前限制

- 这是 MVP，不是完整沙箱。
- `search_text` 依赖本机安装 `rg`。
- 高风险操作需要用户确认，但用户仍应检查 diff 和命令内容。
- 上下文压缩依赖 `COMPRESS_MODEL` 可用。
