import fs from "node:fs/promises";
import path from "node:path";

const [sourceArg, targetArg] = process.argv.slice(2);
if (!sourceArg || !targetArg) {
  throw new Error("usage: mirror-to-vault.mjs <source> <target>");
}

const sourceRoot = path.resolve(sourceArg);
const targetRoot = path.resolve(targetArg);

const protectedRoots = new Set([".obsidian", "00-知识库首页.md"]);
const protectedFileNames = new Set(["00-目录.md"]);
const managedTargetRoots = new Set(["数据库"]);

const projections = [
  { sourceName: "会议纪要（待审核）", targetPath: "数据库/源纪要" },
  {
    sourceName: "统一管线 - 行业与市场观点MD",
    targetPath: "数据库/行业与市场观点/MD",
  },
  {
    sourceName: "统一管线 - 行业与市场观点JSON",
    targetPath: "数据库/行业与市场观点/JSON",
  },
  { sourceName: "结构化表格（待审核）", targetPath: "数据库/标的观点/MD" },
  {
    sourceName: "结构化表格（正式JSON）",
    targetPath: "数据库/标的观点/正式JSON",
  },
];

function resolveWithin(root, relativePath) {
  const resolved = path.resolve(root, relativePath);
  if (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)) {
    throw new Error(`unsafe relative path: ${relativePath}`);
  }
  return resolved;
}

function isProtected(relativePath) {
  const rootName = relativePath.split(path.sep)[0];
  return protectedRoots.has(rootName) || protectedFileNames.has(path.basename(relativePath));
}

function isProtectedTarget(relativePath) {
  const rootName = relativePath.split(path.sep)[0];
  return isProtected(relativePath) || managedTargetRoots.has(rootName);
}

async function collectTree(root, { ignore = () => false } = {}) {
  const files = new Map();
  const directories = new Set([""]);

  async function visit(relativeDirectory) {
    const absoluteDirectory = resolveWithin(root, relativeDirectory);
    const entries = await fs.readdir(absoluteDirectory, { withFileTypes: true });
    for (const entry of entries) {
      const relativePath = path.join(relativeDirectory, entry.name);
      if (ignore(relativePath)) continue;

      const absolutePath = resolveWithin(root, relativePath);
      if (entry.isDirectory()) {
        directories.add(relativePath);
        await visit(relativePath);
      } else {
        files.set(relativePath, await fs.lstat(absolutePath));
      }
    }
  }

  await visit("");
  return { files, directories };
}

async function lstatOrNull(filePath) {
  try {
    return await fs.lstat(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function syncTree(sourceDirectory, targetDirectory, {
  ignoreSource = () => false,
  ignoreTarget = () => false,
} = {}) {
  await fs.mkdir(sourceDirectory, { recursive: true });
  await fs.mkdir(targetDirectory, { recursive: true });

  const source = await collectTree(sourceDirectory, { ignore: ignoreSource });
  let changedFiles = 0;

  const sourceDirectories = [...source.directories]
    .filter(Boolean)
    .sort((a, b) => a.split(path.sep).length - b.split(path.sep).length);

  for (const relativeDirectory of sourceDirectories) {
    const targetPath = resolveWithin(targetDirectory, relativeDirectory);
    const targetStat = await lstatOrNull(targetPath);
    if (targetStat && !targetStat.isDirectory()) {
      await fs.rm(targetPath, { force: true, recursive: true });
    }
    await fs.mkdir(targetPath, { recursive: true });
  }

  for (const [relativePath, sourceStat] of source.files) {
    const sourcePath = resolveWithin(sourceDirectory, relativePath);
    const targetPath = resolveWithin(targetDirectory, relativePath);
    const targetStat = await lstatOrNull(targetPath);
    const unchanged = targetStat?.isFile()
      && targetStat.size === sourceStat.size
      && Math.abs(targetStat.mtimeMs - sourceStat.mtimeMs) < 1000;
    if (unchanged) continue;

    await fs.mkdir(path.dirname(targetPath), { recursive: true });
    const temporaryPath = `${targetPath}.research-sync-${process.pid}`;
    await fs.copyFile(sourcePath, temporaryPath);
    await fs.chmod(temporaryPath, sourceStat.mode);
    await fs.utimes(temporaryPath, sourceStat.atime, sourceStat.mtime);
    if (targetStat?.isDirectory()) {
      await fs.rm(targetPath, { recursive: true, force: true });
    }
    await fs.rename(temporaryPath, targetPath);
    changedFiles += 1;
  }

  const target = await collectTree(targetDirectory, { ignore: ignoreTarget });
  let deletedFiles = 0;
  let deletedDirectories = 0;

  for (const relativePath of target.files.keys()) {
    if (!source.files.has(relativePath)) {
      await fs.rm(resolveWithin(targetDirectory, relativePath), { force: true });
      deletedFiles += 1;
    }
  }

  const targetDirectories = [...target.directories]
    .filter(Boolean)
    .sort((a, b) => b.split(path.sep).length - a.split(path.sep).length);

  for (const relativeDirectory of targetDirectories) {
    if (!source.directories.has(relativeDirectory)) {
      try {
        await fs.rmdir(resolveWithin(targetDirectory, relativeDirectory));
        deletedDirectories += 1;
      } catch (error) {
        if (error?.code !== "ENOTEMPTY" && error?.code !== "ENOENT") throw error;
      }
    }
  }

  return {
    changed_files: changedFiles,
    deleted_files: deletedFiles,
    deleted_directories: deletedDirectories,
  };
}

await fs.mkdir(sourceRoot, { recursive: true });
await fs.mkdir(targetRoot, { recursive: true });

// Resolve every required folder before changing the vault. Folder relocation is
// tolerated, but a missing or duplicated name fails closed to avoid deleting a
// valid local projection from an ambiguous source.
const sourceIndex = await collectTree(sourceRoot, { ignore: isProtected });
const resolvedProjections = projections.map((projection) => {
  const matches = [...sourceIndex.directories].filter(
    (relativePath) => path.basename(relativePath) === projection.sourceName,
  );
  if (matches.length !== 1) {
    throw new Error(
      `required projection source must be unique: ${projection.sourceName}; matches=${matches.length}`,
    );
  }
  return { ...projection, sourcePath: matches[0] };
});

const baseSummary = await syncTree(sourceRoot, targetRoot, {
  ignoreSource: isProtected,
  ignoreTarget: isProtectedTarget,
});

const projectionSummaries = {};
for (const projection of resolvedProjections) {
  const summary = await syncTree(
    resolveWithin(sourceRoot, projection.sourcePath),
    resolveWithin(targetRoot, projection.targetPath),
  );
  projectionSummaries[projection.targetPath] = {
    source: projection.sourcePath,
    ...summary,
  };
}

const projectionTotals = Object.values(projectionSummaries).reduce(
  (totals, summary) => ({
    changed_files: totals.changed_files + summary.changed_files,
    deleted_files: totals.deleted_files + summary.deleted_files,
    deleted_directories: totals.deleted_directories + summary.deleted_directories,
  }),
  { changed_files: 0, deleted_files: 0, deleted_directories: 0 },
);

process.stdout.write(`${JSON.stringify({
  ok: true,
  changed_files: baseSummary.changed_files + projectionTotals.changed_files,
  deleted_files: baseSummary.deleted_files + projectionTotals.deleted_files,
  deleted_directories:
    baseSummary.deleted_directories + projectionTotals.deleted_directories,
  base: baseSummary,
  projections: projectionSummaries,
  protected_local: [...protectedRoots, "*/00-目录.md"],
  managed_local: [...managedTargetRoots],
})}\n`);
