(() => {
  "use strict";

  const banner = document.querySelector("[data-live-banner]");
  const target = document.querySelector("[data-live-refresh]");
  if (!banner || !target) {
    return;
  }

  const eventsUrl = target.getAttribute("data-live-events-url");
  const stateUrl = target.getAttribute("data-live-state-url");
  const partialUrl = target.getAttribute("data-live-partial-url");
  let currentVersion = target.getAttribute("data-live-version") || "";
  let isChecking = false;
  let isPatching = false;

  const showBanner = (message) => {
    const textNode = banner.querySelector("[data-live-banner-text]");
    if (textNode && message) {
      textNode.textContent = message;
    }
    banner.hidden = false;
  };

  const applyDomPatch = (fragments) => {
    for (const [key, html] of Object.entries(fragments)) {
      const el = document.querySelector(`[data-dashboard-${key}]`);
      if (el) {
        const temp = document.createElement("div");
        temp.innerHTML = html;
        if (temp.firstElementChild) {
          el.replaceWith(temp.firstElementChild);
        }
      }
    }
  };

  const checkVersion = async () => {
    if (isChecking || isPatching || !stateUrl) return;
    isChecking = true;
    try {
      const response = await fetch(stateUrl, { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      if (data.version && data.version !== currentVersion) {
        if (partialUrl) {
          isPatching = true;
          try {
            const partResp = await fetch(partialUrl, { cache: "no-store" });
            if (partResp.ok) {
              const partData = await partResp.json();
              if (partData.fragments) {
                applyDomPatch(partData.fragments);
                currentVersion = partData.version || data.version;
                target.setAttribute("data-live-version", currentVersion);
                return;
              }
            }
          } finally {
            isPatching = false;
          }
        }
        showBanner("Session updates available.");
      }
    } catch (_err) {
      // Ignored
    } finally {
      isChecking = false;
    }
  };

  if (eventsUrl && typeof window.EventSource === "function") {
    const es = new EventSource(eventsUrl);
    es.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "dashboard_update" || payload.type === "session_update") {
          checkVersion();
        }
      } catch (_e) {}
    };
    es.onerror = () => {
      // Will reconnect automatically
    };
  }

  setInterval(checkVersion, 15000);
})();
