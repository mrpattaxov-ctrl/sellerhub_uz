const fs = require("node:fs/promises");
const path = require("node:path");
let playwright;
try {
  playwright = require("playwright");
} catch {
  playwright = require(process.env.PLAYWRIGHT_MODULE_PATH
    || "C:/Users/Abdulaziz/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright");
}
const { chromium } = playwright;

const ROOT = path.resolve(__dirname, "..");
const ARTIFACTS = path.join(ROOT, "artifacts", "uzum-product-form-reference");
const AUDIT_PATH = path.join(ARTIFACTS, "ALL_CATEGORY_SCHEMA_AUDIT.json");
const OUT_DIR = path.join(ARTIFACTS, "screenshots", "categories");
const CHECKPOINT_PATH = path.join(OUT_DIR, "CATEGORY_VISUAL_CHECKPOINT.json");
const TARGET_URL = "https://seller.uzum.uz/seller/7138/products/new";
const CDP_URL = process.env.UZUM_CDP_URL || "http://127.0.0.1:9222";

function arg(name, fallback) {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 ? process.argv[at + 1] : fallback;
}

function screenshotName(category) {
  const rank = String(category.rank).padStart(3, "0");
  const replacement = category.currentCategoryId
    ? `_current-${category.currentCategoryId}`
    : "";
  return `${rank}_category-${category.id}${replacement}_${category.hash}_category-fields.png`;
}

function characteristicOptionsName(category) {
  return screenshotName(category).replace(
    "_category-fields.png",
    "_characteristic-options.png",
  );
}

async function writeCheckpoint(checkpoint) {
  await fs.writeFile(CHECKPOINT_PATH, `${JSON.stringify(checkpoint, null, 2)}\n`, "utf8");
}

async function visibleExactText(page, text) {
  const deadline = Date.now() + 12_000;
  while (Date.now() < deadline) {
    const matches = page.getByText(text, { exact: true });
    const count = await matches.count();
    for (let index = count - 1; index >= 0; index -= 1) {
      const candidate = matches.nth(index);
      if (await candidate.isVisible()) return candidate;
    }
    await page.waitForTimeout(300);
  }
  throw new Error(`Visible option not found: ${text}`);
}

async function selectCategory(page, category) {
  await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 45_000 });
  const rootInput = page.locator('input[placeholder="Название категории или товара"]');
  try {
    await rootInput.waitFor({ state: "visible", timeout: 8_000 });
  } catch {
    const changeButton = page.getByRole("button", { name: "Изменить", exact: true });
    if (await changeButton.count()) {
      await changeButton.click();
      await rootInput.waitFor({ state: "visible", timeout: 15_000 });
    } else {
      await page.reload({ waitUntil: "domcontentloaded", timeout: 45_000 });
      const changeAfterReload = page.getByRole("button", { name: "Изменить", exact: true });
      if (await changeAfterReload.count()) await changeAfterReload.click();
      await rootInput.waitFor({ state: "visible", timeout: 20_000 });
    }
  }
  await rootInput.click();
  await rootInput.fill(category.path[0]);

  for (let level = 0; level < category.path.length; level += 1) {
    const currentTitle = category.path[level] === "Ювелирные украшения"
      ? "Бижутерные украшения"
      : category.path[level];
    const option = await visibleExactText(page, currentTitle);
    await option.click();
    if (level < category.path.length - 1) {
      const subInputs = page.locator('input[placeholder="Выбрать подкатегорию"]');
      await subInputs.nth(level).waitFor({ state: "visible", timeout: 15_000 });
      await subInputs.nth(level).click();
      const nextTitle = category.path[level + 1] === "Ювелирные украшения"
        ? "Бижутерные украшения"
        : category.path[level + 1];
      if (["Ремешки, застежки для часов", "Часы"].includes(nextTitle)) {
        for (let step = 0; step < 35; step += 1) {
          const target = page.getByText(nextTitle, { exact: true });
          let visible = false;
          for (let index = 0; index < await target.count(); index += 1) {
            if (await target.nth(index).isVisible()) {
              visible = true;
              break;
            }
          }
          if (visible) break;
          await subInputs.nth(level).press("ArrowDown");
          await page.waitForTimeout(60);
        }
        await page.waitForTimeout(500);
      }
    }
  }

  const accept = page.getByRole("button", { name: "Принять", exact: true });
  await accept.waitFor({ state: "visible", timeout: 10_000 });
  const effectiveCategoryId = category.currentCategoryId || category.id;
  const characteristicsResponse = page.waitForResponse(
    (response) => response.url().includes(
      `getDefinedCharacteristics?categoryIds=${effectiveCategoryId}`,
    ),
    { timeout: 30_000 },
  ).catch(() => null);
  await accept.click();
  const liveResponse = await characteristicsResponse;
  const liveDefinedCharacteristics = liveResponse
    ? await liveResponse.json().catch(() => null)
    : null;

  const characteristics = page.getByText("Характеристики товара", { exact: true });
  await characteristics.waitFor({ state: "visible", timeout: 30_000 });
  await characteristics.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  return {
    effectiveCategoryId,
    definedCharacteristics: liveDefinedCharacteristics,
  };
}

