# schwab-marketdata-mcp

[English](./README.md) | [简体中文](./README_zh.md)

[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](./README.md)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](#运行环境要求)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#运行环境要求)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

生产级 **Model Context Protocol（MCP）** 服务端，把 Charles Schwab 的
**Market Data Production** API 封装为 **12 个 tool**（10 个 endpoint + 2 个元
工具），可在 Cursor、Claude Code 以及任何支持 MCP 的 agent 中调用。

> **只读** —— 本项目仅调用 Schwab Market Data API，**不**调用 Schwab Trader
> API，**不会**下单。Schwab 服务条款相关说明见 [合规使用](#合规使用)。

---

## 项目概述

`schwab-marketdata-mcp` 是双仓库系统的服务端：

- **本仓库** —— MCP 服务端。负责 OAuth、限流、重试与超时退避、refresh token
  轮换、结构化错误映射、stdio 流保护。
- **配套仓库** —— [`schwab-marketdata-skill`](../schwab-marketdata-skill)
  提供两个 Cursor / Claude **Skill**，分别对应单工具调用（`ops`）和多步
  playbook（`workflows`）。

服务端在非官方 SDK [`schwab-py`](https://github.com/alexgolec/schwab-py)
之上做了一层生产级加固，满足常驻 MCP host 的稳定性需求：

- 用 `fcntl.flock` 实现跨进程安全的原子 refresh token 轮换。
- 每次读取 token 文件都校验权限位（强制 `0600`）。
- 进程内限流器（默认 120 req/min，可按调用方自定义）。
- 自适应 429 / 5xx 重试，指数退避，并解析 `Retry-After` 头。
- 结构化错误体系（`SchwabAuthError`、`SchwabRateLimitError` 等），
  让 agent 能输出可操作的提示而不是 stack trace。
- stdio 流保护，确保日志不会污染 JSON-RPC 通道。

完整架构与威胁模型请见 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)。

---

## 快速开始

> **第 0 步（必须）** —— 在把任何凭证拷进工作目录**之前**，先安装 pre-commit
> hook：
>
> ```bash
> uv sync --extra dev
> uv run pre-commit install
> ```

```bash
# 1. 同步依赖（使用仓库里 commit 过的 uv.lock）
uv sync --extra dev

# 2. 安装 pre-commit hooks（gitleaks + detect-secrets + ruff + mypy）
uv run pre-commit install

# 3. 配置 Schwab Developer Portal 的应用凭证
cp .env.example .env
# 然后编辑 .env，把 `dummy-not-a-real-secret` 占位符替换为
# https://developer.schwab.com/dashboard/apps 上拿到的真实值

# 4. 一次性 OAuth 登录（会打开浏览器，自签名证书页面点继续即可）
uv run python -m schwab_marketdata_mcp.auth login_flow
# 或（容器/无界面环境）：manual_flow
uv run python -m schwab_marketdata_mcp.auth manual_flow

# 5. 配置健康检查任务（每个项目一次，参考 docs/cron.example）
#    macOS：把 launchd plist 拷到 ~/Library/LaunchAgents/
#    Linux：用 `crontab -e` 追加 crontab 片段

# 6. 验证整体可用
uv run python -m schwab_marketdata_mcp.health   # 健康时退出码 0
uv run pytest --cov                              # 整体 ≥85%，关键模块 100%
```

完整的客户端注册指南（Cursor / Claude Code / VS Code / Claude Desktop）见
[`docs/REGISTER.md`](docs/REGISTER.md)。

---

## 功能特性

### 认证与 token 生命周期

- **OAuth 2.0 授权码流程**，提供两种易用的 CLI：`login_flow`（自动打开浏览
  器并捕获回调）与 `manual_flow`（手动粘贴 URL，适用于无图形界面或容器
  环境）。
- **原子 refresh token 轮换** —— 每次刷新都先写入临时文件，再用
  `os.replace` 替换正式 token 文件，整个过程被 `fcntl.flock` 保护，多个
  agent 并发时不会出现竞争或文件损坏。
- **权限位审计** —— 拒绝加载组可读或全局可读的 token 文件；抛出
  `SchwabAuthError(reason="insecure_token_perms")` 并附带可直接复制的
  `chmod 600` 提示。
- **7 天 refresh window 检测** —— 把 Schwab 模糊的 `invalid_grant` 翻译为
  `SchwabAuthError(reason="refresh_token_expired")`，agent 据此提示
  「请重新连接 Schwab」而不是死循环重试。

### 可靠性

- **进程级限流器** —— token-bucket 算法，默认 120 req/min，可通过环境变量
  `SCHWAB_RATE_LIMIT_PER_MIN` 调整。
- **自适应重试**：对 `429` 与 `5xx` 默认重试 2 次，指数退避，并解析
  `Retry-After` 头。
- **健康检查**（`schwab_marketdata_mcp.health`）针对 token 寿命、丢失、
  格式错误、权限不安全等情况返回不同退出码，可直接接入 cron / launchd
  告警。
- **滚动文件日志**：写入 `${XDG_STATE_HOME}/schwab-marketdata-mcp/logs/`
  （10 MB × 5），无论 MCP host 是否支持 `stderr` 字段。

### 安全

- **stdio 流保护** —— `bootstrap_dotenv()` 在任何 `print()` 之前先执行，
  确保 `.env` 加载过程不会污染 JSON-RPC 通道。
- **路径注入防护** —— 故意**不支持** `SCHWAB_TOKEN_PATH` 环境变量，避免
  通过 `mcp.json` 实施路径注入；如需覆盖路径请用 `--config-dir` CLI 参数。
- **Pre-commit 守卫** —— 每次 commit 都会跑 `gitleaks`、`detect-secrets`、
  `ruff`、`mypy` 与 `markdownlint`，并 commit 了 `.secrets.baseline`。
- **不可二次分发数据约束** —— 配套的 workflows skill 在写入前会先调用
  `gh repo view --json isPrivate`，拒绝把 Schwab 数据写入公开仓库。

### 工具面 —— 12 个 MCP tool

| #  | Tool                          | Endpoint                                   |
| -- | ----------------------------- | ------------------------------------------ |
| 1  | `get_quote`                   | `GET /{symbol_id}/quotes`                  |
| 2  | `get_quotes`                  | `GET /quotes`                              |
| 3  | `get_price_history`           | `GET /pricehistory`                        |
| 4  | `get_option_chain`            | `GET /chains`                              |
| 5  | `get_option_expiration_chain` | `GET /expirationchain`                     |
| 6  | `get_market_hours`            | `GET /markets`                             |
| 7  | `get_market_hour_single`      | `GET /markets/{market_id}`                 |
| 8  | `get_movers`                  | `GET /movers/{symbol_id}`                  |
| 9  | `search_instruments`          | `GET /instruments`                         |
| 10 | `get_instrument_by_cusip`     | `GET /instruments/{cusip_id}`              |
| 11 | `health_check`                | 本地 —— token 寿命 + 最近错误数            |
| 12 | `get_server_info`             | 本地 —— 版本号 + 支持的 tool 列表          |

---

## 运行环境要求

| 依赖 | 版本 | 说明 |
| ---- | ---- | ---- |
| Python | `>=3.11` | 类型注解使用 PEP 695 语法。 |
| `uv`   | `>=0.4` | 用于环境管理与 lockfile 锁定的安装。 |
| 操作系统 | macOS 11+ / Linux / WSL2 | 跨进程 token 锁依赖 `fcntl.flock`。 |
| Schwab Developer 账号 | — | 必须自行在 <https://developer.schwab.com/dashboard/apps> 注册应用。 |

### 平台支持

|              | macOS 11+ | Linux | WSL2（Linux 子系统） | Windows 原生 |
| ------------ | :-------: | :---: | :------------------: | :----------: |
| **v1（当前）** |     ✅    |   ✅  |   ✅（避开 `/mnt/c`） |       ❌      |
| **v2（待定）** |     ✅    |   ✅  |          ✅           | ⏳（计划：`msvcrt.locking`） |

Windows 原生支持需要把 `fcntl.flock` 替换为 `msvcrt.locking`，已列入 v2
路线图。WSL2 当前可用，但请将仓库放在 Linux 文件系统上（避开 `/mnt/c`，
那里 `flock` 行为不稳定）。

---

## 架构

```text
┌─────────────────────┐      stdio (JSON-RPC)      ┌─────────────────────┐
│  Cursor / Claude    │ ─────────────────────────▶ │  schwab-marketdata- │
│  / Claude Code      │ ◀───────────────────────── │  mcp（本仓库）      │
└─────────────────────┘                            └──────────┬──────────┘
                                                              │
                                              HTTPS + OAuth   │
                                                              ▼
                                                  ┌─────────────────────┐
                                                  │ Schwab Market Data  │
                                                  │ Production API      │
                                                  └─────────────────────┘
```

完整数据流图、威胁模型与信任边界见
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)。

---

## 集成

按你使用的 MCP 客户端，参考 [`docs/REGISTER.md`](docs/REGISTER.md) 中对应
段落：

- Cursor —— `~/.cursor/mcp.json`
- Claude Code —— `~/.claude/mcp.json`
- VS Code（Continue / Cline）—— workspace 设置
- Claude Desktop —— `~/Library/Application Support/Claude/claude_desktop_config.json`

> **关键注意事项**（详见 `docs/REGISTER.md`）：
>
> - 如果客户端不展开 `${HOME}`，请用 `echo $HOME` 的输出替换为绝对路径。
> - 如果使用 mise / pyenv，把 `${HOME}/.local/bin/uv` 替换为 `which uv`
>   返回的绝对路径。
> - **不要**把 `SCHWAB_TOKEN_PATH` 加进 `env` —— 这个变量被故意设计为
>   **不支持**，目的是避免通过 `mcp.json` 实施路径注入。如需覆盖路径，
>   请用 `--config-dir` CLI 参数。
> - **绝对不要**把 `schwab_marketdata_mcp.auth` 注册成 MCP server ——
>   auth CLI 用 stdout 与浏览器对话，会污染 JSON-RPC 流。

---

## 开发

```bash
# 同步依赖、安装 hooks
cd /path/to/schwab-marketdata-mcp
uv sync --extra dev
uv run pre-commit install

# 跑完整本地 CI（与 GitHub Actions 行为一致）
bash scripts/local-ci.sh

# 也可以单独跑某一阶段
uv run ruff check .
uv run mypy src
uv run pytest --cov
uv run pre-commit run --all-files
```

`scripts/local-ci.sh` 会依次执行 lint、类型检查、带覆盖率的完整测试、
pre-commit hooks 与 `markdownlint-cli2`，任何一步失败都会以非零退出码
中止。CI 在每次 push 上跑同一个脚本。

---

## 测试

| 指标 | 目标 | 当前 |
| ---- | ---- | ---- |
| 测试数量 | ≥ 200 | 已收集 **250** |
| 整体覆盖率 | ≥ 85% | 由 `--cov-fail-under=85` 强制 |
| 关键模块覆盖率 | 100% | `auth.py`、`rate_limiter.py`、`health.py` |
| OWASP 覆盖 | 2017 + 2021 + 2025 | 由 `tests/security/` 中的矩阵跟踪 |

测试分类：

- **单元测试** —— `tests/unit/`（按模块组织，mock Schwab API）。
- **集成测试** —— `tests/integration/`（真实调用 `schwab-py`，通过
  `vcrpy` 录制的 cassette 回放）。
- **安全测试** —— `tests/security/`（OWASP Top 10 矩阵：注入、认证失效、
  敏感数据泄露、SSRF 等）。
- **边界测试** —— 空值、最大值、畸形输入、时区边界等。
- **异常测试** —— 把每个 `SchwabAuthError` / `SchwabRateLimitError` /
  `SchwabRetryableError` 的 reason code 都跑一遍。

运行某个子集：

```bash
uv run pytest tests/security -v
uv run pytest -k "rate_limit" --cov
uv run pytest --cov --cov-report=html  # 输出到 htmlcov/
```

---

## 健康检查（cron / launchd）

```bash
uv run python -m schwab_marketdata_mcp.health
# 退出码：0=健康，1=token 寿命 < 24h，2=< 12h 或已过期，
#         3=token 缺失，4=token 格式错误，5=权限不安全
```

参考 [`docs/cron.example`](docs/cron.example)，里面有可直接粘贴的
**launchd plist**（周日 20:00 + 周三 21:00 + 每 4 小时一次的 fallback，
覆盖合盖待机场景）以及 **crontab** 片段。

安装完成后跑一次 `bash scripts/notifier-self-test.sh`，确认
`osascript`（macOS）或 `notify-send`（Linux）能正常弹通知。

---

## 故障排查

| 现象 | 处理方式 |
| ---- | -------- |
| Cursor 报 `Invalid JSON` 在第一个字节 | 大概率是把 `auth` 注册成 MCP server，重新注册即可。 |
| `SchwabAuthError(reason="refresh_token_expired")` | 跑 `uv run python -m schwab_marketdata_mcp.auth login_flow`。 |
| `SchwabAuthError(reason="insecure_token_perms")` | 按错误信息里给出的 `chmod 600 …` 提示执行。 |
| agent 收到 `429 Rate limit exceeded` | 已经自动重试 2 次；如果仍频繁出现，调低 `SCHWAB_RATE_LIMIT_PER_MIN`。 |

---

## 合规使用

本服务调用 Schwab Market Data Production API。**你**有义务阅读并遵守：

- <https://www.schwab.com/legal/terms> —— Schwab 在线服务协议
- <https://developer.schwab.com/legal> —— Developer Portal 条款（需登录查看）

特别注意：

- Schwab 行情数据**不可二次分发**。本服务（或 workflows skill）写出的任何
  markdown / 报告**必须**保存在**私有**仓库中。配套的 workflows skill 在
  写入前会强制调用 `gh repo view --json isPrivate` 校验。
- `schwab-py` 是**非官方**封装；本项目对其与 Schwab 服务条款的合规性不作
  任何保证。你必须在 Schwab Developer Portal 注册自己的应用。

完整威胁模型见 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)，相关 ToS
原文摘录见配套 skill 仓库的 `references/tos-snapshot.md`。

