// The response plot: magnitude over a log frequency axis, with optional phase and group-delay panels under it, and
// one handle per enabled crossover and EQ band in use. Drawing and the maths that turn pointer, wheel and key input
// into field values live here; posting them is the caller's job (see `createPlot`'s hooks).
//
// Geometry follows the mockup's <svg>: 1320 x 260 drawing units for the magnitude panel, 20 Hz at x = 0 and 20 kHz
// at x = 1320, +10 dB at y = 0 and -40 dB at y = 260 unless a curve needs more room.

const SVG = "http://www.w3.org/2000/svg";
const WIDTH = 1320;
const MAG_HEIGHT = 260;
const PANEL_HEIGHT = 96;
const PANEL_GAP = 24;
const AXIS_HEIGHT = 22;
const LEFT = 36;
const RIGHT = 12;
const TOP = 8;
const LOW_HZ = 20;
const DECADES = 3;  // 20 Hz to 20 kHz
const DB_STEP = 10;
const DB_DEFAULT = [-40, 10];
const DB_LIMITS = [-80, 30];
const HANDLE_FLOOR_DB = 10;  // the least room kept between a handle and the bottom edge
const FREQ_LABELS = [[20, "20"], [50, "50"], [100, "100"], [200, "200"], [500, "500"], [1000, "1k"], [2000, "2k"],
  [5000, "5k"], [10000, "10k"], [20000, "20k"]];
const MINOR_HZ = [50, 200, 500, 2000, 5000];
const MAJOR_HZ = [100, 1000, 10000];
const HANDLE_RADIUS = 9;
const DRAG_THRESHOLD_PX = 3;
const Q_PER_NOTCH = 1.05;
const OCTAVE_STEP = 1 / 24;
const LEGEND_CHAR = 5.8;  // average width of an 11-unit Inter character, for laying out the legend

// Same rule as dragging.drag_frequency in src/sinbosen_la548/dragging.py: round half up, clamp to 10 Hz-20 kHz.
export function dragFrequency(value) {
  if (!Number.isFinite(value)) throw new Error("frequency must be finite");
  return Math.min(20000, Math.max(10, Math.floor(value + 0.5)));
}

const xOf = (hz) => Math.min(WIDTH, Math.max(0, (WIDTH * Math.log10(hz / LOW_HZ)) / DECADES));
const hzAt = (x) => LOW_HZ * 10 ** ((DECADES * x) / WIDTH);
const clamp = (value, [low, high]) => Math.min(high, Math.max(low, value));
const round = (value, digits) => Number(value.toFixed(digits));
const minus = (text) => text.replace("-", "−");

