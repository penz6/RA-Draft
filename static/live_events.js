(() => {
  "use strict";

  const liveRegion = document.querySelector("[data-live-refresh]");
  if (!liveRegion || !("EventSource" in window)) return;

  const eventsUrl = liveRegion.dataset.liveEventsUrl;
  if (!eventsUrl) return;

  let currentVersion = liveRegion.dataset.liveVersion || "";
  let pendingVersion = null;
  let source = null;
  let formIsDirty = false;

  const refreshBlocked = () => Boolean(
    document.hidden
    || formIsDirty
    || document.querySelector("dialog[open]")
    || document.querySelector(".is-manager-selecting")
    || document.querySelector(".is-dragging")
  );

  const closeSource = () => {
    if (source) {
      source.close();
      source = null;
    }
  };

  const reloadForVersion = (version) => {
    if (!version || version === currentVersion) return;
    if (refreshBlocked()) {
      pendingVersion = version;
      return;
    }
    closeSource();
    window.location.reload();
  };

  const readVersion = (event) => {
    try {
      const payload = JSON.parse(event.data);
      return typeof payload.version === "string" ? payload.version : "";
    } catch (_error) {
      return "";
    }
  };

  const connect = () => {
    if (source || document.hidden) return;

    source = new window.EventSource(eventsUrl);
    source.addEventListener("state", (event) => {
      const version = readVersion(event);
      if (!currentVersion) currentVersion = version;
      else reloadForVersion(version);
    });
    source.addEventListener("update", (event) => {
      reloadForVersion(readVersion(event));
    });
    source.addEventListener("reload", () => {
      if (refreshBlocked()) {
        pendingVersion = `access-${Date.now()}`;
        return;
      }
      closeSource();
      window.location.reload();
    });
    source.addEventListener("reconnect", () => {
      // The server periodically ends streams so authentication is rechecked.
      // EventSource reconnects automatically using the retry value it received.
    });
  };

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (
      target instanceof HTMLElement
      && target.closest("form")
      && target.getAttribute("type") !== "hidden"
    ) {
      formIsDirty = true;
    }
  });
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (
      target instanceof HTMLElement
      && target.closest("form")
      && target.getAttribute("type") !== "hidden"
    ) {
      formIsDirty = true;
    }
  });
  document.addEventListener("submit", closeSource);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      closeSource();
      return;
    }
    if (pendingVersion && !refreshBlocked()) {
      closeSource();
      window.location.reload();
      return;
    }
    connect();
  });

  window.setInterval(() => {
    if (pendingVersion && !refreshBlocked()) {
      closeSource();
      window.location.reload();
    }
  }, 500);
  window.addEventListener("pagehide", closeSource);

  connect();
})();
