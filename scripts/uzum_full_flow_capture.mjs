import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const ROOT = path.resolve(import.meta.dirname, "..");
const ARTIFACTS = path.join(ROOT, "artifacts", "uzum-product-form-reference");
const AUTH_HAR = path.join(ARTIFACTS, "uzum-sacvoyage-product-flow.har");
const FULL_FLOW_DIR = path.join(ARTIFACTS, "har", "full-flow");
const CATEGORY_HAR_DIR = path.join(ARTIFACTS, "har", "categories");
const CATEGORY_AUDIT = path.join(ARTIFACTS, "ALL_CATEGORY_SCHEMA_AUDIT.json");
const CHECKPOINT_PATH = path.join(FULL_FLOW_DIR, "FULL_FLOW_CHECKPOINT.json");
const API_ORIGIN = "https://api-seller.uzum.uz";
const SHOP_ID = 7138;

function arg(name, fallback = undefined) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

async function loadReferenceHar() {
  return JSON.parse(await fs.readFile(AUTH_HAR, "utf8"));
}

async function recoverAuthorization(document = null) {
  document ??= await loadReferenceHar();
  for (const entry of document.log?.entries ?? []) {
    if (!entry.request?.url?.includes(`/api/seller/shop/${SHOP_ID}/`)) continue;
    const header = (entry.request?.headers ?? []).find(
      (item) => item.name.toLowerCase() === "authorization",
    );
    if (header?.value?.startsWith("Bearer ")) return header.value;
  }
  throw new Error("Authorization header was not found in the reference HAR");
}

function headerArray(headers) {
  return [...headers.entries()].map(([name, value]) => ({ name, value }));
}

function queryArray(url) {
  return [...new URL(url).searchParams.entries()].map(([name, value]) => ({
    name,
    value,
  }));
}

function makeHar(entries, title) {
  return {
    log: {
      version: "1.2",
      creator: { name: "Codex Uzum full-flow recorder", version: "1.0" },
      pages: [
        {
          startedDateTime: entries[0]?.startedDateTime ?? new Date().toISOString(),
          id: "page_0",
          title,
          pageTimings: { onContentLoad: -1, onLoad: -1 },
        },
      ],
      entries,
    },
  };
}

async function createRecorder(authorization, seedEntries = []) {
  const entries = structuredClone(seedEntries);

  async function request(method, url, body = undefined) {
    const startedDateTime = new Date().toISOString();
    const started = performance.now();
    const requestHeaders = {
      Authorization: authorization,
      Accept: "application/json, text/plain, */*",
      Origin: "https://seller.uzum.uz",
      Referer: "https://seller.uzum.uz/",
    };
    let bodyText;
    if (body !== undefined) {
      requestHeaders["Content-Type"] = "application/json";
      bodyText = JSON.stringify(body);
    }

    let response;
    let responseText = "";
    let fetchError;
    try {
      response = await fetch(url, {
        method,
        headers: requestHeaders,
        body: bodyText,
        signal: AbortSignal.timeout(30_000),
      });
      responseText = await response.text();
    } catch (error) {
      fetchError = error;
    }
    const elapsed = performance.now() - started;

    const requestHeaderList = Object.entries(requestHeaders).map(([name, value]) => ({
      name,
      value,
    }));
    const contentType = response?.headers.get("content-type") ?? "application/json";
    entries.push({
      startedDateTime,
      time: elapsed,
      request: {
        method,
        url,
        httpVersion: "HTTP/2",
        headers: requestHeaderList,
        queryString: queryArray(url),
        cookies: [],
        headersSize: -1,
        bodySize: bodyText ? Buffer.byteLength(bodyText) : 0,
        ...(bodyText
          ? {
              postData: {
                mimeType: "application/json",
                text: bodyText,
              },
            }
          : {}),
      },
      response: {
        status: response?.status ?? 0,
        statusText: response?.statusText ?? String(fetchError ?? "Network error"),
        httpVersion: "HTTP/2",
        headers: response ? headerArray(response.headers) : [],
        cookies: [],
        content: {
          size: Buffer.byteLength(responseText),
          mimeType: contentType.split(";")[0],
          text: responseText,
        },
        redirectURL: response?.headers.get("location") ?? "",
        headersSize: -1,
        bodySize: Buffer.byteLength(responseText),
      },
      cache: {},
      timings: { blocked: 0, dns: -1, connect: -1, ssl: -1, send: 0, wait: elapsed, receive: 0 },
      pageref: "page_0",
      _serverIPAddress: null,
      _connection: null,
    });

    if (fetchError) throw fetchError;
    let json = null;
    try {
      json = JSON.parse(responseText);
    } catch {
      // Preserve non-JSON bodies in the HAR and expose null to callers.
    }
    return { status: response.status, text: responseText, json };
  }

  return { entries, request };
}

