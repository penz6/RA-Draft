(function () {
  "use strict";

  const partnerSelect = document.getElementById("swap-partner-select");
  const form = document.getElementById("swap-request-form");
  const submitBtn = document.getElementById("swap-submit-btn");
  const dataContainer = document.getElementById("swap-partner-data");

  if (!partnerSelect || !form || !submitBtn || !dataContainer) return;

  const picksByUser = {};
  dataContainer.querySelectorAll("[data-swap-partner-pick]").forEach(function (item) {
    const userId = item.dataset.userId;
    if (!userId) return;
    if (!picksByUser[userId]) picksByUser[userId] = [];
    picksByUser[userId].push({
      id: item.dataset.assignmentId,
      date: item.dataset.dateLabel,
      rawDate: item.dataset.rawDate,
    });
  });

  Object.values(picksByUser).forEach(function (picks) {
    picks.sort(function (a, b) {
      return (a.rawDate || "").localeCompare(b.rawDate || "");
    });
  });

  function updateSubmitState() {
    const partnerId = partnerSelect.value;
    let anyChecked = false;
    let allValid = true;

    document.querySelectorAll("[data-swap-row]").forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (check.checked) {
        anyChecked = true;
        if (!select.value) allValid = false;
      }
    });

    submitBtn.disabled = !partnerId || !anyChecked || !allValid;
  }

  function updateTargetSelects() {
    const partnerId = partnerSelect.value;
    const available = picksByUser[partnerId] || [];

    document.querySelectorAll("[data-swap-row]").forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      select.replaceChildren();

      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = available.length ? "-- Select --" : "No shifts available";
      select.appendChild(placeholder);

      available.forEach(function (pick) {
        const opt = document.createElement("option");
        opt.value = pick.id;
        opt.textContent = pick.date;
        select.appendChild(opt);
      });

      select.disabled = !check.checked || !partnerId || available.length === 0;
    });

    updateSubmitState();
  }

  partnerSelect.addEventListener("change", updateTargetSelects);

  document.addEventListener("change", function (event) {
    if (event.target.matches(".swap-include-check")) {
      const row = event.target.closest("[data-swap-row]");
      const select = row.querySelector(".swap-target-select");
      const partnerId = partnerSelect.value;
      const available = picksByUser[partnerId] || [];
      select.disabled = !event.target.checked || !partnerId || available.length === 0;
      if (!event.target.checked) select.value = "";
      updateSubmitState();
    }

    if (event.target.matches(".swap-target-select")) {
      updateSubmitState();
    }
  });

  form.addEventListener("submit", function (event) {
    form.querySelectorAll("input[name='my_assignment_ids'], input[name='target_assignment_ids']")
      .forEach(function (input) { input.remove(); });

    let count = 0;
    document.querySelectorAll("[data-swap-row]").forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (!check.checked || !select.value) return;

      const myInput = document.createElement("input");
      myInput.type = "hidden";
      myInput.name = "my_assignment_ids";
      myInput.value = check.dataset.myAssignmentId;
      form.appendChild(myInput);

      const targetInput = document.createElement("input");
      targetInput.type = "hidden";
      targetInput.name = "target_assignment_ids";
      targetInput.value = select.value;
      form.appendChild(targetInput);
      count += 1;
    });

    if (count === 0) {
      event.preventDefault();
      window.alert("Select at least one date pair to swap.");
    }
  });
})();
