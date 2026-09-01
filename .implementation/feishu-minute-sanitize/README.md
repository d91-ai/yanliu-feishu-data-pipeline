# feishu-minute-sanitize

已审核会议纪要的独立脱敏分支服务。服务只负责飞书 Base/Drive 编排、门禁、哈希、幂等和版本证据；脱敏 Markdown 业务规则由外部 `minute-sanitization-skill` 负责。本目录不包含也不修改 skill。

本期终态是“审核后脱敏 Markdown 已归档，审核前后版本证据已完成”。不生成正式 JSON，不执行 RAG/Dify 入库。

## HTTP 契约

- `GET /healthz`：始终返回进程存活状态，`skill_ready` 单独表示 skill 是否可处理业务。
- `POST /generate-review-md`：请求体仅 `{"record_id":"..."}`。
- `POST /archive-review-md`：请求体仅 `{"record_id":"..."}`。

两个 POST 端点使用 `Authorization: Bearer <token>`。`generate-review-md` 依赖 skill doctor：未通过时，它在任何飞书读写之前返回 `503 skill_not_ready`。`archive-review-md` 不调用 skill，不受 doctor 状态影响。

若完成态写入后的飞书响应丢失，服务会重读并精确核对终态证据。核对成功时仍返回原成功状态，并附加 `"reconciled": true`；无法确认时返回 HTTP 503、`"status": "outcome_uncertain"`，错误码为 `review_commit_outcome_uncertain` 或 `archive_commit_outcome_uncertain`。此状态不写成“生成失败”或“归档失败”，恢复动作是使用同一记录 ID 重试。

直接运行镜像默认只执行本地 `doctor`。只有 Compose 中明确的
`serve --apply` 才启动可接收外部写请求的服务，漏写确认参数会在读取运行配置前失败。

对已处于终态的归档请求，服务不会盲目返回成功：必须重新下载归档 Markdown，验证 UTF-8、`.md` 类型，并确认其 SHA256 与 Base 的 `审核后内容SHA256` 完全一致，才返回 `skipped_existing`。

## Skill CLI 契约

`SANITIZE_SKILL_COMMAND_JSON` 是不经 shell 执行的 JSON 字符串数组，必须仅包含 Python 解释器和 skill 脚本绝对路径：

```text
["python", "/skills/meeting-minutes-sanitizer/scripts/sanitize_minutes.py"]
```

运行时还必须固定：

- `SANITIZE_SKILL_SOURCE_REVISION`：完整 40 位 Git 提交；
- `SANITIZE_SKILL_SCRIPT_SHA256`：部署脚本的 64 位 SHA256。

adapter 自行实现内部 doctor：先核对命令形状、skill 根目录/`scripts`目录/脚本均非符号链接、固定提交与脚本 SHA256 属于代码内批准组合，再用无隐私合成输入在临时目录执行一次真实 smoke。smoke 必须只产生非空 UTF-8 的`review_sanitized.md`，通过真实日历日期、脱敏等级校验，并确认探针身份值已移除，才向服务声明`minute-sanitization/v2 + review-md`就绪。标题、章节、主题标记和待确认段落等后置格式检查只记安全告警，不阻止文件上传或 Base 完成态写回。

真实生成时，adapter 以位置参数传入`input.md`，追加从 Base 规范化并校验的`--meeting-date`、`--output-dir`和固定`--output-stem review`。输出日期必须与 Base 日期完全一致。只允许唯一`review_sanitized.md`，不解析 stdout，不要求或生成 result manifest。内容 SHA256 由服务计算；`脱敏规则版本`由代码批准的固定提交和脚本 SHA256 组合生成。skill 的 stdout/stderr、临时路径、会议正文和飞书资源路径不会进入 HTTP、Base 或日志。

## 来源契约适配

当前会议纪要的规范层级与 pinned 脱敏 skill 的输入层级不同。服务在内存中运行确定性 `source_contract_adapter`：保留会议日期、类型、标题/系列/标的、发言人、主题、证券标的、问答阶段及“存疑与待确认”业务列，只改写标题层级，不写出第二份来源正文。

- 规范来源必须只有一个标题和“发言整理”章节；日期须为真实 `YYYY-MM-DD` 且与 Base 完全一致。
- 多人复盘按发言人/主题/标的改写；公司和专家交流按阶段/问题改写。像人名的含混阶段会要求人工处理，不做猜测。
- “不要传出去”和“以我为准”允许空白变体匹配，命中即在任何脱敏输出或飞书写入前失败关闭。
- 时间戳仅是来源定位信息，不传给脱敏正文；待确认表的四个业务列全部保留。
- `脱敏规则版本`同时包含 adapter 版本和当前 adapter 文件 SHA256，部署字节变化会改变幂等版本。

## 失败关闭和数据边界

- Workflow 只传 `record_id`，链接、审核状态与 SHA256 全部由服务重读 Base。
- 服务再次校验来源记录的`归档时间`不早于 `FEISHU_SANITIZE_SOURCE_CUTOFF`，避免绕过 Workflow 直调处理历史记录。
- 同名同哈希复用，同名异哈希拒绝。完成态始终最后写入。
- 新上传的待审核 Markdown 若还没有 Drive 历史版本，服务会对同一 `file_token` 做一次同内容覆盖，取得首个可审计版本号，并按该版本重新下载校验 SHA256。该 bootstrap 版本与初始上传字节完全一致；已有版本时不会重复覆盖。
- 临时正文只存在隔离临时目录，任务后清理。本地 manifest 只保存 record id、token 和 SHA256，不保存正文或私有链接。
- 日志不输出正文、Authorization、token、环境变量或完整私有链接。
- 本服务不生成正式 JSON，不含 RAG/Dify API，不会自动入库。

## 本地验证

```bash
PYTHONPYCACHEPREFIX=/tmp/minute-sanitize-pyc python3 -m py_compile \
  skill_adapter.py feishu_gateway.py minute_sanitize_service.py
PYTHONPYCACHEPREFIX=/tmp/minute-sanitize-pyc python3 -m unittest discover -s tests -v
```

## 容器配置

1. 复制 `.env.example` 为 `.env`，仅填写非密钥 ID/token、宿主路径、固定 skill 提交和脚本 SHA256。
2. 将飞书 app secret 和 HTTP Bearer token 分别写入权限收紧的宿主文件。
3. 只读挂载 skill 目录。skill 未兼容时容器可存活，脱敏 Markdown 生成保持失败关闭，归档端点仍可处理已生成并过审的 Markdown。
4. 在 skill 就绪前不启用任何飞书 Workflow。
