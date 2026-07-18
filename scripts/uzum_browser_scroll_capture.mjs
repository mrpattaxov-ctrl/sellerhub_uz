import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

const ROOT = "C:/Users/Abdulaziz/Desktop/SellerHubAbdulaziz/sellerhub_uz";
const ARTIFACTS = path.join(ROOT, "artifacts", "uzum-product-form-reference");
const AUDIT_PATH = path.join(ARTIFACTS, "ALL_CATEGORY_SCHEMA_AUDIT.json");
const OUT_ROOT = path.join(ARTIFACTS, "screenshots", "category-scroll-sequences");
const TARGET_URL = "https://seller.uzum.uz/seller/7138/products/new";

function cleanName(value) {
  return String(value || "unknown")
    .normalize("NFKD")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "unknown";
}

function categoryDirectory(category) {
  return `${String(category.rank).padStart(3, "0")}_${category.hash}_category-${category.currentCategoryId || category.id}`;
}

async function loadCategories(requestedRanks = []) {
  const document = JSON.parse(await fs.readFile(AUDIT_PATH, "utf8"));
  const ranks = new Set(requestedRanks.map(Number));
  return document.groups
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
      if (category.rank === 145) {
        return {
          ...category,
          path: ["Туризм, рыбалка и охота", "Кемпинг", "Палатки"],
        };
      }
      if (category.rank === 205) {
        return {
          ...category,
          path: ["Туризм, рыбалка и охота", "Кемпинг", "Аксессуары для палаток и тентов"],
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
    .filter((category) => !ranks.size || ranks.has(category.rank));
}

async function visibleExactText(tab, texts, timeoutMs = 12_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const text of texts.filter(Boolean)) {
      const locator = tab.playwright.getByText(text, { exact: true });
      const count = await locator.count();
      for (let index = count - 1; index >= 0; index -= 1) {
        const candidate = locator.nth(index);
        if (await candidate.isVisible()) return candidate;
      }
    }
    await tab.playwright.waitForTimeout(200);
  }
  throw new Error(`Visible option not found: ${texts.filter(Boolean).join(" | ")}`);
}

async function uniqueLocator(locator, label) {
  const count = await locator.count();
  if (count !== 1) throw new Error(`${label} count=${count}`);
  return locator;
}

async function selectCategory(tab, category) {
  await tab.goto(TARGET_URL);
  await tab.playwright.waitForLoadState({ state: "domcontentloaded", timeoutMs: 45_000 });
  // Chrome's page model only reports controls inside the current scroll viewport
  // as visible, so always return to the true top before locating the category box.
  await moveToEdge(tab, "PAGEUP");
  const rootInput = tab.playwright.getByPlaceholder("Название категории или товара", { exact: true });
  await rootInput.waitFor({ state: "visible", timeoutMs: 30_000 });
  await uniqueLocator(rootInput, "root category input");
  await rootInput.click();
  await rootInput.fill(category.path[0]);

  for (let level = 0; level < category.path.length; level += 1) {
    const title = category.path[level] === "Ювелирные украшения"
      ? "Бижутерные украшения"
      : category.path[level];
    const option = await visibleExactText(tab, [title]);
    await option.click();
    if (level >= category.path.length - 1) continue;

    await tab.playwright.waitForTimeout(250);
    const subInputs = tab.playwright.getByPlaceholder("Выбрать подкатегорию", { exact: true });
    const count = await subInputs.count();
    if (count <= level) throw new Error(`subcategory input missing at level ${level}; count=${count}`);
    const subInput = subInputs.nth(level);
    await subInput.click();

    const nextTitle = category.path[level + 1] === "Ювелирные украшения"
      ? "Бижутерные украшения"
      : category.path[level + 1];
    if (["Ремешки, застежки для часов", "Часы"].includes(nextTitle)) {
      for (let step = 0; step < 35; step += 1) {
        const target = tab.playwright.getByText(nextTitle, { exact: true });
        const targetCount = await target.count();
        let visible = false;
        for (let index = 0; index < targetCount; index += 1) {
          if (await target.nth(index).isVisible()) {
            visible = true;
            break;
          }
        }
        if (visible) break;
        await subInput.press("ArrowDown");
        await tab.playwright.waitForTimeout(60);
      }
    }
  }

  const accept = tab.playwright.getByRole("button", { name: "Принять", exact: true });
  await accept.waitFor({ state: "visible", timeoutMs: 15_000 });
  await uniqueLocator(accept, "accept category button");
  await accept.click();
  const characteristics = tab.playwright.getByText("Характеристики товара", { exact: true });
  await characteristics.waitFor({ state: "visible", timeoutMs: 30_000 });
  await tab.playwright.waitForTimeout(800);
}

