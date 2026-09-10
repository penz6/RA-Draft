// Run with: node tests/test_submit_lifecycle.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const root = new URL("../", import.meta.url);
const base = readFileSync(new URL("templates/base.html", root), "utf8");
const scripts = ["live_stream.js", "app.js"].sort(
  (a, b) => base.indexOf(a) - base.indexOf(b)
);

function submit({ confirmed = true, buttonConfirm, formConfirm, pick = false,
  enhanced = false, canceled = false } = {}) {
  const listeners = [];
  const state = { saved: 0, disconnected: 0, confirmations: 0 };
  class HTMLFormElement {
    dataset = { confirm: formConfirm };
    matches() { return pick; }
  }
  const context = {
    HTMLFormElement,
    liveSubmitting: false,
    saveViewState: () => state.saved++,
    disconnectStream: () => state.disconnected++,
    window: { confirm: () => { state.confirmations++; return confirmed; } },
    document: {
      addEventListener: (_type, callback, capture = false) => {
        listeners.push({ callback, capture });
      },
    },
  };
  // Execute the real submit listeners without needing the rest of the page DOM.
  // Preserve script order and capture/bubble phases, including stopImmediatePropagation.
  for (const filename of scripts) {
    const source = readFileSync(new URL(`static/${filename}`, root), "utf8");
    const handlers = source.match(
      /document\.addEventListener\("submit", \(event\) => \{[\s\S]*?\n  \}, (?:true|false)\);/g
    );
    assert.equal(handlers?.length, 1, `${filename}: expected one submit listener`);
    runInNewContext(handlers[0], context);
  }
  // session_live_ui.js handles enhanced submissions in the capture phase.
  listeners.push({ capture: true, callback: (event) => {
    if (enhanced) event.preventDefault();
  } });
  const event = {
    target: new HTMLFormElement(),
    submitter: buttonConfirm ? { dataset: { confirm: buttonConfirm } } : null,
    defaultPrevented: canceled,
    stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  for (const capture of [true, false]) {
    for (const listener of listeners.filter((item) => item.capture === capture)) {
      if (event.stopped) break;
      listener.callback(event);
    }
  }
  return { ...state, submitting: context.liveSubmitting, canceled: event.defaultPrevented };
}

for (const confirmation of [{ buttonConfirm: "Remove?" }, { formConfirm: "Delete?" }]) {
  const canceled = submit({ ...confirmation, confirmed: false });
  assert.equal(canceled.confirmations, 1);
  assert.equal(canceled.canceled, true);
  assert.equal(canceled.disconnected, 0, "Cancel must keep the live stream connected");
  assert.equal(canceled.saved, 0, "Cancel must not begin navigation");
  assert.equal(canceled.submitting, false, "Cancel must not block live refreshes");

  const accepted = submit(confirmation);
  assert.equal(accepted.confirmations, 1);
  assert.equal(accepted.disconnected, 1);
  assert.equal(accepted.saved, 1);
  assert.equal(accepted.submitting, true);
}
for (const options of [{ pick: true }, { enhanced: true }, { canceled: true }]) {
  const result = submit(options);
  assert.equal(result.disconnected, 0);
  assert.equal(result.submitting, false);
}
const ordinary = submit();
assert.equal(ordinary.disconnected, 1);
assert.equal(ordinary.submitting, true);
console.log("Passed 8 submit lifecycle scenarios.");
