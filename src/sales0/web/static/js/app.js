/* sales0 — the only custom JS. Everything else is server-rendered HTML + htmx.
 *
 * 1. htmx config (outerHTML swaps by default).
 * 2. Toast auto-dismiss: ok toasts vanish after 6s; error toasts stay until closed.
 * 3. Bulk select in the inbox: "Select all on page" + "N selected" in .bulk-bar.
 * 4. hx-confirm uses an accessible <dialog> instead of window.confirm.
 * 5. Forms marked data-reset-on-success are cleared after a successful (non-error) swap.
 */
(function () {
  "use strict";

  if (!window.htmx) {
    return;
  }
  var htmx = window.htmx;
  htmx.config.defaultSwapStyle = "outerHTML";
  htmx.config.historyCacheSize = 0;

  var TOAST_MS = 6000;

  // ------------------------------------------------------------------ toasts
  function armToast(toast) {
    if (toast.dataset.armed) return;
    toast.dataset.armed = "1";
    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast__close";
    close.textContent = "Dismiss";
    close.addEventListener("click", function () { toast.remove(); });
    toast.appendChild(close);
    if (!toast.classList.contains("toast--error")) {
      window.setTimeout(function () { toast.remove(); }, TOAST_MS);
    }
  }

  function armAllToasts() {
    document.querySelectorAll("#toasts .toast").forEach(armToast);
  }

  // ------------------------------------------------------------------ bulk select
  function selectionBoxes() {
    return Array.prototype.slice.call(document.querySelectorAll("#inbox-list input.js-select"));
  }

  function refreshBulkBar() {
    var bar = document.getElementById("bulk-bar");
    if (!bar) return;
    var boxes = selectionBoxes();
    var checked = boxes.filter(function (b) { return b.checked; });
    var count = bar.querySelector(".bulk-bar__count");
    if (count) count.textContent = checked.length + " selected";
    bar.classList.toggle("bulk-bar--active", checked.length > 0);
    var all = bar.querySelector("input.js-select-all");
    if (all) {
      all.checked = boxes.length > 0 && checked.length === boxes.length;
      all.indeterminate = checked.length > 0 && checked.length < boxes.length;
    }
    boxes.forEach(function (b) {
      var card = b.closest(".card");
      if (card) card.classList.toggle("card--selected", b.checked);
    });
  }

  document.addEventListener("change", function (evt) {
    var target = evt.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.classList.contains("js-select-all")) {
      selectionBoxes().forEach(function (b) { b.checked = target.checked; });
      refreshBulkBar();
    } else if (target.classList.contains("js-select")) {
      refreshBulkBar();
    }
  });

  // ------------------------------------------------------------------ confirm dialog
  function confirmDialog() {
    var dialog = document.getElementById("confirm-dialog");
    if (dialog && typeof dialog.showModal === "function") return dialog;
    return null;
  }

  document.addEventListener("htmx:confirm", function (evt) {
    var question = evt.detail.question;
    if (!question) return;            // no hx-confirm on this element
    var dialog = confirmDialog();
    if (!dialog) return;              // fall back to htmx's window.confirm
    evt.preventDefault();
    dialog.querySelector(".confirm-dialog__message").textContent = question;
    dialog.returnValue = "";
    var onClose = function () {
      dialog.removeEventListener("close", onClose);
      if (dialog.returnValue === "confirm") {
        evt.detail.issueRequest(true);
      }
    };
    dialog.addEventListener("close", onClose);
    dialog.showModal();
    var ok = dialog.querySelector("[value=confirm]");
    if (ok) ok.focus();
  });

  // ------------------------------------------------------------------ form reset on success
  document.addEventListener("htmx:afterRequest", function (evt) {
    var elt = evt.detail.elt;
    if (!(elt instanceof HTMLFormElement) || !elt.hasAttribute("data-reset-on-success")) return;
    var xhr = evt.detail.xhr;
    var retargetedToToasts = xhr && (xhr.getResponseHeader("HX-Retarget") || "") === "#toasts";
    if (evt.detail.successful && !retargetedToToasts) {
      elt.reset();
      var first = elt.querySelector("input:not([type=hidden])");
      if (first) first.focus();
    }
  });

  // A network failure or 5xx must never be silent.
  document.addEventListener("htmx:responseError", function (evt) {
    showToast("The server could not complete that (HTTP " + evt.detail.xhr.status + ") — try again, or check the server log.", "error");
  });
  document.addEventListener("htmx:sendError", function () {
    showToast("Couldn't reach sales0 — is the server still running?", "error");
  });

  function showToast(message, kind) {
    var region = document.getElementById("toasts");
    if (!region) return;
    var toast = document.createElement("div");
    toast.className = "toast toast--" + (kind || "ok");
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    toast.textContent = message;
    region.appendChild(toast);
    armToast(toast);
  }

  document.addEventListener("htmx:afterSettle", function () {
    armAllToasts();
    refreshBulkBar();
  });
  document.addEventListener("DOMContentLoaded", function () {
    armAllToasts();
    refreshBulkBar();
  });
})();