function responseJsonFromHar(document, urlNeedle) {
  const entry = document.log.entries.find(
    (item) => item.request.method === "GET" && item.request.url.includes(urlNeedle),
  );
  if (!entry) throw new Error(`Missing category response for ${urlNeedle}`);
  return JSON.parse(entry.response.content.text);
}

async function findSharedDefinedCharacteristic(characteristicId) {
  const files = (await fs.readdir(CATEGORY_HAR_DIR)).filter((name) => name.endsWith(".har"));
  for (const file of files) {
    try {
      const document = JSON.parse(await fs.readFile(path.join(CATEGORY_HAR_DIR, file), "utf8"));
      const defined = responseJsonFromHar(document, "getDefinedCharacteristics");
      const match = defined.find(
        (item) => item.characteristicId === characteristicId && item.characteristicValues?.length,
      );
      if (match) return structuredClone(match);
    } catch {
      // Continue until a category with values for this shared characteristic is found.
    }
  }
  return null;
}

function chooseCharacteristics(defined, requiredResponse, categoryPath = []) {
  const required = requiredResponse?.characteristics ?? [];
  const definedIdsWithValues = new Set(
    defined
      .filter((item) => item.characteristicValues?.length)
      .map((item) => item.characteristicId),
  );
  const selectedIds = new Set();
  for (const item of required) {
    if (
      item.requiredType === "REQUIRED" &&
      definedIdsWithValues.has(item.characteristicId)
    ) {
      selectedIds.add(item.characteristicId);
    }
  }
  const availableSizeOptions = required
    .filter(
      (item) =>
        item.requiredType === "REQUIRED_ONE_OF_SIZE" &&
        definedIdsWithValues.has(item.characteristicId),
    );
  const normalizedPath = categoryPath.join(" ").toLocaleLowerCase("ru");
  const preferredSizeIds = normalizedPath.includes("головные уборы")
    ? [-23]
    : normalizedPath.includes("новорожд")
      ? [-15, -11, -25]
      : normalizedPath.includes("для девочек") || normalizedPath.includes("для мальчиков")
        ? [-11, -15, -25]
        : [];
  const oneOfSize = preferredSizeIds
    .map((characteristicId) =>
      availableSizeOptions.find((item) => item.characteristicId === characteristicId))
    .find(Boolean)
    ?? availableSizeOptions.sort((a, b) => b.characteristicId - a.characteristicId)[0];
  if (oneOfSize) selectedIds.add(oneOfSize.characteristicId);
  return defined
    .filter((item) => selectedIds.has(item.characteristicId) && item.characteristicValues?.length)
    .sort((a, b) => a.orderingNumber - b.orderingNumber)
    .map((item) => ({ ...structuredClone(item), characteristicValues: [structuredClone(item.characteristicValues[0])] }));
}

