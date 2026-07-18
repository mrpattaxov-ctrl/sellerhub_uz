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
const {
  ARTIFACTS,
  AUDIT_PATH,
  TARGET_URL,
  CDP_URL,
  selectCategory,
} = require("./uzum_capture_category_visuals.cjs");

const CATEGORY_HAR_DIR = path.join(ARTIFACTS, "har", "categories");
const OUT_ROOT = path.join(ARTIFACTS, "screenshots", "category-interactions");
const INTERACTION_HAR_DIR = path.join(ARTIFACTS, "har", "category-interactions");
const CHECKPOINT_PATH = path.join(OUT_ROOT, "CATEGORY_INTERACTION_CHECKPOINT.json");

function arg(name, fallback) {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 ? process.argv[at + 1] : fallback;
}

function cleanName(value) {
  return String(value || "unknown")
    .normalize("NFKD")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "unknown";
}

function rankPrefix(category) {
  return `${String(category.rank).padStart(3, "0")}_${category.hash}_category-${category.currentCategoryId || category.id}`;
}

async function loadCategories() {
  const audit = JSON.parse(await fs.readFile(AUDIT_PATH, "utf8"));
  return audit.groups.map((group) => ({
    rank: group.rank,
    hash: group.hash,
    id: group.representative.categoryId,
    title: group.representative.title,
    path: group.representative.path,
  })).map((category) => {
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
  });
}

async function loadDefinedCharacteristics(category) {
  const prefix = `${String(category.rank).padStart(3, "0")}_${category.hash}_category-`;
  const harName = (await fs.readdir(CATEGORY_HAR_DIR)).find((name) => name.startsWith(prefix) && name.endsWith(".har"));
  if (!harName) throw new Error(`Category HAR missing for rank ${category.rank}`);
  const document = JSON.parse(await fs.readFile(path.join(CATEGORY_HAR_DIR, harName), "utf8"));
  const entry = document.log.entries.find((item) =>
    item.request?.url?.includes("getDefinedCharacteristics") && item.response?.content?.text);
  if (!entry) throw new Error(`Defined characteristics response missing for rank ${category.rank}`);
  const defined = JSON.parse(entry.response.content.text);
  const requiredEntry = document.log.entries.find((item) =>
    item.request?.url?.includes("required-characteristics") && item.response?.content?.text);
  const required = requiredEntry ? JSON.parse(requiredEntry.response.content.text) : { characteristics: [] };
  const requiredIds = new Set((required.characteristics || []).map((item) => item.characteristicId));
  return {
    sourceHar: harName,
    defined: defined.map((item) => ({ ...item, required: requiredIds.has(item.characteristicId) })),
  };
}

function headersArray(headers = {}) {
  return Object.entries(headers).map(([name, value]) => ({ name, value: String(value) }));
}

