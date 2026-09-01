# 结构化观点字段

## 输入契约

`claim_units` 根对象固定为 `{"claim_units": [...]}`。它只在当前生成进程中使用，不要求落盘。

| 字段 | 含义 | 规则 |
| --- | --- | --- |
| `presenter` | 原始发言人 | 由模型结合全文确定。 |
| `source_quotes` | 原文证据 | 一个或多个有序、最小但完整的原文语义范围；不依赖 Markdown 标题或分隔线。 |
| `direction` | 观点方向 | `看多`、`看空`、`关注`、`中性`或`信息不足`。 |
| `time_horizon` | 观点周期 | `短期`、`中期`、`长期`或`未说明`。 |
| `targets` | 标的 | 非空数组；每项至少含 `target_name` 和 `market`。 |
| `conditions` | 限定条件 | 可选的有序 `{text, types}` 数组。 |
| `targets[].position` | 标的持仓信息 | 可选；只记录与该标的明确绑定的信息，多标的分别填写。 |

模型不输出股票代码、规范发言人、观点 ID、原文定位、哈希或版本。

## JSON 行

每行表示一个 `发言人 × 原文判断 × 标的`：

| 字段 | 生成方式 |
| --- | --- |
| `viewpoint_id` | 由 `meeting_id` 和该行全部业务内容确定性派生；同内容重复出现时使用出现次序形成不同 ID，不合并行。 |
| `meeting_date` / `viewpoint_date` | 来自当前内容；无单独观点日期时回落到会议日期。 |
| `target_name` / `market` | 首次生成时，本地主数据唯一命中后使用规范证券名称；从当前 Markdown 重生成时保留可见值。 |
| `stock_code` / `target_key` | 由同一次本地证券身份解析生成。 |
| `presenter` | 模型根据会议全文确定的原始发言人。 |
| `presenter_normalized` | 首次生成时由可选规范名单精确唯一匹配；未命中时等于 `presenter`；从 Markdown 重生成时保留可见值。 |
| `direction` / `time_horizon` | 来自观点内容，异常值使用保守缺省值。 |
| `position_context` / `conditions` | 来自观点内容。 |
| `source_evidence` | 有序 `{text, locator}` 数组；每段证据独立保留，不拼接、不合并。唯一定位使用全文语义字符范围，无法唯一定位时使用内容哈希定位。 |

JSON metadata 只有四个字段：数据管线必传的 `meeting_id`、导出器实际读取的完整结构化 Markdown 原始字节哈希 `structured_markdown_sha256`、`schema_version`、`security_master_version`。主数据不可用时版本为 `unavailable`。metadata 不包含生成时间、审核状态、文件记录 ID、URL 或行数。同一 Markdown、`meeting_id` 和证券主数据快照生成字节一致的 JSON。

`structured_markdown_sha256` 是完整输入 Markdown UTF-8 字节的 SHA-256。修改任何字符，包括新增一张无法解析的卡片，都会改变该值；无需 sidecar 或其他中间产物。

## 规范发言人

数据管线可通过 `--speaker-master` 提供 CSV，必需列为 `presenter_id`、`canonical_name`、`aliases` 和 `status`。只有 `status=confirmed` 的记录参与匹配；规范名和以 `|` 分隔的别名都可命中。比较时只做 Unicode NFKC、首尾空白清理和英文 `casefold`。同一别名对应多个身份、未命中或名单不可用时，`presenter_normalized` 保持为原始 `presenter`。

规范名单只用于首次生成 Markdown。Markdown 同时显示 `原始发言人` 与 `规范发言人`；从当前 Markdown 重生成 JSON 时保留人工可见值，不再查询名单覆盖。

## 证券代码

运行时只读取 `data/security_master.csv` 或调用方指定的同格式本地快照：

1. 首次生成 Markdown 时，先按市场限定候选，再对 `target_name` 做 Unicode、空白归一化后的精确唯一匹配；规范名称和确认别名都可命中。
2. Markdown 重新生成 JSON 时，当前可见名称和代码是业务输入；本地主数据只提示无法验证或名称与代码冲突，不改写当前值。
3. 明确填写 `原文未提供` 时保持未解析；代码为空时才按当前名称尝试解析。
4. 不使用模型输出代码、同段落其他代码、模糊搜索、模型记忆或运行时网络搜索绑定证券。
5. 主数据单条坏记录提示并跳过，不禁用其他有效记录。

本地证券主数据可以由独立任务使用交易所数据或 `a-stock-data` 等工具维护；生成过程保持离线，不自行刷新。

## Markdown 与数据管线边界

Markdown 只保存可读、可编辑的观点内容，不保存 `meeting_id`、`viewpoint_id`、标的键、哈希或版本。原始发言人与规范发言人均为可见业务字段。JSON 始终由当前 Markdown 使用同一入口重新生成，不区分审核前后状态。

审核状态、生成时间、文件版本、原始来源记录、归档、替换和下游取用由数据管线负责，不进入本 Skill 契约。正式 JSON 结构以 [机器 Schema](../contract/viewpoints.schema.json) 为准。