function referenceImage(referenceHar) {
  for (const entry of referenceHar.log.entries) {
    if (entry.request.method !== "GET" || !entry.request.url.includes("/product?productId=")) continue;
    try {
      const body = JSON.parse(entry.response.content.text);
      if (body.productImages?.[0]?.key && body.productImages?.[0]?.url) {
        return {
          key: body.productImages[0].key,
          url: body.productImages[0].url,
          deletable: true,
          status: "ACTIVE",
        };
      }
    } catch {
      // Continue until a usable product body is found.
    }
  }
  throw new Error("Reference product image was not found");
}

function productPayload({ rank, categoryId, title, selected, filters, certification, image }) {
  const code = String(rank).padStart(3, "0");
  const productCertificates = certification?.fillType === "REQUIRED"
    ? [{
        expirationDate: "2027-12-31",
        number: `CODEX-HAR-${code}`,
        certificateImages: [{ key: image.key, url: image.url, ordering: 0 }],
      }]
    : [];
  return {
    attributes: { ru: [], uz: [] },
    blockReason: "",
    blockReasons: [],
    blockedImages: {},
    createdByFlowB: false,
    categoryId,
    categoryEditable: true,
    colorCollectionImages: [],
    colorImages: [],
    colorVideos: [],
    comments: [],
    customCharacteristics: [],
    dateModerated: null,
    definedCharacteristics: selected,
    description: {
      ru: `Временный тестовый товар для HAR-аудита категории ${title}. Не отправлять на модерацию.`,
      uz: `${title} kategoriyasi HAR auditi uchun vaqtinchalik sinov mahsuloti. Moderatsiyaga yuborilmasin.`,
    },
    filterValues: filters
      .filter((item) => item.emptyValue?.id != null)
      .map((item) => ({ filterId: item.id, filterValueId: item.emptyValue.id })),
    skuList: [],
    filters: [],
    imageCollection: null,
    okpd2: null,
    photoOnPreview: false,
    productFields: {},
    productImages: [image],
    ratingInfo: null,
    shortDescription: { ru: "", uz: "" },
    skuBlockReason: null,
    status: null,
    title: {
      ru: `CODEX HAR TEST ${code} — ${title}`,
      uz: `CODEX HAR TEST ${code} — ${title}`,
    },
    video: null,
    productCertificates,
    switchbackActive: false,
  };
}

function skuPayload({ rank, product, selected, dimensionalGroup, ikpu }) {
  const code = `HAR${String(rank).padStart(3, "0")}`;
  const suffixes = selected.flatMap((item) =>
    item.characteristicValues.map((value) => value.skuValue || String(value.value).replace(/\W+/g, "")),
  );
  const skuTitle = [product.shopSkuTitle, code, ...suffixes].filter(Boolean).join("-");
  const dimensions = dimensionalGroup === "LARGE"
    ? { width: 500, height: 500, length: 1000, weight: 10000 }
    : dimensionalGroup === "MEDIUM"
      ? { width: 300, height: 200, length: 500, weight: 3000 }
      : { width: 100, height: 30, length: 250, weight: 200 };
  const skuCharacteristicList = selected.flatMap((item) =>
    item.characteristicValues.map((value) => ({
      characteristicTitle: {
        ru: item.characteristicTitle.ru,
        uz: item.characteristicTitle.uz,
      },
      definedType: true,
      characteristicValue: { ru: value.title.ru, uz: value.title.uz },
    })),
  );
  return {
    productId: product.id,
    skuForProduct: code,
    skuList: [{
      fullPrice: 100000,
      sellPrice: 100000,
      ikpu,
      skuTitle,
      barcode: null,
      dimensions,
      skuCharacteristicList,
      status: null,
    }],
    skuTitlesForCustomCharacteristics: [],
  };
}

