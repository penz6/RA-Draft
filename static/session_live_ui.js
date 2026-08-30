(function () {
  function initModals() {
    const dialogs = document.querySelectorAll("dialog");
    dialogs.forEach((d) => {
      d.querySelectorAll("[data-close-dialog]").forEach((btn) => {
        btn.addEventListener("click", () => d.close());
      });
    });

    const helpBtn = document.querySelector("[data-open-role-help]");
    const helpDialog = document.querySelector("#roleHelpDialog");
    if (helpBtn && helpDialog) {
      helpBtn.addEventListener("click", () => helpDialog.showModal());
    }

    const swapBtn = document.querySelector("[data-open-swap-dialog]");
    const swapDialog = document.querySelector("#swapRequestDialog");
    if (swapBtn && swapDialog) {
      swapBtn.addEventListener("click", () => swapDialog.showModal());
    }
  }

  function initCalendarInteractions() {
    const pickForm = document.querySelector("#selfPickForm");
    const pickInput = document.querySelector("#selfPickDateInput");

    document.querySelectorAll(".calendar-day:not(:disabled)").forEach((btn) => {
      btn.addEventListener("click", function () {
        const date = this.getAttribute("data-date");
        if (!date) return;

        if (pickInput && pickForm) {
          pickInput.value = date;
          const confirmMsg = `Confirm picking duty for ${date}?`;
          if (window.confirm(confirmMsg)) {
            pickForm.submit();
          }
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initModals();
      initCalendarInteractions();
    });
  } else {
    initModals();
    initCalendarInteractions();
  }

  window.addEventListener("ra:fragment_updated", function () {
    initModals();
    initCalendarInteractions();
  });
})();