function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (value !== null && value !== undefined && value !== false) el.setAttribute(name, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Ranges

function finite(...series) {
  return series.flatMap((values) => (values ?? []).filter((v) => v !== null && Number.isFinite(v)));
}

// The magnitude range, in 10 dB steps: +10 to -40 dB (the mockup's) unless something the user works with would not
// fit. The top rises to the highest point of the draft or device curve and every handle (at most +30). The bottom
// drops only to keep every handle, and both curves' level at each handle, at least 10 dB above it (at most -80).
// A crossover's rolloff heads to minus infinity, so the curves alone never lower the bottom: below it they are clipped.
export function magnitudeRange(plot) {
  const levels = finite(plot.handles.flatMap((h) => {
    const i = plot.freq.indexOf(h.freq_hz);
    return [h.db, i >= 0 ? plot.draft_db[i] : null, i >= 0 ? plot.device_db?.[i] : null];
  }));
  const high = Math.max(...finite(plot.draft_db, plot.device_db), ...levels);
  const low = Math.min(...levels);
  return [
    clamp(Math.floor((low - HANDLE_FLOOR_DB) / DB_STEP) * DB_STEP, [DB_LIMITS[0], DB_DEFAULT[0]]),
    clamp(Math.ceil(high / DB_STEP) * DB_STEP, [DB_DEFAULT[1], DB_LIMITS[1]]),
  ];
}

// From 0 (or below, if the delay goes clearly negative) to at least 1 ms, in at most four gridline steps.
function delayRange(values) {
  const found = finite(values);
  const high = Math.max(1, ...found);
  // A dip a hair below zero (a rounding residue) does not earn a whole step below the axis.
  const low = Math.min(0, ...found.filter((v) => v < -0.02 * high));
  for (const step of [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]) {
    const range = [Math.floor(low / step) * step, Math.ceil(high / step) * step, step];
    if ((range[1] - range[0]) / step <= 4) return range;
  }
  return [low, high, high - low];
}

// Drawing

function path(freq, values, y, { wraps = false } = {}) {
  let d = "";
  let previous = null;
  values.forEach((value, i) => {
    if (value === null || !Number.isFinite(value)) {
      previous = null;
      return;
    }
    // A wrapped phase jumps from +180 to -180: break the line there instead of drawing the jump.
    const jump = wraps && previous !== null && Math.abs(value - previous) > 180;
    d += `${previous === null || jump ? "M" : "L"}${xOf(freq[i]).toFixed(1)} ${y(value).toFixed(1)}`;
    previous = value;
  });
  return d;
}

function grid(height, rows) {
  const lines = (hz) => hz.map((f) => `M${xOf(f).toFixed(1)} 0V${height}`).join("");
  return [
    s("path", { class: "grid-minor", d: lines(MINOR_HZ) + rows.minor.map((y) => `M0 ${y}H${WIDTH}`).join("") }),
    s("path", { class: "grid-major", d: lines(MAJOR_HZ) + rows.major.map((y) => `M0 ${y}H${WIDTH}`).join("") }),
    s("rect", { class: "frame", x: 0, y: 0, width: WIDTH, height }),
  ];
}

function axisLabels(name, labels) {
  return s("g", { class: "axis", "data-axis": name, "text-anchor": "end" },
    labels.map(([y, text]) => s("text", { x: -6, y }, text)));
}

function frequencyLabels(top) {
  return s("g", { class: "axis", "data-axis": "hz", "text-anchor": "middle" },
    FREQ_LABELS.map(([hz, text]) => s("text", { x: clamp(xOf(hz), [6, WIDTH - 10]), y: top + 16 }, text)));
}

function clipped(id, height, ...content) {
  return [
    s("clipPath", { id }, s("rect", { x: 0, y: 0, width: WIDTH, height })),
    s("g", { "clip-path": `url(#${id})` }, content),
  ];
}

function legend(plot, labels) {
  const items = [["draft", "Your edits"]];
  if (plot.device_db) items.push(["device", "On device"]);
  if (plot.filters?.length) {
    items.push(["filter", plot.filters.length === 1 ? `${labels.get(plot.filters[0].key) ?? "Filter"} alone`
      : "Each filter"]);
  }
  let right = WIDTH - 4;
  const placed = items.reverse().map(([kind, text]) => {
    const x = right - text.length * LEGEND_CHAR - 26;
    right = x - 16;
    return s("g", null, s("path", { class: `curve ${kind}`, d: `M${x.toFixed(1)} 14h20` }),
      s("text", { x: (x + 26).toFixed(1), y: 18 }, text));
  });
  return s("g", { class: "legend" }, placed.reverse());
}

function magnitudePanel(plot, range, labels) {
  const [low, high] = range;
  const y = (db) => (MAG_HEIGHT * (high - db)) / (high - low);
  const rows = { minor: [], major: [] };
  const ticks = [];
  for (let db = high; db >= low; db -= DB_STEP) {
    if (db !== high && db !== low) (db === 0 ? rows.major : rows.minor).push(y(db));
    ticks.push([y(db) + (db === high ? 8 : db === low ? 0 : 4), minus(db > 0 ? `+${db}` : String(db))]);
  }
  const curves = [
    ...(plot.filters ?? []).map((f) => s("path", { class: "curve filter", d: path(plot.freq, f.db, y) })),
    plot.device_db ? s("path", { class: "curve device", d: path(plot.freq, plot.device_db, y) }) : null,
    s("path", { class: "curve draft", d: path(plot.freq, plot.draft_db, y) }),
  ];
  return { y, height: MAG_HEIGHT, content: [grid(MAG_HEIGHT, rows), axisLabels("db", ticks),
    clipped("clip-magnitude", MAG_HEIGHT, curves), legend(plot, labels)] };
}

function lowerPanel(name, title, freq, values, [low, high, step],
  { wraps = false, digits = 0, labelEvery = step } = {}) {
  const y = (v) => (PANEL_HEIGHT * (high - v)) / (high - low);
  const rows = { minor: [], major: [] };
  const ticks = [];
  for (let v = high; v >= low - 1e-9; v -= step) {
    if (v !== high && Math.abs(v - low) > 1e-9) (Math.abs(v) < 1e-9 ? rows.major : rows.minor).push(y(v));
    if (Math.abs(v / labelEvery - Math.round(v / labelEvery)) > 1e-9) continue;
    const text = Math.abs(v) < 1e-9 ? "0" : `${v > 0 && wraps ? "+" : ""}${round(v, digits)}`;
    ticks.push([y(v) + (v === high ? 8 : Math.abs(v - low) < 1e-9 ? 0 : 4), minus(text)]);
  }
  return { name, height: PANEL_HEIGHT, content: [grid(PANEL_HEIGHT, rows), axisLabels(name, ticks),
    clipped(`clip-${name}`, PANEL_HEIGHT, s("path", { class: "curve lower", d: path(freq, values, y, { wraps }) })),
    s("text", { class: "panel-title", x: WIDTH - 6, y: 14, "text-anchor": "end" }, title)] };
}

// Handles

const shortLabel = (key) => (key === "hp" ? "HP" : key === "lp" ? "LP" : String(Number(key.split(":")[1]) + 1));

function fieldOf(filter, key) {
  return filter?.fields.find((field) => field.key === key) ?? null;
}

function valueText(values) {
  const parts = [`${values.freq_hz} Hz`];
  if (values.gain_db !== undefined) parts.push(`${minus(values.gain_db.toFixed(1))} dB`);
  if (values.q !== undefined) parts.push(`Q ${values.q.toFixed(2)}`);
  return parts.join(", ");
}

export function createPlot(container, hooks) {
  const svg = s("svg", { role: "group", "aria-label": "Response plot", class: "plot-svg" });
  const panels = s("g");
  const handleLayer = s("g", { class: "handles" });
  svg.append(panels, handleLayer);
  container.replaceChildren(svg);

  let current = null;  // {plot, filters, edit, range, scale}: what was drawn last
  let drag = null;     // {key, pointerId, start: {x, y}, values0, values, moved, scale, range}

  // The values a handle stands for: the state's, with the gesture in progress laid over them.
  function valuesOf(key) {
    const filter = current.filters.find((f) => f.key === key);
    const values = {};
    for (const name of ["freq_hz", "gain_db", "q"]) {
      const field = fieldOf(filter, name);
      if (field) values[name] = field.value;
    }
    return current.edit?.key === key ? { ...values, ...current.edit.values } : values;
  }

  function bounds(key, name) {
    const field = fieldOf(current.filters.find((f) => f.key === key), name);
    return field ? [field.min, field.max] : [-Infinity, Infinity];
  }

  function point(event) {
    const matrix = svg.getScreenCTM()?.inverse();
    return matrix ? new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix) : { x: 0, y: 0 };
  }

  function dragValues(event) {
    const at = point(event);
    const { key, values0, start, hasGain } = drag;
    const values = { freq_hz: dragFrequency(hzAt(xOf(values0.freq_hz) + at.x - start.x)) };
    if (hasGain) {
      values.gain_db = round(clamp(values0.gain_db - (at.y - start.y) / drag.scale, bounds(key, "gain_db")), 1);
    }
    return values;
  }

  function endDrag(el, cancelled) {
    const ending = drag;
    drag = null;
    if (el.hasPointerCapture(ending.pointerId)) el.releasePointerCapture(ending.pointerId);
    if (cancelled) hooks.cancel(ending.key);
    else if (ending.moved) hooks.end(ending.key, ending.values);
    else hooks.select(ending.key);
  }

  handleLayer.addEventListener("pointerdown", (event) => {
    const el = event.target.closest(".handle");
    if (!el || event.button !== 0 || drag) return;
    const key = el.dataset.key;
    const handle = current.plot.handles.find((h) => h.key === key);
    const values0 = valuesOf(key);
    drag = { key, pointerId: event.pointerId, start: point(event), client: [event.clientX, event.clientY], values0,
      values: null, moved: false, hasGain: handle.has_gain && values0.gain_db !== undefined, scale: current.scale,
      range: current.range };
    el.setPointerCapture(event.pointerId);
    const before = { freq_hz: values0.freq_hz };
    if (drag.hasGain) before.gain_db = values0.gain_db;
    hooks.begin(key, before);
    event.preventDefault();
    el.focus({ preventScroll: true });
  });

  handleLayer.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!drag.moved && Math.hypot(event.clientX - drag.client[0], event.clientY - drag.client[1]) < DRAG_THRESHOLD_PX) {
      return;
    }
    drag.moved = true;
    drag.values = dragValues(event);
    hooks.change(drag.key, drag.values);
  });

  handleLayer.addEventListener("pointerup", (event) => {
    if (drag && event.pointerId === drag.pointerId) endDrag(event.target.closest(".handle") ?? svg, false);
  });

  handleLayer.addEventListener("lostpointercapture", (event) => {
    if (drag && event.pointerId === drag.pointerId) endDrag(event.target, false);
  });

  // Escape during a drag puts the handle back where it was; it is caught before the page's own Escape handling.
  window.addEventListener("keydown", (event) => {
    if (!drag || event.key !== "Escape") return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const el = handleLayer.querySelector(`[data-key="${CSS.escape(drag.key)}"]`);
    endDrag(el ?? svg, true);
  }, true);

  // The wheel over a handle changes Q by 5% per notch and never scrolls the page. The blocking listener sits on the
  // handles only, so the wheel over the rest of the plot stays on the browser's fast scroll path.
  handleLayer.addEventListener("wheel", (event) => {
    const el = event.target.closest(".handle");
    if (!el) return;
    event.preventDefault();
    const key = el.dataset.key;
    const q = valuesOf(key).q;
    if (q === undefined || drag) return;
    const notches = -event.deltaY / [100, 3, 1][event.deltaMode];
    const next = round(clamp(q * Q_PER_NOTCH ** notches, bounds(key, "q")), 3);
    if (next !== q) hooks.nudge(key, { q: next });
  }, { passive: false });

  handleLayer.addEventListener("keydown", (event) => {
    const el = event.target.closest(".handle");
    if (!el || drag) return;
    const key = el.dataset.key;
    const values = valuesOf(key);
    const handle = current.plot.handles.find((h) => h.key === key);
    let change = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const sign = event.key === "ArrowRight" ? 1 : -1;
      let hz = dragFrequency(values.freq_hz * 2 ** (sign * OCTAVE_STEP));
      // Low down an octave step rounds back to the same hertz; move by at least one.
      if (hz === values.freq_hz) hz = dragFrequency(values.freq_hz + sign);
      if (hz !== values.freq_hz) change = { freq_hz: hz };
    } else if ((event.key === "ArrowUp" || event.key === "ArrowDown") && handle?.has_gain
      && values.gain_db !== undefined) {
      const step = (event.shiftKey ? 1 : 0.1) * (event.key === "ArrowUp" ? 1 : -1);
      const db = round(clamp(values.gain_db + step, bounds(key, "gain_db")), 1);
      if (db !== values.gain_db) change = { gain_db: db };
    } else {
      return;
    }
    event.preventDefault();
    if (change) hooks.nudge(key, change);
  });

  function drawHandles(plot, y) {
    const wanted = new Set(plot.handles.map((h) => h.key));
    for (const el of [...handleLayer.children]) if (!wanted.has(el.dataset.key)) el.remove();
    plot.handles.forEach((handle, i) => {
      let el = handleLayer.querySelector(`[data-key="${CSS.escape(handle.key)}"]`);
      if (!el) {
        el = s("g", { class: "handle", "data-key": handle.key, tabindex: 0, role: "slider" },
          s("circle", { class: "hit", r: HANDLE_RADIUS + 5 }), s("circle", { class: "ring", r: HANDLE_RADIUS }),
          s("text", { y: 3.5 }, shortLabel(handle.key)));
      }
      if (handleLayer.children[i] !== el) handleLayer.insertBefore(el, handleLayer.children[i] ?? null);
      const values = valuesOf(handle.key);
      const editing = current.edit?.key === handle.key;
      // While a gesture is in progress the handle follows it, not the last state: x from the edited frequency,
      // y from the curve plus whatever gain the server has not echoed yet.
      const hz = editing ? values.freq_hz : handle.freq_hz;
      const stateGain = fieldOf(current.filters.find((f) => f.key === handle.key), "gain_db")?.value ?? 0;
      const db = (handle.db ?? 0) + (editing && handle.has_gain ? (values.gain_db ?? stateGain) - stateGain : 0);
      el.setAttribute("transform", `translate(${xOf(hz).toFixed(1)} ${clamp(y(db), [0, MAG_HEIGHT]).toFixed(1)})`);
      el.classList.toggle("pending", handle.pending || editing);
      el.setAttribute("aria-label", handle.label);
      el.setAttribute("aria-valuenow", String(values.freq_hz));
      el.setAttribute("aria-valuemin", "10");
      el.setAttribute("aria-valuemax", "20000");
      el.setAttribute("aria-valuetext", valueText(values));
    });
  }

  function draw(plot, filters, edit) {
    // The range holds still while a handle is dragged, so the handle stays under the pointer.
    const range = drag?.range ?? magnitudeRange(plot);
    const labels = new Map(plot.handles.map((h) => [h.key, h.label]));
    const magnitude = magnitudePanel(plot, range, labels);
    current = { plot, filters, edit, range, scale: MAG_HEIGHT / (range[1] - range[0]) };
    const list = [{ name: "magnitude", height: magnitude.height, content: magnitude.content }];
    if (plot.phase_deg) {
      list.push(lowerPanel("phase", "Phase (°)", plot.freq, plot.phase_deg, [-180, 180, 90],
        { wraps: true, labelEvery: 180 }));
    }
    if (plot.group_delay_ms) {
      const range = delayRange(plot.group_delay_ms);
      list.push(lowerPanel("group-delay", "Group delay (ms)", plot.freq, plot.group_delay_ms, range,
        { digits: range[2] < 1 ? 1 : 0 }));
    }
    let top = 0;
    const groups = list.map((panel, i) => {
      const g = s("g", { "data-panel": panel.name, transform: `translate(0 ${top})` }, panel.content);
      top += panel.height + (i < list.length - 1 ? PANEL_GAP : 0);
      return g;
    });
    groups.push(frequencyLabels(top));
    panels.replaceChildren(...groups);
    svg.setAttribute("viewBox", `${-LEFT} ${-TOP} ${LEFT + WIDTH + RIGHT} ${TOP + top + AXIS_HEIGHT}`);
    drawHandles(plot, magnitude.y);
  }

  return { draw };
}
