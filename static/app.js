(() => {
  "use strict";

  document.documentElement.classList.add("js");

  const navToggle = document.querySelector("[data-nav-toggle]");
  const primaryNav = document.querySelector("[data-primary-nav]");
  if (navToggle && primaryNav) {
    const setNavOpen = (open) => {
      navToggle.setAttribute("aria-expanded", String(open));
      primaryNav.classList.toggle("is-open", open);
    };

    navToggle.addEventListener("click", () => {
      setNavOpen(navToggle.getAttribute("aria-expanded") !== "true");
    });
    primaryNav.addEventListener("click", (event) => {
      if (event.target.closest("a")) setNavOpen(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && navToggle.getAttribute("aria-expanded") === "true") {
        setNavOpen(false);
        navToggle.focus();
      }
    });
  }

  // Live session transport and compatibility polling (pollLiveState) live in
  // live_stream.js. Keep this file focused on non-live page interactions.

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
    if (helpDialog.dataset.autoOpen === "true") openHelp();
  }

  const setupSortableParticipants = (participantList) => {
    if (!participantList) return null;
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
      if (direction < 0) participantList.insertBefore(row, target);
      else participantList.insertBefore(target, row);
      updateOrders();
      row.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };

    participantList.querySelectorAll("[data-participant-row]").forEach((row) => {
      row.addEventListener("dragstart", (event) => {
        draggedRow = row;
        row.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", "participant");
      });
      row.addEventListener("dragend", () => {
        row.classList.remove("is-dragging");
        draggedRow = null;
        updateOrders();
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
    });

    updateOrders();
    return { activeRows, updateOrders };
  };

  document.querySelectorAll("[data-sortable-participants]").forEach((list) => {
    setupSortableParticipants(list);
  });

  const sessionForm = document.querySelector("[data-session-form]");
  if (sessionForm) {
    const buildingPicker = sessionForm.querySelector("[data-building-picker]");
    const participantList = sessionForm.querySelector("[data-participant-list]");
    const rows = Array.from(sessionForm.querySelectorAll("[data-participant-row]"));
    const emptyMessage = sessionForm.querySelector("[data-no-participants]");
    const sortable = setupSortableParticipants(participantList);
    const startInput = sessionForm.querySelector('[name="start_date"]');
    const endInput = sessionForm.querySelector('[name="end_date"]');
    const overlapWarning = sessionForm.querySelector("[data-session-overlap-warning]");
    const overrideField = sessionForm.querySelector("[data-session-overlap-override]");
    const ranges = JSON.parse(sessionForm.dataset.sessionRanges || "[]");

    const updateOverlapWarning = () => {
      const buildingId = buildingPicker ? buildingPicker.value : (rows[0]?.dataset.buildingId || "");
      const start = startInput?.value;
      const end = endInput?.value;
      const conflicts = start && end ? ranges.filter((item) => (
        String(item.building_id) === String(buildingId)
        && item.start_date <= end
        && item.end_date >= start
      )) : [];
      if (overlapWarning) {
        overlapWarning.hidden = conflicts.length === 0;
        overlapWarning.textContent = conflicts.length
          ? "Warning: these dates are already covered by " + conflicts.map((item) => (
            item.name + " (" + item.start_date + " to " + item.end_date + ")"
          )).join(", ") + "."
          : "";
      }
      if (overrideField) {
        overrideField.hidden = conflicts.length === 0;
        if (!conflicts.length) overrideField.querySelector("input").checked = false;
      }
      return conflicts;
    };

    startInput?.addEventListener("change", updateOverlapWarning);
    endInput?.addEventListener("change", updateOverlapWarning);

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
      sortable?.updateOrders();
    };

    if (buildingPicker) {
      buildingPicker.addEventListener("change", () => {
        syncBuilding(true);
        updateOverlapWarning();
      });
      syncBuilding(true);
    } else {
      syncBuilding(false);
    }

    sessionForm.querySelector("[data-participant-select-all]")?.addEventListener("click", () => {
      sortable?.activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        if (checkbox) checkbox.checked = true;
      });
      sortable?.updateOrders();
    });

    sessionForm.querySelector("[data-participant-clear]")?.addEventListener("click", () => {
      sortable?.activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        if (checkbox) checkbox.checked = false;
      });
      sortable?.updateOrders();
    });

    sessionForm.addEventListener("submit", (event) => {
      const conflicts = updateOverlapWarning();
      if (!conflicts.length) return;
      const override = sessionForm.querySelector('[name="override_overlap"]:checked');
      if (sessionForm.dataset.isAdmin !== "true" || !override) {
        event.preventDefault();
        window.alert(sessionForm.dataset.isAdmin === "true"
          ? "These dates overlap an existing session. Review the warning and check the admin override to continue."
          : "These dates overlap an existing session. Only an admin can override this conflict.");
      }
    });
  }

  // Listen on the document so confirmations also protect controls inserted by
  // live updates. Handling submit (rather than click) also covers keyboard and
  // assistive-technology form submission.
  document.addEventListener("submit", (event) => {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (!form) return;
    const message = event.submitter?.dataset.confirm || form.dataset.confirm;
    if (message && !window.confirm(message)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
})();
