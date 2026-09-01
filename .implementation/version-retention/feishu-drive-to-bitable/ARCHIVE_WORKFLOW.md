# Drive 与多维表格归档工作流

本模块提供飞书 Drive 文件归档、Base 状态写回、审核前后版本留存和统一 Router 兼容能力。公开仓库不包含任何真实资源标识或历史生产结果。

## 配置文件

- `.env.router.example`：共享自建应用连接和 Router 配置。
- `.env.meeting-minutes.example`：会议纪要路由。
- `.env.structured.example`：可选的旧结构化路由，仅在兼容部署中启用。
- `.env.example`：全部受支持配置的高级参考。

复制所需示例为本地 `.env.*`，填写自己的 Base、table、字段绑定、Drive 文件夹和凭据。真实配置必须被 Git 忽略。

## 安全默认值

- 默认 `FEISHU_DRY_RUN=true` 和 `FEISHU_ARCHIVE_DRY_RUN=true`。
- 外部写入命令必须显式使用 `--apply`。
- 归档前重新读取 Base、下载远端文件并校验 SHA-256。
- 审核状态、数据版本或输入哈希变化时拒绝提交。
- 新产物写入并确认成功后才清理旧的非权威文件。

## 初始化月份目录

先使用部署环境中的年月做 dry-run：

```bash
FEISHU_ENV_FILE=.env.meeting-minutes python3 feishu_drive_to_bitable.py ensure-month YYYY-MM --subscribe
```

确认计划后再执行：

```bash
FEISHU_ENV_FILE=.env.meeting-minutes python3 feishu_drive_to_bitable.py ensure-month YYYY-MM --subscribe --apply
```

兼容结构化路由如需启用，应使用独立配置和独立测试 Base 执行同样步骤。

## HTTP 与 Workflow

HTTP 归档入口应只监听受控网络，并要求独立 bearer token。多维表格 Workflow 负责在审核字段变化后调用相应入口；服务再次核验记录状态，因此 Workflow 触发不等同于审核授权。

## 验收

1. 运行单元测试和 dry-run。
2. 在合成记录上验证目录创建、文件上传、Base 写回和幂等重试。
3. 验证审核前基线、审核后归档及版本哈希。
4. 模拟过期任务、部分提交、网络超时和重复事件，确认流程失败关闭。
5. 最后在部署者自己的测试 Base 中完成端到端验收。
