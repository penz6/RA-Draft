(() => {
  "use strict";

  let liveEditing = false;
  let liveDragging = false;
  let pendingLiveRefresh = "";
  let applyPendingLiveRefresh = () => {};

  const markLiveEditing = () => {
    liveEditing = true;
  };

  const helpDialog = document.querySelector("[data-role-help]");
  if (helpDialog) {
    const openHelp = () => {
      if (typeof helpDialog.showModal === "function") {
        if (!helpDialog.open) helpDialog.showModal();
      } else {
        helpDialog.setAttribute("open", "");
      }
    };
    const closeHelp = () => {
      if (typeof helpDialog.close === "function") {
        if (helpDialog.open) helpDialog.close();
      } else {
        helpDialog.removeAttribute("open");
      }
      applyPendingLiveRefresh();
    };
    document.querySelectorAll("[data-help-open]").forEach((button) => {
      button.addEventListener("click", openHelp);
    });
    helpDialog.querySelectorAll("[data-help-close]").forEach((button) => {
      button.addEventListener("click", closeHelp);
    });
    helpDialog.addEventListener("click", (event) => {
      if (event.target === helpDialog) closeHelp();
    });
    helpDialog.addEventListener("close", applyPendingLiveRefresh);
    if (helpDialog.dataset.autoOpen === "true") openHelp();
  }

  const sessionForm = document.querySelector("[data-session-form]");
  if (sessionForm) {
    const buildingPicker = sessionForm.querySelector("[data-building-picker]");
    const participantList = sessionForm.querySelector("[data-participant-list]");
    const rows = Array.from(sessionForm.querySelectorAll("[data-participant-row]"));
    const emptyMessage = sessionForm.querySelector("[data-no-participants]");
    let draggedRow = null;

    const activeRows = () => Array.from(
      participantList.querySelectorAll("[data-participant-row]")
    ).filter((row) => !row.hidden);

    const updateOrders = () => {
      activeRows().forEach((row, index) => {
        const order = row.querySelector("[data-order-input]");
        const label = row.querySelector("[data-order-label]");
        if (order) order.value = String(index + 1);
        if (label) label.textContent = String(index + 1);
      });
    };

    const moveRow = (row, direction) => {
      const visibleRows = activeRows();
      const index = visibleRows.indexOf(row);
      const target = visibleRows[index + direction];
      if (!target) return;
      markLiveEditing();
      if (direction < 0) participantList.insertBefore(row, target);
      else participantList.insertBefore(target, row);
      updateOrders();
      row.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };

    const syncBuilding = (selectVisible) => {
      const selectedBuilding = buildingPicker ? buildingPicker.value : null;
      let visibleCount = 0;
      rows.forEach((row) => {
        const matches = !selectedBuilding || row.dataset.buildingId === selectedBuilding;
        const checkbox = row.querySelector("[data-participant-check]");
        row.hidden = !matches;
        if (checkbox) {
          checkbox.disabled = !matches;
          if (!matches) checkbox.checked = false;
          if (matches && selectVisible) checkbox.checked = true;
        }
        if (matches) visibleCount += 1;
      });
      if (emptyMessage) emptyMessage.hidden = visibleCount !== 0;
      updateOrders();
    };

    rows.forEach((row) => {
      row.addEventListener("dragstart", (event) => {
        markLiveEditing();
        liveDragging = true;
        draggedRow = row;
        row.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", "participant");
      });
      row.addEventListener("dragend", () => {
        row.classList.remove("is-dragging");
        draggedRow = null;
        liveDragging = false;
        updateOrders();
        applyPendingLiveRefresh();
      });
      row.addEventListener("dragover", (event) => {
        if (!draggedRow || row.hidden || row === draggedRow) return;
        event.preventDefault();
        const box = row.getBoundingClientRect();
        const insertAfter = event.clientY > box.top + box.height / 2;
        participantList.insertBefore(draggedRow, insertAfter ? row.nextSibling : row);
      });
      row.querySelector("[data-move-up]")?.addEventListener("click", () => moveRow(row, -1));
      row.querySelector("[data-move-down]")?.addEventListener("click", () => moveRow(row, 1));
      row.querySelector("[data-participant-check]")?.addEventListener("change", markLiveEditing);
    });

    if (buildingPicker) {
      buildingPicker.addEventListener("change", () => {
        markLiveEditing();
        syncBuilding(true);
      });
      syncBuilding(true);
    } else {
      syncBuilding(false);
    }

    sessionForm.querySelector("[data-participant-select-all]")?.addEventListener("click", () => {
      markLiveEditing();
      activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        if (checkbox) checkbox.checked = true;
      });
      updateOrders();
    });

    sessionForm.querySelector("[data-participant-clear]")?.addEventListener("click", () => {
      markLiveEditing();
      activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        if (checkbox) checkbox.checked = false;
      });
      updateOrders();
    });
  }

  const calendar = document.querySelector("[data-duty-calendar]");
  if (calendar) {
    const selfUserId = calendar.dataset.selfUserId;
    const currentUserId = calendar.dataset.currentUserId;
    const canManage = calendar.dataset.canManage === "true";
    const selfDialog = calendar.querySelector("[data-self-pick-dialog]");
    const managerDialog = calendar.querySelector("[data-manager-pick-dialog]");
    const managerBanner = calendar.querySelector("[data-manager-mode]");
    let managerTarget = null;

    const showDialog = (dialog) => {
      if (!dialog) return;
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    };
    const closeDialog = (dialog) => {
      if (!dialog) return;
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
      applyPendingLiveRefresh();
    };
    const stopManagerMode = () => {
      managerTarget = null;
      calendar.classList.remove("is-manager-selecting");
      if (managerBanner) managerBanner.hidden = true;
      applyPendingLiveRefresh();
    };

    document.querySelectorAll("[data-manager-pick]").forEach((button) => {
      button.addEventListener("click", () => {
        managerTarget = {
          id: button.dataset.userId,
          name: button.dataset.userName,
        };
        calendar.classList.add("is-manager-selecting");
        if (managerBanner) {
          managerBanner.hidden = false;
          managerBanner.querySelector("[data-manager-name]").textContent = managerTarget.name;
        }
        calendar.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    calendar.querySelector("[data-manager-cancel]")?.addEventListener("click", stopManagerMode);

    calendar.querySelectorAll("[data-calendar-day]").forEach((dayButton) => {
      dayButton.addEventListener("click", () => {
        if (dayButton.dataset.full === "true") return;
        const assignedIds = dayButton.dataset.assignedUserIds
          ? dayButton.dataset.assignedUserIds.split(",")
          : [];

        if (managerTarget && canManage) {
          if (assignedIds.includes(managerTarget.id)) {
            window.alert(`${managerTarget.name} is already assigned to this date.`);
            return;
          }
          managerDialog.querySelector("[data-manager-pick-user]").value = managerTarget.id;
          managerDialog.querySelector("[data-manager-pick-date]").value = dayButton.dataset.date;
          managerDialog.querySelector("[data-manager-pick-name]").textContent = managerTarget.name;
          managerDialog.querySelector("[data-manager-pick-label]").textContent = dayButton.dataset.dateLabel;
          showDialog(managerDialog);
          return;
        }

        if (selfUserId !== currentUserId) {
          if (canManage) window.alert("Choose Pick for them in the turn order first.");
          return;
        }
        if (dayButton.dataset.selfSelectable !== "true") {
          window.alert("That date is not available in the current selection phase, or you are already assigned to it.");
          return;
        }
        selfDialog.querySelector("[data-self-pick-date]").value = dayButton.dataset.date;
        selfDialog.querySelector("[data-self-pick-label]").textContent = dayButton.dataset.dateLabel;
        showDialog(selfDialog);
      });
    });

    calendar.querySelectorAll("[data-dialog-close]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(button.closest("dialog")));
    });
    calendar.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) closeDialog(dialog);
      });
      dialog.addEventListener("close", applyPendingLiveRefresh);
    });
    managerDialog?.querySelector("form")?.addEventListener("submit", stopManagerMode);
  }

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (target instanceof HTMLElement && target.closest("form") && target.getAttribute("type") !== "hidden") {
      markLiveEditing();
    }
  });
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target instanceof HTMLElement && target.closest("form") && target.getAttribute("type") !== "hidden") {
      markLiveEditing();
    }
  });

  const liveRegion = document.querySelector("[data-live-refresh]");
  if (liveRegion) {
    const liveStateUrl = liveRegion.dataset.liveStateUrl;
    const liveEventsUrl = liveRegion.dataset.liveEventsUrl;
    let liveVersion = liveRegion.dataset.liveVersion || "";
    let fallbackTimer = null;
    let fallbackInFlight = false;
    let eventSource = null;
    let errorCheckTimer = null;

    const liveRefreshBlocked = () => Boolean(
      document.hidden
      || liveEditing
      || liveDragging
      || document.querySelector("dialog[open]")
      || document.querySelector(".is-manager-selecting")
    );

    const requestLiveRefresh = (version, force = false) => {
      if (!force && (!version || version === liveVersion)) return;
      const target = force ? "__reload__" : version;
      if (liveRefreshBlocked()) {
        pendingLiveRefresh = target;
        return;
      }
      window.location.reload();
    };

    applyPendingLiveRefresh = () => {
      if (pendingLiveRefresh && !liveRefreshBlocked()) {
        window.location.reload();
      }
    };

    const readVersionEvent = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (!payload || typeof payload.version !== "string") return;
        if (!liveVersion) {
          liveVersion = payload.version;
          return;
        }
        requestLiveRefresh(payload.version);
      } catch (_error) {
        // Ignore malformed or incomplete event payloads and wait for reconnect.
      }
    };

    const checkLiveState = async () => {
      if (!liveStateUrl || fallbackInFlight || document.hidden) return;
      fallbackInFlight = true;
      try {
        const response = await window.fetch(liveStateUrl, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (response.status === 401 || response.status === 403 || response.redirected) {
          requestLiveRefresh("", true);
          return;
        }
        if (!response.ok) return;
        const payload = await response.json();
        if (!payload || typeof payload.version !== "string") return;
        if (!liveVersion) liveVersion = payload.version;
        else requestLiveRefresh(payload.version);
      } catch (_error) {
        // Network loss should not interrupt a pick or form in progress.
      } finally {
        fallbackInFlight = false;
      }
    };

    // This named alias is the compatibility-only poll used when EventSource is
    // unavailable. Supported browsers use the push stream below instead.
    const pollLiveState = checkLiveState;

    const startFallbackPolling = () => {
      if (fallbackTimer || !liveStateUrl) return;
      pollLiveState();
      fallbackTimer = window.setInterval(pollLiveState, 10000);
    };

    if (liveEventsUrl && "EventSource" in window) {
      eventSource = new window.EventSource(liveEventsUrl, { withCredentials: true });
      eventSource.addEventListener("state", readVersionEvent);
      eventSource.addEventListener("update", readVersionEvent);
      eventSource.addEventListener("reload", () => requestLiveRefresh("", true));
      eventSource.addEventListener("error", () => {
        // EventSource reconnects automatically. A delayed one-shot state check
        // detects an expired login or access change without returning to polling.
        if (!errorCheckTimer) {
          errorCheckTimer = window.setTimeout(() => {
            errorCheckTimer = null;
            checkLiveState();
          }, 5000);
        }
      });
    } else {
      // Older browsers without EventSource retain a slow compatibility poll.
      startFallbackPolling();
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        applyPendingLiveRefresh();
        if (!eventSource) checkLiveState();
      }
    });
    window.addEventListener("pageshow", () => {
      applyPendingLiveRefresh();
      if (!eventSource) checkLiveState();
    });
    window.addEventListener("beforeunload", () => {
      eventSource?.close();
      if (fallbackTimer) window.clearInterval(fallbackTimer);
      if (errorCheckTimer) window.clearTimeout(errorCheckTimer);
    });
  }

  document.querySelectorAll("[data-confirm]").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });
})();
