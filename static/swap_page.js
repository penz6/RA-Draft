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
  const myDates = new Set(
    rows.map(function (row) { return row.dataset.myRawDate; }).filter(Boolean)
  );
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

  function differentDatePartnerPicks(row, available) {
    const myRawDate = row.dataset.myRawDate || "";
    if (!myRawDate) return [];
    return available.filter(function (pick) {
      return pick.rawDate && pick.rawDate !== myRawDate;
    });
  }

  function pickById(available, assignmentId) {
    return available.find(function (pick) {
      return String(pick.id) === String(assignmentId);
    }) || null;
  }

  function hasDuplicateDates(dates) {
    return new Set(dates).size !== dates.length;
  }

  function projectedBatchIssue(partnerId, selectedPairs) {
    const available = picksByUser[partnerId] || [];
    const partnerDates = new Set(
      available.map(function (pick) { return pick.rawDate; }).filter(Boolean)
    );
    const usedTargetIds = new Set();
    const myOutgoingDates = new Set();
    const partnerOutgoingDates = new Set();
    const myIncomingDates = [];
    const partnerIncomingDates = [];

    for (const pair of selectedPairs) {
      if (!pair.targetPick) return "incomplete";
      if (usedTargetIds.has(String(pair.targetPick.id))) return "duplicate-target";
      usedTargetIds.add(String(pair.targetPick.id));

      if (pair.myRawDate === pair.targetPick.rawDate) return "same-date";

      myOutgoingDates.add(pair.myRawDate);
      partnerOutgoingDates.add(pair.targetPick.rawDate);
      myIncomingDates.push(pair.targetPick.rawDate);
      partnerIncomingDates.push(pair.myRawDate);
    }

    const projectedMyDates = [];
    myDates.forEach(function (date) {
      if (!myOutgoingDates.has(date)) projectedMyDates.push(date);
    });
    projectedMyDates.push.apply(projectedMyDates, myIncomingDates);
    if (hasDuplicateDates(projectedMyDates)) return "requester-duplicate";

    const projectedPartnerDates = [];
    partnerDates.forEach(function (date) {
      if (!partnerOutgoingDates.has(date)) projectedPartnerDates.push(date);
    });
    projectedPartnerDates.push.apply(projectedPartnerDates, partnerIncomingDates);
    if (hasDuplicateDates(projectedPartnerDates)) return "partner-duplicate";

    return null;
  }

  function updateSummary(partnerId, checkedCount, validCount, allComplete, issue) {
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

    if (!allComplete) {
      summaryTitle.textContent = checkedCount + " shift" + (checkedCount === 1 ? "" : "s") + " selected";
      summaryCopy.textContent = "Choose a " + name + " shift for every selected row before sending.";
      return;
    }

    if (issue === "duplicate-target") {
      summaryTitle.textContent = "Use each " + name + " shift only once";
      summaryCopy.textContent = "Choose a different return shift for one of the selected rows.";
      return;
    }

    if (issue === "same-date") {
      summaryTitle.textContent = "Those shifts are already on the same date";
      summaryCopy.textContent = "Choose two different duty dates to make a meaningful trade.";
      return;
    }

    if (issue === "requester-duplicate") {
      summaryTitle.textContent = "That combination would duplicate one of your duty dates";
      summaryCopy.textContent = "You can include another shift in the same request if that shift is being traded away, or choose a different return date.";
      return;
    }

    if (issue === "partner-duplicate") {
      summaryTitle.textContent = "That combination would duplicate one of " + name + "'s duty dates";
      summaryCopy.textContent = "Choose a different shift combination so neither RA ends up assigned twice on one date.";
      return;
    }

    summaryTitle.textContent = "Ready to request " + validCount + " swap" + (validCount === 1 ? "" : "s") + " with " + name;
    summaryCopy.textContent = name + " will review the exact dates first, then the HRA must give final approval.";
  }

  function updateSubmitState() {
    const partnerId = partnerSelect.value;
    const available = picksByUser[partnerId] || [];
    let checkedCount = 0;
    let validCount = 0;
    let allComplete = true;
    const selectedPairs = [];

    rows.forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      const selected = Boolean(check && check.checked);

      row.classList.toggle("is-selected", selected);
      if (!selected) return;

      checkedCount += 1;
      const targetPick = select && select.value ? pickById(available, select.value) : null;
      if (!targetPick) {
        allComplete = false;
      } else {
        validCount += 1;
      }
      selectedPairs.push({
        myRawDate: row.dataset.myRawDate || "",
        targetPick: targetPick,
      });
    });

    const issue = allComplete && checkedCount > 0
      ? projectedBatchIssue(partnerId, selectedPairs)
      : null;
    const ready = Boolean(partnerId) && checkedCount > 0 && allComplete && !issue;
    submitBtn.disabled = !ready;
    updateSummary(partnerId, checkedCount, validCount, allComplete, issue);
  }

  function updateTargetSelects() {
    const partnerId = partnerSelect.value;
    const available = picksByUser[partnerId] || [];

    rows.forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (!check || !select) return;

      const eligible = differentDatePartnerPicks(row, available);
      select.replaceChildren();

      const placeholder = document.createElement("option");
      placeholder.value = "";
      if (!partnerId) {
        placeholder.textContent = "Choose a partner first";
      } else if (available.length === 0) {
        placeholder.textContent = "No shifts available";
      } else if (eligible.length === 0) {
        placeholder.textContent = "No different-date shifts";
      } else {
        placeholder.textContent = "Choose their shift";
      }
      select.appendChild(placeholder);

      eligible.forEach(function (pick) {
        const opt = document.createElement("option");
        opt.value = pick.id;
        opt.textContent = pick.date;
        select.appendChild(opt);
      });

      select.disabled = !check.checked || !partnerId || eligible.length === 0;
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
      const eligible = row ? differentDatePartnerPicks(row, available) : [];

      if (select) {
        select.disabled = !event.target.checked || !partnerId || eligible.length === 0;
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

    if (submitBtn.disabled) {
      event.preventDefault();
      return;
    }

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
