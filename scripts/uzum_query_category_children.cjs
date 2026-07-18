const fs = require("node:fs/promises");
const path = require("node:path");

async function main() {
  const root = path.resolve(__dirname, "..");
  const harPath = path.join(
    root,
    "artifacts",
    "uzum-product-form-reference",
    "uzum-sacvoyage-product-flow.har",
  );
  const document = JSON.parse(await fs.readFile(harPath, "utf8"));
  let authorization;
  for (const entry of document.log?.entries || []) {
    const header = (entry.request?.headers || []).find(
      (item) => item.name.toLowerCase() === "authorization",
    );
    if (header?.value?.startsWith("Bearer ")) {
      authorization = header.value;
      break;
    }
  }
  if (!authorization) throw new Error("Authorization header not found");
  const parentId = process.argv[2];
  const response = await fetch(
    `https://api-seller.uzum.uz/api/seller/shop/7138/product/childCategories?parentId=${parentId}`,
    {
      headers: {
        Authorization: authorization,
        Accept: "application/json, text/plain, */*",
        Origin: "https://seller.uzum.uz",
        Referer: "https://seller.uzum.uz/",
      },
    },
  );
  const body = await response.json();
  process.stdout.write(`${JSON.stringify({ status: response.status, categories: body }, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
