# Feishu Structured Generate 兼容实现

本目录保留早期结构化 Markdown 生成接口，供离线回归和兼容迁移使用。现行服务位于 `../version-retention/feishu-structured-generate/`。

兼容服务默认不可启动：Compose 使用 `legacy-disabled` profile，服务没有默认主机端口，并要求显式兼容开关。不要与现行服务使用相同名称、端口或数据目录。

## 能力

- 根据显式提供的 Base 记录读取已经审核并归档的源 Markdown。
- 调用配置的表格生成 CLI。
- 上传待审核结构化 Markdown，并写回状态、链接、时间和行数。
- 支持健康检查、字段初始化、单记录生成和本地备份。

## 使用边界

部署者若仍需兼容接口，应复制 `.env.example` 为私有 `.env`，填写自己的资源标识和凭据，并在独立测试 Base 中完成回归。生产凭据、记录 ID、链接、日志和产物不得提交到 Git。

新部署应使用现行统一 Worker 和结构化生成服务；本目录不得作为默认安装入口。
