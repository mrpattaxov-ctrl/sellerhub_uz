const dot = document.getElementById("status-dot");
const box = document.getElementById("status-box");
const urlInput = document.getElementById("app-url");
const btnSave = document.getElementById("btn-save");
const btnSync = document.getElementById("btn-sync");

function formatTime(ts) {
  if (!ts) return "никогда";
  const d = new Date(ts);
  const now = Date.now();
  const diff = Math.round((now - ts) / 1000);
  if (diff < 60) return `${diff} сек. назад`;
  if (diff < 3600) return `${Math.round(diff / 60)} мин. назад`;
  return d.toLocaleTimeString("ru-RU");
}

function renderStatus(data) {
  const { appUrl, lastSyncTime, lastSyncStatus } = data;

  if (!appUrl) {
    dot.className = "dot warn";
    box.innerHTML = `<span class="warn">⚠ URL приложения не задан.</span><br>Введите адрес вашего приложения выше.`;
    return;
  }

  const timeStr = formatTime(lastSyncTime);

  if (!lastSyncStatus || lastSyncStatus === "ok") {
    dot.className = lastSyncStatus === "ok" ? "dot ok" : "dot";
    const statusText = lastSyncStatus === "ok"
      ? `<span class="ok">✓ Последняя синхронизация: ${timeStr}</span>`
      : `<span class="warn">Ожидание первого токена…</span><br>Откройте <a href="https://seller.uzum.uz" target="_blank" style="color:#38bdf8">seller.uzum.uz</a> — токен будет захвачен автоматически.`;
    box.innerHTML = `<b style="color:#e2e8f0">Приложение:</b> ${appUrl}<br>${statusText}`;
  } else {
    dot.className = "dot err";
    const errMap = {
      unreachable: "Приложение недоступно. Проверьте URL и что сервер запущен.",
      error_401: "Токен отклонён приложением (401).",
      error_403: "Доступ запрещён (403).",
      error_500: "Ошибка сервера (500). Проверьте логи.",
    };
    const msg = errMap[lastSyncStatus] || `Ошибка: ${lastSyncStatus}`;
    box.innerHTML = `<span class="err">✗ ${msg}</span><br><small>Последняя попытка: ${timeStr}</small>`;
  }
}

// Load saved URL and status on open
chrome.storage.local.get(["appUrl", "lastSyncTime", "lastSyncStatus"], (data) => {
  if (data.appUrl) urlInput.value = data.appUrl;
  renderStatus(data);
});

btnSave.addEventListener("click", () => {
  const url = urlInput.value.trim();
  if (!url) {
    urlInput.classList.add("error");
    return;
  }
  urlInput.classList.remove("error");
  chrome.storage.local.set({ appUrl: url }, () => {
    btnSave.textContent = "Сохранено ✓";
    setTimeout(() => (btnSave.textContent = "Сохранить"), 1500);
  });
});

btnSync.addEventListener("click", () => {
  btnSync.textContent = "…";
  btnSync.disabled = true;
  chrome.runtime.sendMessage({ type: "FORCE_SYNC" }, (resp) => {
    btnSync.disabled = false;
    if (resp && resp.ok) {
      btnSync.textContent = "Отправлено ✓";
    } else {
      btnSync.textContent = resp?.reason ? "Нет токена — откройте Uzum" : "Ошибка";
    }
    setTimeout(() => (btnSync.textContent = "Синхронизировать сейчас"), 2500);
    // Refresh status display
    chrome.runtime.sendMessage({ type: "GET_STATUS" }, renderStatus);
  });
});
