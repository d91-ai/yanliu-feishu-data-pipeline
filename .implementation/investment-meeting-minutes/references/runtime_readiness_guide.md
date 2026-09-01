# 运行环境 Readiness 指南

首次在本机使用、迁移到新机器后，或执行耗时较高的音频、文档、导出任务前，先按本指南检查本地环境。

## 运行原则

正式处理会议任务时，不应临时下载模型、安装依赖或静默切换引擎。依赖安装、模型下载和服务配置属于部署准备步骤，必须和单次会议处理分开。

会议任务中允许：

- 使用已经安装好的 Python 依赖。
- 使用已经缓存好的 ASR 模型。
- 使用已经配置好的本地服务。
- 音频转写无法运行时，暂停处理并修复本地 ASR 运行环境，再回到正常转写流程。
- 当前工作时段无法恢复运行环境且用户接受音频复核不完整时，才改为仅根据用户提供的文字继续。

会议任务中不允许：

- 现场下载 SenseVoice 模型。
- 现场安装 Python 包。
- 从 SenseVoice 静默切换到 Whisper 或其他 ASR。
- 根据清洗后纪要的位置估算时间戳。
- 在 Markdown 导出或本地归档失败时声称已经成功。

## Readiness Profile

使用和下一步动作匹配的最小检查范围：

```bash
python3 scripts/check_investment_workflow_health.py --profile asr --strict
python3 scripts/check_investment_workflow_health.py --profile document
python3 scripts/check_investment_workflow_health.py --profile export
python3 scripts/check_investment_workflow_health.py --profile full --strict
```

各 profile 含义：

- `asr`：只检查本地转写前置条件、SenseVoice/Paraformer 服务和模型缓存。音频转写前使用。
- `document`：检查 UTF-8 文本处理、DOCX 解析、本地输入/临时/输出目录权限。文档密集任务前使用。
- `export`：检查本地 Markdown validator、Markdown 导出器、本地输出目录和同目录 hard-link 原子发布能力。最终 Markdown 导出前使用。
- `full`：运行全部本地基础检查。用于部署验收，不建议每次会议纪要都跑。

strict 模式按 profile 检查对应资源：

- `asr --strict`：缺少 `funasr`、`modelscope`、`soundfile`、`librosa`、本地 SenseVoice 主模型缓存、本地 Paraformer 辅助模型缓存、本地 fsmn-vad 模型缓存、运行中的 SenseVoice bridge 健康检查接口或服务上报的模型缓存时应失败。
- `document --strict`：缺少 `docx`、UTF-8 文本处理、临时目录、本地输入目录或本地输出目录时应失败。
- `export --strict`：缺少本地输出目录、Markdown validator、Markdown 导出器或安全原子发布能力时应失败。
- `full --strict`：运行以上全部本地基础检查。

当前维护的音频工作流是：SenseVoice 作为主转写来源，Paraformer 只作为辅助校对和时间戳证据。`timestamp_index.json` 可由脚本在 SenseVoice 与 Paraformer 可用时间戳 anchor 间选择，并记录 `timestamp_index_source`。Paraformer 结果不得自动替换 SenseVoice 主转写。额外 ASR 引擎、说话人分离、无关分段路径不属于当前基础 skill 合约，只有用户明确要求时才启用。

只检查 ASR 缓存时使用：

```bash
python3 scripts/transcribe_audio.py --check-model-cache
```

该命令只报告缓存状态，不应下载缺失文件。

## 本地目录策略

所有默认业务目录从 `INVESTMENT_MINUTES_WORKSPACE` 推导。未设置时，本地脚本默认使用：

```bash
Path.home() / "Documents/会议纪要整理"
```

建议在本机 shell 或启动脚本中显式设置：

```bash
export INVESTMENT_MINUTES_WORKSPACE="$HOME/Documents/会议纪要整理"
```

readiness 默认只检查目录，不自动创建目录。若目录不存在，检查应失败并提示设置 `INVESTMENT_MINUTES_WORKSPACE` 或先创建目录，避免误写到用户不期望的位置。

首次部署且确认目标 workspace 无误时，可显式创建本地目录：

```bash
python3 scripts/check_investment_workflow_health.py --profile document --prepare-local-dirs
python3 scripts/check_investment_workflow_health.py --profile export --prepare-local-dirs
```

`--prepare-local-dirs` 只用于本地部署准备，不应在普通会议处理过程中默认启用。

## 部署准备

如果 strict 模式报告依赖或模型缺失，应先修复部署环境，再处理正式会议。任何下载或安装都应作为人工部署步骤执行，不应藏在会议处理脚本里。

本地路径和 Python 选择：

- 设置 `INVESTMENT_MINUTES_WORKSPACE` 作为会议纪要工作流根目录；原始输入和最终输出目录都从该根目录推导。
- 当 Markdown 检查需要使用非系统默认 `python3` 时，设置 `INVESTMENT_MINUTES_PYTHON`。
- 当 ASR 检查需要使用独立转写运行时时，设置 `SENSEVOICE_PYTHON`。
- 模型缓存优先使用 `SENSEVOICE_MODEL_CACHE`；未设置时使用 `FUNASR_MODEL_CACHE`，再退回 `$HOME/Documents/Codex/asr-model-cache`。
- SenseVoice HTTP bridge 默认只接受不超过 `67108864` bytes（64 MiB）的 multipart 上传，可用 `SENSEVOICE_MAX_UPLOAD_BYTES` 在部署时显式调整。超过上限的音频应走本地 `transcribe_audio.py` 路径；不要为了接收大文件盲目放宽内存上限。
- `SENSEVOICE_MAX_CONCURRENT_TRANSCRIPTIONS` 默认是 `1`，用于避免多个模型进程同时争抢内存。只有在实测内存充足且完成并发压测后才提高。
- 同一输出目录和文件 stem 的 SenseVoice 任务会串行执行；本轮失败时保留上一轮已提交的完整输出，本轮成功后整组替换受管输出，避免跨轮混写。若进程在提交窗口被强制终止，下次同 stem 运行会先恢复遗留事务，再启动模型工作。
- Markdown 导出和原始材料归档要求目标目录支持同目录 hard-link；若 exFAT、部分 SMB/FUSE 等文件系统不支持，脚本会安全失败而不采用可能覆盖已有文件或暴露半文件的降级方案。迁移工作区后先运行 `--profile export` readiness 检查。
- 日志和访问控制等用户级配置应使用 `$HOME/...`、环境变量或 CLI 参数，不应写入 reusable artifact 的私有绝对路径。

skill 包内应保留：

- 规则和 prompt。
- 确定性脚本。
- reference 和模板。
- regression 样例。
- health check。
- 不含密钥的可选示例配置。

部署环境中保存：

- 已下载的 ASR 模型缓存。
- Python 虚拟环境。
- 平台相关二进制。
- 本地日志和运行状态。

如需 runtime asset manifest，应从部署环境生成，并放在 reusable skill package 之外；除非只是脱敏示例，否则不要提交。

## PDF 边界

PDF 不属于基础正文解析能力。用户提供 PDF 时，默认只作为原始附件归档；若会议内容必须来自 PDF，应要求用户另行提供可读文本、DOCX、TXT 或 Markdown，再进入文稿整理流程。
