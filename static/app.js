(() => {
  "use strict";

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
    const rows = Array.from(sessionForm.querySelectorAll("[data-participant-row]"));
    const emptyMessage = sessionForm.querySelector("[data-no-participants]");

    const activeRows = () => rows.filter((row) => !row.hidden);

    const updateOrders = () => {
      let position = 1;
      activeRows().forEach((row) => {
        const checkbox = row.querySelector("[data-participant-check]");
        const order = row.querySelector("[data-order-input]");
        if (checkbox && order && checkbox.checked) {
          order.value = String(position);
          position += 1;
        }
      });
    };

    const syncBuilding = (selectVisible) => {
      const selectedBuilding = buildingPicker ? buildingPicker.value : null;
      let visibleCount = 0;
      rows.forEach((row) => {
        const matches = !selectedBuilding || row.dataset.buildingId === selectedBuilding;
        const checkbox = row.querySelector("[data-participant-check]");
        const order = row.querySelector("[data-order-input]");
        row.hidden = !matches;
        if (checkbox) {
          checkbox.disabled = !matches;
          if (!matches) checkbox.checked = false;
          if (matches && selectVisible) checkbox.checked = true;
        }
        if (order) order.disabled = !matches;
        if (matches) visibleCount += 1;
      });
      if (emptyMessage) emptyMessage.hidden = visibleCount !== 0;
      updateOrders();
    };

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

    sessionForm.querySelectorAll("[data-participant-check]").forEach((checkbox) => {
      checkbox.addEventListener("change", updateOrders);
    });
  }

  const dateFilter = document.querySelector("[data-date-filter]");
  if (dateFilter) {
    const cards = Array.from(document.querySelectorAll("[data-date-card]"));
    dateFilter.addEventListener("input", () => {
      const query = dateFilter.value.trim().toLowerCase();
      cards.forEach((card) => {
        card.hidden = Boolean(query) && !card.dataset.search.includes(query);
      });
    });
  }

  document.querySelectorAll("[data-confirm]").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });
})();