async function createNetworkRecorder(context, page) {
  const session = await context.newCDPSession(page);
  const requests = new Map();
  const bodies = new Map();
  const pendingBodies = new Set();
  let active = false;
  let startedAt = Date.now();

  await session.send("Network.enable", { maxTotalBufferSize: 100000000, maxResourceBufferSize: 20000000 });

  session.on("Network.requestWillBeSent", (event) => {
    if (!active) return;
    requests.set(event.requestId, {
      requestId: event.requestId,
      startedDateTime: new Date().toISOString(),
      wallTime: event.wallTime,
      request: event.request,
      type: event.type,
      response: null,
      requestExtraHeaders: null,
      responseExtraHeaders: null,
      failed: null,
      finished: false,
    });
  });

  session.on("Network.requestWillBeSentExtraInfo", (event) => {
    const record = requests.get(event.requestId);
    if (record) record.requestExtraHeaders = event.headers;
  });

  session.on("Network.responseReceived", (event) => {
    const record = requests.get(event.requestId);
    if (record) record.response = event.response;
  });

  session.on("Network.responseReceivedExtraInfo", (event) => {
    const record = requests.get(event.requestId);
    if (record) record.responseExtraHeaders = event.headers;
  });

  session.on("Network.loadingFailed", (event) => {
    const record = requests.get(event.requestId);
    if (record) {
      record.failed = event;
      record.finished = true;
    }
  });

  session.on("Network.loadingFinished", (event) => {
    const record = requests.get(event.requestId);
    if (!record) return;
    record.finished = true;
    const mimeType = String(record.response?.mimeType || "").toLowerCase();
    const captureBody = record.request.url.includes("api-seller.uzum.uz")
      || ["XHR", "Fetch"].includes(record.type)
      || mimeType.includes("json")
      || mimeType.includes("javascript")
      || mimeType.includes("html");
    if (!captureBody) {
      bodies.set(event.requestId, { body: "", base64Encoded: false, omitted: "static-binary-body" });
      return;
    }
    const promise = session.send("Network.getResponseBody", { requestId: event.requestId })
      .then((body) => bodies.set(event.requestId, body))
      .catch(() => {})
      .finally(() => pendingBodies.delete(promise));
    pendingBodies.add(promise);
  });

  return {
    start() {
      requests.clear();
      bodies.clear();
      startedAt = Date.now();
      active = true;
    },
    async stop(title) {
      active = false;
      await Promise.allSettled([...pendingBodies]);
      const entries = [...requests.values()].map((record) => {
        const response = record.response || {};
        const body = bodies.get(record.requestId);
        const requestHeaders = { ...(record.request.headers || {}), ...(record.requestExtraHeaders || {}) };
        const responseHeaders = { ...(response.headers || {}), ...(record.responseExtraHeaders || {}) };
        const content = {
          size: Number(response.encodedDataLength || (body?.body?.length || 0)),
          mimeType: response.mimeType || "",
          text: body?.body || "",
        };
        if (body?.base64Encoded) content.encoding = "base64";
        if (body?.omitted) content._bodyOmitted = body.omitted;
        const request = {
          method: record.request.method,
          url: record.request.url,
          httpVersion: response.protocol || "HTTP/2",
          headers: headersArray(requestHeaders),
          queryString: [...new URL(record.request.url).searchParams].map(([name, value]) => ({ name, value })),
          cookies: [],
          headersSize: -1,
          bodySize: record.request.postData ? Buffer.byteLength(record.request.postData) : 0,
        };
        if (record.request.postData) {
          request.postData = {
            mimeType: requestHeaders["content-type"] || requestHeaders["Content-Type"] || "",
            text: record.request.postData,
          };
        }
        return {
          startedDateTime: record.startedDateTime,
          time: 0,
          request,
          response: {
            status: record.failed ? 0 : Number(response.status || 0),
            statusText: record.failed?.errorText || response.statusText || "",
            httpVersion: response.protocol || "HTTP/2",
            headers: headersArray(responseHeaders),
            cookies: [],
            content,
            redirectURL: responseHeaders.location || responseHeaders.Location || "",
            headersSize: -1,
            bodySize: content.size,
          },
          cache: {},
          timings: { send: 0, wait: 0, receive: 0 },
          _resourceType: record.type,
        };
      });
      return {
        log: {
          version: "1.2",
          creator: { name: "Uzum category interaction capture", version: "1.0" },
          pages: [{ startedDateTime: new Date(startedAt).toISOString(), id: "page_1", title, pageTimings: {} }],
          entries,
        },
      };
    },
    async close() {
      await session.detach().catch(() => {});
    },
  };
}

async function visibleExactText(page, texts, timeoutMs = 0) {
  const deadline = Date.now() + timeoutMs;
  do {
    for (const text of texts.filter(Boolean)) {
      const locator = page.getByText(text, { exact: true });
      const count = await locator.count();
      for (let index = count - 1; index >= 0; index -= 1) {
        if (await locator.nth(index).isVisible()) return locator.nth(index);
      }
    }
    if (Date.now() >= deadline) break;
    await page.waitForTimeout(150);
  } while (true);
  return null;
}

async function saveShot(page, directory, sequence, slug, options = {}) {
  const name = `${String(sequence.value++).padStart(3, "0")}_${slug}.png`;
  const file = path.join(directory, name);
  await page.screenshot({ path: file, fullPage: Boolean(options.fullPage), animations: "allow" });
  return name;
}

