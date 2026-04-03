// ============================================================
//  Uzum Warehouse Token Sync — background service worker
//  Intercepts any request to api-seller.uzum.uz, grabs the
//  Authorization header, and silently POSTs it to your app.
// ============================================================

const UZUM_URLS = ["*://api-seller.uzum.uz/*", "*://seller.uzum.uz/*"];

// How often to re-sync (in minutes) even if no new network requests fired.
const PERIODIC_SYNC_ALARM = "uzum_token_periodic_sync";
const PERIODIC_INTERVAL_MIN = 20;

// Throttle: don't hammer the app with a POST on every single request.
let lastSentToken = "";
let lastSentAt = 0;
const THROTTLE_MS = 60_000; // 1 minute between identical sends

// ----------------------------------------------------------------
// Listen for outgoing requests to Uzum and grab the Bearer token
// ----------------------------------------------------------------
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details.requestHeaders) return;
    const authHeader = details.requestHeaders.find(
      (h) => h.name.toLowerCase() === "authorization"
    );
    if (!authHeader || !authHeader.value) return;
    const token = authHeader.value.trim();
    if (!token.startsWith("Bearer ")) return;

    // Throttle identical tokens
    const now = Date.now();
    if (token === lastSentToken && now - lastSentAt < THROTTLE_MS) return;

    lastSentToken = token;
    lastSentAt = now;

    sendTokenToApp(token);
  },
  { urls: UZUM_URLS },
  ["requestHeaders", "extraHeaders"]
);

// ----------------------------------------------------------------
// Periodic alarm: re-send last known token every 20 min so it
// stays fresh even if the seller hasn't visited Uzum recently.
// ----------------------------------------------------------------
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== PERIODIC_SYNC_ALARM) return;
  if (!lastSentToken) return;
  sendTokenToApp(lastSentToken, /* force= */ true);
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(PERIODIC_SYNC_ALARM, {
    periodInMinutes: PERIODIC_INTERVAL_MIN,
  });
  updateBadge("?", "#888");
});

// Restore alarm after browser restart (service workers can be killed).
chrome.alarms.get(PERIODIC_SYNC_ALARM, (existing) => {
  if (!existing) {
    chrome.alarms.create(PERIODIC_SYNC_ALARM, {
      periodInMinutes: PERIODIC_INTERVAL_MIN,
    });
  }
});

// ----------------------------------------------------------------
// Core: POST token to the Flask app's /api/auth/uzum-sso endpoint
// ----------------------------------------------------------------
async function sendTokenToApp(token, force = false) {
  let appUrl = await getAppUrl();
  if (!appUrl) {
    updateBadge("!", "#e55");
    return;
  }
  appUrl = appUrl.replace(/\/$/, "");

  try {
    const resp = await fetch(`${appUrl}/api/auth/uzum-sso`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      credentials: "include", // send session cookie so login_user() sticks
    });

    if (resp.ok) {
      updateBadge("✓", "#3a3");
      await chrome.storage.local.set({
        lastSyncTime: Date.now(),
        lastSyncStatus: "ok",
      });
    } else {
      const body = await resp.json().catch(() => ({}));
      console.warn("[UzumSync] App returned", resp.status, body);
      updateBadge(String(resp.status), "#e55");
      await chrome.storage.local.set({
        lastSyncTime: Date.now(),
        lastSyncStatus: `error_${resp.status}`,
      });
    }
  } catch (err) {
    console.error("[UzumSync] Failed to reach app:", err);
    updateBadge("✗", "#e55");
    await chrome.storage.local.set({
      lastSyncTime: Date.now(),
      lastSyncStatus: "unreachable",
    });
  }
}

// ----------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------
function updateBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}

async function getAppUrl() {
  return new Promise((resolve) => {
    chrome.storage.local.get("appUrl", (data) => {
      resolve((data.appUrl || "").trim() || null);
    });
  });
}

// Expose for popup
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "GET_STATUS") {
    chrome.storage.local.get(
      ["appUrl", "lastSyncTime", "lastSyncStatus"],
      (data) => sendResponse(data)
    );
    return true; // async
  }
  if (msg.type === "FORCE_SYNC") {
    if (lastSentToken) {
      sendTokenToApp(lastSentToken, true).then(() => sendResponse({ ok: true }));
    } else {
      sendResponse({ ok: false, reason: "No token captured yet. Open Uzum Seller dashboard first." });
    }
    return true;
  }
});
