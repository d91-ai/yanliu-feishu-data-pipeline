import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const helper = path.join(here, "mirror-to-vault.mjs");

async function write(root, relativePath, contents) {
  const target = path.join(root, relativePath);
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, contents);
}

test("mirrors vault and maintains the five curated database projections", async () => {
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), "research-vault-test-"));
  const source = path.join(temporaryRoot, "source");
  const target = path.join(temporaryRoot, "target");

  await write(source, "会议纪要（待审核）/2032-08/source.md", "source");
  await write(source, "nested/统一管线 - 行业与市场观点MD/2032-08/industry.md", "industry-md");
  await write(source, "nested/统一管线 - 行业与市场观点JSON/2032-08/industry.json", "industry-json");
  await write(source, "结构化表格（待审核）/2032-08/structured.md", "structured-md");
  await write(source, "结构化表格（正式JSON）/2032-08/formal.json", "formal-json");

  await write(target, ".obsidian/config", "keep");
  await write(target, "00-知识库首页.md", "keep");
  await write(target, "数据库/说明.md", "keep");
  await write(target, "数据库/源纪要/stale.md", "delete");
  await write(target, "obsolete.md", "delete");

  const result = spawnSync(process.execPath, [helper, source, target], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const summary = JSON.parse(result.stdout);

  assert.equal(await fs.readFile(path.join(target, ".obsidian/config"), "utf8"), "keep");
  assert.equal(await fs.readFile(path.join(target, "00-知识库首页.md"), "utf8"), "keep");
  assert.equal(await fs.readFile(path.join(target, "数据库/说明.md"), "utf8"), "keep");
  assert.equal(
    await fs.readFile(path.join(target, "数据库/源纪要/2032-08/source.md"), "utf8"),
    "source",
  );
  assert.equal(
    await fs.readFile(path.join(target, "数据库/行业与市场观点/MD/2032-08/industry.md"), "utf8"),
    "industry-md",
  );
  assert.equal(
    await fs.readFile(path.join(target, "数据库/行业与市场观点/JSON/2032-08/industry.json"), "utf8"),
    "industry-json",
  );
  assert.equal(
    await fs.readFile(path.join(target, "数据库/标的观点/MD/2032-08/structured.md"), "utf8"),
    "structured-md",
  );
  assert.equal(
    await fs.readFile(path.join(target, "数据库/标的观点/正式JSON/2032-08/formal.json"), "utf8"),
    "formal-json",
  );
  await assert.rejects(fs.access(path.join(target, "数据库/源纪要/stale.md")));
  await assert.rejects(fs.access(path.join(target, "obsolete.md")));
  assert.equal(Object.keys(summary.projections).length, 5);
});

test("fails before changing the vault when a required source is missing", async () => {
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), "research-vault-test-"));
  const source = path.join(temporaryRoot, "source");
  const target = path.join(temporaryRoot, "target");
  await write(source, "会议纪要（待审核）/source.md", "source");
  await write(target, "must-remain.md", "keep");

  const result = spawnSync(process.execPath, [helper, source, target], { encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert.equal(await fs.readFile(path.join(target, "must-remain.md"), "utf8"), "keep");
});
