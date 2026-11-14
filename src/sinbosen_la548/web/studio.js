// The Tune screen. Everything on it is drawn from the last state the server sent; the page keeps no model of its own
// beyond the field being typed in and which menu is open. Each region is rebuilt from the state and patched into the
// page, so focus, caret and open dropdowns survive the once-a-second refresh.

import { ApiError, getState, post } from "./api.js";
import { createHandleGestures, targetOf } from "./handles.js";
import { createPlot } from "./plot.js";
import { createSavePopover } from "./save.js";

const POLL_MS = 1000;
const HINT = "Gain is sent when you press Enter; polarity and mute take effect immediately.";
const HINT_DRAFT = "Filters and delay wait for Apply.";
const HINT_LIVE = "Filters and delay are sent as you edit.";
const SECTION_LABELS = { crossover: "Crossover", eq: "EQ", delay: "Delay", limiter: "Limiter" };
// [option posted to /api/plot, label, the plot key present while the option is on]
const PLOT_OPTIONS = [
  ["phase", "Phase", "phase_deg"],
  ["group_delay", "Group delay", "group_delay_ms"],
  ["each_filter", "Each filter", "filters"],
  ["on_device", "On device", "device_db"],
];
// Shorter option text where the full name would be clipped on a 132 px card; the full name stays as the title.
const SHORT_OPTIONS = {
  "Parametric EQ": "Parametric", "Allpass (1st order)": "Allpass 1", "Allpass (2nd order)": "Allpass 2",
  "Linkwitz–Riley": "Linkwitz",
};
const FIELD_NAMES = {
  freq_hz: "frequency", filter_type: "family", slope_db_per_oct: "slope", type: "type", gain_db: "gain", q: "Q",
};
const PLUS_ICON = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';

let state = null;
const ui = {
  typing: null,        // {id, text, error, busy}: the field being typed in
  menu: null,          // {kind: "connection" | "link" | "sync" | "save", id}
  focusMenu: false,
  address: "usb",
  connecting: false,
  errors: [],          // messages from requests, until dismissed
  dismissed: new Set(),
  serverDown: null,
  menuBefore: null,    // the menu open before the current click
  handle: null,        // {key, target, before, values, dragging}: a plot handle being dragged, wheeled or nudged
  naming: null,        // the id of the channel whose name is being edited in its strip
  selectedCard: null,  // {channel, key}: the card whose handle was clicked
  notice: null,        // {text, message}: a confirmation shown in place of the status message `message`
};
let fields = new Map();   // data-field -> field spec, rebuilt on every render
let actions = new Map();  // data-action -> handler, rebuilt on every render
let sequence = 0;
let shown = 0;
let busy = 0;
let plot = null;
const gestures = createHandleGestures({ ui, selected: () => state.selected, request, render });
const { editing } = gestures;

