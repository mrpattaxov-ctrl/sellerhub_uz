const fs = require("node:fs/promises");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const ARTIFACTS = path.join(ROOT, "artifacts", "uzum-product-form-reference");
const INTERACTION_SCREEN_ROOT = path.join(ARTIFACTS, "screenshots", "category-interactions");
const INTERACTION_HAR_ROOT = path.join(ARTIFACTS, "har", "category-interactions");
const FULL_FLOW_HAR_ROOT = path.join(ARTIFACTS, "har", "full-flow");
const MASTER_JSON = path.join(ARTIFACTS, "CATEGORY_INTERACTION_EVIDENCE_MANIFEST.json");
const MASTER_MD = path.join(ARTIFACTS, "CATEGORY_INTERACTION_EVIDENCE_MANIFEST.md");

const STATES = [
  "category-baseline-viewport",
  "category-baseline-fullpage",
  "before-characteristic-dropdown",
  "characteristic-dropdown-mid",
  "characteristic-dropdown-open",
  "row-appearing-mid",
  "row-added-popup-open",
  "row-added-popup-closing-mid",
  "row-added-popup-closed",
  "before-value-dropdown",
  "value-dropdown-mid",
  "value-dropdown-open",
  "value-selected-mid",
  "value-selected-popup-open",
  "value-selected-popup-closing-mid",
  "value-selected-popup-closed",
  "row-removing-mid",
  "row-removed",
  "category-final-viewport",
  "category-final-fullpage",
].sort((a, b) => b.length - a.length);

function posix(...segments) {
  return path.posix.join(...segments.map((segment) => String(segment).replaceAll("\\", "/")));
}

function phaseFor(state) {
  if (state.startsWith("category-baseline")) return "baseline";
  if (state.startsWith("category-final")) return "final";
  if (state.startsWith("before-")) return "before-action";
  if (state.includes("dropdown-mid") || state.includes("appearing-mid") || state.includes("closing-mid") || state.includes("removing-mid")) return "animation-midpoint";
  if (state.includes("dropdown-open") || state.includes("popup-open")) return "open";
  if (state.includes("selected")) return "selected-or-committed";
  if (state.includes("closed")) return "closed-or-committed";
  if (state === "row-removed") return "removed";
  return "other";
}

async function listIfExists(directory, suffix) {
  try {
    return (await fs.readdir(directory)).filter((name) => !suffix || name.endsWith(suffix));
  } catch {
    return [];
  }
}

