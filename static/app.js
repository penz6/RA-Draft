(() => {
  "use strict";

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

    if (buildingPicker) {
      buildingPicker.addEventListener("change", () => syncBuilding(true));
      syncBuilding(true);
    } else {
      syncBuilding(false);
    }

    sessionForm.querySelector("[data-participant-select-all]")?.addEventListener("click", () => {
      activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        if (checkbox) checkbox.checked = true;
      });
      updateOrders();
    });

    sessionForm.querySelector("[data-participant-clear]")?.addEventListener("click", () => {
      activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        if (checkbox) checkbox.checked = false;
      });
      updateOrders();
    });
  }

  document.querySelectorAll("[data-confirm]").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });
})();
