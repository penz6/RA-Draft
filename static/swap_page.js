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

  function pickById(available, assignmentId) {
    return available.find(function (pick) {
      return String(pick.id) === String(assignmentId);
    }) || null;
  }

  function hasDuplicateDates(dates) {
    return new Set(dates).size !== dates.length;
  }

  function selectedOutgoingDates() {
    const dates = new Set();
    rows.forEach(function (row) {
      const check = row.querySelector(".swap-include-check");
      if (check && check.checked && row.dataset.myRawDate) {
        dates.add(row.dataset.myRawDate);
      }
    });
    return dates;
  }

  function selectedTargetIds(excludeRow) {
    const ids = new Set();
    rows.forEach(function (row) {
      if (row === excludeRow) return;
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (check && check.checked && select && select.value) {
        ids.add(String(select.value));
      }
    });
    return ids;
  }

  function selectedPartnerOutgoingDates(partnerId, excludeRow) {
    const available = picksByUser[partnerId] || [];
    const dates = new Set();
    rows.forEach(function (row) {
      if (row === excludeRow) return;
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (!check || !check.checked || !select || !select.value) return;
      const pick = pickById(available, select.value);
      if (pick && pick.rawDate) dates.add(pick.rawDate);
    });
    return dates;
  }

  function eligiblePartnerPicksForRow(row, partnerId, available) {
    const myRawDate = row.dataset.myRawDate || "";
    if (!myRawDate) return [];

    const outgoingDates = selectedOutgoingDates();
    const remainingMyDates = new Set();
    myDates.forEach(function (date) {
      if (!outgoingDates.has(date)) remainingMyDates.add(date);
    });

    const usedTargetIds = selectedTargetIds(row);
    const partnerOutgoingDates = selectedPartnerOutgoingDates(partnerId, row);
    const partnerAlreadyWorksMyDate = available.some(function (pick) {
      return pick.rawDate === myRawDate;
    });

    return available.filter(function (pick) {
      if (!pick.rawDate || pick.rawDate === myRawDate) return false;
      if (remainingMyDates.has(pick.rawDate)) return false;
      if (usedTargetIds.has(String(pick.id))) return false;
      if (partnerAlreadyWorksMyDate && !partnerOutgoingDates.has(myRawDate)) {
        return false;
      }
      return true;
    });
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

  function selectedRowWithoutEligibleChoice() {
    return rows.some(function (row) {
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      return Boolean(check && check.checked && select && select.options.length <= 1);
    });
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
      if (selectedRowWithoutEligibleChoice()) {
        summaryTitle.textContent = "No eligible return shift for one selected date";
        summaryCopy.textContent = "Try another shift, or include the conflicting shift in the same request.";
      } else {
        summaryTitle.textContent = checkedCount + " shift" + (checkedCount === 1 ? "" : "s") + " selected";
        summaryCopy.textContent = "Choose a " + name + " shift for every selected row before sending.";
      }
      return;
    }

    if (issue) {
      summaryTitle.textContent = "Choose another available shift";
      summaryCopy.textContent = "The selected combination cannot produce a duplicate-free schedule.";
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

  function rebuildTargetSelect(row, partnerId, available) {
    const check = row.querySelector(".swap-include-check");
    const select = row.querySelector(".swap-target-select");
    if (!check || !select) return false;

    const previousValue = select.value;
    const eligible = eligiblePartnerPicksForRow(row, partnerId, available);
    select.replaceChildren();

    const placeholder = document.createElement("option");
    placeholder.value = "";
    if (!partnerId) {
      placeholder.textContent = "Choose a partner first";
    } else if (available.length === 0) {
      placeholder.textContent = "No shifts available";
    } else if (eligible.length === 0) {
      placeholder.textContent = "No eligible shifts";
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

    if (previousValue && eligible.some(function (pick) {
      return String(pick.id) === String(previousValue);
    })) {
      select.value = previousValue;
    }

    select.disabled = !check.checked || !partnerId || eligible.length === 0;
    return Boolean(previousValue && !select.value);
  }

  function refreshTargetSelects() {
    const partnerId = partnerSelect.value;
    const available = picksByUser[partnerId] || [];

    for (let pass = 0; pass <= rows.length; pass += 1) {
      let clearedChoice = false;
      rows.forEach(function (row) {
        if (rebuildTargetSelect(row, partnerId, available)) {
          clearedChoice = true;
        }
      });
      if (!clearedChoice) break;
    }

    updateSubmitState();
  }

  partnerSelect.addEventListener("change", function () {
    rows.forEach(function (row) {
      const select = row.querySelector(".swap-target-select");
      if (select) select.value = "";
    });
    refreshTargetSelects();
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches(".swap-include-check")) {
      const row = event.target.closest("[data-swap-row]");
      const select = row ? row.querySelector(".swap-target-select") : null;
      if (select && !event.target.checked) select.value = "";
      refreshTargetSelects();
    }

    if (event.target.matches(".swap-target-select")) {
      refreshTargetSelects();
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

  refreshTargetSelects();
})();
