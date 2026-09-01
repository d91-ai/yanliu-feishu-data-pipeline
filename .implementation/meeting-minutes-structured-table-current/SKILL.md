---
name: meeting-minutes-structured-table
description: 将中文投资会议纪要中的证券观点整理为可编辑 Markdown，并从当前结构化 Markdown 重新生成 JSON；使用本地证券主数据确定性校对证券名称和代码。适用于完整纪要到结构化观点，以及结构化观点更新后的 JSON 重生成。
---

# 结构化投资观点

## 边界

只生成两类产物：结构化观点 Markdown，以及由当前 Markdown 重新生成的结构化 JSON。会议转写、审核状态、文件版本、产物存放、替换和下游取用由数据管线负责。

会议正文是观点事实的唯一来源。本地证券主数据只校对证券名称与代码，不补写会议观点、判断、数字或关系。

## 输入

- 完整会议纪要 Markdown；
- 数据管线提供的 `meeting_id`；
- 模型内部生成的 `claim_units` 对象；它是进程输入，不是需要保存的产物；
- `data/security_master.csv` 本地证券主数据快照；公开包只附带空表头，部署者可在本地替换为自己的 UTF-8 CSV；
- 数据管线可选提供的规范发言人 CSV。

生成 `claim_units` 前，读取 [语义抽取提示词](contract/semantic_prompt.md) 和 [机器 Schema](contract/claim_units.schema.json)。默认一次理解全文，不按 Markdown 行、标题或分隔线预切割。只有输入或输出预计超出容量时，才按连续语义范围分批并携带边界上下文，以上一批最后证据位置继续到文末；边界上下文只用于理解，不重复输出已经覆盖的观点。分批结果只在当前进程中合并。

## 观点规则

一个观点单元由同一发言人、同一判断结果和共同证据组成，可以包含多个标的；导出时按标的拆为独立行。判断、条件或证据不共同成立时拆分。

只记录对具体证券自身价值、价格或操作的判断。行业、板块、客户、供应商、交易对手、比较对象或事件主体，只有同时形成该证券自身判断时才成为观点标的。

模型负责结合全文判断发言人、简称、指代、观点方向、期限、条件和持仓归属。程序负责派生原文定位、观点 ID、标的键和代码。模型不要输出证券代码或技术字段。

## 生成 Markdown

1. 按 Schema 形成 `claim_units` 对象。每个单元至少包含 `presenter`、`source_quotes`、`direction`、`time_horizon` 和 `targets`：

   ```json
   {
     "claim_units": [
       {
         "presenter": "发言人",
         "source_quotes": ["能够完整支持判断的原文摘录"],
         "direction": "看多",
         "time_horizon": "短期",
         "conditions": [],
         "targets": [
           {
             "target_name": "原文中的证券名称或简称",
             "market": "A股",
             "position": {"state": "未持有", "detail": "", "plan": "计划买入"}
           }
         ]
       }
     ]
   }
   ```

2. 通过 stdin 传入该对象并生成 Markdown；下面的命令包含完整输入，不会等待未关闭的 stdin：

   ```bash
   printf '%s\n' '{"claim_units":[{"presenter":"发言人","source_quotes":["能够完整支持判断的原文摘录"],"direction":"看多","time_horizon":"短期","targets":[{"target_name":"证券名称或简称","market":"A股"}]}]}' | \
   python3 scripts/generate_table.py \
     --claim-units - \
     --meeting-markdown "会议纪要.md" \
     --meeting-id "数据管线会议ID" \
     --speaker-master "/path/to/speaker_master.csv" \
     --output "结构化观点.md"
   ```

首次生成时，代码只由 `target_name` 和市场在本地证券主数据中做精确唯一匹配。唯一命中时同时使用主数据中的规范证券名称和代码；模型给出的代码、同段落其他标的的代码和模型记忆均不作为绑定依据。无法唯一确定时保留当前名称，代码写入 `原文未提供`，其他内容继续生成。

提供 `--speaker-master` 时，只读取其中 `status=confirmed` 的记录，对原始发言人做 Unicode NFKC、首尾空白和英文大小写归一化后的精确唯一匹配。唯一命中时填写可见的 `规范发言人`，原始发言人保持不变；未命中、冲突、坏记录或名单不可用时沿用原始发言人并继续生成。

## 生成 JSON

Markdown 的业务字段可编辑。每次需要 JSON 时，都从当前 Markdown 使用同一入口重新生成：

```bash
python3 scripts/generate_table.py \
  --structured-markdown "结构化观点.md" \
  --meeting-id "数据管线会议ID" \
  --output "structured-viewpoints.json"
```

重新生成时，当前 Markdown 的可见名称和代码是业务输入；本地证券主数据只提示无法验证或名称与代码冲突，不改写当前值。明确填写 `原文未提供` 时保持未解析。JSON metadata 只记录 `meeting_id`、当前输入 Markdown 的原始字节 SHA-256、Schema 版本和证券主数据快照版本。同一 Markdown、`meeting_id` 与同一主数据快照必须生成字节一致的 JSON。不生成 sidecar 或其他中间产物，不区分审核前后 JSON。

## 降级行为

- 单个坏标的只跳过该标的，不影响同一观点的其他有效标的。
- 单条观点无法形成完整判断时跳过该条并提示，其他观点继续产出。
- 可选字段缺失时使用 Schema 中的保守值。
- 受控字段出现异常值时提示具体字段，使用保守值并继续产出。
- Markdown 表头未知或业务字段缺失时提示；除标的名称或原文缺失外，继续使用保守值生成。
- 证券主数据中的单条坏记录只提示并跳过；其他有效记录继续使用。
- 首次生成时，证券主数据缺失或名称歧义则保留 `原文未提供`；重新生成时保留当前 Markdown 值并提示校对问题。
- 规范发言人名单缺失、坏行、未命中或冲突时保留原始发言人，其他内容继续生成。
- 输入文件不可读取或 JSON 语法错误时无法继续；没有观点时仍生成空 Markdown 或空 JSON。

字段定义与边界见 [references/schema.md](references/schema.md)。
