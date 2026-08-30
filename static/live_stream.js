(() => {
  "use strict";

  const refreshRoot = document.querySelector("[data-live-refresh]");
  if (!refreshRoot) return;

  const liveEventsUrl = refreshRoot.dataset.liveEventsUrl || "";
  const liveStateUrl = refreshRoot.dataset.liveStateUrl || "";
  const livePartialUrl = refreshRoot.dataset.livePartialUrl || "";
  const hasPartialSessionUpdates = Boolean(livePartialUrl);
  let liveVersion = refreshRoot.dataset.liveVersion || "";
  let pendingVersion = "";
  let partialUpdateInFlight = false;
  let partialUpdateFailures = 0;
  let stateCheckInFlight = false;
  let eventSource = null;
  let fallbackTimer = null;
  let eventSourceFailures = 0;
  let liveSubmitting = false;
  let liveDragging = false;
  let noticeSnoozed = false;

  const dirtyForms = new Set();
  const formSnapshots = new Map();

  const notice = document.createElement("aside");
  notice.className = "live-update-notice";
  notice.setAttribute("aria-live", "polite");
  notice.hidden = true;
  notice.innerHTML = `
    <div>
      <strong>Updates available</strong>
      <span>Another participant or manager changed the schedule.</span>
    </div>
    <div class="live-update-actions">
      <button class="button small" type="button" data-live-apply>Update now</button>
      <button class="button ghost small" type="button" data-live-snooze>Dismiss</button>
    </div>
  `;
  document.body.appendChild(notice);

  notice.querySelector("[data-live-apply]")?.addEventListener("click", () => {
    if (pendingVersion) applyPendingRefresh();
  });
  notice.querySelector("[data-live-snooze]")?.addEventListener("click", () => {
    noticeSnoozed = true;
    notice.hidden = true;
  });

  const formFingerprint = (form) => {
    if (!(form instanceof HTMLFormElement)) return "";
    const entries = [];
    const elements = Array.from(form.elements);
    for (const element of elements) {
      if (!element || !element.name || element.disabled) continue;
      if (element.type === "hidden" && element.name === "csrf") continue;
      if ((element.type === "checkbox" || element.type === "radio") && !element.checked) continue;
      entries.push(`${encodeURIComponent(element.name)}=${encodeURIComponent(element.value)}`);
    }
    return entries.join("&");
  };

  const initializeFormSnapshots = () => {
    formSnapshots.clear();
    dirtyForms.clear();
    document.querySelectorAll("form").forEach((form) => {
      formSnapshots.set(form, formFingerprint(form));
    });
  };

  const hasUnsavedFormInput = () => dirtyForms.size > 0;

  const openManagerDialogs = () => {
    const dialogs = Array.from(document.querySelectorAll("dialog[open]"));
    return dialogs.filter((dialog) => !dialog.matches("[data-role-help]"));
  };

  const activeInteractiveElement = () => {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    return active.closest("input, select, textarea, button, summary, details[open], dialog[open]");
  };

  const refreshBlocked = () => {
    if (liveSubmitting || liveDragging) return true;
    if (hasUnsavedFormInput()) return true;
    if (openManagerDialogs().length > 0) return true;
    const active = activeInteractiveElement();
    if (active && !active.matches("a, .button, [data-help-open]")) return true;
    return false;
  };

  const showPendingNotice = () => {
    if (noticeSnoozed) return;
    notice.hidden = false;
  };

  const saveViewState = () => {
    try {
      const scrollPosition = { x: window.scrollX, y: window.scrollY };
      sessionStorage.setItem("ra_draft_live_scroll", JSON.stringify(scrollPosition));
    } catch (_error) {
      // Session storage failures must not break draft participation.
    }
  };

  const restoreViewState = () => {
    try {
      const raw = sessionStorage.getItem("ra_draft_live_scroll");
      if (!raw) return;
      sessionStorage.removeItem("ra_draft_live_scroll");
      const position = JSON.parse(raw);
      if (typeof position?.x === "number" && typeof position?.y === "number") {
        window.scrollTo(position.x, position.y);
      }
    } catch (_error) {
      // Ignore scroll restoration errors.
    }
  };

  const reloadPreservingView = (delay = 0) => {
    saveViewState();
    window.setTimeout(() => window.location.reload(), delay);
  };

  const targetSelectorForFragment = (fragmentName) => {
    switch (fragmentName) {
      case "heading":
        return "[data-session-live-heading]";
      case "summary":
        return "[data-session-live-summary]";
      case "status":
        return "[data-session-live-status]";
      case "turn_order":
        return "[data-session-live-order]";
      case "calendar":
        return "[data-session-live-calendar]";
      case "assignments":
        return "[data-session-live-assignments]";
      default:
        return null;
    }
  };

  const captureSessionViewState = () => {
    const calendarScroll = document.querySelector(".calendar-scroll");
    const activeElement = document.activeElement;
    let activeSelector = "";
    let activeDataDate = "";
    if (activeElement instanceof HTMLElement) {
      activeDataDate = activeElement.getAttribute("data-date") || "";
      if (activeElement.id) {
        activeSelector = `#${CSS.escape(activeElement.id)}`;
      } else if (activeElement.name) {
        activeSelector = `[name="${CSS.escape(activeElement.name)}"]`;
      }
    }
    return {
      windowScrollY: window.scrollY,
      windowScrollX: window.scrollX,
      calendarScrollLeft: calendarScroll ? calendarScroll.scrollLeft : 0,
      activeSelector,
      activeDataDate,
    };
  };

  const restoreCapturedViewState = (viewState, { playNewTurnDing = false } = {}) => {
    if (!viewState) return;
    window.scrollTo(viewState.windowScrollX, viewState.windowScrollY);
    const calendarScroll = document.querySelector(".calendar-scroll");
    if (calendarScroll && typeof viewState.calendarScrollLeft === "number") {
      calendarScroll.scrollLeft = viewState.calendarScrollLeft;
    }
    if (viewState.activeDataDate) {
      const match = document.querySelector(
        `.calendar-day:not(:disabled)[data-date="${CSS.escape(viewState.activeDataDate)}"]`
      );
      if (match instanceof HTMLElement && !refreshBlocked()) {
        match.focus({ preventScroll: true });
        return;
      }
    }
    if (viewState.activeSelector) {
      const match = document.querySelector(viewState.activeSelector);
      if (match instanceof HTMLElement && !refreshBlocked()) {
        match.focus({ preventScroll: true });
      }
    }
    if (playNewTurnDing) {
      const yourTurnAlert = document.querySelector(".your-turn-alert");
      if (yourTurnAlert) {
        yourTurnAlert.classList.remove("your-turn-alert-pop");
        void yourTurnAlert.offsetWidth;
        yourTurnAlert.classList.add("your-turn-alert-pop");
      }
    }
  };

  const applyPartialSessionUpdate = async ({ force = false } = {}) => {
    if (!hasPartialSessionUpdates || partialUpdateInFlight) return false;
    if (!force && refreshBlocked()) {
      showPendingNotice();
      return false;
    }

    partialUpdateInFlight = true;
    const viewState = captureSessionViewState();
    const requestedVersion = pendingVersion;

    try {
      const response = await window.fetch(livePartialUrl, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401 || response.status === 403 || response.redirected) {
        reloadPreservingView();
        return false;
      }
      if (!response.ok) {
        throw new Error(`Partial refresh failed with HTTP ${response.status}`);
      }
      const payload = await response.json();
      if (!payload || typeof payload.version !== "string" || !payload.fragments) {
        throw new Error("Invalid partial refresh payload");
      }

      const fragmentEntries = Object.entries(payload.fragments);
      const targets = [];
      for (const [name, html] of fragmentEntries) {
        const selector = targetSelectorForFragment(name);
        if (!selector) continue;
        const currentTarget = document.querySelector(selector);
        if (!currentTarget) {
          throw new Error(`Missing partial target for ${name}`);
        }
        targets.push({ name, selector, currentTarget, html });
      }

      const parser = new DOMParser();
      for (const target of targets) {
        const doc = parser.parseFromString(target.html, "text/html");
        const replacement = doc.body.firstElementChild;
        if (!(replacement instanceof HTMLElement)) {
          throw new Error(`Invalid fragment HTML for ${target.name}`);
        }
        target.currentTarget.replaceWith(replacement);
      }

      liveVersion = payload.version;
      refreshRoot.dataset.liveVersion = payload.version;
      if (
        !pendingVersion
        || pendingVersion === requestedVersion
        || pendingVersion === payload.version
      ) {
        pendingVersion = "";
      }
      partialUpdateFailures = 0;
      notice.hidden = true;
      noticeSnoozed = false;
      dirtyForms.clear();
      initializeFormSnapshots();
      window.RADraftSessionUI?.resetAfterLivePatch?.();
      restoreCapturedViewState(viewState, { playNewTurnDing: true });
      return true;
    } catch (_error) {
      partialUpdateFailures += 1;
      if (!force) {
        if (partialUpdateFailures >= 2) {
          reloadPreservingView();
        } else {
          showPendingNotice();
          window.setTimeout(() => applyPendingRefresh(), 1500);
        }
      }
      return false;
    } finally {
      partialUpdateInFlight = false;
      if (pendingVersion && pendingVersion !== requestedVersion) {
        window.setTimeout(() => applyPendingRefresh(), 0);
      }
    }
  };

  const applyPendingRefresh = () => {
    if (!pendingVersion) return;
    if (refreshBlocked()) {
      showPendingNotice();
      return;
    }
    if (pendingVersion === "__reload__" || !hasPartialSessionUpdates) {
      reloadPreservingView();
      return;
    }
    void applyPartialSessionUpdate();
  };

  const requestRefresh = (version, force = false) => {
    if (!force && (!version || version === liveVersion)) return;
    pendingVersion = force ? "__reload__" : version;
    noticeSnoozed = false;
    applyPendingRefresh();
  };

  const refreshSessionNow = async () => {
    if (!hasPartialSessionUpdates) return false;
    if (refreshBlocked()) {
      pendingVersion = pendingVersion || "__local__";
      noticeSnoozed = false;
      showPendingNotice();
      return true;
    }
    return applyPartialSessionUpdate({ force: true });
  };

  window.RADraftLiveSession = {
    supportsPartial: hasPartialSessionUpdates,
    refreshNow: refreshSessionNow,
    reload: reloadPreservingView,
  };

  const syncFormDirty = (form) => {
    if (!(form instanceof HTMLFormElement)) return;
    const baseline = formSnapshots.get(form);
    if (baseline === undefined) return;
    if (formFingerprint(form) === baseline) dirtyForms.delete(form);
    else dirtyForms.add(form);
    applyPendingRefresh();
  };

  document.addEventListener("input", (event) => {
    const form = event.target instanceof HTMLElement ? event.target.closest("form") : null;
    if (form) syncFormDirty(form);
  });
  document.addEventListener("change", (event) => {
    const form = event.target instanceof HTMLElement ? event.target.closest("form") : null;
    if (form) window.setTimeout(() => syncFormDirty(form), 0);
  });
  document.addEventListener("reset", (event) => {
    const form = event.target;
    window.setTimeout(() => syncFormDirty(form), 0);
  });
  document.addEventListener("click", (event) => {
    const control = event.target instanceof HTMLElement
      ? event.target.closest(
        "[data-move-up],[data-move-down],[data-participant-select-all],[data-participant-clear]"
      )
      : null;
    const form = control?.closest("form");
    if (form) window.setTimeout(() => syncFormDirty(form), 0);
    if (event.target instanceof HTMLElement && event.target.closest(
      "[data-manager-cancel],[data-dialog-close],[data-help-close]"
    )) {
      window.setTimeout(applyPendingRefresh, 0);
    }
  });
  document.addEventListener("submit", (event) => {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (form?.matches("[data-live-pick-form]")) return;

    // Non-enhanced actions still use normal POST/redirect. Preserve the current
    // session view for those less frequent manager actions.
    saveViewState();
    liveSubmitting = true;
    disconnectStream();
  }, true);
  document.addEventListener("dragstart", () => {
    liveDragging = true;
  }, true);
  document.addEventListener("dragend", (event) => {
    liveDragging = false;
    const form = event.target instanceof HTMLElement ? event.target.closest("form") : null;
    if (form) syncFormDirty(form);
    applyPendingRefresh();
  }, true);
  document.addEventListener("close", applyPendingRefresh, true);

  const stopFallbackPolling = () => {
    if (fallbackTimer) window.clearInterval(fallbackTimer);
    fallbackTimer = null;
  };

  function disconnectStream() {
    eventSource?.close();
    eventSource = null;
  }

  const readVersionEvent = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (!payload || typeof payload.version !== "string") return;
      eventSourceFailures = 0;
      stopFallbackPolling();
      if (!liveVersion) {
        liveVersion = payload.version;
        return;
      }
      requestRefresh(payload.version);
    } catch (_error) {
      // Ignore malformed events; reconnect/state fallback will recover.
    }
  };

  const checkLiveState = async () => {
    if (!liveStateUrl || stateCheckInFlight || document.hidden) return;
    stateCheckInFlight = true;
    try {
      const response = await window.fetch(liveStateUrl, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401 || response.status === 403 || response.redirected) {
        requestRefresh("", true);
        return;
      }
      if (!response.ok) return;
      const payload = await response.json();
      if (!payload || typeof payload.version !== "string") return;
      if (!liveVersion) liveVersion = payload.version;
      else requestRefresh(payload.version);
    } catch (_error) {
      // Temporary network loss must not interrupt an action or unsaved form.
    } finally {
      stateCheckInFlight = false;
    }
  };

  const pollLiveState = checkLiveState;

  const startFallbackPolling = () => {
    if (fallbackTimer || !liveStateUrl || document.hidden) return;
    pollLiveState();
    fallbackTimer = window.setInterval(pollLiveState, 10000);
  };

  const connectStream = () => {
    if (
      eventSource
      || document.hidden
      || !liveEventsUrl
      || !("EventSource" in window)
    ) return;

    eventSource = new window.EventSource(liveEventsUrl, { withCredentials: true });
    eventSource.addEventListener("open", () => {
      eventSourceFailures = 0;
      stopFallbackPolling();
    });
    eventSource.addEventListener("state", readVersionEvent);
    eventSource.addEventListener("update", readVersionEvent);
    eventSource.addEventListener("reload", (event) => {
      let delay = 0;
      try {
        const payload = JSON.parse(event.data);
        if (payload?.reason === "signed-out") delay = 600;
      } catch (_error) {
        // A malformed reload event still requires a safe page reload.
      }
      disconnectStream();
      reloadPreservingView(delay);
    });
    eventSource.addEventListener("reconnect", () => {
      // The server intentionally rotates streams; EventSource reconnects itself.
    });
    eventSource.addEventListener("error", () => {
      eventSourceFailures += 1;
      if (eventSourceFailures >= 2) startFallbackPolling();
      window.setTimeout(checkLiveState, 5000);
    });
  };

  const resumeLiveUpdates = () => {
    applyPendingRefresh();
    checkLiveState();
    if ("EventSource" in window) connectStream();
    else startFallbackPolling();
  };

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      disconnectStream();
      stopFallbackPolling();
    } else {
      resumeLiveUpdates();
    }
  });
  window.addEventListener("pageshow", resumeLiveUpdates);
  window.addEventListener("pagehide", () => {
    disconnectStream();
    stopFallbackPolling();
  });

  window.setInterval(() => {
    if (pendingVersion) applyPendingRefresh();
  }, 500);

  restoreViewState();
  window.setTimeout(() => {
    initializeFormSnapshots();
    resumeLiveUpdates();
  }, 0);
})();
