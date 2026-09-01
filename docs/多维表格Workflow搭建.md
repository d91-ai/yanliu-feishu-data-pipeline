# 多维表格 Workflow 搭建

Router 和 Worker 负责事件接收、AI 生成、版本控制和写回；Workflow 只负责用户协作、审核提醒和结果通知，不能重复调用 AI 或覆盖产物链接。

## Workflow 1：上传登记提醒

- 触发：新记录创建，且 `会议纪要上传附件` 不为空。
- 条件：`会议日期`、`会议系列`、`会议类型` 均不为空。
- 动作：向指定审核群或审核人发送“纪要已登记，系统开始处理”的通知。
- 不执行：不要修改会议 ID、数据版本或三个审核状态，Router 会确定性写入这些值。

## Workflow 2：源纪要待审核

- 触发：`会议纪要MD` 由空变为非空。
- 条件：`源纪要审核` 为 `未审核` 或 `需重审`。
- 动作：通知审核人打开 `会议纪要MD`；人工确认后把 `源纪要审核` 改为 `已审核`。

## Workflow 3：双路观点待审核

分别创建两条规则：

- `行业与市场观点MD` 由空变为非空，且 `行业与市场观点审核` 为 `未审核` 或 `需重审`；
- `标的观点MD` 由空变为非空，且 `标的观点审核` 为 `未审核` 或 `需重审`。

动作是通知对应审核人。人工校对 Markdown 后，只修改该分支的审核字段为 `已审核`。Worker 会从当前审核后 Markdown 重新生成正式 JSON。

## Workflow 4：正式结果完成

- 触发：`行业与市场观点JSON` 或 `标的观点JSON` 由空变为非空。
- 动作：通知提交人结果可用，并附对应 JSON 与 Markdown 字段链接。
- 条件：只发送通知，不移动 Drive 文件，不修改数据版本，不覆盖任何链接。

## 核对清单

- 三个审核字段的选项必须严格为 `未审核`、`已审核`、`需重审`。
- Workflow 不写 `会议ID`、`数据版本` 和任何产物链接。
- 同一分支的通知允许重复触发，但不得再次生成或复制产物。
- 先在测试 Base 中验证每条规则，再复制到正式 Base。
- 如使用 `lark-cli` 创建或更新 Workflow，先运行脚本的 dry-run/计划模式，检查目标 Base token、Workflow ID 和动作 JSON 后再显式应用。
- 当前链路实际是五条 Workflow：上传登记、源纪要审核、行业市场观点审核、标的观点审核、正式结果完成；“双路观点”不能只创建其中一条。
- 使用页面搭建时，每完成一条都点击“保存并启用”，最后回到 Workflow 列表确认五条均显示“已启用”。只保存草稿不会触发通知。

## 使用仓库脚本创建

在未跟踪的 `.env.meeting-minutes` 中填写审核人的 `FEISHU_WORKFLOW_REVIEWER_OPEN_ID`。先运行计划模式，再显式应用：

```bash
cd "$(git rev-parse --show-toplevel)/.implementation/meeting-pipeline-ops"
python3 -B provision_collaboration_workflows.py \
  --env-file ../version-retention/feishu-drive-to-bitable/.env.meeting-minutes
python3 -B provision_collaboration_workflows.py \
  --env-file ../version-retention/feishu-drive-to-bitable/.env.meeting-minutes \
  --apply
```

脚本只创建通知动作，不调用 AI、不修改会议ID、数据版本或产物链接，也不会在输出中显示审核人 open_id。旧版 `create_structured_generation_workflow.py` 面向另一条历史链路，不用于这里。
