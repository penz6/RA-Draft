(() => {
  "use strict";

  let managerTarget = null;

  const activeCalendar = () => document.querySelector("[data-duty-calendar]");

  const inPatchedRegion = (element) => Boolean(
    element?.closest?.('[data-live-patched="true"]')
  );

  const showDialog = (dialog) => {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  };

  const closeDialog = (dialog) => {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  };

  const stopManagerMode = () => {
    managerTarget = null;
    const calendar = activeCalendar();
    if (!calendar) return;
    calendar.classList.remove("is-manager-selecting");
    const banner = calendar.querySelector("[data-manager-mode]");
    if (banner) banner.hidden = true;
  };

  const refreshAuthoritativeState = async () => {
    const live = window.RADraftLiveSession;
    if (!live?.supportsPartial || typeof live.refreshNow !== "function") return false;
    const refreshed = await live.refreshNow();
    if (!refreshed) live.reload?.();
    return refreshed;
  };

  const parseActionPayload = async (response) => {
    const contentType = response.headers.get("Content-Type") || "";
    if (!contentType.includes("application/json")) return null;
    try {
      return await response.json();
    } catch (_error) {
      return null;
    }
  };

  document.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    if (!target) return;

    const swapTrigger = target.closest("[data-swap-trigger]");
    if (swapTrigger) {
      const dialog = document.querySelector("[data-swap-dialog]");
      if (!dialog) return;
      const myIdInput = dialog.querySelector("[data-swap-my-id]");
      const myDateLabel = dialog.querySelector("[data-swap-my-date]");
      if (myIdInput) myIdInput.value = swapTrigger.dataset.assignmentId;
      if (myDateLabel) myDateLabel.textContent = swapTrigger.dataset.dutyDate;
      showDialog(dialog);
      return;
    }

    const managerPick = target.closest("[data-manager-pick]");
    if (managerPick) {
      const calendar = activeCalendar();
      if (!calendar) return;
      managerTarget = {
        id: managerPick.dataset.userId,
        name: managerPick.dataset.userName,
      };
      calendar.classList.add("is-manager-selecting");
      const banner = calendar.querySelector("[data-manager-mode]");
      if (banner) {
        banner.hidden = false;
        const name = banner.querySelector("[data-manager-name]");
        if (name) name.textContent = managerTarget.name;
      }
      calendar.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const calendar = target.closest("[data-duty-calendar]");
    if (!calendar) return;

    const cancelManager = target.closest("[data-manager-cancel]");
    if (cancelManager) {
      stopManagerMode();
      return;
    }

    const closeButton = target.closest("[data-dialog-close]");
    if (closeButton) {
      closeDialog(closeButton.closest("dialog"));
      return;
    }

    if (target.matches("dialog") && target.open) {
      closeDialog(target);
      return;
    }

    const dayButton = target.closest("[data-calendar-day]");
    if (!dayButton || dayButton.disabled || dayButton.dataset.full === "true") return;

    const assignedIds = dayButton.dataset.assignedUserIds
      ? dayButton.dataset.assignedUserIds.split(",")
      : [];
    const canManage = calendar.dataset.canManage === "true";

    if (managerTarget && canManage) {
      if (assignedIds.includes(managerTarget.id)) {
        window.alert(`${managerTarget.name} is already assigned to this date.`);
        return;
      }
      const managerDialog = calendar.querySelector("[data-manager-pick-dialog]");
      if (!managerDialog) return;
      managerDialog.querySelector("[data-manager-pick-user]").value = managerTarget.id;
      managerDialog.querySelector("[data-manager-pick-date]").value = dayButton.dataset.date;
      managerDialog.querySelector("[data-manager-pick-name]").textContent = managerTarget.name;
      managerDialog.querySelector("[data-manager-pick-label]").textContent = dayButton.dataset.dateLabel;
      showDialog(managerDialog);
      return;
    }

    if (calendar.dataset.selfUserId !== calendar.dataset.currentUserId) {
      if (canManage) window.alert("Choose Pick for them in the turn order first.");
      return;
    }
    if (dayButton.dataset.selfSelectable !== "true") {
      window.alert(
        "That date is not available in the current selection phase, or you are already assigned to it."
      );
      return;
    }

    const selfDialog = calendar.querySelector("[data-self-pick-dialog]");
    if (!selfDialog) return;
    selfDialog.querySelector("[data-self-pick-date]").value = dayButton.dataset.date;
    selfDialog.querySelector("[data-self-pick-label]").textContent = dayButton.dataset.dateLabel;
    showDialog(selfDialog);
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (!form?.matches("[data-live-pick-form],[data-live-action-form]")) return;

    const live = window.RADraftLiveSession;
    if (!live?.supportsPartial || typeof window.fetch !== "function") {
      // Progressive enhancement: without the live client, the real form keeps
      // the existing server-side POST/redirect behavior.
      return;
    }

    event.preventDefault();
    if (form.dataset.submitting === "true") return;
    form.dataset.submitting = "true";

    const submitButton = form.querySelector('button[type="submit"]');
    const originalLabel = submitButton?.textContent || "";
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Saving...";
    }

    const dialog = form.closest("dialog");
    try {
      const response = await window.fetch(form.action, {
        method: "POST",
        body: new window.FormData(form),
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "X-RA-Draft-Async": "1",
        },
      });

      if (response.redirected || response.status === 401 || response.status === 403) {
        live.reload?.();
        return;
      }

      const payload = await parseActionPayload(response);
      closeDialog(dialog);
      stopManagerMode();

      if (!response.ok || !payload?.ok) {
        await refreshAuthoritativeState();
        window.alert(payload?.message || "That pick could not be completed. The schedule was refreshed.");
        return;
      }

      // Do not wait for our own SSE event. Pull the authoritative post-commit
      // fragments immediately so the picker sees the same update as observers.
      await refreshAuthoritativeState();
    } catch (_error) {
      closeDialog(dialog);
      stopManagerMode();
      const refreshed = await refreshAuthoritativeState();
      if (refreshed) {
        window.alert(
          "The connection was interrupted. The schedule was refreshed before another pick can be attempted."
        );
      }
    } finally {
      delete form.dataset.submitting;
      if (submitButton?.isConnected) {
        submitButton.disabled = false;
        submitButton.textContent = originalLabel;
      }
    }
  }, true);

  window.RADraftSessionUI = {
    resetAfterLivePatch: stopManagerMode,
  };
})();