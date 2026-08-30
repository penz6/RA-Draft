(function () {
  const meta = document.querySelector('meta[name="session-live-stream-url"]');
  if (!meta) return;

  const streamUrl = meta.getAttribute("content");
  let eventSource = null;
  let reconnectAttempts = 0;
  const maxReconnectDelay = 30000;

  function connect() {
    if (eventSource) {
      eventSource.close();
    }

    eventSource = new EventSource(streamUrl);

    eventSource.onopen = function () {
      reconnectAttempts = 0;
    };

    eventSource.onmessage = function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.type === "session_update" || data.type === "dashboard_update") {
          window.dispatchEvent(
            new CustomEvent("ra:live_update", { detail: data })
          );
        }
      } catch (err) {
        console.error("Failed to parse SSE payload", err);
      }
    };

    eventSource.onerror = function () {
      eventSource.close();
      reconnectAttempts++;
      const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), maxReconnectDelay);
      setTimeout(connect, delay);
    };
  }

  connect();

  window.addEventListener("beforeunload", function () {
    if (eventSource) {
      eventSource.close();
    }
  });
})();
