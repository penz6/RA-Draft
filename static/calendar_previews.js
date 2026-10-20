(() => {
  "use strict";
  const selector = "[data-calendar-preview]";
  const panel = document.createElement("div");
  panel.id = "calendar-day-details";
  panel.className = "calendar-detail-popover";
  panel.setAttribute("popover", "manual");
  panel.setAttribute("role", "region");
  panel.setAttribute("aria-label", "Calendar day details");
  panel.hidden = true;
  document.body.append(panel);
  let current = null;
  let pinned = false;
  let hideTimer;
  let restoringFocus = false;

  const hide = () => {
    clearTimeout(hideTimer);
    if (panel.matches(":popover-open")) panel.hidePopover();
    panel.hidden = true;
    current?.removeAttribute("aria-describedby");
    current?.removeAttribute("aria-expanded");
    current = null;
    pinned = false;
  };
  const closeSoon = () => {
    if (!pinned) hideTimer = setTimeout(hide, 180);
  };
  const position = () => {
    if (!current || panel.hidden) return;
    const rect = current.getBoundingClientRect();
    const box = panel.getBoundingClientRect();
    panel.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - box.width - 12))}px`;
    const below = rect.bottom + 6;
    panel.style.top = `${Math.max(12, below + box.height <= innerHeight - 12 ? below : rect.top - box.height - 6)}px`;
  };
  const show = (target, pin = false) => {
    clearTimeout(hideTimer);
    if (current === target && !panel.hidden) {
      pinned = pinned || pin;
      if (pin) panel.querySelector("button")?.focus({preventScroll: true});
      return;
    }
    const template = target.querySelector("template[data-calendar-details]");
    if (!template) return;
    hide();
    current = target;
    pinned = pin;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "calendar-detail-close";
    close.textContent = "×";
    close.setAttribute("aria-label", "Close day details");
    close.addEventListener("click", () => {
      const previous = current;
      hide();
      restoringFocus = true;
      previous?.focus({preventScroll: true});
      restoringFocus = false;
    });
    const details = template.content.cloneNode(true);
    const query = document.querySelector("input[data-staff-search]")?.value.trim().toLowerCase();
    if (query) {
      details.querySelectorAll("[data-staff-search]").forEach(item => {
        if (!item.dataset.staffSearch.toLowerCase().includes(query)) item.remove();
      });
    }
    panel.replaceChildren(close, details);
    panel.hidden = false;
    if (typeof panel.showPopover === "function") panel.showPopover();
    target.setAttribute("aria-describedby", panel.id);
    if (target.hasAttribute("data-calendar-readonly")) target.setAttribute("aria-expanded", "true");
    position();
    if (pin) close.focus({preventScroll: true});
  };

  document.addEventListener("pointerover", event => {
    if (panel.contains(event.target)) { clearTimeout(hideTimer); return; }
    const target = event.target.closest(selector);
    if (target && !target.contains(event.relatedTarget) && !pinned) show(target);
  });
  document.addEventListener("pointerout", event => {
    const target = event.target.closest(selector);
    if ((target && !target.contains(event.relatedTarget)) || (panel.contains(event.target) && !panel.contains(event.relatedTarget))) closeSoon();
  });
  document.addEventListener("focusin", event => {
    if (restoringFocus) return;
    if (panel.contains(event.target)) { clearTimeout(hideTimer); return; }
    const target = event.target.closest(selector);
    if (target) show(target);
  });
  document.addEventListener("focusout", event => {
    if (!panel.contains(event.relatedTarget) && !current?.contains(event.relatedTarget)) closeSoon();
  });
  document.addEventListener("click", event => {
    const target = event.target.closest("[data-calendar-readonly]");
    if (target) show(target, true);
    else if (!panel.contains(event.target)) hide();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const previous = current;
      const restore = panel.contains(document.activeElement);
      hide();
      if (restore) {
        restoringFocus = true;
        previous?.focus({preventScroll: true});
        restoringFocus = false;
      }
    }
    const target = event.target.closest("[data-calendar-readonly]");
    if (target && (event.key === "Enter" || event.key === " ") && target.tagName !== "BUTTON") {
      event.preventDefault();
      show(target, true);
    }
  });
  window.addEventListener("resize", hide);
  document.addEventListener("scroll", event => {
    if (!panel.contains(event.target) && current) {
      const rect = current.getBoundingClientRect();
      if (!current.isConnected || rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) hide();
      else position();
    }
  }, true);
})();