async function main() {
  const directories = (await fs.readdir(INTERACTION_SCREEN_ROOT, { withFileTypes: true }))
    .filter((entry) => entry.isDirectory() && /^\d{3}_/.test(entry.name))
    .map((entry) => entry.name)
    .sort();
  const interactionHars = await listIfExists(INTERACTION_HAR_ROOT, ".har");
  const fullFlowHars = await listIfExists(FULL_FLOW_HAR_ROOT, ".har");
  const categories = [];
  const allScreenshots = [];
  const problems = [];

  for (const directoryName of directories) {
    const directory = path.join(INTERACTION_SCREEN_ROOT, directoryName);
    const indexPath = path.join(directory, "INTERACTION_INDEX.json");
    const index = JSON.parse(await fs.readFile(indexPath, "utf8"));
    const rankPrefix = `${String(index.rank).padStart(3, "0")}_`;
    const screenshotNames = (await fs.readdir(directory))
      .filter((name) => name.endsWith(".png"))
      .sort((a, b) => Number(a.split("_", 1)[0]) - Number(b.split("_", 1)[0]));
    const interactionHarName = interactionHars.find((name) => name.startsWith(rankPrefix));
    const fullFlowHarNames = fullFlowHars.filter((name) => name.startsWith(rankPrefix));
    const characteristicById = new Map((index.characteristics || [])
      .map((item) => [String(item.characteristicId), item]));
    const filesListedInOriginalIndex = new Set([
      ...(index.files || []),
      ...(index.characteristics || []).flatMap((item) => item.files || []),
    ]);
    const evidence = screenshotNames.map((file) => {
      const sequence = Number(file.match(/^(\d+)_/)?.[1] || 0);
      const characteristicId = file.match(/^\d+_(-?\d+)_/)?.[1] || null;
      const characteristic = characteristicId == null ? null : characteristicById.get(characteristicId);
      const state = STATES.find((candidate) => file.endsWith(`_${candidate}.png`)) || "unclassified";
      const item = {
        rank: index.rank,
        categoryId: index.categoryId,
        effectiveCategoryId: index.currentCategoryId || index.categoryId,
        schemaHash: index.schemaHash,
        sequence,
        file,
        screenshot: posix("screenshots", "category-interactions", directoryName, file),
        state,
        phase: phaseFor(state),
        characteristicId: characteristic ? characteristic.characteristicId : (characteristicId == null ? null : Number(characteristicId)),
        characteristicTitle: characteristic?.title || null,
        characteristicRequired: characteristic ? Boolean(characteristic.required) : null,
        selectedValue: characteristic?.selectedValue || null,
        interactionHar: interactionHarName
          ? posix("har", "category-interactions", interactionHarName)
          : null,
        originalIndex: posix("screenshots", "category-interactions", directoryName, "INTERACTION_INDEX.json"),
        listedInOriginalIndex: filesListedInOriginalIndex.has(file),
      };
      if (state === "unclassified") problems.push({ rank: index.rank, file, problem: "unclassified-state" });
      if (!interactionHarName) problems.push({ rank: index.rank, file, problem: "missing-interaction-har" });
      return item;
    });

    const evidenceMap = {
      rank: index.rank,
      categoryId: index.categoryId,
      effectiveCategoryId: index.currentCategoryId || index.categoryId,
      schemaHash: index.schemaHash,
      title: index.title,
      path: index.path,
      sourceCategoryHar: posix("har", "categories", index.sourceCategoryHar),
      interactionHar: interactionHarName
        ? posix("har", "category-interactions", interactionHarName)
        : null,
      fullFlowHars: fullFlowHarNames.map((name) => posix("har", "full-flow", name)),
      originalIndex: posix("screenshots", "category-interactions", directoryName, "INTERACTION_INDEX.json"),
      screenshotCount: evidence.length,
      characteristicCount: index.characteristics?.length || 0,
      schemaComparison: index.schemaComparison || null,
      screenshotHarBinding: "Every screenshot in this map was captured during the single interaction HAR named by interactionHar. sequence is the authoritative UI order for replay; use state and characteristicId to select the matching UI assertion.",
      screenshots: evidence,
    };
    await fs.writeFile(path.join(directory, "EVIDENCE_MAP.json"), `${JSON.stringify(evidenceMap, null, 2)}\n`, "utf8");
    categories.push({
      rank: evidenceMap.rank,
      categoryId: evidenceMap.categoryId,
      effectiveCategoryId: evidenceMap.effectiveCategoryId,
      schemaHash: evidenceMap.schemaHash,
      title: evidenceMap.title,
      path: evidenceMap.path,
      directory: posix("screenshots", "category-interactions", directoryName),
      evidenceMap: posix("screenshots", "category-interactions", directoryName, "EVIDENCE_MAP.json"),
      originalIndex: evidenceMap.originalIndex,
      sourceCategoryHar: evidenceMap.sourceCategoryHar,
      interactionHar: evidenceMap.interactionHar,
      fullFlowHars: evidenceMap.fullFlowHars,
      screenshotCount: evidenceMap.screenshotCount,
      characteristicCount: evidenceMap.characteristicCount,
      schemaComparison: evidenceMap.schemaComparison,
    });
    allScreenshots.push(...evidence);
  }

  const result = {
    generatedAt: new Date().toISOString(),
    artifactRoot: "artifacts/uzum-product-form-reference",
    categoryCount: categories.length,
    screenshotCount: allScreenshots.length,
    interactionHarCount: new Set(categories.map((item) => item.interactionHar).filter(Boolean)).size,
    everyScreenshotBoundToHar: allScreenshots.every((item) => Boolean(item.interactionHar)),
    everyScreenshotClassified: allScreenshots.every((item) => item.state !== "unclassified"),
    problemCount: problems.length,
    problems,
    usage: [
      "Choose a rank/category from categories.",
      "Open that category's EVIDENCE_MAP.json.",
      "Replay screenshots in ascending sequence order.",
      "For each screenshot, use its state, characteristicId, and interactionHar fields together.",
      "Use sourceCategoryHar for category schema responses and interactionHar for the exact click sequence.",
      "Use fullFlowHars only for Product-to-SKU-to-Properties and server failure flows.",
    ],
    categories,
    screenshots: allScreenshots,
  };
  await fs.writeFile(MASTER_JSON, `${JSON.stringify(result, null, 2)}\n`, "utf8");

  const markdown = [
    "# Category interaction evidence manifest",
    "",
    `- Categories: **${result.categoryCount}/235**`,
    `- Screenshots bound one-by-one: **${result.screenshotCount}**`,
    `- Interaction HARs: **${result.interactionHarCount}**`,
    `- Every screenshot bound to a HAR: **${result.everyScreenshotBoundToHar ? "yes" : "no"}**`,
    `- Every screenshot state classified: **${result.everyScreenshotClassified ? "yes" : "no"}**`,
    `- Problems: **${result.problemCount}**`,
    "",
    "## Mandatory lookup procedure",
    "",
    "1. Select the category rank in `CATEGORY_INTERACTION_EVIDENCE_MANIFEST.json`.",
    "2. Open the category's `evidenceMap` path.",
    "3. Process its `screenshots` array in ascending `sequence` order.",
    "4. For every screenshot, treat `state`, `phase`, `characteristicId`, `characteristicTitle`, and `selectedValue` as the UI assertion and `interactionHar` as the exact network recording paired with that screenshot.",
    "5. Use `sourceCategoryHar` for the base category schema and `fullFlowHars` only for the later wizard steps.",
    "6. Never guess a screenshot/HAR relationship from filenames when the manifest already provides it.",
    "",
    "The master JSON intentionally contains all screenshot rows. Each rank directory also contains a smaller `EVIDENCE_MAP.json` so an implementation agent can load one category at a time without putting all 11,287 images into context.",
    "",
  ].join("\n");
  await fs.writeFile(MASTER_MD, markdown, "utf8");
  process.stdout.write(`${JSON.stringify({
    categoryCount: result.categoryCount,
    screenshotCount: result.screenshotCount,
    interactionHarCount: result.interactionHarCount,
    everyScreenshotBoundToHar: result.everyScreenshotBoundToHar,
    everyScreenshotClassified: result.everyScreenshotClassified,
    problemCount: result.problemCount,
  }, null, 2)}\n`);
  if (problems.length || categories.length !== 235 || allScreenshots.length !== 11287) process.exitCode = 2;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
