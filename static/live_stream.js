(() => {
  "use strict";

  const liveRegion = document.querySelector("[data-live-refresh]");
  if (!liveRegion) return;

  // The legacy polling block in app.js looks for this attribute. Claim the
  // region before app.js executes so only this hardened stream client runs.
  liveRegion.removeAttribute("data-live-refresh");
  liveRegion.dataset.liveStreamActive = "true";

  const liveStateUrl = liveRegion.dataset.liveStateUrl;
  const liveEventsUrl = liveRegion.dataset.liveEventsUrl;
  let liveVersion = liveRegion.dataset.liveVersion || "";
  let pendingVersion = "";
  let noticeSnoozed = false;
  let eventSource = null;
  let eventSourceFailures = 0;
  let fallbackTimer = null;
  let stateCheckInFlight = false;
  let liveSubmitting = false;
  let liveDragging = false;

  const formSnapshots = new WeakMap();
  const dirtyForms = new Set();

  const formFingerprint = (form) => {
    const values = [];
    new window.FormData(form).forEach((value, key) => {
      values.push([
        key,
        typeof value === "string"
          ? value
          : `file:${value.name}:${value.size}:${value.lastModified}`,
      ]);
    });
    return JSON.stringify(values);
  };

  const pruneDirtyForms = () => {
    dirtyForms.forEach((form) => {
      if (!form.isConnected) dirtyForms.delete(form);
    });
  };

  const refreshBlocked = () => {
    pruneDirtyForms();
    return Boolean(
      document.hidden
      || liveSubmitting
      || liveDragging
      || dirtyForms.size
      || document.querySelector("dialog[open]")
      || document.querySelector(".is-manager-selecting")
    );
  };

  const notice = document.createElement("aside");
  notice.className = "live-update-notice";
  notice.hidden = true;
  notice.setAttribute("role", "status");
  notice.setAttribute("aria-live", "polite");

  const noticeCopy = document.createElement("div");
  const noticeTitle = document.createElement("strong");
  noticeTitle.textContent = "The duty schedule changed";
  const noticeText = document.createElement("span");
  noticeText.textContent = "Reload to see the newest turn and assignments. Your current edits have not been discarded.";
  noticeCopy.append(noticeTitle, noticeText);

  const noticeActions = document.createElement("div");
  noticeActions.className = "live-update-actions";
  const reloadButton = document.createElement("button");
  reloadButton.type = "button";
  reloadButton.className = "button small";
  reloadButton.textContent = "Reload updates";
  reloadButton.addEventListener("click", () => window.location.reload());
  const keepEditingButton = document.createElement("button");
  keepEditingButton.type = "button";
  keepEditingButton.className = "button ghost small";
  keepEditingButton.textContent = "Keep editing";
  keepEditingButton.addEventListener("click", () => {
    noticeSnoozed = true;
    notice.hidden = true;
  });
  noticeActions.append(reloadButton, keepEditingButton);
  notice.append(noticeCopy, noticeActions);
  document.body.append(notice);

  const showPendingNotice = () => {
    if (!noticeSnoozed) notice.hidden = false;
  };

  const applyPendingRefresh = () => {
    if (!pendingVersion) return;
    if (refreshBlocked()) {
      showPendingNotice();
      return;
    }
    window.location.reload();
  };

  const requestRefresh = (version, force = false) => {
    if (!force && (!version || version === liveVersion)) return;
    pendingVersion = force ? "__reload__" : version;
    noticeSnoozed = false;
    applyPendingRefresh();
  };

  const syncFormDirty = (form) => {
    if (!(form instanceof HTMLFormElement)) return;
    const baseline = formSnapshots.get(form);
    if (baseline === undefined) return;
    if (formFingerprint(form) === baseline) dirtyForms.delete(form);
    else dirtyForms.add(form);
    applyPendingRefresh();
  };

  const initializeFormSnapshots = () => {
    document.querySelectorAll("form").forEach((form) => {
      formSnapshots.set(form, formFingerprint(form));
    });
  };

  document.addEventListener("input", (event) => {
    const form = event.target instanceof HTMLElement ? event.target.closest("form") : null;
    if (form) syncFormDirty(form);
  });
  document.addEventListener("change", (event) => {
    const form = event.target instanceof HTMLElement ? event.target.closest("form") : null;
    if (form) syncFormDirty(form);
  });
  document.addEventListener("reset", (event) => {
    const form = event.target;
    window.setTimeout(() => syncFormDirty(form), 0);
  });
  document.addEventListener("submit", () => {
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
      // Ignore malformed or incomplete events; reconnect/state fallback wins.
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
      // Temporary network loss must not interrupt a pick or unsaved form.
    } finally {
      stateCheckInFlight = false;
    }
  };

  // Compatibility name retained for existing deployments/tests. Modern
  // browsers use EventSource and do not run this interval during healthy SSE.
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
      window.setTimeout(() => window.location.reload(), delay);
    });
    eventSource.addEventListener("reconnect", () => {
      // The server intentionally closes streams periodically. EventSource
      // reconnects automatically and the next request revalidates the session.
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

  // Manager mode can end through code in app.js, so periodically apply only an
  // already-pending refresh. This is not network polling.
  window.setInterval(() => {
    if (pendingVersion) applyPendingRefresh();
  }, 500);

  window.setTimeout(() => {
    initializeFormSnapshots();
    resumeLiveUpdates();
  }, 0);
})();
