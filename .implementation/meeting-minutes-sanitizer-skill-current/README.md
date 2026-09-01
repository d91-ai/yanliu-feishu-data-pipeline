# Minute Sanitization Skill

用于对已经人工审阅的中文投研或研究会议纪要执行有限规则脱敏，并且只生成一份 Markdown 交付文件。

本 skill 依赖明确身份字段、明确发言人标题和有限正则规则。它会移除已收集的会议发言人身份值、发言归因、录音偏移和直接引语标点，并对有限的口语表达进行中性化；业务事实、否定和不确定性是保留目标。它不执行外部事实核验，也不构成完整匿名化认证。

## 目录

- skills/meeting-minutes-sanitizer/SKILL.md
- skills/meeting-minutes-sanitizer/scripts/sanitize_minutes.py
- skills/meeting-minutes-sanitizer/agents/openai.yaml
- tests/fixtures/regression_input.md
- tests/test_sanitize_minutes.py

运行时只使用 Python 标准库，不需要安装第三方依赖。

## 使用

在仓库根目录运行：

~~~bash
python3 skills/meeting-minutes-sanitizer/scripts/sanitize_minutes.py path/to/minutes.md --output-dir outputs
~~~

支持 UTF-8 .md 和 .txt 输入。音频、扫描件或其他格式需要先转换并人工确认成一个单一来源的文本版本。

输入必须已经解决混合来源、候选选择、外部核验冲突和用户修正。脚本识别到相关章节或决策表时会停止，不会自行选择主源。

如提供 --meeting-date，该值始终优先于来源元数据；只能填写已经人工确认的真实日期。

## 输入约定

- 元数据独占一行：会议日期：YYYY-MM-DD、会议类型：...。
- 主题独占一行：例如【订单｜A公司】。
- 发言人身份使用明确字段或标题，例如姓名：张三、发言人称谓：张总、### 发言人：张三、发言机构：某机构。
- 业务存疑项置于 ### 存疑与待确认。
- 普通人名形态标题、身份与业务对象同名、未知人物引用等歧义会失败关闭，需先人工澄清。
- 已明确身份的发言人在正文中提到另一位已明确身份者时，仅在命中有限归因规则时删除；其他残留身份会阻止输出，不会静默放行。

## 唯一输出

成功运行只生成：

~~~text
<meeting-date>_脱敏会议纪要_<content-hash>_sanitized.md
~~~

Markdown 结构为：

1. 文档信息：会议日期、会议类型、脱敏等级和处理边界；
2. 主题纪要：每个主题使用独立的【X】标记，不添加主题：前缀；
3. 存疑与待确认：仅在存在真实存疑项时输出 ## 三、存疑与待确认 及原条目；无真实存疑时整节省略。

输出不得包含内部标签待确认业务事项。

默认文件名不复用可能包含身份信息的输入文件名。--output-stem 只接受无路径、无扩展名的 stem；脚本只校验结构、已收集身份和有限敏感模式，调用方必须人工确认自定义 stem 不含其他身份信息。

输出已存在时默认拒绝覆盖；--force 只允许在临时文件写入和验证成功后原子替换目标 Markdown。输出路径与输入源文件相同时始终拒绝，--force 也不能绕过。

## 发布前门禁

唯一 Markdown 在写入前会检查：

- 已收集身份值、发言人标记、会议角色归因；
- 有限规则识别的未知人物引用和人物形态主题/标的；
- 电话、邮箱、证件号、微信、联系人和 URL；
- 原文位置、源文件名、记录 ID、附件页码和音频定位；
- 录音偏移、长直接引语、有限第一人称口语模式；
- 提取实体是否仍能在脱敏后的主题或正文中找到。

名称（02331.HK）这类“名称（市场代码）”格式可用于消解短业务标的的人名歧义。其他写法不自动推断；不确定项失败关闭，脚本不通过宽泛删词来强行产出。

这些门禁仍是有限规则，可能漏检组合重识别风险，也可能对公共人物来源或短公司名产生保守阻断。交付、共享或知识库入库前必须人工复核。

本工具不执行上传、同步、API 调用、模型下载或凭据读取。

## 验证

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" skills/meeting-minutes-sanitizer
~~~

仓库不包含私有会议纪要、原始录音、生成产物、API 密钥、缓存、虚拟环境或模型权重。
