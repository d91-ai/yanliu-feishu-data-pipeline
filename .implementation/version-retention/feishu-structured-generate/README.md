# Feishu Structured Generate

Independent service that generates a reviewed meeting-minutes structured Markdown table from an archived source Markdown file.

## Responsibilities

- Receive a source non-structured Bitable `record_id`.
- Verify the source record is reviewed and archived.
- Read the archived `.md` file from `归档链接`.
- Queue a semantic-generation job after the approved archive and SHA256 are verified.
- Load contract v9/schema v9 from the installed Skill's `contract/manifest.json`.
- Let the host-only Codex worker read the complete `source.md` and create one source-grounded, transient `claim_units.json` using the manifest prompt and claim schema.
- Call the manifest's unified `scripts/generate_table.py` entrypoint to generate draft review Markdown only.
- After upload, idempotently create or refresh the matching structured Base row
  from service-held `source_record_id`, `meeting_uid`, row count, and Drive URL,
  then require the structured review baseline capture to complete before the
  source record is marked generated. Schema-v9 review Markdown remains free of
  machine frontmatter.
- Upload the generated `YYYY-MM-DD - 会议系列.md` table file to the structured pending month folder.
- Write a local backup under the mounted structured output directory.
- Update the source record fields: `表格生成状态`, `表格链接`, `生成时间`, `表格行数`, `表格生成错误`.

Once the skill has successfully written a structured artifact, the
service does not run a later Markdown layout/heading gate. Upload, hash/link
resolution, backup, and Base writes remain hard requirements; cosmetic format
differences do not overturn generation success.

It does not archive structured files. The structured workflow reads `待审核MD链接`, preserves `审核前基线MD链接`, and writes the approved copy to `审核后归档MD链接`.

It also exposes a separate fail-closed official JSON endpoint for reviewed structured Markdown records. Review Markdown stays human-editable and draft Markdown generation never invokes JSON export. The endpoint re-reads the structured Base record, requires explicit human review plus the archive/version gates, writes the downloaded archive bytes unchanged to a private temporary file, and invokes the same manifest `generate_table` entrypoint with `--structured-markdown`. The v9 JSON keeps only Skill-native metadata. Review state, record IDs, URLs, timestamps, and pipeline versions remain in Base or the pipeline registry. The service validates meeting ID, schema version, security-master version, and the exact archive-byte SHA-256 before upload. Empty `rows` is valid. Once the new Base pointers are confirmed, cleanup considers only the previous authoritative file token resolved by the pipeline; it does not inspect legacy metadata embedded in JSON.

## API

### GET /healthz

Returns service health and local configuration status without secrets.

### POST /generate

Headers:

```text
X-Structured-Token: <shared token>
Content-Type: application/json
```

Body:

```json
{"record_id":"recxxxx"}
```

The endpoint returns quickly after writing an auditable job under
`STRUCTURED_SEMANTIC_JOB_DIR`. It does not wait for a model call and does not
generate official JSON.

Status codes:

- `202 queued`
- `202 queued_existing`
- `200 skipped_existing`
- `200 skipped_no_rows`
- `400 invalid_request`
- `401 unauthorized`
- `409 already_running`
- `409 not_ready`
- `500 failed`
- `500 config_error`

### POST /generate-official-json

Headers:

```text
X-Structured-Token: <shared token>
Content-Type: application/json
```

Body:

```json
{"record_id":"recxxxx"}
```

Status codes:

- `200 generated`
- `200 generated_reconciled`（写回响应丢失后，重读两张 Base 记录确认完整终态）
- `200 skipped_up_to_date`
- `401 unauthorized`
- `409 not_ready`
- `409 source_md_hash_mismatch`
- `409 row_count_mismatch`
- `500 failed`
- `500 config_error`
- `503 official_json_commit_outcome_uncertain`（终态重读也失败；不得降级或盲目重跑，先对账正式记录、源记录的链接/hash/行数后再幂等重试）
- `503 official_json_upload_outcome_uncertain`（Drive 上传响应与按确切文件名/hash 的对账都失败；保留本地 upload intent，恢复网络后按同一记录重试）
- `503 generated_cleanup_pending`（新 JSON 已成为权威链接，但旧文件清理尚未完成；重试时只清理旧文件，不与 MD 同时生成 JSON）

## CLI

```bash
python structured_generate_service.py doctor --online
python structured_generate_service.py init-fields
python structured_generate_service.py init-fields --apply
python structured_generate_service.py generate-record recxxxx --apply
python structured_generate_service.py complete-job recxxxx-sha-a01 --apply
python structured_generate_service.py fail-job recxxxx-sha-a01 --apply
python structured_generate_service.py generate-official-json-record recxxxx --apply
python structured_generate_service.py serve --apply
```

`init-fields --apply` creates missing source-table fields and may add missing select options to `表格生成状态`. It does not change existing field types and does not delete fields or options.
All service or one-record commands that can write Drive/Base or publish job
state require explicit `--apply`.

## Deployment

Copy `.env.example` to `.env` on the deployment host. In addition to the private
credentials and resource identifiers, set `STRUCTURED_SKILL_HOST_DIR` to the
reviewed structured-table skill directory and `STRUCTURED_OUTPUT_HOST_DIR` to a
private durable backup directory. Compose reads these host-only values from the
project `.env` during interpolation. The mounted Skill must expose contract
v9/schema v9, unified `scripts/generate_table.py`, and bundled
`data/security_master.csv`; startup fails closed when those contract files are
missing or the configured official JSON entrypoint differs from the manifest.

```bash
docker compose config --quiet
docker compose up -d --build
curl http://127.0.0.1:8790/healthz
```

## Host semantic worker

The worker intentionally runs on the Mac host instead of inside Docker. It uses
the current signed-in Codex CLI without mounting Codex credentials into the
container. Each job is processed in one claim-unit model stage with
`--ephemeral`, `--sandbox read-only`, high reasoning, and a provider-compatible
schema derived from the canonical claim schema. The meeting file is treated as
untrusted data. Queue files remain private, atomic, and directory-fsynced.

Copy `org.example.researchpipeline.feishu-structured-semantic-worker.plist.example` outside the
repository, replace every `/absolute/path/` value and
`REPLACE_WITH_EXACT_CODEX_MODEL_ID`, then validate that rendered plist before
installation. The example itself is intentionally not loadable. Also copy
`semantic_worker.py` and `skill_contract.py` to the reviewed live service
directory. Do not configure `STRUCTURED_SYMBOL_UNIVERSE_PATH`; both worker and
service read the Skill-bundled security master declared by the manifest. The
queue is single-worker and persists pending, processing,
done, and failed job directories under the service-local `data/semantic-jobs`
mount for audit and retry. This queue mount is separate from the final
structured-output backup mount, so the LaunchAgent does not depend on continuous
access to Documents.

The upload entry remains unchanged: users either upload a complete meeting file
manually or use the existing upload skill. No semantic artifacts are created
before human review and archive completion.

Nginx should proxy:

- `/feishu-structured/healthz` -> `http://host.docker.internal:8790/healthz`
- `/feishu-structured/generate` -> `http://host.docker.internal:8790/generate`
- `/feishu-structured/generate-official-json` -> `http://host.docker.internal:8790/generate-official-json`