function emptyPropertiesPayload(properties) {
  const skuIds = (properties.sku ?? []).map((item) => item.skuId);
  const skuFilters = skuIds.flatMap((skuId) =>
    (properties.filters ?? []).map((filter) => ({
      filterId: filter.id,
      skuId,
      values: filter.type === "MULTIPLE_CHOICE" ? [] : [null],
      customValues: [],
    })),
  );
  const skuAttributeValues = (properties.skuAttributes ?? []).map((row) => ({
    skuId: row.skuId,
    attributes: (row.attributes ?? []).map((attribute) => ({
      attributeCode: attribute.attributeCode,
      attributeName: attribute.attributeName?.ru,
    })),
  }));
  return { skuFilters, skuAttributeValues };
}

async function updateCheckpoint(result) {
  let checkpoint = { updatedAt: null, completed: [], failed: [] };
  try {
    checkpoint = JSON.parse(await fs.readFile(CHECKPOINT_PATH, "utf8"));
  } catch {
    // First run.
  }
  checkpoint.completed = checkpoint.completed.filter((item) => item.rank !== result.rank);
  checkpoint.failed = checkpoint.failed.filter((item) => item.rank !== result.rank);
  (result.valid ? checkpoint.completed : checkpoint.failed).push(result);
  checkpoint.completed.sort((a, b) => a.rank - b.rank);
  checkpoint.failed.sort((a, b) => a.rank - b.rank);
  checkpoint.updatedAt = new Date().toISOString();
  await fs.writeFile(CHECKPOINT_PATH, JSON.stringify(checkpoint, null, 2));
}

