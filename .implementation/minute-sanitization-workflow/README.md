# 会议纪要脱敏资源配置

本目录只管理飞书资源的审计与幂等配置，不包含脱敏 skill、常驻服务、部署或 RAG 入库逻辑。

## 安全边界

- 默认命令是只读 dry-run；只有显式增加 `--apply` 才会写入。
- 不删除表、字段、文件夹或 Workflow，不修改已有字段类型和选项。
- 发现同名重复资源、字段类型冲突、选项缺失或 Workflow 定义漂移时失败关闭。
- 两个 Workflow 创建后保持 disabled；脚本不会启用它们。
- 常规 provisioner 只创建或核对活动契约，不删除现网已有表、字段、文件夹或 Workflow；旧资源清理由独立、显式的迁移操作负责。
- token、Bearer 值和完整私有 URL 不写入代码或报告。Workflow 请求体文件仅临时写入 `/private/tmp`，权限为 `0600`，调用后删除。

## 配置

将 `.env.example` 中的变量放入部署环境，不要把真实值写回本目录：

- `FEISHU_BASE_TOKEN`：`示例组织数据库`的 Base token。
- `FEISHU_KB_ROOT_FOLDER_TOKEN`：`example.org知识库`根文件夹 token。
- `FEISHU_SANITIZE_SERVICE_BASE_URL`：独立服务的 HTTPS 基址。
- `FEISHU_SANITIZE_WORKFLOW_TOKEN`：两个 Workflow 使用的 Bearer token。
- `FEISHU_SANITIZE_SOURCE_CUTOFF`：Workflow A 的归档时间下限，格式为 `YYYY-MM-DD HH:MM`。

也可以使用同名命令行参数。生产环境优先使用环境变量，避免值进入 shell history。

## 使用

只读审计并输出拟执行动作：

```bash
python3 provision.py
```

确认 dry-run 报告后再执行幂等写入：

```bash
python3 provision.py --apply
```

指定月份目录：

```bash
python3 provision.py --month 2032-07
```

脚本每次都会实时读取以下对象后再决定动作：

- `示例组织数据库`的表、源表字段、目标表字段和 Workflow；
- `example.org知识库`下三条目标目录路径；
- 两个 Workflow 的完整定义与 enabled/disabled 状态。

## 资源范围

- `非结构化数据库`新增四个源表反馈字段。
- 新建或核对`脱敏数据库`的完整 schema。
- 建立待审核、已审核 Markdown 目录和`审核版本留存/脱敏会议纪要/.../审核前`目录。
- 新建或核对`审核后脱敏MD生成`、`审核归档工作流 - 脱敏数据库`，全部保持 disabled。

审核后的正式 Markdown 是本工作流唯一正式产物。正式 JSON Workflow、字段、目录和服务端点均不属于活动契约。若现网仍有此前创建的空 JSON 资源，本脚本会保持非破坏性并忽略它们，不会隐式删除。

本脚本不会处理历史记录、不会调用服务业务端点，也不会调用 Dify/RAG API。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
