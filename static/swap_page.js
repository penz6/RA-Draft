(function () {
  "use strict";

  const partnerSelect = document.getElementById("swap-partner-select");
  const form = document.getElementById("swap-request-form");
  const submitBtn = document.getElementById("swap-submit-btn");
  const dataContainer = document.getElementById("swap-partner-data");
  const summaryTitle = document.querySelector("[data-swap-summary-title]");
  const summaryCopy = document.querySelector("[data-swap-summary-copy]");

  if (!partnerSelect || !form || !submitBtn || !dataContainer) return;

  const rows = Array.from(document.querySelectorAll("[data-swap-row]"));
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

  function partnerName() {
    const option = partnerSelect.options[partnerSelect.selectedIndex];
    return option && partnerSelect.value ? option.textContent.trim() : "";
  }

  function updateSummary(partnerId, checkedCount, validCount, allValid) {
    if (!summaryTitle || !summaryCopy) return;

    const available = picksByUser[partnerId] || [];
    const name = partnerName();

    if (!partnerId) {
      summaryTitle.textContent = "Choose a swap partner to begin";
      summaryCopy.textContent = "Then select one or more of your shifts and choose what you want in return.";
      return;
    }

    if (available.length === 0) {
      summaryTitle.textContent = name + " has no shifts available to trade";
      summaryCopy.textContent = "Choose a different RA to build a swap request.";
      return;
    }

    if (checkedCount === 0) {
      summaryTitle.textContent = "Trading with " + name;
      summaryCopy.textContent = "Select at least one of your shifts below.";
      return;
    }

    if (!allValid) {
      summaryTitle.textContent = checkedCount + " shift" + (checkedCount === 1 ? "" : "s") + " selected";
      summaryCopy.textContent = "Choose a " + name + " shift for every selected row before sending.";
      return;
    }

    summaryTitle.textContent = "Ready to request " + validCount + " swap" + (validCount === 1 ? "" : "s") + " with " + name;
    summaryCopy.textContent = name + " will review the exact dates first, then the HRA must give final approval.";
  }

  function updateSubmitState() {
    const partnerId = partnerSelect.value;
    let checkedCount = 0;
    let validCount = 0;
    let allValid = true;

    rows.forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      const selected = Boolean(check && check.checked);

      row.classList.toggle("is-selected", selected);

      if (!selected) return;
      checkedCount += 1;
      if (select && select.value) {
        validCount += 1;
      } else {
        allValid = false;
      }
    });

    const ready = Boolean(partnerId) && checkedCount > 0 && allValid;
    submitBtn.disabled = !ready;
    updateSummary(partnerId, checkedCount, validCount, allValid);
  }

  function updateTargetSelects() {
    const partnerId = partnerSelect.value;
    const available = picksByUser[partnerId] || [];

    rows.forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (!check || !select) return;

      select.replaceChildren();

      const placeholder = document.createElement("option");
      placeholder.value = "";
      if (!partnerId) {
        placeholder.textContent = "Choose a partner first";
      } else if (available.length === 0) {
        placeholder.textContent = "No shifts available";
      } else {
        placeholder.textContent = "Choose their shift";
      }
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
      const select = row ? row.querySelector(".swap-target-select") : null;
      const partnerId = partnerSelect.value;
      const available = picksByUser[partnerId] || [];

      if (select) {
        select.disabled = !event.target.checked || !partnerId || available.length === 0;
        if (!event.target.checked) select.value = "";
      }
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
    rows.forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (!check || !select || !check.checked || !select.value) return;

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
      window.alert("Select at least one complete shift pair to swap.");
    }
  });

  updateTargetSelects();
})();
