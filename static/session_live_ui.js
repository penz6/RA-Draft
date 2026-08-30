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
    } catch (_error) {\n      return null;\n    }\n  };\n\n  document.addEventListener("click", (event) => {\n    const target = event.target instanceof HTMLElement ? event.target : null;\n    if (!target) return;\n\n    const swapTrigger = target.closest("[data-swap-trigger]");\n    if (swapTrigger) {\n      const dialog = document.querySelector("[data-swap-dialog]");\n      if (!dialog) return;\n      const myIdInput = dialog.querySelector("[data-swap-my-id]");\n      const myDateLabel = dialog.querySelector("[data-swap-my-date]");\n      if (myIdInput) myIdInput.value = swapTrigger.dataset.assignmentId;\n      if (myDateLabel) myDateLabel.textContent = swapTrigger.dataset.dutyDate;\n      showDialog(dialog);\n      return;\n    }\n\n    const managerPick = target.closest("[data-manager-pick]");\n    if (managerPick) {\n      const calendar = activeCalendar();\n      if (!calendar) return;\n      managerTarget = {\n        id: managerPick.dataset.userId,\n        name: managerPick.dataset.userName,\n      };\n      calendar.classList.add("is-manager-selecting");\n      const banner = calendar.querySelector("[data-manager-mode]");\n      if (banner) {\n        banner.hidden = false;\n        const name = banner.querySelector("[data-manager-name]");\n        if (name) name.textContent = managerTarget.name;\n      }\n      calendar.scrollIntoView({ behavior: "smooth", block: "start" });\n      return;\n    }\n\n    const calendar = target.closest("[data-duty-calendar]");\n    if (!calendar) return;\n\n    const cancelManager = target.closest("[data-manager-cancel]");\n    if (cancelManager) {\n      stopManagerMode();\n      return;\n    }\n\n    const closeButton = target.closest("[data-dialog-close]");\n    if (closeButton) {\n      closeDialog(closeButton.closest("dialog"));\n      return;\n    }\n\n    if (target.matches("dialog") && target.open) {\n      closeDialog(target);\n      return;\n    }\n\n    const dayButton = target.closest("[data-calendar-day]");\n    if (!dayButton || dayButton.disabled || dayButton.dataset.full === "true") return;\n\n    const assignedIds = dayButton.dataset.assignedUserIds\n      ? dayButton.dataset.assignedUserIds.split(",")\n      : [];\n    const canManage = calendar.dataset.canManage === "true";\n\n    if (managerTarget && canManage) {\n      if (assignedIds.includes(managerTarget.id)) {\n        window.alert(`${managerTarget.name} is already assigned to this date.`);\n        return;\n      }\n      const managerDialog = calendar.querySelector("[data-manager-pick-dialog]");\n      if (!managerDialog) return;\n      managerDialog.querySelector("[data-manager-pick-user]").value = managerTarget.id;\n      managerDialog.querySelector("[data-manager-pick-date]").value = dayButton.dataset.date;\n      managerDialog.querySelector("[data-manager-pick-name]").textContent = managerTarget.name;\n      managerDialog.querySelector("[data-manager-pick-label]").textContent = dayButton.dataset.dateLabel;\n      showDialog(managerDialog);\n      return;\n    }\n\n    if (calendar.dataset.selfUserId !== calendar.dataset.currentUserId) {\n      if (canManage) window.alert("Choose Pick for them in the turn order first.");\n      return;\n    }\n    if (dayButton.dataset.selfSelectable !== "true") {\n      window.alert(\n        "That date is not available in the current selection phase, or you are already assigned to it."\n      );\n      return;\n    }\n\n    const selfDialog = calendar.querySelector("[data-self-pick-dialog]");\n    if (!selfDialog) return;\n    selfDialog.querySelector("[data-self-pick-date]").value = dayButton.dataset.date;\n    selfDialog.querySelector("[data-self-pick-label]").textContent = dayButton.dataset.dateLabel;\n    showDialog(selfDialog);\n  });\n\n  document.addEventListener("submit", async (event) => {\n    const form = event.target instanceof HTMLFormElement ? event.target : null;\n    if (!form?.matches("[data-live-pick-form],[data-live-action-form]")) return;\n\n    const live = window.RADraftLiveSession;\n    if (!live?.supportsPartial || typeof window.fetch !== "function") {\n      // Progressive enhancement: without the live client, the real form keeps\n      // the existing server-side POST/redirect behavior.\n      return;\n    }\n\n    event.preventDefault();\n    if (form.dataset.submitting === "true") return;\n    form.dataset.submitting = "true";\n\n    const submitButton = form.querySelector('button[type="submit"]');\n    const originalLabel = submitButton?.textContent || "";\n    if (submitButton) {\n      submitButton.disabled = true;\n      submitButton.textContent = "Saving...";\n    }\n\n    const dialog = form.closest("dialog");\n    try {\n      const response = await window.fetch(form.action, {\n        method: "POST",\n        body: new window.FormData(form),\n        credentials: "same-origin",\n        cache: "no-store",\n        headers: {\n          Accept: "application/json",\n          "X-RA-Draft-Async": "1",\n        },\n      });\n\n      if (response.redirected || response.status === 401 || response.status === 403) {\n        live.reload?.();\n        return;\n      }\n\n      const payload = await parseActionPayload(response);\n      closeDialog(dialog);\n      stopManagerMode();\n\n      if (!response.ok || !payload?.ok) {\n        await refreshAuthoritativeState();\n        window.alert(payload?.message || "That pick could not be completed. The schedule was refreshed.");\n        return;\n      }\n\n      // Do not wait for our own SSE event. Pull the authoritative post-commit\n      // fragments immediately so the picker sees the same update as observers.\n      await refreshAuthoritativeState();\n    } catch (_error) {\n      closeDialog(dialog);\n      stopManagerMode();\n      const refreshed = await refreshAuthoritativeState();\n      if (refreshed) {\n        window.alert(\n          "The connection was interrupted. The schedule was refreshed before another pick can be attempted."\n        );\n      }\n    } finally {\n      delete form.dataset.submitting;\n      if (submitButton?.isConnected) {\n        submitButton.disabled = false;\n        submitButton.textContent = originalLabel;\n      }\n    }\n  }, true);\n\n  window.RADraftSessionUI = {\n    resetAfterLivePatch: stopManagerMode,\n  };\n})();