async function scrollToCharacteristics(page) {
  const heading = page.getByText("Характеристики товара", { exact: true });
  await heading.waitFor({ state: "visible", timeout: 30000 });
  await heading.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
}

async function closeOpenPopup(page) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const dialogs = page.locator("dialog[open]");
    if (!(await dialogs.count())) return;
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(120);
  }
}

async function saveAndCloseOpenPopup(page) {
  const dialogs = page.locator("dialog[open]");
  const count = await dialogs.count();
  if (!count) return false;
  const dialog = dialogs.nth(count - 1);
  const save = dialog.getByRole("button", { name: "Сохранить", exact: true });
  if (await save.count()) {
    await save.click();
    await page.waitForTimeout(70);
    return true;
  }
  await closeOpenPopup(page);
  return false;
}

async function findCharacteristicRow(page, characteristic) {
  const uz = characteristic.characteristicTitle?.uz;
  const ru = characteristic.characteristicTitle?.ru;
  const labels = [`${uz} / ${ru}`, `${ru} / ${uz}`, ru, uz].filter(Boolean);
  const label = await visibleExactText(page, labels);
  if (!label) return null;
  const candidates = [
    label.locator("xpath=ancestor::*[.//button[normalize-space()='Добавить'] and .//button[normalize-space()='Удалить']][1]"),
    label.locator("xpath=ancestor::*[.//button[normalize-space()='Удалить']][1]"),
    label.locator("xpath=ancestor::*[.//button[normalize-space()='Добавить']][1]"),
  ];
  for (const candidate of candidates) {
    if (await candidate.count()) return candidate;
  }
  return label.locator("xpath=parent::*");
}

async function addCharacteristic(page, characteristic, directory, sequence, record) {
  const ru = characteristic.characteristicTitle?.ru;
  const uz = characteristic.characteristicTitle?.uz;
  const slug = `${characteristic.characteristicId}_${cleanName(ru || uz)}`;
  let row = await findCharacteristicRow(page, characteristic);
  if (row) return { row, slug, preExisting: true };

  const control = page.getByText("Добавить характеристику", { exact: true });
  await control.waitFor({ state: "visible", timeout: 10000 });
  await saveShot(page, directory, sequence, `${slug}_before-characteristic-dropdown`);
  await control.click();
  await page.waitForTimeout(60);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_characteristic-dropdown-mid`));
  await page.waitForTimeout(300);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_characteristic-dropdown-open`));
  const option = await visibleExactText(page, [ru, uz], 7000);
  if (!option) throw new Error(`Characteristic option not visible: ${ru || uz}`);
  await option.click();
  await page.waitForTimeout(60);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_row-appearing-mid`));
  await page.waitForTimeout(300);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_row-added-popup-open`));
  const characteristicSaved = await saveAndCloseOpenPopup(page);
  record.notes.push(characteristicSaved ? "Characteristic popup committed with Save" : "Characteristic popup closed without Save control");
  record.files.push(await saveShot(page, directory, sequence, `${slug}_row-added-popup-closing-mid`));
  await page.waitForTimeout(400);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_row-added-popup-closed`));
  row = await findCharacteristicRow(page, characteristic);
  if (!row) throw new Error(`Characteristic row not found after add: ${ru || uz}`);
  return { row, slug, preExisting: false };
}

async function selectFirstValue(page, row, characteristic, directory, sequence, record, slug) {
  const values = characteristic.characteristicValues || [];
  const addButton = row.getByRole("button", { name: "Добавить", exact: true });
  if (!(await addButton.count())) {
    record.notes.push("No Add value button found");
    return null;
  }
  await saveShot(page, directory, sequence, `${slug}_before-value-dropdown`);
  await addButton.click();
  await page.waitForTimeout(60);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_value-dropdown-mid`));
  await page.waitForTimeout(300);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_value-dropdown-open`));
  const first = values.find((value) => value?.title?.ru || value?.title?.uz);
  if (!first) {
    record.notes.push("Characteristic has no predefined values; text/custom input requires separate handling");
    await page.keyboard.press("Escape").catch(() => {});
    return null;
  }
  let valueOption = await visibleExactText(page, [first.title?.ru, first.title?.uz, `${first.title?.uz} / ${first.title?.ru}`], 5000);
  let selectedTitle = first.title;
  if (!valueOption) {
    const dialog = page.locator("dialog[open]");
    const dialogCount = await dialog.count();
    if (dialogCount) {
      const labels = dialog.nth(dialogCount - 1).locator("label");
      const labelCount = await labels.count();
      for (let index = 0; index < labelCount; index += 1) {
        const label = labels.nth(index);
        if (!(await label.isVisible())) continue;
        const text = String(await label.innerText()).trim();
        if (!text || /выбрать все|select all/i.test(text)) continue;
        valueOption = label;
        selectedTitle = { ru: text, uz: text };
        record.notes.push(`HAR first value was outside the rendered list; selected first visible value: ${text}`);
        break;
      }
    }
  }
  if (!valueOption) {
    record.notes.push(`No selectable visible value found; HAR first value: ${first.title?.ru || first.title?.uz}`);
    await closeOpenPopup(page);
    return null;
  }
  await valueOption.click();
  await page.waitForTimeout(60);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_value-selected-mid`));
  await page.waitForTimeout(300);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_value-selected-popup-open`));
  const valueSaved = await saveAndCloseOpenPopup(page);
  record.notes.push(valueSaved ? "Value selection committed with Save" : "Value popup closed without Save control");
  record.files.push(await saveShot(page, directory, sequence, `${slug}_value-selected-popup-closing-mid`));
  await page.waitForTimeout(400);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_value-selected-popup-closed`));
  return { ...first, title: selectedTitle };
}

