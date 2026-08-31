(function () {
  "use strict";

  function schoolTodayIso() {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date());
    const values = {};
    parts.forEach(function (part) {
      if (part.type !== "literal") values[part.type] = part.value;
    });
    return values.year + "-" + values.month + "-" + values.day;
  }

  const today = schoolTodayIso();
  const isPast = function (rawDate) {
    return Boolean(rawDate && rawDate < today);
  };

  // Manager dropdowns use the same school-date rule as the server. Removing
  // these choices is only a convenience; the backend independently rejects
  // any forged request involving an elapsed duty date.
  document.querySelectorAll(".manager-swap-card option[data-duty-date]").forEach(function (option) {
    if (isPast(option.dataset.dutyDate)) option.remove();
  });

  const partnerSelect = document.getElementById("swap-partner-select");
  const form = document.getElementById("swap-request-form");
  const submitBtn = document.getElementById("swap-submit-btn");
  const dataContainer = document.getElementById("swap-partner-data");
  const summaryTitle = document.querySelector("[data-swap-summary-title]");
  const summaryCopy = document.querySelector("[data-swap-summary-copy]");

  if (!partnerSelect || !form || !submitBtn || !dataContainer) return;

  const allRows = Array.from(document.querySelectorAll("[data-swap-row]"));
  const rows = allRows.filter(function (row) {
    const past = isPast(row.dataset.myRawDate || "");
    if (past) {
      row.hidden = true;
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (check) {
        check.checked = false;
        check.disabled = true;
      }
      if (select) {
        select.value = "";
        select.disabled = true;
      }
    }
    return !past;
  });

  const myDates = new Set(
    rows.map(function (row) { return row.dataset.myRawDate; }).filter(Boolean)
  );
  const picksByUser = {};

  dataContainer.querySelectorAll("[data-swap-partner-pick]").forEach(function (item) {
    const userId = item.dataset.userId;
    const rawDate = item.dataset.rawDate || "";
    if (!userId || !rawDate || isPast(rawDate)) return;
    if (!picksByUser[userId]) picksByUser[userId] = [];
    picksByUser[userId].push({
      id: item.dataset.assignmentId,
      date: item.dataset.dateLabel,
      rawDate: rawDate,
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

  function partnerAlreadyWorksDate(available, rawDate) {
    return available.some(function (pick) {
      return pick.rawDate === rawDate;
    });
  }

  function rowBlockedByPartner(row, available) {
    const rawDate = row.dataset.myRawDate || "";
    return Boolean(rawDate && partnerAlreadyWorksDate(available, rawDate));
  }

  function eligiblePartnerPicksForRow(row, partnerId, available) {
    const myRawDate = row.dataset.myRawDate || "";
    if (!myRawDate || rowBlockedByPartner(row, available)) return [];

    const outgoingDates = selectedOutgoingDates();
    const remainingMyDates = new Set();
    myDates.forEach(function (dutyDate) {
      if (!outgoingDates.has(dutyDate)) remainingMyDates.add(dutyDate);
    });

    const usedTargetIds = selectedTargetIds(row);
    return available.filter(function (pick) {
      if (!pick.rawDate || pick.rawDate === myRawDate) return false;
      if (remainingMyDates.has(pick.rawDate)) return false;
      if (usedTargetIds.has(String(pick.id))) return false;
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
      if (partnerDates.has(pair.myRawDate)) return "partner-already-on-date";

      myOutgoingDates.add(pair.myRawDate);
      partnerOutgoingDates.add(pair.targetPick.rawDate);
      myIncomingDates.push(pair.targetPick.rawDate);
      partnerIncomingDates.push(pair.myRawDate);
    }

    const projectedMyDates = [];
    myDates.forEach(function (dutyDate) {
      if (!myOutgoingDates.has(dutyDate)) projectedMyDates.push(dutyDate);
    });
    projectedMyDates.push.apply(projectedMyDates, myIncomingDates);
    if (hasDuplicateDates(projectedMyDates)) return "requester-duplicate";

    const projectedPartnerDates = [];
    partnerDates.forEach(function (dutyDate) {
      if (!partnerOutgoingDates.has(dutyDate)) projectedPartnerDates.push(dutyDate);
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

    if (rows.length === 0) {
      summaryTitle.textContent = "No future duty shifts to swap";
      summaryCopy.textContent = "Past duty shifts cannot be traded.";
      return;
    }

    if (!partnerId) {
      summaryTitle.textContent = "Choose a swap partner to begin";
      summaryCopy.textContent = "Then select one or more of your future shifts and choose what you want in return.";
      return;
    }

    if (available.length === 0) {
      summaryTitle.textContent = name + " has no future shifts available to trade";
      summaryCopy.textContent = "Choose a different RA.";
      return;
    }

    if (checkedCount === 0) {
      summaryTitle.textContent = "Trading with " + name;
      summaryCopy.textContent = "Select one of your available shifts below.";
      return;
    }

    if (!allComplete) {
      if (selectedRowWithoutEligibleChoice()) {
        summaryTitle.textContent = "No eligible return shift for one selected date";
        summaryCopy.textContent = "You already work the dates this RA could give you. Try another shift or another RA.";
      } else {
        summaryTitle.textContent = checkedCount + " shift" + (checkedCount === 1 ? "" : "s") + " selected";
        summaryCopy.textContent = "Choose a " + name + " shift for every selected row before sending.";
      }
      return;
    }

    if (issue) {
      summaryTitle.textContent = "Choose another available shift";
      summaryCopy.textContent = "That combination cannot produce a duplicate-free schedule.";
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

    const partnerBlocksRow = Boolean(partnerId) && rowBlockedByPartner(row, available);
    if (partnerBlocksRow) {
      check.checked = false;
      check.disabled = true;
      row.classList.remove("is-selected");
    } else {
      check.disabled = false;
    }

    const previousValue = select.value;
    const eligible = eligiblePartnerPicksForRow(row, partnerId, available);
    select.replaceChildren();

    const placeholder = document.createElement("option");
    placeholder.value = "";
    if (!partnerId) {
      placeholder.textContent = "Choose a partner first";
    } else if (partnerBlocksRow) {
      placeholder.textContent = partnerName() + " already works this date";
    } else if (available.length === 0) {
      placeholder.textContent = "No future shifts available";
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

    select.disabled = partnerBlocksRow || !check.checked || !partnerId || eligible.length === 0;
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
      const check = row.querySelector(".swap-include-check");
      const select = row.querySelector(".swap-target-select");
      if (check) check.checked = false;
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
