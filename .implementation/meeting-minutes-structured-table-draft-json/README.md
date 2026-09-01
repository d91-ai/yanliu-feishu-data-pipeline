# Structured Table Draft JSON Candidate

本目录是现行 `meeting-minutes-structured-table` 的 draft JSON 出口候选，
不属于已安装 Skill，也不修改生产挂载。

当前仓库中的 `.implementation/meeting-minutes-structured-table/` 是退役旧版，
不能作为 schema-v7 源码继续开发。因此本候选通过显式 `--skill-root` 只读加载
当前 Skill contract v4/schema v7，直接从 source-grounded `claim_units` 和源纪要
生成同一批 review Markdown 与 draft JSON；审核后再通过当前 schema-v7 的
人工 Markdown 解析器和证券主数据校验导出 reviewed JSON。draft 路径不调用
approved-only 正式 JSON 出口，也不接受“伪造已审核”参数。

reviewed 导出示例：

```bash
python3 scripts/generate_draft_json.py \
  --reviewed-markdown reviewed.md \
  --context reviewed-context.json \
  --skill-root /path/to/meeting-minutes-structured-table \
  --pipeline-contract ../meeting-pipeline-contract/meeting_pipeline_contract.py \
  --json-output reviewed.json
```

验证通过后，应把候选入口合并到正式 Skill 源仓库、更新其 manifest，再经独立
授权安装和切换。不得直接把本目录覆盖到 `~/.codex/skills`。

## 验证

```bash
python3 -B -m unittest discover \
  -s .implementation/meeting-minutes-structured-table-draft-json/tests \
  -p 'test_*.py'
```
