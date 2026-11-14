// The Save popover: choose one of the unit's 30 program slots and overwrite it with what is live on the unit. The
// overwrite button names the slot, and a second step asks again, with Cancel focused so Enter never confirms. With
// unapplied edits the save runs Apply first and saves only if Apply reports ok. studio.js owns `ui.menu` and the
// page; this builds the popover from them and the last state.

import { ApiError, getPrograms } from "./api.js";

const slotName = (number, name) => `slot ${number} (${name || "empty name"})`;
const edits = (count) => `${count} unapplied ${count === 1 ? "edit" : "edits"}`;

// `state()` returns the last state; `h`, `act`, `cls`, `below`, `send` and `render` are studio.js's own.
export function createSavePopover({ ui, state, h, act, cls, below, send, render }) {
  // Called once `ui.menu` is the newly opened save menu: reads the slots, then preselects the current one.
  function load() {
    const menu = ui.menu;
    getPrograms().then((programs) => {
      if (ui.menu !== menu) return;
      Object.assign(menu, { programs, slot: programs.current, confirming: false });
      ui.focusMenu = true;
      render();
    }, (error) => {
      if (ui.menu === menu) ui.menu = null;
      ui.errors.push(error instanceof ApiError ? error.message : String(error));
      render();
    });
  }

  async function save(menu, slot) {
    menu.saving = true;
    const data = await send("/api/programs/save", { slot: slot.index, apply_first: state().status.pending_count > 0 });
    // A refusal is already in the error bar, and an Apply that was not ok has put its result there.
    if (ui.menu === menu) ui.menu = null;
    if (data?.saved) {
      ui.notice = { text: `Saved to ${slotName(data.saved.slot + 1, data.saved.name)}.`, message: data.status.message };
    }
    render();
  }

  function slotRow(menu, slot) {
    const name = slot.name || "empty name";
    return h("button", {
      type: "button", class: "menu-item slot", "aria-pressed": String(slot.index === menu.slot),
      "aria-label": `Slot ${slot.number}, ${name}${slot.current ? ", current" : ""}`,
      "data-autofocus": slot.index === menu.slot, "data-action": act(() => { menu.slot = slot.index; }),
    },
    h("span", { class: "slot-number" }, String(slot.number)),
    h("span", { class: cls("slot-name", !slot.name && "empty") }, name),
    slot.current ? h("span", { class: "slot-current" }, "current") : null);
  }

  function popover(anchor) {
    const menu = ui.menu;
    const status = state().status;
    const pending = status.pending_count;
    const chosen = menu.programs?.slots[menu.slot] ?? null;
    const named = chosen && slotName(chosen.number, chosen.name);
    const items = [h("div", { class: "popover-title" }, "Save to program slot")];
    if (pending) {
      items.push(h("div", { class: "note" },
        `You have ${edits(pending)}. The unit saves what is live on it, not your drafts.`));
    }
    if (!menu.programs) {
      items.push(h("div", { class: "muted" }, "Reading the slots…"));
    } else if (menu.confirming && chosen) {
      items.push(
        h("div", { class: "confirm", role: "alert" },
          `${named[0].toUpperCase()}${named.slice(1)} will be replaced with the unit's current live settings. `
          + "This cannot be undone.", status.live ? " Live tuning will be switched off." : null),
        h("div", { class: "row" },
          h("button", { type: "button", class: "button danger", disabled: menu.saving,
            "data-action": act(() => save(menu, chosen)) }, menu.saving ? "Saving…" : "Overwrite"),
          h("button", { type: "button", class: "button", disabled: menu.saving, "data-autofocus": true,
            "data-action": act(() => { menu.confirming = false; ui.focusMenu = true; }) }, "Cancel")));
    } else {
      items.push(
        h("div", { class: "slot-list", role: "group", "aria-label": "Program slots" },
          menu.programs.slots.map((slot) => slotRow(menu, slot))),
        h("div", { class: "row" },
          h("button", { type: "button", class: "button primary", disabled: !chosen,
            "data-action": act(() => { menu.confirming = true; ui.focusMenu = true; }) },
          chosen ? `${pending ? "Apply and overwrite" : "Overwrite"} ${named}` : "Overwrite slot"),
          h("button", { type: "button", class: "button", "data-action": act(() => {
            ui.menu = null;
            setTimeout(() => anchor.focus());
          }) }, "Cancel")));
    }
    return h("div", { class: "menu popover save-popover", role: "dialog", "aria-label": "Save to program slot",
      style: below(anchor, 320) }, items);
  }

  return { load, popover };
}