async function removeOptionalRow(page, row, directory, sequence, record, slug) {
  const deleteButton = row.getByRole("button", { name: "Удалить", exact: true });
  if (!(await deleteButton.count())) {
    record.notes.push("No Delete row button found");
    return false;
  }
  await deleteButton.click();
  await page.waitForTimeout(60);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_row-removing-mid`));
  await page.waitForTimeout(300);
  record.files.push(await saveShot(page, directory, sequence, `${slug}_row-removed`));
  return true;
}

async function captureCategory(page, context, category) {
  const prefix = rankPrefix(category);
  const directory = path.join(OUT_ROOT, prefix);
  await fs.mkdir(directory, { recursive: true });
  await fs.mkdir(INTERACTION_HAR_DIR, { recursive: true });
  const { sourceHar, defined } = await loadDefinedCharacteristics(category);
  const recorder = await createNetworkRecorder(context, page);
  const sequence = { value: 1 };
  const index = {
    rank: category.rank,
    categoryId: category.id,
    currentCategoryId: category.currentCategoryId || null,
    schemaHash: category.hash,
    title: category.title,
    path: category.path,
    sourceCategoryHar: sourceHar,
    startedAt: new Date().toISOString(),
    files: [],
    characteristics: [],
    errors: [],
  };

  recorder.start();
  try {
    const selection = await selectCategory(page, category);
    const sourceById = new Map(defined.map((item) => [item.characteristicId, item]));
    const liveDefined = Array.isArray(selection?.definedCharacteristics)
      ? selection.definedCharacteristics.map((item) => ({
        ...item,
        required: Boolean(sourceById.get(item.characteristicId)?.required),
      }))
      : defined;
    const sourceIds = defined.map((item) => item.characteristicId);
    const liveIds = liveDefined.map((item) => item.characteristicId);
    index.schemaComparison = {
      sourceDefinedCount: defined.length,
      liveDefinedCount: liveDefined.length,
      sourceIds,
      liveIds,
      missingFromLive: sourceIds.filter((id) => !liveIds.includes(id)),
      addedInLive: liveIds.filter((id) => !sourceIds.includes(id)),
      usedLiveResponse: Array.isArray(selection?.definedCharacteristics),
    };
    await scrollToCharacteristics(page);
    index.files.push(await saveShot(page, directory, sequence, "category-baseline-viewport"));
    index.files.push(await saveShot(page, directory, sequence, "category-baseline-fullpage", { fullPage: true }));

    for (const characteristic of liveDefined) {
      await scrollToCharacteristics(page);
      const record = {
        characteristicId: characteristic.characteristicId,
        title: characteristic.characteristicTitle,
        required: Boolean(characteristic.required),
        valueCount: (characteristic.characteristicValues || []).length,
        selectedValue: null,
        files: [],
        notes: [],
        error: null,
      };
      index.characteristics.push(record);
      try {
        const { row, slug, preExisting } = await addCharacteristic(page, characteristic, directory, sequence, record);
        record.preExisting = preExisting;
        const selected = await selectFirstValue(page, row, characteristic, directory, sequence, record, slug);
        if (selected) record.selectedValue = selected.title;
        // Remove every row that this audit added, including required variants.
        // Some categories mark many mutually exclusive size families as required;
        // retaining them would hit Uzum's maximum-5 row limit before all families
        // can be inspected. Rows that existed when the category loaded are kept.
        if (!preExisting) {
          await removeOptionalRow(page, row, directory, sequence, record, slug);
        }
      } catch (error) {
        record.error = String(error?.message || error);
        index.errors.push({ characteristicId: characteristic.characteristicId, error: record.error });
        await page.keyboard.press("Escape").catch(() => {});
      }
    }

    await scrollToCharacteristics(page);
    index.files.push(await saveShot(page, directory, sequence, "category-final-viewport"));
    index.files.push(await saveShot(page, directory, sequence, "category-final-fullpage", { fullPage: true }));
  } finally {
    const har = await recorder.stop(`Uzum category interaction: ${category.path.join(" > ")}`);
    const harName = `${prefix}_interaction.har`;
    await fs.writeFile(path.join(INTERACTION_HAR_DIR, harName), `${JSON.stringify(har, null, 2)}\n`, "utf8");
    index.interactionHar = path.posix.join("har", "category-interactions", harName);
    index.finishedAt = new Date().toISOString();
    await fs.writeFile(path.join(directory, "INTERACTION_INDEX.json"), `${JSON.stringify(index, null, 2)}\n`, "utf8");
    await recorder.close();
  }
  return index;
}

async function main() {
  const start = Number(arg("start", "1"));
  const end = Number(arg("end", String(start)));
  const rankList = String(arg("ranks", ""))
    .split(",")
    .map((value) => Number(value.trim()))
    .filter(Number.isFinite);
  const requestedRanks = new Set(rankList);
  const categories = (await loadCategories()).filter((category) => requestedRanks.size
    ? requestedRanks.has(category.rank)
    : category.rank >= start && category.rank <= end);
  await fs.mkdir(OUT_ROOT, { recursive: true });
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  let page = context.pages().find((item) => item.url().includes("seller.uzum.uz/seller/7138/products/new"));
  if (!page) page = await context.newPage();
  await page.bringToFront();
  await page.setViewportSize({ width: 1920, height: 1080 });

  const checkpoint = {
    startedAt: new Date().toISOString(),
    requested: { start, end, ranks: rankList },
    completed: [],
    failed: [],
  };
  for (const category of categories) {
    try {
      const index = await captureCategory(page, context, category);
      checkpoint.completed.push({ rank: category.rank, errors: index.errors.length, directory: rankPrefix(category) });
      process.stdout.write(`OK rank=${category.rank} characteristics=${index.characteristics.length} errors=${index.errors.length}\n`);
    } catch (error) {
      checkpoint.failed.push({ rank: category.rank, error: String(error?.stack || error) });
      process.stdout.write(`FAIL rank=${category.rank} ${error?.message || error}\n`);
    }
    checkpoint.updatedAt = new Date().toISOString();
    await fs.writeFile(CHECKPOINT_PATH, `${JSON.stringify(checkpoint, null, 2)}\n`, "utf8");
  }
  checkpoint.finishedAt = new Date().toISOString();
  await fs.writeFile(CHECKPOINT_PATH, `${JSON.stringify(checkpoint, null, 2)}\n`, "utf8");
  await browser.close();
  process.stdout.write(`DONE completed=${checkpoint.completed.length} failed=${checkpoint.failed.length}\n`);
  if (checkpoint.failed.length) process.exitCode = 2;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
