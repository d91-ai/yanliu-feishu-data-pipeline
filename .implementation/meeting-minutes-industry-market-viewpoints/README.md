# Meeting Minutes Industry and Market Viewpoints Skill

从中文投资会议纪要中抽取市场级和行业级观点，生成一份可人工审核的
Markdown，并导出 draft 或 reviewed JSON。单一证券观点不在本 Skill 范围内。
每条观点按“日期、主题、发言人、观点类型、观点”展示，观点类型为
看多/看空/中性。纯事件关注不生成观点，观点正文压缩为适合数据看板展示的
一至两句话。

## 设计边界

- 会议纪要是唯一语义来源。
- 市场观点和行业观点保存在同一份 Markdown/JSON 产物中。
- 审核后 Markdown 是 reviewed JSON 的语义权威来源。
- Skill 不持有飞书凭据，不负责 Base、Drive、任务队列或发布。
- `contract/manifest.json` 是唯一机器契约入口。

## 使用

完整调用顺序、命令和输入输出要求见 [SKILL.md](SKILL.md)，数据字段见
[`references/schema.md`](references/schema.md)。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/generate_viewpoints.py --help
```

当前 Skill 契约版本为 v3，管线元数据契约仍为 v1。发布到生产管线前，仍应对真实会议样例做业务审核，
并对安装副本与运行时挂载做 commit/SHA 校验。
