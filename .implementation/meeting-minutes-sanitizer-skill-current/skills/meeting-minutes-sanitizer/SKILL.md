---
name: meeting-minutes-sanitizer
description: Sanitize one reviewed Chinese investment or research meeting-minutes .md/.txt source with explicit identity fields and bounded rules, then emit exactly one Markdown file requiring human review. Use only when the user explicitly requests full meeting-minutes 脱敏, 匿名化, or 去发言人. A request only to remove speaking style or make a partial edit is not enough. Do not use for ordinary RAG chunking, general legal/compliance PII redaction, raw audio/scans, or unresolved mixed sources and user corrections.
---

# Meeting Minutes Sanitizer

## Scope and precedence

Use this skill on one reviewed Chinese investment or research meeting-minutes source. A successful run emits exactly one Markdown file.

Do not expand a partial editing or ordinary RAG request into full anonymization. Do not upload, sync, ingest, or overwrite an existing output unless the user explicitly authorizes that action.

Resolve these before sanitization:

- mixed sources or conflicting versions;
- external-verification conflicts;
- candidate, confirmed, or rejected decisions;
- confirmed user corrections.

The script must not guess a primary source or apply corrections. It stops on recognized evidence-layer, candidate/confirmation, mixed-source, or correction headings and decision-table headers. Resolve any unrecognized source structure before invoking the skill.

## Method boundary

This is an explicit-field and bounded-rule sanitizer, not a general named-entity recognizer and not an anonymity certification.

It collects meeting-speaker names, aliases, titles, and affiliations only from documented identity fields and explicit speaker headings. It removes collected identities in bounded attribution forms. If an identified speaker mentions another identified speaker, the second identity is removed only when it is also collected and the surrounding phrase matches a documented attribution rule. Any collected identity that remains blocks publication.

A person-like reference that was not collected is not silently deleted. Conservative rules block the output and require the reviewed source to clarify whether the value is a person, organization, or business object.

## Evidence discipline

- Treat source statements as source statements, not verified facts.
- Preserve public-source category, attribution context, negation, uncertainty, and pending status unless the source value is itself a removed meeting-speaker identity.
- Never add an entity, product attribute, causal claim, or business conclusion absent from the input.
- 未执行外部事实核验 is a processing boundary, not a factual-accuracy certification.
- Business facts are a preservation target, not proof that every original statement was retained or correct.

## Input contract

Accept UTF-8 .md and .txt only. Convert audio, scans, or other formats first and review the converted text.

Use these conventions:

- Metadata: 会议日期：YYYY-MM-DD and 会议类型：... on their own lines.
- Topics: standalone 【主题｜标的】 lines.
- Speakers, aliases, and affiliations: explicit headings such as ### 发言人：张三 or fields such as 姓名：张三, 发言人称谓：张总, 身份：张三, 主讲人：张三, or 发言机构：某机构.
- Pending items: a supported heading such as ### 存疑与待确认 containing business uncertainty only.

A plain person-like Markdown heading is ambiguous with a company or business object. Unless the same value is identified by an explicit speaker heading or identity field, the script fails closed.

If a collected identity value also occurs as a business-object name, the quality gate fails instead of globally deleting the shared string. Resolve the collision in the reviewed source before rerunning.

## Quick start

Resolve the bundled script relative to this skill directory and use python3:

~~~bash
python3 scripts/sanitize_minutes.py path/to/minutes.md --output-dir outputs --meeting-date 2032-06-14
~~~

Options:

- --output-dir DIR: output directory; default outputs.
- --meeting-date YYYY-MM-DD: when provided, always override source metadata; use only an already confirmed real calendar date.
- --output-stem SAFE_STEM: stem without a path or extension. The script checks syntax, collected identities, and bounded sensitive patterns; the caller must review it for any other identity.
- --force: atomically replace the existing Markdown only after temporary-file validation.

There is no format-selection option. A successful run always creates one *_sanitized.md file.

## Output contract

The safe default does not reuse the possibly identifying input stem:

