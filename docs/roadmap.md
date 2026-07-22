# data2agent Roadmap

> Status: current planning baseline after `v0.3.0` (`2aff25b`), updated 2026-07-22.

data2agent has finished the v0.2 observable console and v0.3 verifiable data chain. The project is now at the boundary between controlled factory shadow trial and production pilot hardening.

## Current Release: v0.3.0

v0.3.0 is suitable for a controlled, read-only factory shadow trial where the system runs beside existing operations and does not replace ERP workflows.

Completed:

- Vue Console `/v1` with Dashboard, Pipeline, Runs, Audit, Data, Quarantine, Templates, MCP Lab, Settings and Validation views.
- Typed management API, OpenAPI snapshot and generated TypeScript client.
- Raw/object browsing with server-side paging, masking, access audit and honest unknown/stale/error states.
- Dataset and object version metadata, immutable object version tables, atomic dataset publish and rollback to the previous stable version.
- Mapping Preview that reuses the formal transform core and does not mutate published data, quarantine, watermarks or runs.
- Field lineage from object fields back to raw records, source columns, transforms, batches and dataset/object versions.
- MCP evidence with principal/session/query/proposal records, unpredictable query IDs, result digests and proposal evidence checks.
- Validation Run and JSON report covering the v0.3 release gate.
- Windows portable path and local E10-like reference chain remain available for regression and acceptance checks.

Still intentionally not claimed:

- Formal production pilot readiness.
- Cross-machine commit receipts and schema fingerprints.
- E6b cross-machine reconciliation.
- Production HTTPS/mTLS and credential rotation.
- SQLite capacity/concurrency/backup baseline under real factory load.
- ERP writeback, approval workflow, SaaS multi-tenancy or full RBAC.

## Next Release: v0.4

v0.4 is the production pilot reliability release. Its goal is to prove that the cross-machine deployment can run in a real factory with correct data movement, recoverable failures and a written acceptance trail.

Recommended milestone order:

| Milestone | Main Outcome | Release Gate |
| --- | --- | --- |
| M1 Batch commit protocol | Batch ID, row count, content digest, schema fingerprint, durable commit receipt and idempotent retry | Watermark advances only after a valid stored receipt |
| M2 Batch Console | Batch status, failure reason, receipt detail and authorized replay | Factory IT can diagnose missing, duplicate, retrying and mismatched batches without opening SQLite |
| M3 E6b reconciliation | Middle-driven source stats, platform comparison, segment re-extract and soft delete | Deletes and silent changes can be detected and repaired in push topology |
| M4 Transport and credentials | HTTPS/mTLS deployment path, separated ingest/console/MCP credentials, rotation/revocation plan and principal audit | No real cross-machine production path uses plaintext HTTP |
| M5 SQLite pilot baseline | Capacity, concurrency, WAL checkpoint, backup and restore measurements with clear PostgreSQL switch thresholds | Trial load meets documented latency/recovery targets |
| M6 Factory pilot acceptance | Verified dictionary/bindings, one-week continuous run, restart/network/schema drift drills and final report | No unexplained data loss or watermark drift during the pilot window |

## Factory Trial Guidance

Before v0.4 is complete, use v0.3 only as a controlled shadow trial:

- single factory or reference-chain-like environment;
- read-only source account and whitelisted tables;
- no ERP writeback and no replacement of existing business decisions;
- human review of Agent conclusions;
- daily backup of the landing database;
- explicit `MOCK` / `REAL` mode awareness in Console.

After v0.4 passes, the project can enter a formal factory production pilot with documented receipt, reconciliation, transport, capacity and recovery evidence.

## Current Non-Goals

- ERP writeback or "do" tier automation.
- Online production mapping publish without preview, review and rollback protection.
- Complete SaaS multi-tenancy.
- Full RBAC beyond the pilot credential/principal controls.
- PostgreSQL migration before SQLite thresholds are exceeded.
- Expanding all 18 manufacturing objects without a real scenario pulling them.
- Renaming `data2agent/showroom` to a test fixture package is deferred until v0.4 has stable factory acceptance data; before then it remains a regression asset, not a product mode.
