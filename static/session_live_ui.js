(() => {
  "use strict";

  let managerTarget = null;

  const patchedCalendar = () => document.querySelector(
    '[data-duty-calendar][data-live-patched="true"]'
  );

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
    const calendar = patchedCalendar();
    if (!calendar) return;
    calendar.classList.remove("is-manager-selecting");
    const banner = calendar.querySelector("[data-manager-mode]");
    if (banner) banner.hidden = true;
  };

  document.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    if (!target) return;

    const confirmButton = target.closest("[data-confirm]");
    if (confirmButton && inPatchedRegion(confirmButton)) {
      if (!window.confirm(confirmButton.dataset.confirm || "Continue?")) {
        event.preventDefault();
        return;
      }
    }

    const managerPick = target.closest("[data-manager-pick]");
    if (managerPick && inPatchedRegion(managerPick)) {
      const calendar = patchedCalendar();
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

    const calendar = target.closest('[data-duty-calendar][data-live-patched="true"]');
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

  document.addEventListener("submit", (event) => {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (form?.closest('[data-duty-calendar][data-live-patched="true"]')) {
      stopManagerMode();
    }
  }, true);

  window.RADraftSessionUI = {
    resetAfterLivePatch: stopManagerMode,
  };
})();