async function screenshotHash(tab) {
  const bytes = await tab.screenshot({ fullPage: false });
  return {
    bytes,
    hash: crypto.createHash("sha256").update(bytes).digest("hex"),
  };
}

async function moveToEdge(tab, direction, maxSteps = 40) {
  await tab.cua.click({ x: 1840, y: 800 });
  let previous = "";
  for (let step = 1; step <= maxSteps; step += 1) {
    await tab.cua.keypress({ keys: [direction] });
    await tab.playwright.waitForTimeout(250);
    const { hash } = await screenshotHash(tab);
    if (hash === previous) return step;
    previous = hash;
  }
  throw new Error(`Could not reach scroll edge with ${direction}`);
}

async function captureScrollSequence(tab, category) {
  const directoryName = categoryDirectory(category);
  const directory = path.join(OUT_ROOT, directoryName);
  await fs.mkdir(directory, { recursive: true });
  const topSteps = await moveToEdge(tab, "PAGEUP");
  const frames = [];
  let previous = "";

  for (let sequence = 1; sequence <= 60; sequence += 1) {
    const { bytes, hash } = await screenshotHash(tab);
    if (hash === previous) break;
    const file = `${String(sequence).padStart(3, "0")}_${cleanName(category.title)}_scroll.png`;
    await fs.writeFile(path.join(directory, file), bytes);
    frames.push({ sequence, file, sha256: hash, bytes: bytes.length });
    previous = hash;
    await tab.cua.keypress({ keys: ["PAGEDOWN"] });
    await tab.playwright.waitForTimeout(300);
  }

  if (frames.length < 2) throw new Error(`Scroll capture produced only ${frames.length} frame(s)`);
  const interactionDirectory = categoryDirectory(category);
  const interactionHarName = `${interactionDirectory}_interaction.har`;
  const index = {
    capturedAt: new Date().toISOString(),
    rank: category.rank,
    categoryId: category.id,
    effectiveCategoryId: category.currentCategoryId || category.id,
    schemaHash: category.hash,
    title: category.title,
    path: category.path,
    viewport: { width: 1920, height: 912 },
    topResetSteps: topSteps,
    frameCount: frames.length,
    order: "top-to-bottom",
    frames,
    bindings: {
      baseCategoryHar: `har/categories/${String(category.rank).padStart(3, "0")}_${category.hash}_category-${category.id}.har`,
      interactionHar: `har/category-interactions/${interactionHarName}`,
      interactionEvidenceMap: `screenshots/category-interactions/${interactionDirectory}/EVIDENCE_MAP.json`,
    },
  };
  await fs.writeFile(path.join(directory, "SCROLL_INDEX.json"), `${JSON.stringify(index, null, 2)}\n`, "utf8");
  return index;
}

export async function captureCategoryScrollRanks(tab, ranks = []) {
  const categories = await loadCategories(ranks);
  await fs.mkdir(OUT_ROOT, { recursive: true });
  const results = [];
  for (const category of categories) {
    try {
      await selectCategory(tab, category);
      const index = await captureScrollSequence(tab, category);
      results.push({ rank: category.rank, ok: true, frameCount: index.frameCount, directory: categoryDirectory(category) });
    } catch (error) {
      results.push({ rank: category.rank, ok: false, error: String(error?.message || error), directory: categoryDirectory(category) });
    }
  }
  return results;
}
