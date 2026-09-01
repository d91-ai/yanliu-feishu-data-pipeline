# Server

Standard-library Python ingestion service for one meeting-minutes Markdown file.
The local candidate now coordinates the first durable pipeline boundary:

```text
request validation
→ current Drive file
→ immutable pre-review copy
→ meeting Base record
→ two generation jobs
→ meeting registry
→ completed receipt
```

This local candidate is implemented and tested. Production has not been
restarted or switched to this contract.

## Runtime

- Direct Python process on `127.0.0.1:8789`.
- No framework or third-party runtime package.
- User database: `data/upload_users.json`.
- Ingestion receipts: `data/meeting-ingestion-receipts/`.
- Meeting registry: `data/meeting-registry/`.
- Generation spool: `data/meeting-generation-jobs/pending/`.
- Allowed input: one non-empty UTF-8-compatible `.md`, maximum `10 MB` by default.

The repository has no Dockerfile for this service. Any external packaging must
include the exact shared pipeline contract and persist all four data locations.

## Environment

Copy `.env.example` to `.env` on the deployment host. Required pipeline values:

```text
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_PARENT_FOLDER_TOKEN=                 # 数据库/会议纪要 root
FEISHU_BASELINE_PARENT_FOLDER_TOKEN=        # 归档/会议纪要/审核前 root
FEISHU_MEETING_BASE_APP_TOKEN=
FEISHU_MEETING_BASE_TABLE_ID=
FEISHU_OUTPUT_OWNER_OPEN_ID=
FEISHU_PIPELINE_CONTRACT_PATH=../.implementation/meeting-pipeline-contract/meeting_pipeline_contract.py
FEISHU_GENERATION_JOB_SPOOL_PATH=data/meeting-generation-jobs
FEISHU_MEETING_REGISTRY_PATH=data/meeting-registry
```

Both Drive roots must already contain the target `YYYY-MM` folder. The service
does not create folders silently. Do not print or commit `.env`.

The legacy meeting-content validator variables remain diagnostic-only. Uploaded
Markdown is not rejected for body structure; identity and routing metadata are
strictly validated by the shared pipeline contract.

## Permissions

The app needs the existing Drive upload/read and owner-transfer capabilities,
plus Drive copy and Base record read/write for the configured resources. Exact
production scope names and released app version must be verified in the Feishu
permission console before deployment; this local implementation does not change
permissions.

New current and baseline files repeat owner transfer when
`FEISHU_OUTPUT_OWNER_OPEN_ID` is set. The app remains a full-access collaborator
and files stay in their assigned folders.

## API

### `GET /healthz` or `/readyz`

Returns `200` only when credentials, both Drive roots, Base binding, shared
contract, local paths, and at least one enabled upload user are configured. It
does not call Feishu.

### `POST /api/upload`

Accepts `multipart/form-data`:

- `file`: required Markdown file.
- `meeting_date`: required canonical `YYYY-MM-DD`.
- `meeting_series`: required selected value.
- `meeting_type`: required selected value.
- `meeting_uid`: optional; omit for first manual ingestion, supply for an update.
- `dry_run`: optional boolean.

The metadata may instead be sent once in the query string. Duplicate, unknown,
or conflicting query/multipart values are rejected. The former `date` parameter
and arbitrary metadata are no longer accepted after direct cutover.

Authentication uses exactly one of:

- `Authorization: Bearer <token>` (preferred)
- `X-Upload-Token: <token>`

Every non-dry-run request must carry one stable `Idempotency-Key` of 16–128
allowed ASCII characters. Same key + same request replays or resumes; same key
+ different file/metadata/resource binding returns `409`.

The service ignores the original filename for routing and creates:

```text
YYYY-MM-DD - 会议系列 - 会议纪要 - vN.md
```

UID and meeting type remain in Base, receipts, jobs and JSON metadata, not the
display filename. A source update with the same UID and changed content advances
the global integer version. Same UID + same content hash returns `unchanged`.

Success example:

```json
{
  "ok": true,
  "status": "created",
  "meeting_uid": "mtg_550e8400e29b41d4a716446655440000",
  "record_id": "rec...",
  "data_version": 1,
  "original_file_name": "upload.md",
  "normalized_file_name": "2032-08-13 - 示例研究周会 - 会议纪要 - v1.md",
  "file_token": "box...",
  "url": "https://feishu.cn/file/box...",
  "generation_queued": [
    "industry_market_viewpoints",
    "structured_viewpoints"
  ],
  "idempotency_status": "created",
  "request_id": "..."
}
```

Dry-run validates metadata, contract, authentication and both month folders but
makes no Drive/Base/local receipt write. A first dry-run intentionally returns
an empty UID because the permanent UID is generated only inside a durable real
request.

If a remote effect or local durable stage cannot be confirmed, the API returns
HTTP 503 with `status=outcome_uncertain`. Retry only with the same key. The
receipt resumes from `prepared`, `drive_uploaded`, `baseline_captured`,
`base_committed`, or `jobs_queued`; it never reports success before registry and
both jobs are durable.

## CLI

```bash
python3 feishu_upload_service.py doctor
python3 feishu_upload_service.py users add --name dou --source "upload page" --write-token-file secrets/dou-upload-token.txt
python3 feishu_upload_service.py users list
python3 feishu_upload_service.py users disable dou
python3 feishu_upload_service.py users rotate dou --write-token-file secrets/dou-upload-token.txt
python3 feishu_upload_service.py serve --host 127.0.0.1 --port 8789 --apply
```

`serve` keeps the existing explicit `--apply` gate. User token updates retain
the existing private-file and atomic activation behavior.

## Current boundary

- Implemented locally: request/identity/version contract, current file hash
  confirmation, pre-review copy, Base create/update reconciliation, generation
  jobs, registry and idempotent receipts.
- Implemented in adjacent local candidates: dual generation workers, three
  review callbacks, reviewed JSON replacement, post-commit old-file archival,
  publication audit and offline historical migration planning.
- Remaining production integration: this upload transaction itself does not
  archive the prior source file for a non-review upstream re-upload. Until that
  cleanup is integrated, such updates require the publication reconciliation
  step; the human source-review path already archives the superseded current
  source after its new Base authority is confirmed.
- Not performed: production Base/Drive/Workflow changes, permission changes,
  deployment, restart, installed Skill changes or historical writes.
