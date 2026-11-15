// The Limiter and Compressor cards, in the stack at the right of the filter cards (outputs only). They look and edit
// like the filter cards: a tick in the title enables the block, each field is a draft shown unapplied until Apply, and
// a value is sent on Enter or on leaving the field. The ranges come from the server, which takes them from the engine;
// a typed value outside one is refused with that range. A block the engine will not write is shown read-only with the
// reason. studio.js owns the page; this builds the cards from the last state.

const CARDS = [["limiter", "Limiter"], ["compressor", "Compressor"]];
// The label column of a card row is 30 px wide, so the labels are short; the aria-label and title say the rest.
const LABELS = { threshold_db: "Thr", ratio: "Ratio", start_time_ms: "Atk", release_time_ms: "Rel", detection_type: "Det" };
const NAMES = {
  threshold_db: "threshold", ratio: "ratio", start_time_ms: "attack", release_time_ms: "release",
  detection_type: "detection",
};
const UNITS = { threshold_db: "dB", start_time_ms: "ms", release_time_ms: "ms" };
// "4", "4:1" and "4.0 : 1" are all a ratio of 4.
const RATIO = /^([+-]?(?:\d+\.?\d*|\.\d+))\s*(?::\s*1(?:\.0*)?)?$/;

// `state()` returns the last state; the rest are studio.js's own.
export function createDynamics({ state, h, act, cls, send, textInput, fixed, numberParser }) {
  // The attack is stored in 0.1 ms steps up to 1 ms and in whole ms beyond.
  const display = {
    threshold_db: (v) => `${fixed(v, 1)} dB`,
    ratio: (v) => `${fixed(v, 1)} : 1`,
    start_time_ms: (v) => `${fixed(v, v < 1 ? 1 : 0)} ms`,
    release_time_ms: (v) => `${fixed(v, 0)} ms`,
  };
  const parse = {
    threshold_db: numberParser("dB"),
    start_time_ms: numberParser("ms"),
    release_time_ms: numberParser("ms"),
    ratio: (text) => {
      const match = text.trim().match(RATIO);
      if (!match) throw new Error("Enter a ratio such as 4 or 4 : 1.");
      return Number(match[1]);
    },
  };

  function control(target, title, name, field, readonly) {
    const label = `${title} ${NAMES[name]}`;
    if (field.options) {
      return h("select", {
        class: cls("field", field.pending && "pending"), "aria-label": label, disabled: readonly,
        "data-value": String(field.value),
        "data-action": act((el) => send("/api/edit", { ...target, field: name, value: Number(el.value) })),
      }, field.options.map((o) => h("option", { value: String(o.value), selected: o.value === field.value }, o.label)));
    }
    const unit = UNITS[name];
    return textInput({
      id: `${target.id}:${target.kind}:${name}`, label, number: true, display: display[name](field.value),
      pending: field.pending, disabled: readonly, parse: parse[name], path: "/api/edit",
      title: `${label}: ${field.min} to ${field.max}${unit ? ` ${unit}` : ""}, in steps of ${field.step}`,
      body: (value) => ({ ...target, field: name, value }),
    });
  }

  function card(kind, title, block) {
    const target = { id: state().selected.id, kind };
    const readonly = block.readonly_reason !== null;
    return h("div", {
      class: cls("card", block.pending && "pending", !block.enabled && "off", readonly && "readonly"),
      "data-key": `card:${kind}`, role: "group", "aria-label": title,
    },
    h("div", { class: "card-head" },
      h("label", { class: cls("check", block.enabled_pending && "pending") },
        h("input", { type: "checkbox", checked: block.enabled, disabled: readonly,
          "data-action": act((el) => send("/api/edit", { ...target, field: "enabled", value: el.checked })) }),
        h("span", { class: "card-title" }, title))),
    Object.entries(block.fields).map(([name, field]) => h("div", { class: "card-row" },
      h("span", { "aria-hidden": "true" }, LABELS[name]), control(target, title, name, field, readonly))),
    readonly ? h("div", { class: "card-note" }, block.readonly_reason) : null);
  }

  // The cards of the selected channel: none for an input, and none from a server that predates them.
  function cards() {
    const selected = state().selected;
    return CARDS.filter(([kind]) => selected[kind]).map(([kind, title]) => card(kind, title, selected[kind]));
  }

  return { cards };
}