// DOM building and patching

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props ?? {})) {
    if (value === false || value === null || value === undefined) continue;
    el.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function icon(markup) {
  const holder = document.createElement("span");
  holder.innerHTML = markup;
  return holder.firstChild;
}

const cls = (...names) => names.filter(Boolean).join(" ");
const savePopover = createSavePopover({ ui, state: () => state, h, act, cls, below, send, render });

function act(handler) {
  const id = String(actions.size);
  actions.set(id, handler);
  return id;
}

const keyOf = (node) => (node.nodeType === Node.ELEMENT_NODE ? node.getAttribute("data-key") : null);

function compatible(a, b) {
  if (a.nodeType !== b.nodeType) return false;
  if (a.nodeType !== Node.ELEMENT_NODE) return true;
  return a.tagName === b.tagName && keyOf(a) === keyOf(b) && a.getAttribute("type") === b.getAttribute("type");
}

function morph(from, to) {
  if (from.nodeType !== Node.ELEMENT_NODE) {
    if (from.data !== to.data) from.data = to.data;
    return;
  }
  for (const { name } of [...from.attributes]) if (!to.hasAttribute(name)) from.removeAttribute(name);
  for (const { name, value } of [...to.attributes]) if (from.getAttribute(name) !== value) from.setAttribute(name, value);
  morphChildren(from, to);
  if (from.tagName === "INPUT") {
    if (from.type === "checkbox") from.checked = to.hasAttribute("checked");
    else if (from.value !== (to.getAttribute("value") ?? "")) from.value = to.getAttribute("value") ?? "";
  } else if (from.tagName === "SELECT" && to.dataset.value !== undefined && from.value !== to.dataset.value) {
    from.value = to.dataset.value;
  }
}

function morphChildren(parent, next) {
  const wanted = [...next.childNodes];
  const keyed = new Map();
  for (const child of parent.childNodes) if (keyOf(child)) keyed.set(keyOf(child), child);
  wanted.forEach((node, i) => {
    const at = parent.childNodes[i] ?? null;
    const match = keyOf(node) ? keyed.get(keyOf(node)) : at;
    if (match && compatible(match, node)) {
      if (match !== at) parent.insertBefore(match, at);
      morph(match, node);
    } else {
      parent.insertBefore(node, at);
    }
  });
  while (parent.childNodes.length > wanted.length) parent.lastChild.remove();
}

function patch(id, children) {
  morphChildren(document.getElementById(id), h("div", null, children));
}

// Values

function fixed(value, digits) {
  const text = Number(value).toFixed(digits);
  return Object.is(Number(text), -0) ? text.slice(1) : text;
}

function signed(value, digits) {
  const text = fixed(value, digits);
  return Number(text) > 0 ? `+${text}` : text;
}

// Accepts "-3", "-3 dB", "−3dB", "2.35ms", and for frequencies "2k" or "2 kHz".
function numberParser(unit, { kilo = false } = {}) {
  const pattern = new RegExp(`^([+-]?(?:\\d+\\.?\\d*|\\.\\d+))\\s*(${kilo ? "k" : "(?!)"})?\\s*(?:${unit})?$`, "i");
  return (text) => {
    const match = text.trim().replace(/[−–]/g, "-").match(pattern);
    if (!match) throw new Error(unit ? `Enter a number in ${unit}.` : "Enter a number.");
    return Number(match[1]) * (match[2] ? 1000 : 1);
  };
}

const fieldDisplay = {
  freq_hz: (v) => String(Math.round(v)),
  gain_db: (v) => signed(v, 1),
  q: (v) => fixed(v, 2),
};

const fieldParser = {
  freq_hz: numberParser("Hz", { kilo: true }),
  gain_db: numberParser("dB"),
  q: numberParser(""),
};

// Requests

function show(number, data) {
  if (number < shown) return;
  shown = number;
  state = data;
  render();
}

function report(result) {
  if (!result || result.ok) return;
  const lines = [...(result.errors ?? [])];
  if (result.conflicts?.length) {
    lines.push(`Changed on the device since it was last read: ${result.conflicts.join(", ")}.`);
  }
  if (!lines.length && !result.verified) lines.push("The device did not confirm the write.");
  ui.errors.push(...lines);
}

async function request(path, body) {
  const number = ++sequence;
  busy++;
  try {
    const data = await post(path, body);
    report(data.result);
    show(number, data);
    return data;
  } finally {
    busy--;
  }
}

// For buttons, ticks and selects: a refusal goes to the error bar.
async function send(path, body) {
  try {
    return await request(path, body);
  } catch (error) {
    ui.errors.push(error instanceof ApiError ? error.message : String(error));
    render();
    return null;
  }
}

async function poll() {
  if (busy || ui.handle || document.hidden) return;
  const number = ++sequence;
  try {
    const data = await getState();
    ui.serverDown = null;
    show(number, data);
  } catch (error) {
    ui.serverDown = error instanceof ApiError ? error.message : String(error);
    render();
  }
}

function focusField(id) {
  document.querySelector(`[data-field="${CSS.escape(id)}"]`)?.focus();
}

async function commit(el) {
  const id = el.dataset.field;
  const spec = fields.get(id);
  const typing = ui.typing;
  if (!spec || !typing || typing.id !== id || typing.busy) return;
  const text = typing.text;
  if (text.trim() === spec.display) {
    ui.typing = null;
    render();
    return;
  }
  let value;
  try {
    value = spec.parse(text);
  } catch (error) {
    typing.error = error.message;
    render();
    focusField(id);
    return;
  }
  typing.busy = true;
  try {
    await request(spec.path, spec.body(value));
    if (ui.typing === typing) {
      ui.typing = typing.text === text ? null : { ...typing, busy: false };
    }
    render();
  } catch (error) {
    typing.busy = false;
    if (ui.typing === typing) {
      typing.error = error instanceof ApiError ? error.message : String(error);
      render();
      focusField(id);
    } else {
      ui.errors.push(error.message);
      render();
    }
  }
}

function connect() {
  ui.connecting = true;
  render();
  send("/api/connect", { address: ui.address }).then((data) => {
    ui.connecting = false;
    if (data) ui.menu = null;
    render();
  });
}

function openMenu(kind, id = null) {
  const open = ui.menuBefore?.kind === kind && ui.menuBefore?.id === id;
  ui.menu = open ? null : { kind, id };
  ui.focusMenu = !open;
}

// Controls

// A strip field is about 47 px wide: "-10.0" fills it, so the unit is the placeholder and the tooltip, not a suffix.
// An `enterOnly` field (gain, written to the unit at once) is sent only by Enter; until then it shows that it is unsent.
function textInput(spec) {
  fields.set(spec.id, spec);
  const typing = ui.typing?.id === spec.id ? ui.typing : null;
  const unsent = spec.enterOnly && typing && !typing.error;
  return h("input", {
    type: "text",
    class: cls("field", spec.number && "number", spec.className, spec.pending && "pending", typing?.error && "invalid",
      unsent && "unsent"),
    "data-field": spec.id,
    "aria-label": spec.label,
    "aria-invalid": typing?.error ? "true" : false,
    "aria-describedby": typing?.error ? "field-error" : false,
    value: typing ? typing.text : spec.display,
    placeholder: spec.placeholder ?? spec.unit,
    title: unsent ? "Press Enter to send" : spec.title,
    disabled: spec.disabled,
    autocomplete: "off",
    spellcheck: "false",
  });
}

function check(label, checked, onChange) {
  return h("label", { class: "check" },
    h("input", { type: "checkbox", checked, "data-action": act(onChange) }), label);
}

function connectionText(status) {
  if (status.connected) {
    const device = status.device ?? "";
    return `${device.startsWith("usb") ? "USB" : device} · connected`;
  }
  return status.demo ? "Offline demo" : "Not connected";
}

// Regions

function topBar() {
  const status = state.status;
  const pending = status.pending_count;
  return [
    h("div", { class: "app-name" }, "DSP Studio"),
    h("button", {
      type: "button", id: "connection", class: "chip", "aria-haspopup": "dialog",
      "aria-expanded": String(ui.menu?.kind === "connection"), "data-action": act(() => openMenu("connection")),
    },
    h("span", { class: cls("dot", status.connected && "connected", !status.connected && !status.demo && "offline"),
      "aria-hidden": "true" }),
    h("span", null, connectionText(status))),
    h("nav", { class: "tabs", "aria-label": "Screens" },
      h("button", { type: "button", class: "tab", "aria-current": "page" }, "Tune")),
    latencyReadout(state.selected.latency),
    h("label", { class: "check live", title: status.connected ? "" : "Live needs a connected unit" },
      h("input", { type: "checkbox", checked: status.live, disabled: !status.connected,
        "data-action": act((el) => send("/api/live", { enabled: el.checked })) }), "Live"),
    h("div", { id: "unapplied", class: "unapplied", "aria-live": "polite" }, pending ? `${pending} unapplied` : ""),
    h("button", { type: "button", class: "button", disabled: !pending,
      "data-action": act(() => send("/api/revert")) }, "Revert"),
    h("button", { type: "button", class: "button primary", disabled: !pending,
      "data-action": act(() => send("/api/apply")) }, "Apply"),
    h("button", {
      type: "button", id: "save", class: "button", "aria-haspopup": "dialog",
      "aria-expanded": String(ui.menu?.kind === "save"), disabled: !status.connected && !status.demo,
      title: "Save the unit's live settings to a program slot", "data-action": act(() => {
        openMenu("save");
        if (ui.menu) savePopover.load();
      }),
    }, "Save"),
  ];
}

// The selected channel's latency, in the space between the tab and Live; a long channel name is ellipsised first so
// the bar stays on one line. A server started before the readout existed sends none: the space stays empty.
function latencyReadout(latency) {
  if (!latency) return h("div", { class: "spacer" });
  return h("div", { class: "latency spacer", title: latency.detail, "aria-description": latency.detail },
    h("span", { class: "latency-name" }, latency.channel), h("span", { class: "latency-text" }, ` ${latency.text}`));
}

function standingErrors() {
  const status = state?.status ?? {};
  const standing = [ui.serverDown, status.warning, status.live_errors && `Live: ${status.live_errors}`].filter(Boolean);
  // A dismissal lasts only while its message stands, so the same failure coming back later shows again.
  for (const message of ui.dismissed) if (!standing.includes(message)) ui.dismissed.delete(message);
  return standing.filter((message) => !ui.dismissed.has(message));
}

function errorBar() {
  const messages = [...new Set([...standingErrors(), ...ui.errors])];
  if (!messages.length) return [];
  return [
    h("ul", null, messages.map((message) => h("li", null, message))),
    h("button", { type: "button", class: "dismiss", "aria-label": "Dismiss errors", "data-action": act(() => {
      standingErrors().forEach((message) => ui.dismissed.add(message));
      ui.errors = [];
    }) }, "Dismiss"),
  ];
}

// The strip header selects its channel. The name may be wider than its box: it then covers the fixed label, up to the
// strip's width. Double-clicking the name, or Enter or F2 on the header, edits it in place.
function stripHead(channel) {
  const { label, nickname } = channel;
  const fieldId = `nick:${channel.id}`;
  return h("div", { class: "strip-head" },
    h("button", { type: "button", class: "strip-select", "aria-label": nickname ? `${label}, ${nickname}` : label,
      title: label, "aria-current": channel.selected ? "true" : false, "data-channel": channel.id,
      "data-action": act(() => send("/api/select", { id: channel.id })) },
    h("span", { class: "strip-label" }, label),
    h("span", { class: cls("strip-name", !nickname && "empty") }, nickname || "Name")),
    ui.naming === channel.id || ui.typing?.id === fieldId ? textInput({
      id: fieldId, label: `${label} nickname`, display: nickname, placeholder: "Name", className: "nickname",
      parse: (text) => text.trim(), path: "/api/nickname", body: (name) => ({ id: channel.id, name }),
    }) : null);
}

// A name too wide to sit beside the label covers the whole header, so no sliver of the label shows next to it.
function coverLabels() {
  for (const name of document.querySelectorAll("#strips .strip-name")) {
    const label = name.previousElementSibling;
    name.classList.remove("covers");  // measured at its own width
    name.classList.toggle("covers", name.scrollWidth > name.parentElement.clientWidth - label.offsetWidth - 4);
  }
}

function startNaming(id) {
  ui.naming = id;
  render();
  const field = document.querySelector(`[data-field="nick:${CSS.escape(id)}"]`);
  field?.focus();
  field?.select();
}

const focusStrip = (id) => document.querySelector(`.strip-select[data-channel="${CSS.escape(id)}"]`)?.focus();

function strip(channel) {
  const link = channel.link;
  const label = channel.label;
  return h("div", {
    class: cls("strip", channel.id.startsWith("out:") ? "output" : "input", channel.selected && "selected"),
    "data-key": channel.id,
  },
  stripHead(channel),
  h("div", { class: "strip-pair" },
    textInput({
      id: `gain:${channel.id}`, label: `${label} gain in dB`, number: true, unit: "dB", title: "Gain (dB)",
      enterOnly: true, display: fixed(channel.gain_db, 1), parse: numberParser("dB"), path: "/api/now",
      body: (value) => ({ id: channel.id, block: "gain", value }),
    }),
    textInput({
      id: `delay:${channel.id}`, label: `${label} delay in ms`, number: true, unit: "ms", title: "Delay (ms)",
      display: fixed(channel.delay_ms, 2), pending: channel.delay_pending, parse: numberParser("ms"),
      path: "/api/edit", body: (value) => ({ id: channel.id, kind: "delay", field: "delay_ms", value }),
    })),
  h("div", { class: "strip-buttons" },
    h("button", { type: "button", class: "toggle polarity", "aria-label": `${label} polarity`,
      "aria-pressed": String(channel.inverted), title: "Invert polarity",
      "data-action": act(() => send("/api/now", { id: channel.id, block: "polarity", value: !channel.inverted })) },
    "Ø"),
    h("button", { type: "button", class: "toggle mute", "aria-label": `${label} mute`,
      "aria-pressed": String(channel.muted), title: "Mute",
      "data-action": act(() => send("/api/now", { id: channel.id, block: "mute", value: !channel.muted })) }, "M"),
    h("button", {
      type: "button", id: `link-${channel.id}`, class: cls("link-button", link && !link.in_sync && "out-of-sync"),
      "aria-label": `${label} link`, "aria-haspopup": "menu",
      "aria-expanded": String(ui.menu?.kind === "link" && ui.menu.id === channel.id),
      title: link && !link.in_sync ? "Linked channels differ" : false,
      style: link ? `border-color: ${link.colour}; color: ${link.colour}` : false,
      "data-action": act(() => openMenu("link", channel.id)),
    }, link ? `Link ${link.letter}` : "Link")));
}

// A save's confirmation stands until the status message changes, that is until the next action reports something.
function hint() {
  const message = state.status.message;
  const notice = ui.notice?.message === message ? ui.notice.text : null;
  return [h("span", null, `${HINT} ${state.status.live ? HINT_LIVE : HINT_DRAFT}`),
    h("span", { class: cls("message", notice && "notice") }, notice ?? message)];
}

function selectedBar() {
  const selected = state.selected;
  const channel = state.channels.find((c) => c.id === selected.id);
  const items = [h("div", { class: "selected-name" },
    selected.nickname ? h("span", { class: "nick" }, selected.nickname) : null,
    h("span", { class: "label" }, selected.label))];
  const link = selected.link;
  if (link) {
    const others = link.members.filter((m) => m.id !== selected.id).map((m) => m.nickname || m.label);
    items.push(h("div", { class: "link-summary" },
      h("span", { class: "letter", style: `color: ${channel.link.colour}` }, `Link ${link.letter}`),
      others.length ? ` with ${others.join(", ")}` : " (no other members yet)", " · ties"));
    for (const [section, on] of Object.entries(link.sections)) {
      items.push(check(SECTION_LABELS[section] ?? section, on, (el) => send("/api/link/sections", {
        letter: link.letter,
        sections: Object.keys(link.sections).filter((s) => (s === section ? el.checked : link.sections[s])),
      })));
    }
    if (!channel.link.in_sync) {
      items.push(h("button", {
        type: "button", id: "sync-chip", class: "warning-chip", "aria-haspopup": "menu",
        "aria-expanded": String(ui.menu?.kind === "sync"), "data-action": act(() => openMenu("sync")),
      }, "Out of sync · Copy from…"));
    }
  } else {
    items.push(h("div", { class: "muted" }, "Not linked"));
  }
  items.push(h("div", { class: "spacer" }), h("div", { class: "muted" }, "Show"));
  for (const [option, label, key] of PLOT_OPTIONS) {
    items.push(check(label, key in state.plot, (el) => send("/api/plot", { [option]: el.checked })));
  }
  return items;
}

function cardControl(filter, field, target) {
  const name = `${filter.title} ${FIELD_NAMES[field.key] ?? field.label}`;
  if (field.kind === "select") {
    // A crossover handle dragged up or down changes the slope; the select follows it.
    const value = (editing(filter.key) ? ui.handle.values[field.key] : undefined) ?? field.value;
    return h("select", {
      class: cls("field", field.pending && "pending"), "aria-label": name, disabled: !field.editable,
      "data-value": String(value),
      "data-action": act((el) => send("/api/edit", { ...target, field: field.key, value: Number(el.value) })),
    }, field.options.map((o) => h("option", { value: String(o.value), selected: o.value === value,
      title: SHORT_OPTIONS[o.label] ? o.label : false, "aria-label": SHORT_OPTIONS[o.label] ? o.label : false },
    SHORT_OPTIONS[o.label] ?? o.label)));
  }
  const display = fieldDisplay[field.key] ?? String;
  // A handle gesture shows its values here before the server has answered.
  const edited = editing(filter.key) ? ui.handle.values[field.key] : undefined;
  return textInput({
    id: `${state.selected.id}:${filter.key}:${field.key}`, label: name, number: true,
    display: display(edited ?? field.value),
    pending: field.pending, disabled: !field.editable, parse: fieldParser[field.key] ?? numberParser(""),
    path: "/api/edit", body: (value) => ({ ...target, field: field.key, value }),
  });
}

function filterCard(filter) {
  const id = state.selected.id;
  const target = targetOf(id, filter);
  const selected = ui.selectedCard?.channel === id && ui.selectedCard.key === filter.key;
  return h("div", {
    class: cls("card", filter.pending && "pending", !filter.enabled && "off", selected && "selected"),
    "data-key": `card:${filter.key}`,
    role: "group", "aria-label": filter.title,
  },
  h("div", { class: "card-head" },
    h("label", { class: cls("check", filter.enabled_pending && "pending") },
      h("input", { type: "checkbox", checked: filter.enabled,
        "data-action": act((el) => send("/api/edit", { ...target, field: "enabled", value: el.checked })) }),
      h("span", { class: "card-title" }, filter.title)),
    filter.kind === "eq"
      ? h("button", { type: "button", class: "remove", "aria-label": `Remove ${filter.title}`, title: "Remove band",
        "data-action": act(() => send("/api/band/remove", { id, index: filter.index })) }, "×")
      : null),
  filter.fields.map((field) => h("div", { class: "card-row" },
    h("span", { "aria-hidden": "true" }, field.label), cardControl(filter, field, target))));
}

function cards() {
  const selected = state.selected;
  return [
    ...selected.filters.map(filterCard),
    h("button", {
      type: "button", class: "add-band", "data-key": "add-band", disabled: selected.eq_used >= selected.eq_slots,
      "data-action": act(() => send("/api/band/add", { id: selected.id })),
    }, icon(PLUS_ICON), h("span", null, "Add band"),
    h("span", { class: "count" }, `${selected.eq_used} of ${selected.eq_slots} used`)),
    h("div", { class: "more", "data-key": "more", role: "list", "aria-label": "Other blocks" },
      selected.more.map((block) => h("div", { class: "more-row", role: "listitem" },
        h("span", { class: "name" }, block.name), h("span", { class: "state" }, block.state)))),
  ];
}

// Overlay: menus, the connection popover and the message under a refused field, placed under their anchors.

function below(anchor, width = 200) {
  const rect = anchor.getBoundingClientRect();
  const left = Math.max(4, Math.min(rect.left, window.innerWidth - width - 8));
  return `top: ${Math.round(rect.bottom + 4)}px; left: ${Math.round(left)}px`;
}

function menuItem(label, onClick, { disabled = false, swatch = null } = {}) {
  return h("button", { type: "button", class: "menu-item", role: "menuitem", disabled,
    "data-action": act(() => {
      ui.menu = null;
      return onClick();
    }) },
  swatch ? h("span", { class: "swatch", style: `background: ${swatch}`, "aria-hidden": "true" }) : null, label);
}

function linkMenu(id, anchor) {
  const channel = state.channels.find((c) => c.id === id);
  if (!channel) return null;
  const side = id.split(":")[0];
  const groups = new Map();
  for (const c of state.channels) if (c.link && c.id.startsWith(`${side}:`)) groups.set(c.link.letter, c.link.colour);
  const joinable = [...groups].filter(([letter]) => letter !== channel.link?.letter).sort();
  return h("div", { class: "menu", role: "menu", "aria-label": `${channel.label} link`, style: below(anchor, 160) },
    joinable.map(([letter, colour]) => menuItem(`Link ${letter}`, () => send("/api/link", { id, letter }),
      { disabled: Boolean(channel.link), swatch: colour })),
    joinable.length ? h("div", { class: "menu-rule", role: "separator" }) : null,
    menuItem("New link", () => send("/api/link", { id, letter: null }), { disabled: Boolean(channel.link) }),
    menuItem("Unlink", () => send("/api/unlink", { id }), { disabled: !channel.link }));
}

function syncMenu(anchor) {
  const link = state.selected.link;
  if (!link) return null;
  return h("div", { class: "menu", role: "menu", "aria-label": "Copy tied sections from", style: below(anchor) },
    link.members.map((m) => menuItem(`Copy from ${m.nickname ? `${m.nickname} (${m.label})` : m.label}`,
      () => send("/api/link/sync", { letter: link.letter, from: m.id }))));
}

// Disconnecting or switching to the demo discards unapplied edits, so with any pending the popover asks first.
function leave(path, confirmed = false) {
  if (state.status.pending_count && !confirmed) ui.menu.leaving = path;
  else send(path);
}

function leaveConfirmation(path, pending) {
  return [
    h("div", { class: "confirm", role: "alert" },
      `Discard ${pending} unapplied ${pending === 1 ? "edit" : "edits"}? They have not been sent to the unit.`),
    h("div", { class: "row" },
      h("button", { type: "button", class: "button danger", "data-action": act(() => leave(path, true)) },
        path === "/api/demo" ? "Discard and use demo" : "Discard and disconnect"),
      h("button", { type: "button", class: "button", "data-action": act(() => { ui.menu.leaving = null; }) },
        "Keep edits")),
  ];
}

function connectionPopover(anchor) {
  const status = state.status;
  const leaving = ui.menu.leaving;
  return h("div", { class: "menu popover", role: "dialog", "aria-label": "Connection", style: below(anchor, 260) },
    h("label", { for: "conn-address" }, "Device address (usb, usb:<serial> or an IP address)"),
    h("input", { id: "conn-address", class: "field", type: "text", value: ui.address, "data-local": "address",
      autocomplete: "off", spellcheck: "false" }),
    h("div", { class: "row" },
      h("button", { type: "button", class: "button primary", disabled: ui.connecting, "data-action": act(connect) },
        ui.connecting ? "Connecting…" : "Connect"),
      h("button", { type: "button", class: "button", disabled: !status.connected, title: "Read the unit again",
        "data-action": act(() => send("/api/refresh")) }, "Refresh")),
    h("div", { class: "row" },
      h("button", { type: "button", class: "button", disabled: !status.connected,
        "data-action": act(() => leave("/api/disconnect")) }, "Disconnect"),
      h("button", { type: "button", class: "button", disabled: status.demo,
        "data-action": act(() => leave("/api/demo")) }, "Offline demo")),
    leaving && status.pending_count ? leaveConfirmation(leaving, status.pending_count) : null);
}

function overlay() {
  const items = [];
  const menu = ui.menu;
  if (menu) {
    const anchorId = { connection: "connection", link: `link-${menu.id}`, sync: "sync-chip", save: "save" }[menu.kind];
    const anchor = document.getElementById(anchorId);
    const built = !anchor ? null : menu.kind === "connection" ? connectionPopover(anchor)
      : menu.kind === "link" ? linkMenu(menu.id, anchor) : menu.kind === "save" ? savePopover.popover(anchor)
        : syncMenu(anchor);
    if (built) items.push(built);
    else ui.menu = null;
  }
  const typing = ui.typing;
  if (typing?.error) {
    const field = document.querySelector(`[data-field="${CSS.escape(typing.id)}"]`);
    if (field) {
      const rect = field.getBoundingClientRect();
      items.push(h("div", { id: "field-error", class: "field-error", role: "alert",
        style: `${below(field, 220)}; min-width: ${Math.round(rect.width)}px` },
        `${typing.error.replace(/\.?$/, ".")} Esc to cancel`));
    }
  }
  return items;
}

function render() {
  if (!state) {
    patch("errors", errorBar());
    return;
  }
  fields = new Map();
  actions = new Map();
  patch("topbar", topBar());
  patch("errors", errorBar());
  patch("strips", state.channels.map(strip));
  coverLabels();
  patch("hint", hint());
  patch("selected-bar", selectedBar());
  patch("cards", cards());
  plot ??= createPlot(document.getElementById("plot"), gestures.hooks);
  plot.draw(state.plot, state.selected.filters, ui.handle?.target.id === state.selected.id ? ui.handle : null);
  patch("overlay", overlay());
  if (ui.focusMenu) {
    ui.focusMenu = false;
    (document.querySelector("#overlay [data-autofocus]:not(:disabled)")
      ?? document.querySelector("#overlay .menu-item:not(:disabled), #overlay input"))?.focus();
  }
}

// Events, delegated so that patching never has to rebind handlers.

const isTextField = (el) => el?.matches?.("input[data-field]");

document.addEventListener("click", (event) => {
  const before = ui.menu;
  if (!event.target.closest("#overlay")) ui.menu = null;
  const button = event.target.closest("button[data-action]");
  const handler = button && !button.disabled ? actions.get(button.dataset.action) : null;
  ui.menuBefore = before;
  if (handler) handler(button);
  ui.menuBefore = null;
  if (handler || ui.menu !== before) render();
});

document.addEventListener("change", (event) => {
  const el = event.target;
  if (el.matches("input[type=checkbox][data-action], select[data-action]")) actions.get(el.dataset.action)?.(el);
});

document.addEventListener("input", (event) => {
  const el = event.target;
  if (el.dataset.local === "address") {
    ui.address = el.value;
  } else if (isTextField(el)) {
    const hadError = ui.typing?.id === el.dataset.field && ui.typing.error;
    const started = ui.typing?.id !== el.dataset.field;
    ui.typing = { id: el.dataset.field, text: el.value, error: null, busy: false };
    // Redraw when the "press Enter" cue appears, or when a refusal's message goes.
    if (hadError || (started && fields.get(el.dataset.field)?.enterOnly)) render();
  }
});

document.addEventListener("keydown", (event) => {
  const el = event.target;
  if (event.key === "Escape") {
    if (ui.menu) {
      const anchor = document.getElementById({ connection: "connection", sync: "sync-chip", save: "save" }[ui.menu.kind]
        ?? `link-${ui.menu.id}`);
      ui.menu = null;
      render();
      anchor?.focus();
    } else if (isTextField(el) && (ui.typing?.id === el.dataset.field || el.dataset.field === `nick:${ui.naming}`)) {
      const naming = ui.naming;
      ui.typing = null;
      ui.naming = null;
      render();
      if (naming) focusStrip(naming);
    }
  } else if (event.key === "Enter") {
    if (el.dataset.local === "address") {
      connect();
    } else if (isTextField(el) && el.dataset.field === `nick:${ui.naming}`) {
      const naming = ui.naming;
      ui.naming = null;  // the field stays while its value is being sent, or refused
      commit(el).then(() => {
        render();
        if (!document.querySelector(`[data-field="nick:${CSS.escape(naming)}"]`)) focusStrip(naming);
      });
    } else if (isTextField(el)) {
      commit(el);
    } else if (el.matches(".strip-select")) {
      startNaming(el.dataset.channel);
    } else {
      return;
    }
    event.preventDefault();
  } else if (event.key === "F2" && el.matches(".strip-select")) {
    event.preventDefault();
    startNaming(el.dataset.channel);
  }
});

document.addEventListener("dblclick", (event) => {
  const name = event.target.closest(".strip-name");
  if (name) startNaming(name.closest(".strip-select").dataset.channel);
});

document.addEventListener("focusout", (event) => {
  const el = event.target;
  if (!isTextField(el)) return;
  if (fields.get(el.dataset.field)?.enterOnly) {
    // Leaving a gain field discards what was typed: a half-typed "-1" on the way to "-12" never reaches the unit.
    if (ui.typing?.id === el.dataset.field && !ui.typing.busy) {
      ui.typing = null;
      render();
    }
    return;
  }
  if (el.dataset.field === `nick:${ui.naming}`) {
    // Leaving the name field commits it; the field closes once nothing is left to send.
    ui.naming = null;
    if (ui.typing?.id !== el.dataset.field) render();
  }
  commit(el);
});

window.addEventListener("resize", () => patch("overlay", overlay()));
document.addEventListener("scroll", () => patch("overlay", overlay()), true);

async function start() {
  const number = ++sequence;
  try {
    show(number, await getState());
  } catch (error) {
    ui.serverDown = error instanceof ApiError ? error.message : String(error);
    render();
  }
  setInterval(poll, POLL_MS);
}

start();
