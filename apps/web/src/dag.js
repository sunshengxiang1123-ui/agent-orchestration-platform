// 工作流 DAG 渲染：按依赖深度分列布局，loop / subflow 展开为容器，节点按 Trace 事件着色。
// 纯函数、无 DOM 依赖，返回 SVG 字符串。

const NODE_W = 156;
const NODE_H = 52;
const GAP_X = 44;
const GAP_Y = 16;
const PAD = 14;
const HEAD = 24;
const LOOP_FOOT = 30;

const TYPE_NAMES = {agent: "Agent", tool: "工具", router: "路由", parallel: "并行", loop: "循环", subflow: "子流程", verify: "校验", human_gate: "人工关卡"};
const COLORS = {
  pending: ["#f4f7f8", "#cfdbdf", "#6f828b"],
  running: ["#e5f0fb", "#5b93c9", "#245a8c"],
  succeeded: ["#e0f3e9", "#55a882", "#1b6b50"],
  failed: ["#fbece7", "#d48b78", "#9b4330"],
  awaiting_human: ["#fff1ce", "#d6ad4f", "#7d5d14"],
  skipped: ["#f4f7f8", "#dde5e8", "#9aa9b0"],
};
const STATUS_NAMES = {pending: "未执行", running: "执行中", succeeded: "通过", failed: "未通过", awaiting_human: "待审批", skipped: "跳过"};

// 页面上可能同时有多张图；marker id 必须唯一，否则会引用到隐藏面板里的定义。
let renderCount = 0;

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

/** 从 Trace 事件推导每个节点的最新状态、执行次数和所在轮次。 */
export function nodeStates(trace) {
  const states = {};
  if (!trace) return states;
  const finished = !["queued", "running"].includes(trace.status);
  for (const event of trace.events || []) {
    const id = event.data && event.data.node;
    if (!id) continue;
    const state = states[id] || (states[id] = {status: "pending", runs: 0, iteration: 0});
    if (event.kind === "node_started") {
      state.status = "running";
      if (event.data.candidate === null || event.data.candidate === undefined || event.data.candidate === 0) state.runs += 1;
      state.iteration = Math.max(state.iteration, event.iteration || 1);
    } else if (event.kind === "node_finished") {
      state.status = event.data.awaiting_human ? "awaiting_human" : (event.data.passed ? "succeeded" : "failed");
    }
  }
  if (finished) for (const state of Object.values(states)) if (state.status === "running") state.status = "failed";
  return states;
}

function measure(nodes, subflows, iterations) {
  const byId = Object.fromEntries(nodes.map(node => [node.id, node]));
  const depth = {};
  const depthOf = id => {
    if (depth[id] !== undefined) return depth[id];
    depth[id] = 0;
    const parents = (byId[id].depends_on || []).filter(parent => byId[parent]);
    depth[id] = parents.length ? Math.max(...parents.map(depthOf)) + 1 : 0;
    return depth[id];
  };
  nodes.forEach(node => depthOf(node.id));
  const boxes = nodes.map(node => ({node, ...sizeOf(node, subflows, iterations)}));
  const columns = [];
  boxes.forEach(box => (columns[depth[box.node.id]] ||= []).push(box));
  const heights = columns.map(column => column.reduce((sum, box) => sum + box.h, 0) + GAP_Y * (column.length - 1));
  const height = Math.max(...heights);
  let x = 0;
  columns.forEach((column, index) => {
    const width = Math.max(...column.map(box => box.w));
    let y = (height - heights[index]) / 2;
    for (const box of column) {
      box.x = x + (width - box.w) / 2;
      box.y = y;
      y += box.h + GAP_Y;
    }
    x += width + GAP_X;
  });
  return {w: x - GAP_X, h: height, boxes};
}

function sizeOf(node, subflows, iterations) {
  let inner = null;
  if (node.type === "loop" && node.loop) inner = measure(node.loop.body, subflows, iterations);
  else if (node.type === "subflow" && subflows[node.subflow]) inner = measure(subflows[node.subflow].nodes, subflows, iterations);
  if (!inner) return {w: NODE_W, h: NODE_H, inner: null};
  const foot = node.type === "loop" ? LOOP_FOOT : 0;
  return {w: Math.max(inner.w + PAD * 2, NODE_W + 40), h: inner.h + PAD * 2 + HEAD + foot, inner};
}

function descendants(box) {
  if (!box.inner) return [box.node.id];
  return box.inner.boxes.flatMap(descendants);
}

