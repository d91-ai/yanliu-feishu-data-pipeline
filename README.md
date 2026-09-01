# 研流：会议研究数据管线

研流是一套基于飞书自建应用、飞书 CLI、多维表格 Workflow 与可组合 AI Skill 的会议研究数据管线。它将会议纪要转化为可审核、可追溯、可写回的行业与市场观点及标的观点。

## 工作流程

1. 用户通过 Base 表单或上传 API 登记会议日期、系列、类型和会议纪要 Markdown。
2. Router 创建任务，Worker 分别调用行业与市场观点 Skill、标的观点 Skill。
3. AI 生成待审核 Markdown，并标记需要人工确认的内容。
4. 审核通过后生成正式 JSON，写回 Base，归档 Drive 版本并更新状态。

## 核心实现

- `.implementation/meeting-pipeline-contract/`：统一字段、状态、产物和元数据契约。
- `.implementation/meeting-pipeline-worker/`：任务状态机、双路生成、人工审核和 Base/Drive 提交。
- `.implementation/meeting-minutes-structured-table-current/`：现行标的观点 Skill、Prompt、Schema 与运行代码。
- `.implementation/meeting-pipeline-ops/`：配置生成、迁移、基线修复和发布检查。
- `server/`：显式上传入口。
- `.implementation/feishu-minute-sanitize/`：可选会议纪要脱敏分支。
- `sync/local-vault-mirror/`：可选本地只读镜像工具。

## 部署原则

部署者需要创建自己的飞书自建应用、Base、字段和 Workflow。复制各模块的 `.env.example` 为本地 `.env`，填写自己的资源标识和密钥；不得将 `.env`、业务数据、日志、真实会议材料或凭据提交到 Git。

三个运行 Skill 已嵌入本公开仓库；`.implementation/meeting-pipeline-worker/skill-repositories.v1.json` 仅用于可选的只读更新跟踪，不会覆盖已部署运行树。

仓库内的公司、证券代码、发言人、会议日期和资源标识均为合成示例。兼容结构化表格脚本可通过 `--target-lexicon` 读取部署者在仓库外维护的 UTF-8 CSV（`target_name`、`aliases`）或逐行标的词表；不要把真实证券主数据随公开仓库提交。

## 本地验证

```bash
python3 -B -m unittest discover -s .implementation/meeting-pipeline-contract/tests -p 'test_*.py'
python3 -B -m unittest discover -s .implementation/meeting-pipeline-worker/tests -p 'test_*.py'
python3 -B -m unittest discover -s .implementation/meeting-pipeline-ops/tests -p 'test_*.py'
```

所有外部写入命令必须显式使用 `--apply`。默认检查和 dry-run 不应访问或修改任何真实飞书环境。

## 公开部署 Quick Start

完整的安装、配置、运行与验收说明见 [`docs/研流_操作指南.docx`](docs/研流_操作指南.docx)。

1. 阅读 [`docs/公开部署配置.md`](docs/公开部署配置.md)，创建部署者自己的飞书应用、Base、Drive 目录和字段绑定。
2. 阅读 [`docs/多维表格Workflow搭建.md`](docs/多维表格Workflow搭建.md)，配置只负责提醒和审核协作的 Workflow。
3. 使用 `.implementation/meeting-pipeline-ops/prepare_public_worker_env.py` 自动填入公开运行资产路径和 SHA256。
4. 完成 Router 事件订阅，再依次启动 Router 与 Worker。
5. 使用 `provision_collaboration_workflows.py` 创建并启用五条协作 Workflow，或在页面逐条“保存并启用”。
6. 使用 `.implementation/meeting-pipeline-ops/deployment/` 配置 Router、Worker、漏事件补偿器的常驻和重启恢复。
7. 按 [`docs/端到端快速验收.md`](docs/端到端快速验收.md) 使用合成纪要跑通上传、双路生成、审核、JSON 写回、版本归档和重启恢复。

在填写任何真实配置前可运行只读完整性检查：

```bash
python3 -B tools/check_public_deployment.py
```
