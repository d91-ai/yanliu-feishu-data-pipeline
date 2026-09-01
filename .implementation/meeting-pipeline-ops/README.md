# Meeting Pipeline Operations Candidates

本目录默认只处理离线快照；唯一例外是经过白名单清单约束的历史基线修复工具。

- `audit_publish_directory.py`：对 Base 导出与 Drive JSON manifest 做只读审计；
- `migrate_unified_base.py`：把旧源/结构化/正式 JSON/脱敏表导出映射为统一 20 字段迁移计划；
- `repair_baselines.py`：默认只读校验清单中精确的 Base 记录、Drive token、首个有效版本、SHA256、目标目录与文件名；仅显式 `--apply` 时上传缺失的不可变审核前基线，且上传后重新下载验 hash 并写本地回执；
- `apply_unified_base.py`：按固定资源清单、20 字段契约、四表最终导出和零问题迁移快照执行 Base 直接切换；默认 dry-run，显式 `--apply` 还必须提供旧 Workflow 已停用且服务已暂停的维护证明。创建字段、逐记录更新、字段/表重命名和视图创建均 fresh-read 对账，并用私有 journal 恢复；不删除旧字段、表、视图、记录或 Workflow；
- `plan_publish_reconcile.py`：将 audit 结果转成不可执行的隔离/重建计划；冲突保持 blocked；
- `provision_collaboration_workflows.py`：只读规划或显式创建并启用五个通知型 Workflow；不会调用 AI，也不会打印审核人 open_id；
- `reconcile_missed_ingress.py`：补偿服务停机期间遗漏的附件入口事件；默认只统计候选，必须显式 `--apply` 才恢复；
- `deployment/`：Router、Worker、补偿器的 systemd 与 WSL 启动模板，全部使用通用占位符；
- 迁移工具的 `--apply-local-output` 只固化本地迁移快照，不能写 Base 或移动 Drive 文件；
- 迁移工具可通过 `--baseline-receipts` 读取已验证回执，补入相应审核前链接；回执无法匹配、UID 不一致或 hash 不闭合都会阻断计划；
- 任何歧义、缺元数据、重复 UID 或多重关联都会保留在 `issues` 并阻止该记录进入计划。

`repair_baselines.py` 不写 Base、不移动或删除 Drive 文件，也不会创建目录；生产表迁移仍需在维护窗口内使用另行审阅的 Feishu adapter，并先取得明确授权。