function draw(layout, ox, oy, states, iterations, out, uid) {
  const byId = Object.fromEntries(layout.boxes.map(box => [box.node.id, box]));
  for (const box of layout.boxes) {
    for (const parent of box.node.depends_on || []) {
      const from = byId[parent];
      if (!from) continue;
      const x1 = ox + from.x + from.w, y1 = oy + from.y + from.h / 2;
      const x2 = ox + box.x, y2 = oy + box.y + box.h / 2;
      const mid = (x1 + x2) / 2;
      out.push(`<path d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 - 2},${y2}" fill="none" stroke="#9fb4bc" stroke-width="1.5" marker-end="url(#${uid}-arrow)"/>`);
    }
  }
  for (const box of layout.boxes) {
    const x = ox + box.x, y = oy + box.y, node = box.node;
    if (box.inner) {
      const inside = descendants(box).map(id => states[id]).filter(Boolean);
      const running = inside.some(state => state.status === "running");
      const round = Math.max(0, ...inside.map(state => state.iteration));
      const stroke = running ? COLORS.running[1] : "#c3d2d7";
      let label = `${node.id} · ${TYPE_NAMES[node.type] || node.type}`;
      if (node.type === "loop") label += round ? ` · 第 ${round}/${iterations} 轮` : ` · 最多 ${iterations} 轮`;
      if (node.type === "subflow") label += ` · ${node.subflow}`;
      out.push(`<rect x="${x}" y="${y}" width="${box.w}" height="${box.h}" rx="12" fill="#fbfdfd" stroke="${stroke}" stroke-dasharray="5 4" stroke-width="1.5"/>`);
      out.push(`<text x="${x + 12}" y="${y + 17}" font-size="12" font-weight="600" fill="#4d6670">${esc(label)}</text>`);
      if (node.type === "loop") {
        const top = y + HEAD + PAD + box.inner.h + 4;
        const left = x + 30, right = x + box.w - 30;
        out.push(`<path d="M${right},${top} C${right},${top + 16} ${left},${top + 16} ${left},${top + 2}" fill="none" stroke="#c99a3a" stroke-width="1.4" marker-end="url(#${uid}-warm)"/>`);
        out.push(`<text x="${x + box.w / 2}" y="${top + 24}" font-size="11" text-anchor="middle" fill="#9a7426">未通过 → 带着失败原因进入下一轮</text>`);
      }
      draw(box.inner, x + (box.w - box.inner.w) / 2, y + HEAD + PAD, states, iterations, out, uid);
      continue;
    }
    const state = states[node.id] || {status: "pending", runs: 0};
    const [fill, stroke, ink] = COLORS[state.status] || COLORS.pending;
    const detail = node.agent || node.verifier || node.tool || (node.type === "parallel" ? `×${node.n_candidates || 1} 候选` : "") || (node.condition ? "条件触发" : "");
    out.push(`<rect x="${x}" y="${y}" width="${box.w}" height="${box.h}" rx="9" fill="${fill}" stroke="${stroke}" stroke-width="1.6"><title>${esc(STATUS_NAMES[state.status])}</title></rect>`);
    out.push(`<text x="${x + 12}" y="${y + 21}" font-size="13" font-weight="700" fill="${ink}">${esc(node.id)}</text>`);
    out.push(`<text x="${x + 12}" y="${y + 39}" font-size="11" fill="${ink}">${esc(TYPE_NAMES[node.type] || node.type)}${detail ? " · " + esc(detail) : ""}</text>`);
    if (state.runs > 1) out.push(`<text x="${x + box.w - 10}" y="${y + 21}" font-size="11" text-anchor="end" fill="${ink}">×${state.runs}</text>`);
  }
}

/** 渲染整张工作流图；trace 为空时只画结构。 */
export function renderDag(definition, subflows, trace) {
  const iterations = (trace && trace.inputs && trace.inputs.max_iterations) || (definition.nodes.find(node => node.type === "loop")?.loop?.max_iterations ?? 1);
  const layout = measure(definition.nodes, subflows || {}, iterations);
  const states = nodeStates(trace);
  const margin = 12;
  const out = [];
  const uid = `dag${++renderCount}`;
  draw(layout, margin, margin, states, iterations, out, uid);
  const width = layout.w + margin * 2, height = layout.h + margin * 2 + 18;
  const legend = Object.entries(STATUS_NAMES).filter(([key]) => key !== "skipped").map(([key, name], index) => {
    const lx = margin + index * 78;
    return `<rect x="${lx}" y="${height - 2}" width="11" height="11" rx="3" fill="${COLORS[key][0]}" stroke="${COLORS[key][1]}"/><text x="${lx + 16}" y="${height + 7}" font-size="11" fill="#6b818b">${name}</text>`;
  }).join("");
  return `<svg class="dag" viewBox="0 0 ${Math.max(width, 420)} ${height + 16}" width="${Math.max(width, 420)}" height="${height + 16}" xmlns="http://www.w3.org/2000/svg" font-family="Inter, 'Microsoft YaHei', sans-serif">`
    + `<defs><marker id="${uid}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#9fb4bc"/></marker>`
    + `<marker id="${uid}-warm" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#c99a3a"/></marker></defs>`
    + out.join("") + legend + `</svg>`;
}