~~~text
<meeting-date>_脱敏会议纪要_<content-hash>_sanitized.md
~~~

The required Markdown structure with an optional pending section is:

~~~markdown
# 脱敏会议纪要

## 一、文档信息

- 会议日期：2032-06-14
- 会议类型：专家交流
- 脱敏等级：L2_FACT_PRESERVED
- 处理说明：仅删除有限规则明确识别到的发言人身份值，并对发言风格执行规则化处理；以保留业务事实为目标，未执行外部事实核验，交付前必须人工复核

## 二、主题纪要

【订单｜A公司】

订单可能增长，仍待公司公告确认。

## 三、存疑与待确认

- 订单增幅仍待确认。
~~~

Topic units use standalone 【X】 markers and never add a 主题： prefix. When there is no real pending item, omit the entire pending section. When real pending items exist, preserve their uncertainty under exactly ## 三、存疑与待确认. Never emit the internal label 待确认业务事项.

## Processing rules

Delete or neutralize:

- explicitly identified meeting-speaker headings and identity fields;
- bounded meeting-speaker attribution such as 张三认为 after 张三 is collected;
- a bounded set of fillers and first-person speaking patterns without changing the underlying proposition;
- direct-quote punctuation while retaining the stated content;
- recognized recording offsets such as [00:12], （录音约 00:12）, or a line-start offset before a speaker label;
- collected identity values in parsed meeting metadata and the reviewed output stem.

Preserve:

- company and stock names/codes, customers, products, orders, prices, percentage changes, capacity, yield, delivery, validation progress, technology routes, industry-chain judgments, market judgments, review logic, risk judgments, and business uncertainty;
- 公司：..., 机构：..., and 地区：... when they are business content rather than explicit speaker-affiliation fields;
- business event times such as 2032-07-14 14:30;
- public/external source category, negation, and uncertainty.

Do not over-summarize concrete statements. Prefer failing closed over inventing or broadly deleting a replacement.

## Strict publication gate

Before writing the Markdown, reject recognized:

- collected identity residue, speaker markers, meeting-role attribution, long direct quotes, recording offsets, or bounded first-person speaking style;
- unregistered Chinese or English person-like references and identifying role attribution;
- ambiguous person-like topics, targets, or extracted entities, unless the exact 名称（02331.HK）-style name-and-market-code form disambiguates the business object;
- phone numbers, email addresses, identity numbers, WeChat/contact fields, and URLs;
- source filenames, original-position fields, record/document IDs, attachment page locators, and audio locators;
- extracted entities not grounded in the sanitized topic label or text.

Error messages identify the rule class but do not echo a detected contact value or private URL.

These checks are conservative finite rules. They can miss novel identity forms and combination-based re-identification, and they can block a legitimate public-person source or short company name. Do not add broad fallback deletion merely to make an input pass. Clarify the reviewed source or retain the note in a restricted workflow.

## Publication and recovery

- Refuse unsupported suffixes, missing/unreadable/non-UTF-8 inputs, invalid dates, ambiguous headings, explicit mixed-source sections, empty usable content, and failed quality checks.
- Refuse an existing output unless --force is explicit.
- Never allow the final Markdown path to equal the input source path, even with --force.
- Write one same-directory .sanitizer-*.md temporary file and validate its UTF-8 bytes, required structure, and optional pending section; atomically create an absent target, or atomically replace it only under --force.
- Clean the temporary file on any pre-publication or publication failure.
- Do not delete or rewrite unrelated legacy files in the output directory.

## Delivery review

Before external distribution or broader-access knowledge-base ingestion, manually confirm:

- no speaker identity, alias, affiliation, contact value, source locator, or identifying combination remains;
- business objects, negation, uncertainty, pending items, and event times remain faithful to the reviewed source;
- the file contains the required Markdown sections, only intended topic units, and a pending section only when real pending items exist;
- the output filename is identity-free;
- no extra artifact was generated.

For repository changes, run:

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" skills/meeting-minutes-sanitizer
~~~
