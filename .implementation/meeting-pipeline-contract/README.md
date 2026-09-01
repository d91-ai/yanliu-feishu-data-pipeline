# Meeting Pipeline Contract

本目录是会议数据管线改造的共享机器契约候选，不连接或修改生产飞书资源。

## 文件

- `contract/manifest.json`：会议 UID、审核状态、产物类型、短文件名限制和 21 个业务字段的单一配置源。
- `contract/artifact-metadata.schema.json`：行业与市场、结构化 JSON 共用的 metadata schema v1。
- `contract/unified-base.schema.json`：统一 20 字段表和 4 个审核视图的离线创建契约。
- `meeting_pipeline_contract.py`：无第三方依赖的加载、验证、UID 和文件名函数。
- `tests/test_contract.py`：manifest/schema 一致性、v1/v2/v10 命名和不变量测试。

会议系列和会议类型的具体单选项由上传页和 Base 配置管理，本契约只要求非空、安全且不超过 40 个字符。文件名仅供识别，不承担身份、去重或版本选择职责。

## 本地验证

```bash
python3 -m unittest discover -s .implementation/meeting-pipeline-contract/tests -p 'test_*.py'
```

部署时不得复制常量重新实现。各独立服务必须显式打包同一版本的 `meeting_pipeline_contract.py` 和 `contract/`，并在启动检查中验证契约版本。
