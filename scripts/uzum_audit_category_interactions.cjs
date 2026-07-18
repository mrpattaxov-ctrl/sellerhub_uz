const fs = require("node:fs/promises");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const ARTIFACTS = path.join(ROOT, "artifacts", "uzum-product-form-reference");
const SCREEN_ROOT = path.join(ARTIFACTS, "screenshots", "category-interactions");
const HAR_ROOT = path.join(ARTIFACTS, "har", "category-interactions");
const JSON_OUT = path.join(ARTIFACTS, "CATEGORY_INTERACTION_FINAL_AUDIT.json");
const MD_OUT = path.join(ARTIFACTS, "CATEGORY_INTERACTION_FINAL_AUDIT.md");

async function existingMatches(directory, prefix, suffix = "") {
  return (await fs.readdir(directory)).filter((name) =>
    name.startsWith(prefix) && (!suffix || name.endsWith(suffix)));
}

async function main() {
  const categories = [];
  const missingRanks = [];
  const duplicateRanks = [];
  const invalidHars = [];
  const indexedErrors = [];
  let screenshotCount = 0;
  let totalHarEntries = 0;
  let apiEntries = 0;
  let xhrFetchEntries = 0;
  let characteristicsChecked = 0;

  for (let rank = 1; rank <= 235; rank += 1) {
    const prefix = `${String(rank).padStart(3, "0")}_`;
    const directories = await existingMatches(SCREEN_ROOT, prefix);
    const hars = await existingMatches(HAR_ROOT, prefix, ".har");
    if (directories.length !== 1 || hars.length !== 1) {
      if (!directories.length || !hars.length) missingRanks.push(rank);
      if (directories.length > 1 || hars.length > 1) {
        duplicateRanks.push({ rank, directories, hars });
      }
      continue;
    }

    const directory = path.join(SCREEN_ROOT, directories[0]);
    const index = JSON.parse(await fs.readFile(path.join(directory, "INTERACTION_INDEX.json"), "utf8"));
    const pngs = (await fs.readdir(directory)).filter((name) => name.endsWith(".png"));
    screenshotCount += pngs.length;
    characteristicsChecked += index.characteristics?.length || 0;
    if (index.errors?.length) indexedErrors.push({ rank, errors: index.errors });

    let harEntryCount = 0;
    let categoryApiEntries = 0;
    let categoryXhrFetchEntries = 0;
    try {
      const har = JSON.parse(await fs.readFile(path.join(HAR_ROOT, hars[0]), "utf8"));
      const entries = har.log?.entries || [];
      harEntryCount = entries.length;
      categoryApiEntries = entries.filter((entry) =>
        String(entry.request?.url || "").includes("api-seller.uzum.uz")).length;
      categoryXhrFetchEntries = entries.filter((entry) =>
        ["XHR", "Fetch"].includes(entry._resourceType)).length;
      totalHarEntries += harEntryCount;
      apiEntries += categoryApiEntries;
      xhrFetchEntries += categoryXhrFetchEntries;
    } catch (error) {
      invalidHars.push({ rank, har: hars[0], error: String(error.message || error) });
    }

    categories.push({
      rank,
      directory: directories[0],
      har: hars[0],
      screenshotCount: pngs.length,
      characteristicCount: index.characteristics?.length || 0,
      errorCount: index.errors?.length || 0,
      harEntryCount,
      apiEntries: categoryApiEntries,
      xhrFetchEntries: categoryXhrFetchEntries,
      schemaComparison: index.schemaComparison || null,
    });
  }

  let evidenceManifest = null;
  try {
    const manifest = JSON.parse(await fs.readFile(
      path.join(ARTIFACTS, "CATEGORY_INTERACTION_EVIDENCE_MANIFEST.json"),
      "utf8",
    ));
    evidenceManifest = {
      categoryCount: manifest.categoryCount,
      screenshotCount: manifest.screenshotCount,
      interactionHarCount: manifest.interactionHarCount,
      everyScreenshotBoundToHar: manifest.everyScreenshotBoundToHar,
      everyScreenshotClassified: manifest.everyScreenshotClassified,
      problemCount: manifest.problemCount,
    };
  } catch (error) {
    evidenceManifest = { error: String(error.message || error) };
  }
  const evidenceManifestPassed = evidenceManifest.categoryCount === 235
    && evidenceManifest.screenshotCount === screenshotCount
    && evidenceManifest.interactionHarCount === 235
    && evidenceManifest.everyScreenshotBoundToHar === true
    && evidenceManifest.everyScreenshotClassified === true
    && evidenceManifest.problemCount === 0;

  const summary = {
    auditedAt: new Date().toISOString(),
    expectedCategoryCount: 235,
    completeCategoryCount: categories.length,
    harCount: categories.length,
    screenshotCount,
    characteristicsChecked,
    totalHarEntries,
    apiEntries,
    xhrFetchEntries,
    missingRanks,
    duplicateRanks,
    invalidHars,
    indexedErrors,
    evidenceManifest,
    passed: categories.length === 235
      && !missingRanks.length
      && !duplicateRanks.length
      && !invalidHars.length
      && !indexedErrors.length
      && evidenceManifestPassed,
    harBodyPolicy: "Requests, headers, status codes, request bodies, and API/XHR/JSON response bodies are retained. Static binary response bodies are omitted and marked with _bodyOmitted=static-binary-body.",
  };
  await fs.writeFile(JSON_OUT, `${JSON.stringify({ summary, categories }, null, 2)}\n`, "utf8");

  const drifted = categories.filter((item) => item.schemaComparison
    && (item.schemaComparison.missingFromLive?.length || item.schemaComparison.addedInLive?.length));
  const markdown = [
    "# Uzum Category Interaction Final Audit",
    "",
    `- Result: **${summary.passed ? "PASS" : "FAIL"}**`,
    `- Complete categories: **${summary.completeCategoryCount}/235**`,
    `- Interaction HAR files: **${summary.harCount}**`,
    `- Screenshots: **${summary.screenshotCount}**`,
    `- Characteristics exercised: **${summary.characteristicsChecked}**`,
    `- HAR network entries: **${summary.totalHarEntries}**`,
    `- Uzum Seller API entries: **${summary.apiEntries}**`,
    `- XHR/Fetch entries: **${summary.xhrFetchEntries}**`,
    `- Missing ranks: **${summary.missingRanks.length}**`,
    `- Duplicate ranks: **${summary.duplicateRanks.length}**`,
    `- Invalid HAR files: **${summary.invalidHars.length}**`,
    `- Categories with interaction errors: **${summary.indexedErrors.length}**`,
    `- Screenshots bound one-by-one to interaction HARs: **${summary.evidenceManifest.everyScreenshotBoundToHar === true ? "yes" : "no"}**`,
    `- Screenshot states fully classified: **${summary.evidenceManifest.everyScreenshotClassified === true ? "yes" : "no"}**`,
    `- Evidence-manifest problems: **${summary.evidenceManifest.problemCount ?? "unknown"}**`,
    "",
    "## Current-vs-source schema drift",
    "",
    drifted.length
      ? drifted.map((item) => `- Rank ${item.rank}: source ${item.schemaComparison.sourceDefinedCount}, live ${item.schemaComparison.liveDefinedCount}; missing live IDs [${item.schemaComparison.missingFromLive.join(", ")}], added live IDs [${item.schemaComparison.addedInLive.join(", ")}]`).join("\n")
      : "No schema drift recorded.",
    "",
    "## HAR body policy",
    "",
    summary.harBodyPolicy,
    "",
  ].join("\n");
  await fs.writeFile(MD_OUT, markdown, "utf8");
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
  if (!summary.passed) process.exitCode = 2;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
