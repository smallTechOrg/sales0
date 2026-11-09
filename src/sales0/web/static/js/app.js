/* sales0 — the only custom JS. Everything else is server-rendered HTML + htmx.
 *
 * 1. htmx config (outerHTML swaps by default).
 * 2. Toast auto-dismiss: ok toasts vanish after 6s; error toasts stay until closed.
 * 3. Bulk select in the inbox: "Select all on page" + "N selected" in .bulk-bar.
 * 4. hx-confirm uses an accessible <dialog> instead of window.confirm.
 * 5. Forms marked data-reset-on-success are cleared after a successful (non-error) swap.
 * 6. Pipeline boards: drag a card to another column / the move strip -> the move popup (#move-dialog).
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

  // ------------------------------------------------------------------ inline edit cancel (keyword rows)
  document.addEventListener("click", function (evt) {
    var btn = evt.target instanceof Element ? evt.target.closest("[data-cancel-edit]") : null;
    if (!btn) return;
    var details = btn.closest("details");
    if (details) details.open = false;
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


  // ------------------------------------------------------------------ CSP-safe replacements for inline handlers
  // (the Content-Security-Policy forbids inline event handlers and inline <script>).
  document.addEventListener("change", function (evt) {
    var t = evt.target instanceof Element ? evt.target.closest("[data-autosubmit]") : null;
    if (!t || !t.form) return;
    if (t.getAttribute("data-autosubmit") === "request" && typeof t.form.requestSubmit === "function") {
      t.form.requestSubmit();
    } else {
      t.form.submit();
    }
  });

  // Compose: picking a contact in a <select data-fill-recipient> fills the To field and the contact name.
  document.addEventListener("change", function (evt) {
    var sel = evt.target instanceof HTMLSelectElement && evt.target.hasAttribute("data-fill-recipient") ? evt.target : null;
    if (!sel) return;
    var opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.dataset.email) return;
    var to = document.getElementById(sel.getAttribute("data-fill-recipient"));
    if (to) to.value = opt.dataset.email;
    var name = document.getElementById(sel.getAttribute("data-fill-name") || "");
    if (name && opt.dataset.name) name.value = opt.dataset.name;
  });

  // A header checkbox <input data-select-all="css selector"> ticks / unticks every checkbox matching the selector.
  document.addEventListener("change", function (evt) {
    var box = evt.target instanceof HTMLInputElement && evt.target.hasAttribute("data-select-all") ? evt.target : null;
    if (!box) return;
    document.querySelectorAll(box.getAttribute("data-select-all")).forEach(function (c) { c.checked = box.checked; });
  });

  document.addEventListener("click", function (evt) {
    var t = evt.target instanceof Element ? evt.target.closest("[data-select-on-click]") : null;
    if (t && typeof t.select === "function") t.select();
  });

  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-confirm-submit") && !window.confirm(form.dataset.q || "Are you sure?")) {
      evt.preventDefault();
      evt.stopImmediatePropagation();
      return;
    }
    if (form.hasAttribute("data-busy-submit")) {
      var b = form.querySelector("button");
      if (!b) return;
      if (b.dataset.busy) { evt.preventDefault(); evt.stopImmediatePropagation(); return; }
      b.dataset.busy = "1";
      // Disable after the browser has collected the submitter (a disabled button is not submitted).
      window.setTimeout(function () {
        if (form.hasAttribute("data-busy-disable")) b.disabled = true;
        if (form.dataset.busyText) b.textContent = form.dataset.busyText;
      }, 0);
    }
  }, true);


  // ------------------------------------------------------------------ status help that stays visible while choosing
  // <select data-move-select> in a form: the chosen option's data-help is shown in the form's [data-move-help] while
  // the person is choosing (not a hover tooltip), and the meeting time field shows for "Meeting scheduled".
  function syncMoveHelp(sel) {
    var form = sel.form || sel.closest("form");
    if (!form) return;
    var opt = sel.options[sel.selectedIndex];
    var help = form.querySelector("[data-move-help]");
    if (help) help.textContent = opt ? (opt.getAttribute("data-help") || "") : "";
    var meeting = form.querySelector("[data-move-meeting]");
    if (meeting) meeting.hidden = !(sel.value === "lead:meeting_scheduled");
    var whyLabel = form.querySelector("[data-move-why-label]");
    if (whyLabel) whyLabel.textContent = sel.value.indexOf("closed:") === 0 ? "Why (optional - shown on the company)" : "Why (optional)";
  }
  document.addEventListener("change", function (evt) {
    var sel = evt.target instanceof HTMLSelectElement && evt.target.hasAttribute("data-move-select") ? evt.target : null;
    if (sel) syncMoveHelp(sel);
  });
  function armMoveSelects() {
    document.querySelectorAll("select[data-move-select]").forEach(syncMoveHelp);
  }

  // ------------------------------------------------------------------ pipeline boards: drag a card to move it
  // Cards [data-company-id][draggable] drop on [data-drop-target] (a column or the move strip); the drop opens the
  // #move-dialog popup (same questions as the company page). The card's "Move…" button opens it without dragging.
  function openMove(card, target) {
    var dialog = document.getElementById("move-dialog");
    if (!dialog || typeof dialog.showModal !== "function" || !card) return;
    var form = dialog.querySelector("form");
    if (form) form.reset();
    dialog.querySelector("#move-company-id").value = card.getAttribute("data-company-id");
    dialog.querySelector("#move-company").textContent = card.getAttribute("data-company-name") || "company";
    dialog.querySelector("#move-next").value = window.location.pathname + window.location.search;
    var sel = dialog.querySelector("select[data-move-select]");
    if (sel) {
      sel.value = target || card.getAttribute("data-current") || sel.value;
      if (!sel.value && sel.options.length) sel.selectedIndex = 0;
      syncMoveHelp(sel);
    }
    dialog.showModal();
    var focus = target && target.indexOf("lead:meeting_scheduled") === 0 ? dialog.querySelector("#move-meeting") : dialog.querySelector("#move-why");
    if (focus) focus.focus();
  }
  var dragged = null;
  document.addEventListener("dragstart", function (evt) {
    var card = evt.target instanceof Element ? evt.target.closest("[data-company-id][draggable]") : null;
    if (!card) return;
    dragged = card;
    card.classList.add("pcard--dragging");
    if (evt.dataTransfer) {
      evt.dataTransfer.effectAllowed = "move";
      evt.dataTransfer.setData("text/plain", card.getAttribute("data-company-id"));
    }
    document.body.classList.add("is-dragging");
  });
  document.addEventListener("dragend", function () {
    if (dragged) dragged.classList.remove("pcard--dragging");
    dragged = null;
    document.body.classList.remove("is-dragging");
    document.querySelectorAll(".is-drop-over").forEach(function (z) { z.classList.remove("is-drop-over"); });
  });
  document.addEventListener("dragover", function (evt) {
    var zone = evt.target instanceof Element ? evt.target.closest("[data-drop-target]") : null;
    if (!zone || !dragged) return;
    evt.preventDefault();
    if (evt.dataTransfer) evt.dataTransfer.dropEffect = "move";
    zone.classList.add("is-drop-over");
  });
  document.addEventListener("dragleave", function (evt) {
    var zone = evt.target instanceof Element ? evt.target.closest("[data-drop-target]") : null;
    if (zone && !(evt.relatedTarget instanceof Node && zone.contains(evt.relatedTarget))) zone.classList.remove("is-drop-over");
  });
  document.addEventListener("drop", function (evt) {
    var zone = evt.target instanceof Element ? evt.target.closest("[data-drop-target]") : null;
    if (!zone || !dragged) return;
    evt.preventDefault();
    zone.classList.remove("is-drop-over");
    var target = zone.getAttribute("data-drop-target");
    var card = dragged;
    if (target === card.getAttribute("data-current")) return; // dropped back where it was
    openMove(card, target);
  });
  document.addEventListener("click", function (evt) {
    var t = evt.target instanceof Element ? evt.target : null;
    if (!t) return;
    var open = t.closest("[data-move-open]");
    if (open) { openMove(open.closest("[data-company-id]"), null); return; }
    var close = t.closest("[data-dialog-close]");
    if (close) { var d = close.closest("dialog"); if (d) d.close(); }
  });
  document.addEventListener("htmx:afterRequest", function (evt) {
    var elt = evt.detail.elt;
    if (!(elt instanceof HTMLFormElement) || elt.id !== "move-form" || !evt.detail.successful) return;
    var d = elt.closest("dialog");
    if (d && d.open) d.close();
  });

  // Digest page: "Copy to clipboard" (was an inline <script>).
  document.addEventListener("click", function (evt) {
    var btn = evt.target instanceof Element ? evt.target.closest("#copy-digest") : null;
    var src = document.getElementById("digest-plain-text");
    if (!btn || !src || !navigator.clipboard) return;
    navigator.clipboard.writeText(src.value).then(function () {
      btn.textContent = "Copied";
      window.setTimeout(function () { btn.textContent = "Copy to clipboard"; }, 2000);
    }, function () { btn.textContent = "Copy failed - select the text instead"; });
  });

  function armCopyDigest() {
    var btn = document.getElementById("copy-digest");
    if (btn && navigator.clipboard) btn.hidden = false;
  }
  document.addEventListener("htmx:afterSettle", function () {
    armMoveSelects();
    armCopyDigest();
    armAllToasts();
    refreshBulkBar();
  });
  document.addEventListener("DOMContentLoaded", function () {
    armMoveSelects();
    armCopyDigest();
    armAllToasts();
    refreshBulkBar();
  });
})();