async function captureRank(rank, context) {
  const group = context.audit.groups.find((item) => item.rank === rank);
  if (!group) throw new Error(`Unknown audit rank ${rank}`);
  const representative = group.representative;
  const prefix = `${String(rank).padStart(3, "0")}_${group.hash}_category-${representative.categoryId}`;
  const categoryFile = (await fs.readdir(CATEGORY_HAR_DIR)).find((name) => name.startsWith(prefix));
  if (!categoryFile) throw new Error(`Category HAR not found for rank ${rank}`);
  const categoryHar = JSON.parse(await fs.readFile(path.join(CATEGORY_HAR_DIR, categoryFile), "utf8"));
  const recorder = await createRecorder(context.authorization, categoryHar.log.entries);
  const base = `${API_ORIGIN}/api/seller/shop/${SHOP_ID}`;
  let defined = responseJsonFromHar(categoryHar, "getDefinedCharacteristics");
  const required = responseJsonFromHar(categoryHar, "required-characteristics");
  const normalizedCategoryPath = representative.path.join(" ").toLocaleLowerCase("ru");
  if (
    normalizedCategoryPath.includes("головные уборы") &&
    !defined.some((item) => item.characteristicId === -23 && item.characteristicValues?.length)
  ) {
    const sharedHeadwearSize = await findSharedDefinedCharacteristic(-23);
    if (sharedHeadwearSize) defined = [...defined, sharedHeadwearSize];
  }
  const filters = responseJsonFromHar(categoryHar, "filters/product/active");
  const certification = responseJsonFromHar(categoryHar, "product-certification-filltype");
  const categoryFields = responseJsonFromHar(
    categoryHar,
    `/category/${representative.categoryId}/fields`,
  );
  const allowedClassificationCode =
    categoryFields?.fields?.ALLOWED_PRODUCT_CLASSIFICATION_CODE?.[0];
  let ikpu = "08517002028000000";
  if (allowedClassificationCode) {
    const ikpuSearchTerm = allowedClassificationCode === "08450001"
      ? "kir yuvish mashinasi"
      : allowedClassificationCode === "08508001"
        ? "changyutgich"
      : representative.title;
    const ikpuSearch = await recorder.request(
      "GET",
      `${API_ORIGIN}/api/seller/product/ikpu/search?search=${encodeURIComponent(ikpuSearchTerm)}&size=20&page=0&categoryId=${representative.categoryId}`,
    );
    const validIkpu = ikpuSearch.json?.payload?.find(
      (item) => item.isValidForCategory && item.ikpu,
    );
    if (!validIkpu) {
      throw new Error(
        `No valid IKPU found for category ${representative.categoryId} and classification ${allowedClassificationCode}`,
      );
    }
    ikpu = validIkpu.ikpu;
  }
  let selected = chooseCharacteristics(defined, required, representative.path);
  let payload = productPayload({
    rank,
    categoryId: representative.categoryId,
    title: representative.title,
    selected,
    filters,
    certification,
    image: context.image,
  });
  let productId = null;
  let createStatus = null;
  let skuStatus = null;
  let propertiesStatus = null;
  let propertiesSaveStatus = null;
  let deleteStatus = null;
  let verificationStatus = null;
  let attributeCount = null;
  let requiredAttributeCount = null;
  let error = null;

  try {
    let created = await recorder.request(
      "POST",
      `${base}/product/createProduct?testVariant=B`,
      payload,
    );
    const definedCharacteristicsForbidden =
      created.status === 400 &&
      created.json?.errors?.some(
        (item) => item.code === "category-defined-characteristics-forbidden",
      );
    if (definedCharacteristicsForbidden) {
      payload = productPayload({
        rank,
        categoryId: representative.categoryId,
        title: representative.title,
        selected,
        filters,
        certification,
        image: context.image,
      });
      payload.createdByFlowB = true;
      created = await recorder.request(
        "POST",
        `${base}/product/createProduct?testVariant=B`,
        payload,
      );
    }
    createStatus = created.status;
    if (createStatus !== 201 || !created.json?.id) {
      throw new Error(`createProduct failed: HTTP ${createStatus} ${created.text.slice(0, 500)}`);
    }
    productId = created.json.id;
    await recorder.request("GET", `${base}/product?productId=${productId}`);
    await recorder.request("POST", `${base}/product/editable?productId=${productId}`);
    const sku = await recorder.request(
      "POST",
      `${base}/product/sendSkuData`,
      skuPayload({
        rank,
        product: created.json,
        selected,
        dimensionalGroup: representative.norm.business.DIMENSIONAL_GROUP,
        ikpu,
      }),
    );
    skuStatus = sku.status;
    if (skuStatus !== 201) throw new Error(`sendSkuData failed: HTTP ${skuStatus} ${sku.text.slice(0, 500)}`);

    let propertiesResult = await recorder.request("GET", `${base}/filters/product/${productId}`);
    for (let attempt = 1; propertiesResult.status >= 500 && attempt < 3; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      propertiesResult = await recorder.request("GET", `${base}/filters/product/${productId}`);
    }
    propertiesStatus = propertiesResult.status;
    if (propertiesStatus !== 200 || !propertiesResult.json) {
      throw new Error(`properties GET failed: HTTP ${propertiesStatus}`);
    }
    const attributes = (propertiesResult.json.skuAttributes ?? []).flatMap((row) => row.attributes ?? []);
    attributeCount = attributes.length;
    requiredAttributeCount = attributes.filter((item) => item.required).length;
    const saved = await recorder.request(
      "POST",
      `${base}/filters/product/${productId}`,
      emptyPropertiesPayload(propertiesResult.json),
    );
    propertiesSaveStatus = saved.status;
  } catch (captureError) {
    error = String(captureError?.stack ?? captureError);
  } finally {
    if (productId != null) {
      await recorder.request("POST", `${base}/product/deletable?productId=${productId}`);
      const deleted = await recorder.request("POST", `${base}/product/deleteProduct`, { id: productId });
      deleteStatus = deleted.status;
      const verification = await recorder.request("GET", `${base}/product?productId=${productId}`);
      verificationStatus = verification.status;
    }
  }

  const valid = createStatus === 201
    && skuStatus === 201
    && propertiesStatus === 200
    && deleteStatus === 201
    && verificationStatus === 404;
  const harName = `${prefix}_full-flow.har`;
  const harPath = path.join(FULL_FLOW_DIR, harName);
  await fs.writeFile(
    harPath,
    JSON.stringify(makeHar(recorder.entries, `Uzum full product flow: ${representative.path.join(" > ")}`), null, 2),
  );
  const result = {
    rank,
    hash: group.hash,
    categoryId: representative.categoryId,
    path: representative.path,
    productId,
    createStatus,
    skuStatus,
    propertiesStatus,
    propertiesSaveStatus,
    attributeCount,
    requiredAttributeCount,
    deleteStatus,
    verificationStatus,
    valid,
    error,
    har: path.relative(ARTIFACTS, harPath).replaceAll("\\", "/"),
  };
  await updateCheckpoint(result);
  return result;
}

