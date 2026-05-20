(function () {
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function iconSearch() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>';
  }

  function iconBox() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z"></path><path d="m4 7.5 8 4.5 8-4.5"></path><path d="M12 12v9"></path></svg>';
  }

  function iconCart() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="20" r="1.5"></circle><circle cx="18" cy="20" r="1.5"></circle><path d="M3 4h2l2.4 10.2a1 1 0 0 0 1 .8h9.9a1 1 0 0 0 1-.8L21 7H7"></path></svg>';
  }

  function emptyStateMarkup(icon, title, text) {
    return [
      '<div class="pos-empty-state">',
      icon,
      "<h6>",
      esc(title),
      "</h6>",
      "<p>",
      esc(text),
      "</p>",
      "</div>",
    ].join("");
  }

  function skeletonMarkup(count) {
    var html = [];
    for (var i = 0; i < count; i += 1) {
      html.push('<div class="pos-skeleton-card"><div class="skeleton"></div></div>');
    }
    return html.join("");
  }

  function normalizeShortcut(shortcut) {
    return String(shortcut || "").toLowerCase().trim();
  }

  class PosEngine {
    constructor(config) {
      this.config = Object.assign(
        {
          searchInputId: "posSearch",
          resultsContainerId: "posResults",
          cartContainerId: "cartContainer",
          cartCountId: "cartCount",
          totalQtyId: "totalQty",
          actionBtnId: "btnCheckout",
          invoiceModalId: "invoiceModal",
          invoiceShopSelectId: "invoiceShopSelect",
          invoiceIdInputId: "invoiceIdInput",
          fetchInvoiceBtnId: "btnFetchInvoice",
          searchEndpoint: "/api/pos/search",
          invoiceEndpoint: "/api/pos/fetch-invoice",
          historyContainerId: "posHistory",
          historyEndpoint: "/api/pos/history",
          undoEndpoint: "/api/pos/undo/",
          searchDelay: 260,
          showStock: true,
          showModeToggle: false,
          defaultMode: "sale",
          emptySearchTitle: "Начните поиск",
          emptySearchText: "Введите название, SKU или штрихкод товара.",
          emptyNoResultsTitle: "Ничего не найдено",
          emptyNoResultsText: "Попробуйте другой запрос или загрузите накладную.",
          emptyCartTitle: "Корзина пуста",
          emptyCartText: "Добавьте товары из результатов поиска или загрузите накладную.",
          actionPendingText: "Обработка...",
          actionShortcut: "",
          clearCartOnAction: true,
          clearSearchOnAction: true,
          onAction: null,
          onActionSuccess: null,
          updateActionBtn: null,
          historyEmptyText: "Пока нет действий",
          historyUndoBtnText: "Отменить",
          historyRevertedBadgeText: "Отменено",
          historyConfirmUndoText: "Отменить это действие?",
          historyUndoSuccessText: "Действие отменено",
          historyUndoFailedText: "Не удалось отменить",
          historyUnitsText: "шт.",
          historySkuLabelText: "SKU",
          historyLabels: { sale: "Продажа", stock_in: "Приёмка" },
        },
        config || {}
      );

      this.state = {
        cart: {},
        mode: this.config.defaultMode,
        history: [],
      };

      this.results = [];
      this.searchTimer = null;
      this.toastHost = null;
      this.imagePreview = null;
      this.previewAnchor = null;

      this.refs = {
        searchInput: document.getElementById(this.config.searchInputId),
        results: document.getElementById(this.config.resultsContainerId),
        cart: document.getElementById(this.config.cartContainerId),
        cartCount: document.getElementById(this.config.cartCountId),
        totalQty: document.getElementById(this.config.totalQtyId),
        actionBtn: document.getElementById(this.config.actionBtnId),
        invoiceModal: document.getElementById(this.config.invoiceModalId),
        invoiceShopSelect: document.getElementById(this.config.invoiceShopSelectId),
        invoiceIdInput: document.getElementById(this.config.invoiceIdInputId),
        fetchInvoiceBtn: document.getElementById(this.config.fetchInvoiceBtnId),
        history: document.getElementById(this.config.historyContainerId),
      };

      this.bindEvents();
      this.renderResultsState("idle");
      this.renderCart();
      this.updateActionButton();
      this.renderHistory();
      this.loadHistory();
    }

    bindEvents() {
      var self = this;

      if (this.refs.searchInput) {
        this.refs.searchInput.addEventListener("input", function () {
          self.scheduleSearch();
        });
        this.refs.searchInput.addEventListener("keydown", function (event) {
          if (event.key === "Enter") {
            event.preventDefault();
            self.runSearch();
          }
        });
      }

      if (this.refs.results) {
        this.refs.results.addEventListener("click", function (event) {
          var card = event.target.closest("[data-result-index]");
          if (!card) return;
          var index = parseInt(card.getAttribute("data-result-index"), 10);
          if (!Number.isFinite(index) || !self.results[index]) return;
          self.addToCart(self.results[index]);
        });
      }

      if (this.refs.cart) {
        this.refs.cart.addEventListener("click", function (event) {
          var actionEl = event.target.closest("[data-cart-action]");
          if (!actionEl) return;

          var id = parseInt(actionEl.getAttribute("data-item-id"), 10);
          if (!Number.isFinite(id)) return;

          var action = actionEl.getAttribute("data-cart-action");
          if (action === "remove") self.removeItem(id);
          if (action === "minus") self.updateQty(id, -1);
          if (action === "plus") self.updateQty(id, 1);
        });

        this.refs.cart.addEventListener("change", function (event) {
          var input = event.target.closest("[data-cart-qty-input]");
          if (!input) return;

          var id = parseInt(input.getAttribute("data-item-id"), 10);
          if (!Number.isFinite(id)) return;
          self.setQty(id, input.value);
        });

        this.refs.cart.addEventListener("mouseover", function (event) {
          var media = event.target.closest("[data-preview-src]");
          if (!media || media.contains(event.relatedTarget)) return;
          self.showImagePreview(media);
        });

        this.refs.cart.addEventListener("mouseout", function (event) {
          var media = event.target.closest("[data-preview-src]");
          if (!media || media.contains(event.relatedTarget)) return;
          self.hideImagePreview();
        });

        this.refs.cart.addEventListener("focusin", function (event) {
          var media = event.target.closest("[data-preview-src]");
          if (!media) return;
          self.showImagePreview(media);
        });

        this.refs.cart.addEventListener("focusout", function (event) {
          var media = event.target.closest("[data-preview-src]");
          if (!media || media.contains(event.relatedTarget)) return;
          self.hideImagePreview();
        });
      }

      if (this.refs.actionBtn) {
        this.refs.actionBtn.addEventListener("click", function () {
          self.handleAction();
        });
      }

      if (this.refs.history) {
        this.refs.history.addEventListener("click", function (event) {
          var btn = event.target.closest('[data-history-action="undo"]');
          if (!btn) return;
          var id = parseInt(btn.getAttribute("data-history-id"), 10);
          if (!Number.isFinite(id)) return;
          self.undoAction(id);
        });
      }

      if (this.config.showModeToggle) {
        Array.prototype.forEach.call(document.querySelectorAll('input[name="posMode"]'), function (input) {
          input.addEventListener("change", function (event) {
            self.state.mode = event.target.value;
            self.updateActionButton();
          });
        });
      }

      if (this.refs.invoiceModal) {
        this.refs.invoiceModal.addEventListener("show.bs.modal", function () {
          self.loadShops();
        });
      }

      if (this.refs.fetchInvoiceBtn) {
        this.refs.fetchInvoiceBtn.addEventListener("click", function () {
          self.fetchInvoice();
        });
      }

      if (this.refs.invoiceIdInput) {
        this.refs.invoiceIdInput.addEventListener("keydown", function (event) {
          if (event.key === "Enter") {
            event.preventDefault();
            self.fetchInvoice();
          }
        });
      }

      document.addEventListener("keydown", function (event) {
        self.handleKeyboardShortcut(event);
      });

      window.addEventListener("scroll", function () {
        self.positionImagePreview();
      }, true);

      window.addEventListener("resize", function () {
        self.positionImagePreview();
      });

    }

    isTextContext(target) {
      if (!target) return false;
      var tag = (target.tagName || "").toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
    }

    handleKeyboardShortcut(event) {
      var activeShortcut = normalizeShortcut(this.config.actionShortcut);
      var textContext = this.isTextContext(event.target);

      if (!textContext && (event.key === "F2" || event.key === "/")) {
        event.preventDefault();
        this.focusSearch();
        return;
      }

      if (event.key === "Escape") {
        if (this.refs.searchInput && this.refs.searchInput.value) {
          this.refs.searchInput.value = "";
          this.runSearch();
        }
        return;
      }

      if (activeShortcut && this.matchesShortcut(event, activeShortcut)) {
        event.preventDefault();
        if (this.refs.actionBtn && !this.refs.actionBtn.disabled) {
          this.handleAction();
        }
      }
    }

    matchesShortcut(event, shortcut) {
      var normalized = normalizeShortcut(shortcut);
      if (normalized === "ctrl+enter") {
        return event.ctrlKey && event.key === "Enter";
      }
      if (normalized === "ctrl+p") {
        return event.ctrlKey && event.key.toLowerCase() === "p";
      }
      return false;
    }

    focusSearch() {
      if (!this.refs.searchInput) return;
      this.refs.searchInput.focus();
      this.refs.searchInput.select();
    }

    scheduleSearch() {
      var self = this;
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(function () {
        self.runSearch();
      }, this.config.searchDelay);
    }

    async runSearch() {
      var query = this.refs.searchInput ? this.refs.searchInput.value.trim() : "";
      if (!query) {
        this.results = [];
        this.renderResultsState("idle");
        return;
      }

      this.renderResultsState("loading");
      try {
        var response = await fetch(this.config.searchEndpoint + "?q=" + encodeURIComponent(query));
        var data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "Не удалось выполнить поиск.");
        }
        this.results = Array.isArray(data.items) ? data.items : [];
        this.renderResults(this.results);
      } catch (error) {
        this.results = [];
        this.renderResultsState("error");
        this.showToast(error.message || "Ошибка поиска.", "danger");
      }
    }

    renderResultsState(kind) {
      if (!this.refs.results) return;

      if (kind === "loading") {
        this.refs.results.innerHTML = skeletonMarkup(8);
        return;
      }

      if (kind === "error") {
        this.refs.results.innerHTML = emptyStateMarkup(
          iconSearch(),
          "Поиск временно недоступен",
          "Проверьте соединение и попробуйте снова."
        );
        return;
      }

      this.refs.results.innerHTML = emptyStateMarkup(
        kind === "empty" ? iconBox() : iconSearch(),
        kind === "empty" ? this.config.emptyNoResultsTitle : this.config.emptySearchTitle,
        kind === "empty" ? this.config.emptyNoResultsText : this.config.emptySearchText
      );
    }

    renderResults(items) {
      if (!this.refs.results) return;
      if (!items.length) {
        this.renderResultsState("empty");
        return;
      }

      this.refs.results.innerHTML = items
        .map(
          function (item, index) {
            var media = item.image_url
              ? '<img src="' + esc(item.image_url) + '" alt="' + esc(item.name) + '">'
              : '<div class="pos-product-placeholder">Нет фото</div>';
            var stock = "";
            if (this.config.showStock) {
              var stockClass = Number(item.stock) > 0 ? "pos-stock pos-stock--good" : "pos-stock pos-stock--bad";
              stock = '<span class="' + stockClass + '">Ост: ' + esc(item.stock) + "</span>";
            }

            return [
              '<article class="card product-card pos-product-card" tabindex="0" data-result-index="',
              index,
              '">',
              '<div class="pos-product-media">',
              media,
              "</div>",
              '<div class="pos-product-body">',
              '<div class="pos-product-title">',
              esc(item.name),
              "</div>",
              '<div class="pos-product-meta">',
              '<span class="badge badge-soft mono pos-product-sku">',
              esc(item.sku),
              "</span>",
              stock,
              "</div>",
              "</div>",
              "</article>",
            ].join("");
          }.bind(this)
        )
        .join("");
    }

    addToCart(item, options) {
      var opts = options || {};
      var qty = parseInt(item.qty || 1, 10);
      qty = Number.isFinite(qty) && qty > 0 ? qty : 1;

      if (this.state.cart[item.id]) {
        this.state.cart[item.id].qty += qty;
      } else {
        this.state.cart[item.id] = {
          item: item,
          qty: qty,
        };
      }

      this.renderCart();
      if (!opts.silent) {
        this.showToast("Товар добавлен.", "success");
      }
    }

    updateQty(id, delta) {
      if (!this.state.cart[id]) return;
      this.state.cart[id].qty += delta;
      if (this.state.cart[id].qty <= 0) {
        delete this.state.cart[id];
      }
      this.renderCart();
    }

    setQty(id, value) {
      if (!this.state.cart[id]) return;
      var qty = parseInt(value, 10);
      if (!Number.isFinite(qty) || qty <= 0) {
        delete this.state.cart[id];
      } else {
        this.state.cart[id].qty = qty;
      }
      this.renderCart();
    }

    removeItem(id) {
      if (!this.state.cart[id]) return;
      delete this.state.cart[id];
      this.renderCart();
    }

    cartMediaMarkup(item) {
      if (!item.image_url) {
        return '<div class="pos-cart-media"><div class="pos-cart-thumb--empty">Нет фото</div></div>';
      }

      return [
        '<div class="pos-cart-media" tabindex="0" data-preview-src="',
        esc(item.image_url),
        '" data-preview-name="',
        esc(item.name),
        '">',
        '<div class="pos-cart-thumb-frame">',
        '<img class="pos-cart-thumb" src="',
        esc(item.image_url),
        '" alt="',
        esc(item.name),
        '">',
        "</div>",
        "</div>",
      ].join("");
    }

    ensureImagePreview() {
      if (this.imagePreview) return this.imagePreview;

      var preview = document.createElement("div");
      preview.className = "pos-floating-preview";
      preview.innerHTML = [
        '<div class="pos-floating-preview-media"><img alt=""></div>',
        '<div class="pos-floating-preview-caption"></div>',
      ].join("");
      document.body.appendChild(preview);
      this.imagePreview = preview;
      return preview;
    }

    showImagePreview(target) {
      if (!target) return;

      var src = target.getAttribute("data-preview-src");
      if (!src) return;

      var preview = this.ensureImagePreview();
      preview.querySelector("img").src = src;
      preview.querySelector("img").alt = target.getAttribute("data-preview-name") || "";
      preview.querySelector(".pos-floating-preview-caption").textContent = target.getAttribute("data-preview-name") || "";
      this.previewAnchor = target;
      this.positionImagePreview();
      preview.classList.add("is-visible");
    }

    positionImagePreview() {
      if (!this.imagePreview || !this.previewAnchor) return;

      var rect = this.previewAnchor.getBoundingClientRect();
      if (!rect.width && !rect.height) {
        this.hideImagePreview();
        return;
      }

      var previewWidth = this.imagePreview.offsetWidth || 240;
      var previewHeight = this.imagePreview.offsetHeight || 340;
      var gap = 16;

      var left = rect.left - previewWidth - gap;
      if (left < 12) {
        left = rect.right + gap;
      }
      if (left + previewWidth > window.innerWidth - 12) {
        left = Math.max(12, window.innerWidth - previewWidth - 12);
      }

      var top = rect.top + (rect.height / 2) - (previewHeight / 2);
      if (top < 12) {
        top = 12;
      }
      if (top + previewHeight > window.innerHeight - 12) {
        top = Math.max(12, window.innerHeight - previewHeight - 12);
      }

      this.imagePreview.style.left = left + "px";
      this.imagePreview.style.top = top + "px";
    }

    hideImagePreview() {
      this.previewAnchor = null;
      if (this.imagePreview) {
        this.imagePreview.classList.remove("is-visible");
      }
    }

    renderCart() {
      if (!this.refs.cart) return;
      this.hideImagePreview();

      var items = Object.values(this.state.cart);
      var totalQty = items.reduce(function (sum, row) {
        return sum + row.qty;
      }, 0);

      if (this.refs.cartCount) this.refs.cartCount.textContent = String(items.length);
      if (this.refs.totalQty) this.refs.totalQty.textContent = String(totalQty);

      if (!items.length) {
        this.refs.cart.innerHTML = emptyStateMarkup(iconCart(), this.config.emptyCartTitle, this.config.emptyCartText);
        this.updateActionButton();
        return;
      }

      this.refs.cart.innerHTML = items
        .map(
          function (row) {
            var item = row.item;

            return [
              '<article class="pos-cart-item">',
              this.cartMediaMarkup(item),
              '<div class="pos-cart-copy">',
              '<div class="pos-cart-name">',
              esc(item.name),
              "</div>",
              '<div class="pos-cart-meta mono">SKU: ',
              esc(item.sku),
              "</div>",
              "</div>",
              '<div class="pos-cart-side">',
              '<button type="button" class="pos-remove-btn" data-cart-action="remove" data-item-id="',
              item.id,
              '" aria-label="Удалить">×</button>',
              '<div class="pos-qty-stepper">',
              '<button type="button" data-cart-action="minus" data-item-id="',
              item.id,
              '" aria-label="Уменьшить">-</button>',
              '<input type="number" min="1" value="',
              row.qty,
              '" data-cart-qty-input="1" data-item-id="',
              item.id,
              '" aria-label="Количество">',
              '<button type="button" data-cart-action="plus" data-item-id="',
              item.id,
              '" aria-label="Увеличить">+</button>',
              "</div>",
              "</div>",
              "</article>",
            ].join("");
          }.bind(this)
        )
        .join("");

      this.updateActionButton();
    }

    updateActionButton() {
      if (!this.refs.actionBtn) return;

      var items = Object.values(this.state.cart);
      var totalQty = items.reduce(function (sum, row) {
        return sum + row.qty;
      }, 0);
      this.refs.actionBtn.disabled = items.length === 0;

      if (typeof this.config.updateActionBtn === "function") {
        this.config.updateActionBtn(this.refs.actionBtn, items, totalQty, this.state.mode);
        return;
      }

      this.refs.actionBtn.className = "pos-primary-btn";
      this.refs.actionBtn.textContent = totalQty ? "Подтвердить (" + totalQty + ")" : "Подтвердить";
    }

    async handleAction() {
      if (typeof this.config.onAction !== "function") return;

      var items = Object.values(this.state.cart).map(function (row) {
        return { id: row.item.id, qty: row.qty };
      });
      if (!items.length) return;

      var originalText = this.refs.actionBtn ? this.refs.actionBtn.textContent : "";
      if (this.refs.actionBtn) {
        this.refs.actionBtn.disabled = true;
        this.refs.actionBtn.textContent = this.config.actionPendingText;
      }

      try {
        var result = await this.config.onAction(items, this.state.mode, this);
        if (typeof this.config.onActionSuccess === "function") {
          await this.config.onActionSuccess(result, items, this.state.mode, this);
        }

        if (this.config.clearCartOnAction) {
          this.state.cart = {};
        }
        if (this.config.clearSearchOnAction && this.refs.searchInput) {
          this.refs.searchInput.value = "";
          this.renderResultsState("idle");
          this.focusSearch();
        }

        this.renderCart();
        this.showToast((result && result.message) || "Действие выполнено.", "success");
        this.loadHistory();
      } catch (error) {
        this.showToast(error.message || "Не удалось выполнить действие.", "danger");
        this.updateActionButton();
      } finally {
        if (this.refs.actionBtn && this.refs.actionBtn.textContent === this.config.actionPendingText) {
          this.refs.actionBtn.textContent = originalText;
          this.updateActionButton();
        }
      }
    }

    async loadShops() {
      if (!this.refs.invoiceShopSelect) return;

      try {
        var response = await fetch("/api/shops");
        var data = await response.json();
        var shops = Array.isArray(data.shops) ? data.shops : [];
        if (!shops.length) {
          this.refs.invoiceShopSelect.innerHTML = '<option disabled selected>Нет доступных магазинов</option>';
          return;
        }

        this.refs.invoiceShopSelect.innerHTML = shops
          .map(function (shop) {
            return '<option value="' + esc(shop.uzum_id) + '">' + esc(shop.name) + " (" + esc(shop.uzum_id) + ")</option>";
          })
          .join("");
      } catch (error) {
        this.refs.invoiceShopSelect.innerHTML = '<option disabled selected>Не удалось загрузить магазины</option>';
      }
    }

    async fetchInvoice() {
      if (!this.refs.invoiceShopSelect || !this.refs.invoiceIdInput || !this.refs.fetchInvoiceBtn) return;

      var shopId = this.refs.invoiceShopSelect.value;
      var invoiceId = this.refs.invoiceIdInput.value.trim();
      if (!shopId || !invoiceId) {
        this.showToast("Выберите магазин и укажите ID накладной.", "danger");
        return;
      }

      var originalText = this.refs.fetchInvoiceBtn.textContent;
      this.refs.fetchInvoiceBtn.disabled = true;
      this.refs.fetchInvoiceBtn.textContent = "Загрузка...";

      try {
        var response = await fetch(this.config.invoiceEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            shop_id: shopId,
            invoice_id: invoiceId,
          }),
        });
        var data = await response.json();
        if (!response.ok || data.error) {
          throw new Error(data.error || "Не удалось загрузить накладную.");
        }

        (data.items || []).forEach(
          function (item) {
            this.addToCart(item, { silent: true });
          }.bind(this)
        );

        if (this.refs.invoiceModal && window.bootstrap && bootstrap.Modal) {
          bootstrap.Modal.getOrCreateInstance(this.refs.invoiceModal).hide();
        }

        this.showToast("Накладная загружена: " + (data.items || []).length + " товаров.", "success");
      } catch (error) {
        this.showToast(error.message || "Ошибка загрузки накладной.", "danger");
      } finally {
        this.refs.fetchInvoiceBtn.disabled = false;
        this.refs.fetchInvoiceBtn.textContent = originalText;
      }
    }

    async loadHistory() {
      if (!this.refs.history) return;
      try {
        var response = await fetch(this.config.historyEndpoint);
        var data = await response.json();
        this.state.history = Array.isArray(data.items) ? data.items : [];
      } catch (error) {
        this.state.history = [];
      }
      this.renderHistory();
    }

    formatHistoryTime(iso) {
      if (!iso) return "";
      var d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      try {
        return d.toLocaleString();
      } catch (_e) {
        return d.toISOString();
      }
    }

    renderHistory() {
      if (!this.refs.history) return;

      var entries = Array.isArray(this.state.history) ? this.state.history : [];
      if (!entries.length) {
        this.refs.history.innerHTML =
          '<div class="pos-history-empty">' + esc(this.config.historyEmptyText) + "</div>";
        return;
      }

      var self = this;
      this.refs.history.innerHTML = entries
        .map(function (entry) {
          var reverted = !!entry.reverted_at;
          var label =
            (self.config.historyLabels && self.config.historyLabels[entry.action]) ||
            entry.action ||
            "";
          var actionClass =
            entry.action === "sale"
              ? "pos-history-action--sale"
              : "pos-history-action--stock-in";
          var time = self.formatHistoryTime(entry.created_at);
          var itemsList = (entry.items || [])
            .map(function (it) {
              return [
                '<li class="pos-history-line">',
                '<span class="pos-history-line-name">',
                esc(it.name || ""),
                "</span>",
                '<span class="pos-history-line-sku mono">',
                esc(self.config.historySkuLabelText),
                ": ",
                esc(it.sku || ""),
                "</span>",
                '<span class="pos-history-line-qty">',
                esc(it.qty_before),
                " → ",
                esc(it.qty_after),
                " (",
                Number(it.qty) > 0 ? "+" : "",
                esc(it.qty),
                ")",
                "</span>",
                "</li>",
              ].join("");
            })
            .join("");

          var revertedBadge = reverted
            ? '<span class="pos-history-reverted-badge">' +
              esc(self.config.historyRevertedBadgeText) +
              "</span>"
            : "";
          var undoBtn = reverted
            ? ""
            : [
                '<button type="button" class="pos-history-undo-btn" data-history-action="undo" data-history-id="',
                entry.id,
                '">',
                esc(self.config.historyUndoBtnText),
                "</button>",
              ].join("");

          var rowClass = "pos-history-row" + (reverted ? " pos-history-row--reverted" : "");

          return [
            '<article class="',
            rowClass,
            '">',
            '<header class="pos-history-header">',
            '<span class="pos-history-action ',
            actionClass,
            '">',
            esc(label),
            "</span>",
            '<span class="pos-history-summary">',
            esc(entry.total_qty || 0),
            " ",
            esc(self.config.historyUnitsText),
            "</span>",
            revertedBadge,
            '<span class="pos-history-time">',
            esc(time),
            "</span>",
            "</header>",
            '<ul class="pos-history-items">',
            itemsList,
            "</ul>",
            undoBtn ? '<div class="pos-history-footer">' + undoBtn + "</div>" : "",
            "</article>",
          ].join("");
        })
        .join("");
    }

    async undoAction(id) {
      if (!id) return;
      if (!window.confirm(this.config.historyConfirmUndoText)) return;

      try {
        var response = await fetch(this.config.undoEndpoint + id, { method: "POST" });
        var data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || this.config.historyUndoFailedText);
        }
        this.showToast(this.config.historyUndoSuccessText, "success");
        await this.loadHistory();
      } catch (error) {
        this.showToast(error.message || this.config.historyUndoFailedText, "danger");
      }
    }

    ensureToastHost() {
      if (this.toastHost) return this.toastHost;

      var existing = document.getElementById("posToastHost");
      if (existing) {
        this.toastHost = existing;
        return existing;
      }

      var host = document.createElement("div");
      host.id = "posToastHost";
      host.className = "toast-container position-fixed top-0 end-0 p-3 pos-toast-host";
      document.body.appendChild(host);
      this.toastHost = host;
      return host;
    }

    showToast(message, variant) {
      if (!message || !window.bootstrap || !bootstrap.Toast) return;

      var theme = variant === "danger"
        ? "bg-danger text-white"
        : variant === "success"
          ? "bg-success text-white"
          : "bg-dark text-white";

      var toast = document.createElement("div");
      toast.className = "toast pos-toast " + theme;
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
      toast.setAttribute("aria-atomic", "true");
      toast.innerHTML = [
        '<div class="d-flex align-items-center">',
        '<div class="toast-body">',
        esc(message),
        "</div>",
        '<button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Закрыть"></button>',
        "</div>",
      ].join("");

      this.ensureToastHost().appendChild(toast);
      var instance = bootstrap.Toast.getOrCreateInstance(toast, { delay: 2600 });
      toast.addEventListener("hidden.bs.toast", function () {
        toast.remove();
      });
      instance.show();
    }
  }

  window.PosEngine = PosEngine;
})();