---

## 相关项目

- [`schwab-marketdata-skill`](../schwab-marketdata-skill) —— 配套的 Cursor /
  Claude Skill 仓库，提供 `ops`（单工具调用）与 `workflows`（多步
  playbook）两个 skill，并附带英文镜像。
- [`docs/REGISTER.md`](docs/REGISTER.md) —— 完整客户端注册指南。
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) —— 架构与威胁模型。
- [`docs/cron.example`](docs/cron.example) —— 健康检查的 launchd / crontab
  模板。

---

## 致谢与上游引用

本项目构建于多个优秀开源库之上：

- **[schwab-py](https://github.com/alexgolec/schwab-py)**（作者
  Alex Golec）—— Charles Schwab Trader / Market Data API 的非官方
  Python wrapper（MIT 许可证）。处理三段式 OAuth、token 自动 refresh，
  并为所有 Schwab Market Data Production endpoint 提供类型枚举。若无
  `schwab-py`，本 MCP 服务端需多花 ~1.5 天工作量且稳定性会显著降低。
- **[mcp](https://github.com/modelcontextprotocol/python-sdk)** ——
  Anthropic 官方的 Model Context Protocol Python SDK（MIT 许可证）。
- **[httpx](https://github.com/encode/httpx)** —— 现代 HTTP 客户端
  （BSD-3-Clause 许可证）。
- **[pydantic](https://github.com/pydantic/pydantic)** —— 基于 Python
  类型注解的数据校验库（MIT 许可证）。

本项目**与 Charles Schwab Corporation 或 Alex Golec 无关，未受其背书或
赞助**。使用前请阅读 Schwab 的
[服务条款](https://www.schwab.com/legal/terms) 并自行承担风险。

---

## License

MIT License —— 详见 [LICENSE](./LICENSE)。