async function captureBatch(startRank, count) {
  await fs.mkdir(FULL_FLOW_DIR, { recursive: true });
  const referenceHar = await loadReferenceHar();
  const context = {
    referenceHar,
    authorization: await recoverAuthorization(referenceHar),
    audit: JSON.parse(await fs.readFile(CATEGORY_AUDIT, "utf8")),
    image: referenceImage(referenceHar),
  };
  const results = [];
  for (let rank = startRank; rank < Math.min(startRank + count, 236); rank += 1) {
    try {
      results.push(await captureRank(rank, context));
    } catch (error) {
      const result = { rank, valid: false, fatal: String(error?.stack ?? error) };
      await updateCheckpoint(result);
      results.push(result);
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  console.log(JSON.stringify({ startRank, count, results }, null, 2));
}

async function cleanupPilot(productId) {
  const authorization = await recoverAuthorization();
  const recorder = await createRecorder(authorization);
  const base = `${API_ORIGIN}/api/seller/shop/${SHOP_ID}`;
  const attempts = [];

  const deletable = await recorder.request(
    "POST",
    `${base}/product/deletable?productId=${productId}`,
  );

  const candidateBodies = [
    { productId },
    { id: productId },
    [productId],
    productId,
  ];
  let deleted = false;
  for (const body of candidateBodies) {
    const result = await recorder.request("POST", `${base}/product/deleteProduct`, body);
    attempts.push({ bodyShape: Array.isArray(body) ? "array" : typeof body, status: result.status });
    if (result.status >= 200 && result.status < 300) {
      deleted = true;
      break;
    }
  }

  const verification = await recorder.request(
    "GET",
    `${base}/product?productId=${productId}`,
  );
  await fs.mkdir(FULL_FLOW_DIR, { recursive: true });
  const harPath = path.join(FULL_FLOW_DIR, `pilot-${productId}-cleanup.har`);
  const summaryPath = path.join(FULL_FLOW_DIR, `pilot-${productId}-cleanup.json`);
  await fs.writeFile(
    harPath,
    JSON.stringify(makeHar(recorder.entries, `Uzum temporary pilot ${productId} cleanup`), null, 2),
  );
  const summary = {
    productId,
    deletableStatus: deletable.status,
    deletableResponse: deletable.json,
    attempts,
    deleted,
    verificationStatus: verification.status,
    harPath,
  };
  await fs.writeFile(summaryPath, JSON.stringify(summary, null, 2));
  console.log(JSON.stringify(summary, null, 2));
}

const mode = process.argv[2];
if (mode === "cleanup") {
  const productId = Number(arg("product-id"));
  if (!Number.isInteger(productId)) throw new Error("cleanup requires --product-id");
  await cleanupPilot(productId);
} else if (mode === "capture") {
  const startRank = Number(arg("start-rank"));
  const count = Number(arg("count", "1"));
  if (!Number.isInteger(startRank) || !Number.isInteger(count) || count < 1) {
    throw new Error("capture requires --start-rank and a positive --count");
  }
  await captureBatch(startRank, count);
} else {
  throw new Error("Usage: cleanup --product-id ID | capture --start-rank N --count N");
}
