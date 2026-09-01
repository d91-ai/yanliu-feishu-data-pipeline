# Unified Meeting Pipeline Worker Candidate

该目录实现统一管线的任务状态机、候选 Skill 调用层、Feishu backend 和
默认关闭的常驻服务入口。backend 只有同时满足命令行显式 `--apply` 与
`FEISHU_UNIFIED_PIPELINE_ENABLED=true` 才允许提交；`check`不会写入
Base/Drive。

核心保证：

- 每个生成任务绑定 `meeting_uid + artifact_type + data_version + input token + SHA`；
- 模型执行前和提交前均 fresh-read，过期任务进入 `stale`；
- 三条审核按 `artifact_type + reviewed MD SHA` 幂等；
- 每次新的人工审核只递增一次全局 `data_version`；
- 源纪要审核后自动排入新版本的行业/市场与标的观点任务；
- 两条生成分支独立终态，不互相阻塞；
- 上传和源纪要审核都重新生成两个分支的 Markdown + JSON；标的观点 Markdown 或行业/市场观点 Markdown 审核后，仅从该分支当前 Markdown 原始字节重新生成并发布 JSON；
- Worker 崩溃时 `processing` 文件安全退回 `pending`；
- Base 新链接确认后才将旧 token 移入历史目录；移动响应丢失按目标目录 token
  重读，失败记 `cleanup_pending` 且不回滚新权威；
- Worker 暴露 host-local `/healthz` 与 `/readyz`，不返回 token、路径或记录内容；
- `failed/stale`任务只允许使用精确 job ID 和显式 `retry --apply`回到 pending，
  Base 重复事件不会自动重试失败任务。

配置模板见 `.env.example`。其中 `FEISHU_UNIFIED_PIPELINE_ENABLED`
默认为 `false`。正式切换时必须显式打包共享契约、两个 Skill 出口及当前
结构化 Skill pin，共享契约必须包含 Python 入口、manifest 和两个 schema 的完整运行树；
同时包含 backend 复用的结构化服务源码与其契约加载器。不得依赖
部署目录之外的隐式仓库相对路径。启动前验证上述两个服务文件的 hash、
Skill manifest、入口脚本和 manifest
`runtime_paths`覆盖的完整运行树 hash。自动跟踪仓库只 fetch/报告新提交，
不能直接替换运行目录；升级必须重新测试并更新 runtime hash 后显式 promotion。

公开仓库已经内置行业与市场观点 Skill 和现行标的观点 Skill。部署者填写
`.env.example` 中自己的飞书资源后，可运行下列命令自动补齐公开运行树路径、
共享 Router 队列路径及 SHA256：

```bash
python3 -B ../meeting-pipeline-ops/prepare_public_worker_env.py \
  --source-env .env.worker.source \
  --target-env .env.worker.disabled \
  --apply
```

该脚本保持 `FEISHU_UNIFIED_PIPELINE_ENABLED=false`，不会连接或写入飞书。

结构化观点草稿可通过 `SPEAKER_MASTER_PATH` 读取 repo 内维护的规范发言人
CSV。Worker 只把该路径传给结构化 Skill；原始与规范发言人继续保存在同一份
Markdown/JSON 中。名单文件缺失时不传该参数，Skill 遇到坏行、未命中或冲突
时保留原始发言人并继续。名单 SHA 只参与结构化草稿缓存依赖，审核后 JSON
仍以当前审核 Markdown 为准。

只读本地配置检查：

```bash
python3 -B unified_worker_service.py --env-file .env check
```

生产运行需要双门禁；以下命令在环境开关仍为 false 时会失败关闭：

```bash
python3 -B unified_worker_service.py --env-file .env serve --apply
```

Worker 与 Router 必须指向同一份底层 review spool 和 receipt 目录。Router
容器可以使用其挂载内路径，host Worker 使用对应宿主路径；二者必须映射到
相同字节目录。generation/review/receipt/lock/work 目录不得位于临时目录。

长纪要默认使用 `FEISHU_PIPELINE_MODEL_REASONING_EFFORT=medium` 和
`FEISHU_PIPELINE_MODEL_TIMEOUT_SECONDS=1800`。超时必须进入失败状态，不得生成
空观点或把失败任务标成完成。生产常驻、重启恢复和漏事件补偿模板位于
`../meeting-pipeline-ops/deployment/`。

显式重试一条已审阅失败任务：

```bash
python3 -B unified_worker_service.py --env-file .env retry \
  --queue review --job-id '<exact-job-id>' --from-state failed --apply
```

不支持重试 `done`；同一 job 同时存在于多个状态时失败关闭。

验证：

```bash
python3 -B -m unittest discover \
  -s .implementation/meeting-pipeline-worker/tests -p 'test_*.py'
```

生产部署模板见
`org.example.researchpipeline.feishu-meeting-pipeline-worker.plist.example`。该文件只是候选，
未安装或加载；真实服务路径、环境文件、日志目录和停止超时必须在维护窗口前
核对。

`track_skill_repositories.py`与对应 plist 模板用于每 6 小时更新三个 Skill
的裸仓库镜像并报告远端 HEAD。它不会 checkout、覆盖已安装 Skill 或修改
Worker pin；`promotion_required=true`时必须先做回归、更新完整 runtime hash，
再显式切换运行目录。每个仓库的失败独立记录；Git 禁止交互式凭据提示并
有硬超时，不会无期占用 LaunchAgent。脱敏分支当前未启用，因此只跟踪
远端，不声明已 promotion。
