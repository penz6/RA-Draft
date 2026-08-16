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
  const turnOrder = document.querySelector("[data-turn-order]");
  const viewStateKey = `ra-draft-live-view:${window.location.pathname}${window.location.search}`;
  let liveVersion = liveRegion.dataset.liveVersion || "";
  let pendingVersion = "";
  let noticeSnoozed = false;
  let eventSource = null;
  let eventSourceFailures = 0;
  let fallbackTimer = null;
  let stateCheckInFlight = false;
  let liveSubmitting = false;
  let liveDragging = false;
  let turnAudioContext = null;
  let pendingTurnDing = false;

  const formSnapshots = new WeakMap();
  const dirtyForms = new Set();

  const safeSessionGet = (key) => {
    try {
      return window.sessionStorage.getItem(key);
    } catch (_error) {
      return null;
    }
  };

  const safeSessionSet = (key, value) => {
    try {
      window.sessionStorage.setItem(key, value);
    } catch (_error) {
      // Scroll preservation is a convenience; storage failures must not block picks.
    }
  };

  const safeSessionRemove = (key) => {
    try {
      window.sessionStorage.removeItem(key);
    } catch (_error) {
      // Ignore unavailable session storage.
    }
  };

  const saveViewState = () => {
    if (!turnOrder) return;
    const calendarScrolls = Array.from(document.querySelectorAll(".calendar-scroll"))
      .map((element) => element.scrollLeft);
    safeSessionSet(viewStateKey, JSON.stringify({
      savedAt: Date.now(),
      scrollY: window.scrollY,
      calendarScrolls,
      hadTurnAlert: Boolean(document.querySelector("[data-your-turn-alert]")),
    }));
  };

  const getTurnAudioContext = () => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    if (!turnAudioContext) turnAudioContext = new AudioContextClass();
    return turnAudioContext;
  };

  const playTurnDing = async () => {
    const context = getTurnAudioContext();
    if (!context) return;
    try {
      if (context.state === "suspended") await context.resume();
      if (context.state !== "running") {
        pendingTurnDing = true;
        return;
      }

      const start = context.currentTime;
      const gain = context.createGain();
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.18, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.34);
      gain.connect(context.destination);

      const first = context.createOscillator();
      first.type = "sine";
      first.frequency.setValueAtTime(880, start);
      first.connect(gain);
      first.start(start);
      first.stop(start + 0.14);

      const second = context.createOscillator();
      second.type = "sine";
      second.frequency.setValueAtTime(1174.66, start + 0.13);
      second.connect(gain);
      second.start(start + 0.13);
      second.stop(start + 0.34);

      if (typeof window.navigator.vibrate === "function") {
        window.navigator.vibrate(120);
      }
      pendingTurnDing = false;
    } catch (_error) {
      // Browsers may require a user gesture before sound. Keep the visual alert
      // and play the pending ding on the next tap/key press when possible.
      pendingTurnDing = true;
    }
  };

  const unlockTurnSound = () => {
    const context = getTurnAudioContext();
    if (context?.state === "suspended") context.resume().catch(() => {});
    if (pendingTurnDing) playTurnDing();
  };

  document.addEventListener("pointerdown", unlockTurnSound, true);
  document.addEventListener("keydown", unlockTurnSound, true);

  const restoreViewState = () => {
    const rawState = safeSessionGet(viewStateKey);
    if (!rawState) return;
    safeSessionRemove(viewStateKey);

    let state;
    try {
      state = JSON.parse(rawState);
    } catch (_error) {
      return;
    }
    if (!state || Date.now() - Number(state.savedAt || 0) > 30000) return;

    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        if (Number.isFinite(state.scrollY)) window.scrollTo(0, state.scrollY);
        const calendarScrolls = Array.isArray(state.calendarScrolls)
          ? state.calendarScrolls
          : [];
        document.querySelectorAll(".calendar-scroll").forEach((element, index) => {
          const left = Number(calendarScrolls[index]);
          if (Number.isFinite(left)) element.scrollLeft = left;
        });

        const hasTurnAlert = Boolean(document.querySelector("[data-your-turn-alert]"));
        if (hasTurnAlert && !state.hadTurnAlert) playTurnDing();
      });
    });
  };

  const reloadPreservingView = (delay = 0) => {
    saveViewState();
    window.setTimeout(() => window.location.reload(), delay);
  };

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
  reloadButton.addEventListener("click", () => reloadPreservingView());
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
    reloadPreservingView();
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
    // app.js may make additional checkbox/order changes in response to the same
    // change event (for example when switching buildings). Wait one task so the
    // dirty fingerprint reflects the final form state rather than an interim one.
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
  document.addEventListener("submit", () => {
    // Session actions also use a normal POST/redirect. Preserve the same view for
    // the person making the pick, not only for observers receiving an SSE update.
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

  window.setInterval(() => {
    if (pendingVersion) applyPendingRefresh();
  }, 500);

  restoreViewState();
  window.setTimeout(() => {
    initializeFormSnapshots();
    resumeLiveUpdates();
  }, 0);
})();
