// The Mixer tab: the unit's routing matrix, one row per input and one column per output. Each crossing is a toggle
// staged like any filter edit (never written at once: a wrong click could send a full-range signal to a tweeter), and
// marked unapplied until Apply. Only one cell is in the tab order; the arrow keys move between cells and Space or
// Enter toggles. studio.js owns the page and the tab; this builds the grid from the last state.

const ARROWS = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] };

const nameOf = (channel) => channel.nickname || channel.label;

// `state()` returns the last state; `h`, `act`, `cls`, `send` and `render` are studio.js's own.
export function createMixer({ state, h, act, cls, send, render }) {
  let focus = { input: 0, output: 0 };  // the cell in the tab order

  const mute = () => h("span", { class: "mixer-mute", title: "Muted" }, "M");

  // A column header has three lines, so every column's label sits at the same height: the nickname (blank without
  // one), the label, and "no input" or the mute marker. A row header is the nickname over the label, then the marker.
  function columnHeader(channel) {
    return h("th", { scope: "col", title: channel.nickname ? `${channel.nickname} (${channel.label})` : channel.label },
      h("div", { class: "mixer-head" },
        h("span", { class: "mixer-name" }, channel.nickname),
        h("span", { class: "mixer-label" }, channel.label),
        h("span", { class: "mixer-status" },
          channel.no_input ? h("span", { class: "mixer-none" }, "no input") : null, channel.muted ? mute() : null)));
  }

  function rowHeader(channel) {
    return h("th", { scope: "row", title: channel.nickname ? `${channel.nickname} (${channel.label})` : channel.label },
      h("div", { class: "mixer-row-head" },
        h("div", { class: "mixer-head" },
          channel.nickname ? h("span", { class: "mixer-name" }, channel.nickname) : null,
          h("span", { class: cls("mixer-label", !channel.nickname && "only") }, channel.label)),
        channel.muted ? mute() : null));
  }

  function cell(mixer, input, output) {
    const { on, pending } = mixer.cells[input][output];
    const source = mixer.inputs[input];
    const target = mixer.outputs[output];
    return h("td", null, h("button", {
      type: "button", class: cls("route", on && "on", pending && "pending"), "aria-pressed": String(on),
      "aria-label": `${nameOf(source)} to ${nameOf(target)}, ${on ? "connected" : "not connected"}`,
      "aria-description": pending ? "unapplied" : false, title: pending ? "Unapplied" : false,
      tabindex: focus.input === input && focus.output === output ? "0" : "-1",
      "data-input": input, "data-output": output,
      "data-action": act(() => {
        focus = { input, output };
        send("/api/route", { input, output, on: !on });
      }),
    }));
  }

  // The children of #mixer. A server started before the mixer existed sends none: the tab stays empty.
  function view() {
    const mixer = state().mixer;
    if (!mixer) return [];
    focus = { input: Math.min(focus.input, mixer.inputs.length - 1),
      output: Math.min(focus.output, mixer.outputs.length - 1) };
    return h("table", { class: "mixer-grid", "aria-label": "Routing, inputs to outputs" },
      h("thead", null, h("tr", null,
        h("td", { class: "mixer-corner" }, "In \\ Out"),
        mixer.outputs.map(columnHeader))),
      h("tbody", null, mixer.inputs.map((input, i) => h("tr", null,
        rowHeader(input),
        mixer.outputs.map((_, o) => cell(mixer, i, o))))));
  }

  // An arrow key on a cell moves to the next cell that way, stopping at the edges. Returns whether it was one.
  function keydown(event) {
    const step = ARROWS[event.key];
    const mixer = state()?.mixer;
    if (!step || !mixer || !event.target.matches?.("#mixer .route")) return false;
    const input = Math.max(0, Math.min(mixer.inputs.length - 1, Number(event.target.dataset.input) + step[0]));
    const output = Math.max(0, Math.min(mixer.outputs.length - 1, Number(event.target.dataset.output) + step[1]));
    focus = { input, output };
    render();
    document.querySelector(`#mixer .route[data-input="${input}"][data-output="${output}"]`)?.focus();
    return true;
  }

  return { view, keydown };
}
