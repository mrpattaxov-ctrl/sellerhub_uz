const { chromium } = require("playwright");

const CDP_URL = process.env.UZUM_CDP_URL || "http://127.0.0.1:9222";
const TARGET_URL = "https://seller.uzum.uz/seller/7138/products/new";

async function visibleExactText(page, text) {
  const locator = page.getByText(text, { exact: true });
  const count = await locator.count();
  for (let index = count - 1; index >= 0; index -= 1) {
    if (await locator.nth(index).isVisible()) return locator.nth(index);
  }
  throw new Error(`Visible option not found: ${text}`);
}

async function main() {
  const gender = process.argv[2] === "male" ? "Мужские аксессуары" : "Женские аксессуары";
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  let page = context.pages().find((item) => item.url().includes("seller.uzum.uz"));
  if (!page) page = await context.newPage();
  const captured = [];
  page.on("response", async (response) => {
    if (!response.url().includes("childCategories")) return;
    try {
      captured.push({ url: response.url(), body: await response.json() });
    } catch {
      captured.push({ url: response.url(), body: null });
    }
  });
  await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 45_000 });
  const root = page.locator('input[placeholder="Название категории или товара"]');
  await root.waitFor({ state: "visible", timeout: 30_000 });
  await root.click();
  await root.fill("Аксессуары");
  await (await visibleExactText(page, "Аксессуары")).click();
  const subs = page.locator('input[placeholder="Выбрать подкатегорию"]');
  await subs.nth(0).click();
  await (await visibleExactText(page, gender)).click();
  await subs.nth(1).click();
  await (await visibleExactText(page, "Часы и ремешки")).click();
  await page.waitForTimeout(1500);
  process.stdout.write(`${JSON.stringify(captured, null, 2)}\n`);
  await browser.close();
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
