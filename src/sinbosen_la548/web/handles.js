// Plot-handle gestures: dragging, wheeling and nudging a handle edits its filter. A gesture keeps its values in
// `ui.handle` so the handle and its card show them at once; they are posted at most every EDIT_INTERVAL_MS, one request
// at a time so the server sees them in order, and the gesture ends once the last one is answered. plot.js turns
// pointer, wheel and key input into values and calls the hooks returned here.

import { ApiError } from "./api.js";

const EDIT_INTERVAL_MS = 40;  // the most often a handle gesture posts /api/edit

// The /api/edit target of a filter card on channel `id`.
export function targetOf(id, filter) {
  return { id, kind: filter.kind, ...(filter.kind === "eq" ? { index: filter.index } : {}) };
}

// `ui` is the page's own state object (`ui.handle`, `ui.errors`, `ui.selectedCard`); `selected()` returns the selected
// channel from the last state; `request` posts and shows the answer; `render` redraws the page.
export function createHandleGestures({ ui, selected, request, render }) {
  // Values waiting to be posted, per channel and handle, so a gesture on another handle never drops the last one's.
  const edits = { queued: new Map(), timer: null, flushing: false, last: 0, sent: false };
  const queueKey = () => `${ui.handle.target.id} ${ui.handle.key}`;

  // True when `ui.handle` is a gesture on this handle of the selected channel.
  const editing = (key) => ui.handle?.key === key && ui.handle.target.id === selected().id;

  function queue(values) {
    edits.queued.set(queueKey(), { target: ui.handle.target, values });
    if (edits.flushing || edits.timer) return;
    edits.timer = setTimeout(flush, Math.max(0, edits.last + EDIT_INTERVAL_MS - performance.now()));
  }

  async function flush() {
    edits.timer = null;
    const [key, next] = edits.queued.entries().next().value ?? [];
    if (next) {
      edits.queued.delete(key);
      edits.flushing = true;
      edits.last = performance.now();
      edits.sent = true;
      try {
        await request("/api/edit", { ...next.target, values: next.values });
      } catch (error) {
        ui.errors.push(error instanceof ApiError ? error.message : String(error));
      } finally {
        edits.flushing = false;
      }
    }
    if (edits.queued.size) {
      edits.timer = setTimeout(flush, Math.max(0, edits.last + EDIT_INTERVAL_MS - performance.now()));
    } else if (ui.handle && !ui.handle.dragging) {
      ui.handle = null;
      render();
    }
  }

  function start(key, before, dragging) {
    const channel = selected();
    if (!editing(key)) {
      ui.handle = { key, target: targetOf(channel.id, channel.filters.find((f) => f.key === key)), before,
        values: {}, dragging };
      edits.sent = false;
    } else {
      ui.handle.dragging = dragging;
      ui.handle.before = before;
    }
  }

  const hooks = {
    begin(key, before) {
      start(key, before, true);
    },
    change(key, values) {
      ui.handle.values = { ...ui.handle.values, ...values };
      queue(ui.handle.values);
      render();
    },
    end(key, values) {
      ui.handle.values = { ...ui.handle.values, ...values };
      ui.handle.dragging = false;
      queue(ui.handle.values);
      render();
    },
    cancel() {
      // One post puts back what the drag changed, replacing whatever was still waiting to be sent.
      ui.handle.values = { ...ui.handle.values, ...ui.handle.before };
      ui.handle.dragging = false;
      if (edits.sent) queue(ui.handle.before);
      else edits.queued.delete(queueKey());
      if (!edits.flushing && !edits.timer) flush();
      render();
    },
    select(key) {
      ui.handle = edits.flushing || edits.timer ? { ...ui.handle, dragging: false } : null;
      ui.selectedCard = { channel: selected().id, key };
      render();
      document.querySelector(`[data-key="card:${CSS.escape(key)}"] .card-row :is(input, select):not(:disabled)`)
        ?.focus();
    },
    nudge(key, values) {
      if (!editing(key)) start(key, null, false);
      ui.handle.values = { ...ui.handle.values, ...values };
      queue(ui.handle.values);
      render();
    },
  };

  return { hooks, editing };
}
