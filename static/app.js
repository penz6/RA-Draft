(() => {
  "use strict";

  const confirmButtons = document.querySelectorAll("[data-confirm]");
  for (const button of confirmButtons) {
    button.addEventListener("click", (event) => {
      const message = button.getAttribute("data-confirm") || "Are you sure?";
      if (!window.confirm(message)) {
        event.preventDefault();
      }
    });
  }

  const helpDialog = document.querySelector("[data-help-dialog]");
  const helpOpeners = document.querySelectorAll("[data-help-open]");
  const helpClosers = document.querySelectorAll("[data-help-close]");

  const openHelp = () => {
    if (helpDialog && typeof helpDialog.showModal === "function") {
      helpDialog.showModal();
    }
  };

  const closeHelp = () => {
    if (helpDialog && typeof helpDialog.close === "function") {
      helpDialog.close();
    }
  };

  for (const opener of helpOpeners) {
    opener.addEventListener("click", openHelp);
  }

  for (const closer of helpClosers) {
    closer.addEventListener("click", closeHelp);
  }

  if (helpDialog) {
    helpDialog.addEventListener("click", (event) => {
      if (event.target === helpDialog) {
        closeHelp();
      }
    });
  }

  if (document.body.dataset.autoOpenHelp === "true") {
    openHelp();
  }
})();
