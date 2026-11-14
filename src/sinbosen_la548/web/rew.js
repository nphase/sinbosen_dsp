// Import from REW: the Tune screen's button reads a REW "Export filter settings as text" file in the browser, asks the
// server what importing it would load and overwrite (nothing is staged), and shows that in a dialog styled like the
// Save popover. Only "Replace" stages the drafts. studio.js owns `ui.menu` and the page; this builds the dialog.

import { ApiError, post } from "./api.js";

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
const fixed = (value, digits) => {
  const text = Number(value).toFixed(digits);
  return Object.is(Number(text), -0) ? text.slice(1) : text;
};

// What importing overwrites, as the sentences of the warning block.
function consequences(rew, { input, names }) {
  const lines = [`This replaces all EQ on this channel. ${rew.replaces.eq_in_use
    ? `${plural(rew.replaces.eq_in_use, "band", "bands")} now in use will be overwritten or cleared.`
    : "No EQ bands are in use now."}`];
  if (!input) {
    const crossovers = [["high-pass", rew.high_pass, rew.replaces.high_pass],
      ["low-pass", rew.low_pass, rew.replaces.low_pass]];
    for (const [name, filter, current] of crossovers) {
      if (filter) lines.push(`The ${name} (now ${current}) will be replaced by ${filter.summary}.`);
    }
    const kept = crossovers.filter(([, filter]) => !filter).map(([name]) => name);
    if (kept.length) lines.push(`The ${kept.join(" and ")} ${kept.length > 1 ? "are" : "is"} kept: the file has none.`);
  }
  for (const name of rew.linked) lines.push(`${names(name)} is linked and will change too.`);
  return lines;
}

// `state()` returns the last state; `h`, `act`, `below`, `send` and `render` are studio.js's own.
export function createRewDialog({ ui, state, h, act, below, send, render }) {
  const nameOf = (id) => {
    const channel = state().channels.find((c) => c.id === id);
    return channel ? `${channel.label}${channel.nickname ? ` "${channel.nickname}"` : ""}` : id;
  };
  // Link members come as labels ("Out 4"); the name adds the nickname.
  const withNickname = (label) => {
    const channel = state().channels.find((c) => c.label === label);
    return channel ? nameOf(channel.id) : label;
  };

  // Called with the file the user chose for the selected channel: previews it and opens the dialog on the answer.
  async function preview(file) {
    const channel = state().selected.id;
    const menu = { kind: "rew", id: null, channel };
    try {
      const text = await file.text();
      Object.assign(menu, { text, file: file.name, rew: (await post("/api/rew/preview", { channel, text })).rew });
    } catch (error) {
      menu.error = error instanceof ApiError ? error.message : `The file could not be read: ${error}`;
      menu.file = file.name;
    }
    ui.menu = menu;
    ui.focusMenu = true;
    render();
  }

  async function replace(menu) {
    ui.menu = null;
    await send("/api/rew/import", { channel: menu.channel, text: menu.text });
    render();
  }

  function close(anchor) {
    ui.menu = null;
    setTimeout(() => anchor.focus());
  }

  function bandTable(rew) {
    const crossovers = [["High-pass", rew.high_pass], ["Low-pass", rew.low_pass]].filter(([, filter]) => filter);
    return h("div", { class: "rew-table-wrap" }, h("table", { class: "rew-table" },
      h("thead", null, h("tr", null, ["Band", "Type", "Hz", "dB", "Q", "Notes"].map((name, i) =>
        h("th", { class: i >= 2 && i <= 4 ? "num" : null }, name)))),
      h("tbody", null,
        rew.bands.map((band) => h("tr", null,
          h("td", null, String(band.n)), h("td", null, band.type), h("td", { class: "num" }, String(band.freq_hz)),
          h("td", { class: "num" }, fixed(band.gain_db, 1)), h("td", { class: "num" }, fixed(band.q, 2)),
          h("td", { class: "notes" }, band.notes.join("; ")))),
        crossovers.map(([name, filter]) => h("tr", null,
          h("td", null, name === "High-pass" ? "HP" : "LP"), h("td", { colspan: "5" }, filter.summary,
            h("span", { class: "notes" }, `  (${filter.source})`)))))));
  }

  function body(menu, anchor) {
    const rew = menu.rew;
    const input = menu.channel.startsWith("in:");
    const live = state().status.live;
    const importable = !rew.nothing_to_import;
    const items = [];
    if (importable) {
      items.push(
        h("div", { class: "rew-warning", role: "alert" },
          h("strong", null, "This overrides the channel's current settings."),
          consequences(rew, { input, names: withNickname }).map((line) => h("div", null, line))),
        bandTable(rew));
    } else {
      items.push(h("div", { class: "rew-warning", role: "alert" }, "Nothing in this file can be imported."));
    }
    if (rew.unsupported.length) {
      items.push(h("div", { class: "rew-skipped" }, h("div", { class: "rew-heading" }, "Not imported"),
        h("ul", null, rew.unsupported.map((item) => h("li", null, h("strong", null, item.source), `: ${item.reason}`)))));
    }
    if (importable) {
      items.push(h("div", { class: "muted" }, live ? "Live is on: this will be sent to the unit straight away."
        : "Nothing is sent to the unit until you press Apply."));
    }
    items.push(h("div", { class: "row" },
      importable ? h("button", { type: "button", class: "button primary", "data-action": act(() => replace(menu)) },
      rew.high_pass || rew.low_pass ? "Replace EQ and crossover" : "Replace EQ") : null,
      h("button", { type: "button", class: "button", "data-autofocus": true,
        "data-action": act(() => close(anchor)) }, importable ? "Cancel" : "Close")));
    return items;
  }

  function dialog(anchor) {
    const menu = ui.menu;
    const items = [h("div", { class: "popover-title" }, `Import REW filters into ${nameOf(menu.channel)}`)];
    if (menu.error) {
      items.push(h("div", { class: "rew-warning", role: "alert" }, menu.error),
        h("div", { class: "row" }, h("button", { type: "button", class: "button", "data-autofocus": true,
          "data-action": act(() => close(anchor)) }, "Close")));
    } else {
      items.push(...body(menu, anchor));
    }
    return h("div", { class: "menu popover rew-popover", role: "dialog", "aria-label": "Import REW filters",
      style: below(anchor, 560) }, items);
  }

  // The button on the selected-channel bar and the hidden file input it opens.
  function button() {
    return h("button", {
      type: "button", id: "rew-import", class: "button", "aria-haspopup": "dialog",
      "aria-expanded": String(ui.menu?.kind === "rew"),
      title: "Replace this channel's EQ and crossover with a REW filter export",
      "data-action": act(() => document.getElementById("rew-file").click()),
    }, "Import from REW…");
  }

  const input = document.getElementById("rew-file");
  input.addEventListener("change", () => {
    const [file] = input.files;
    // Reset, so choosing the same file again fires another change.
    const chosen = file;
    input.value = "";
    if (chosen) preview(chosen);
  });

  return { button, dialog };
}