async function main() {
  const start = Number(arg("start", "1"));
  const end = Number(arg("end", "235"));
  const rankList = arg("ranks", "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
    .map((value) => Number(value))
    .filter(Number.isFinite);
  const requestedRanks = new Set(rankList);
  const audit = JSON.parse(await fs.readFile(AUDIT_PATH, "utf8"));
  const categories = audit.groups
    .map((group) => ({
      rank: group.rank,
      hash: group.hash,
      id: group.representative.categoryId,
      title: group.representative.title,
      path: group.representative.path,
    }))
    .map((category) => {
      if (category.rank === 84) {
        return {
          ...category,
          currentCategoryId: 2867,
          path: [
            "Аксессуары",
            "Мужские аксессуары",
            "Часы и ремешки",
            "Ремешки, застежки для часов",
          ],
        };
      }
      if (category.rank === 229) {
        return {
          ...category,
          currentCategoryId: 2607,
          path: ["Аксессуары", "Женские аксессуары", "Часы и ремешки", "Часы"],
        };
      }
      return category;
    })
    .filter((category) => requestedRanks.size
      ? requestedRanks.has(category.rank)
      : category.rank >= start && category.rank <= end);

  await fs.mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  let page = context.pages().find((item) => item.url().includes("seller.uzum.uz"));
  if (!page) page = await context.newPage();
  await page.bringToFront();
  await page.setViewportSize({ width: 1920, height: 1080 });

  const checkpoint = {
    startedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    requested: { start, end },
    completed: [],
    failed: [],
  };

  for (const category of categories) {
    const file = path.join(OUT_DIR, screenshotName(category));
    try {
      await selectCategory(page, category);
      await page.screenshot({ path: file, fullPage: false, animations: "disabled" });
      const optionsFile = path.join(OUT_DIR, characteristicOptionsName(category));
      const characteristicControl = page.getByText("Добавить характеристику", { exact: true });
      if (await characteristicControl.count()) {
        await characteristicControl.click();
        await page.waitForTimeout(350);
        await page.screenshot({
          path: optionsFile,
          fullPage: false,
          animations: "disabled",
        });
      }
      checkpoint.completed.push({
        rank: category.rank,
        categoryId: category.id,
        title: category.title,
        path: category.path,
        file: path.basename(file),
        optionsFile: await fs.access(optionsFile).then(
          () => path.basename(optionsFile),
          () => null,
        ),
      });
      process.stdout.write(`OK ${category.rank}/235 ${category.id} ${category.title}\n`);
    } catch (error) {
      const scrollables = await page.evaluate(() => Array.from(document.querySelectorAll("*"))
        .filter((element) => {
          const style = getComputedStyle(element);
          return style.visibility !== "hidden"
            && style.display !== "none"
            && element.clientHeight > 80
            && element.scrollHeight > element.clientHeight + 20;
        })
        .slice(0, 20)
        .map((element) => ({
          tag: element.tagName,
          className: String(element.className || ""),
          clientHeight: element.clientHeight,
          scrollHeight: element.scrollHeight,
          text: String(element.innerText || "").slice(0, 180),
        }))).catch(() => []);
      process.stdout.write(`SCROLLABLES ${category.rank} ${JSON.stringify(scrollables)}\n`);
      await page.screenshot({
        path: path.join(OUT_DIR, `_failed_rank-${category.rank}.png`),
        fullPage: false,
        animations: "disabled",
      }).catch(() => {});
      checkpoint.failed.push({
        rank: category.rank,
        categoryId: category.id,
        title: category.title,
        path: category.path,
        error: String(error && error.message ? error.message : error),
      });
      process.stdout.write(`FAIL ${category.rank}/235 ${category.id} ${category.title}: ${error.message}\n`);
    }
    checkpoint.updatedAt = new Date().toISOString();
    await writeCheckpoint(checkpoint);
  }

  checkpoint.finishedAt = new Date().toISOString();
  await writeCheckpoint(checkpoint);
  await browser.close();
  process.stdout.write(
    `DONE completed=${checkpoint.completed.length} failed=${checkpoint.failed.length}\n`,
  );
  if (checkpoint.failed.length) process.exitCode = 2;
}

module.exports = {
  ARTIFACTS,
  AUDIT_PATH,
  TARGET_URL,
  CDP_URL,
  selectCategory,
};

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
