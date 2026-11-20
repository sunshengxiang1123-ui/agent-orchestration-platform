// 原生同源控制台：只展示后端实测数据，不使用固定成功结果或离线回退。
import {renderDag} from "./dag.js";
const root = document.querySelector("#app");
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pretty = value => escape(JSON.stringify(value, null, 2));
const statusNames = {queued:"排队中",running:"执行中",succeeded:"已通过",failed:"未通过",cancelled:"已取消",awaiting_human:"待人工审批"};
const eventNames = {queued:"任务入队",scheduled:"优先级调度",node_started:"节点开始",tool_result:"MCP 工具调用",agent_selected:"角色动态分配",tool_decision:"Agent 决定调用工具",tool_calling_fallback:"降级为平台预取",llm_started:"真实模型调用",node_finished:"节点结束",verification:"程序核验",feedback:"反馈与修正",knowledge_retrieved:"知识库检索",finished:"运行结束",human_review:"人工审批",resumed:"从断点继续",cancelled:"取消"};
let activeRun = null;
let refreshing = false;
let config = null;
let agents = [];
let currentTrace = null;
let workflowData = null;
let traceSignature = "";
let agentTemplateReady = false;
async function api(path, body, method) {
  const init = {method: method || (body === undefined ? "GET" : "POST")};
  if (body !== undefined) { init.headers = {"Content-Type":"application/json"}; init.body = JSON.stringify(body); }
  const response = await fetch("/api" + path, init);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function notice(message, error = false) {
  const element = document.querySelector("#notice");
  element.textContent = message;
  element.className = error ? "notice error" : "notice";
}
function badge(status) { return '<span class="badge ' + escape(status) + '">' + escape(statusNames[status] || status) + '</span>'; }
root.innerHTML = '<aside><div class="brand">A<span> / </span>FLOW</div><p>Agent 编排调度平台</p><nav><button data-panel="runs" class="selected">任务与监控</button><button data-panel="workflow">工作流</button><button data-panel="agents">Agent 团队</button><button data-panel="approvals">人工审批</button><button data-panel="settings">模型配置</button></nav><div class="sidebar-note">本地单机原型<br>固定数据 · 真实模型<br>不自动重放收费请求</div></aside><div class="workspace"><header><div><small>未来杯 · 多 Agent 协作原型</small><h1>从任务到可验证的结果</h1></div><div id="connection">检查后端连接…</div></header><div id="notice" class="notice">正在读取运行状态…</div><section id="panel-runs"><div class="grid"><form id="run-form" class="card"><div class="section-label">01 / 新建任务</div><h2>规划一日旅行</h2><p class="muted">使用人工编制的示例景点，价格和营业时间不代表现实数据。</p><label>需求<textarea name="topic" maxlength="500" required>安排一日文化与自然之旅，尽量节省费用，说明安排理由。</textarea></label><div class="row"><label>总预算 / 元<input name="budget" type="number" min="40" max="2000" value="200" required></label><label>至少景点数<input name="min_stops" type="number" min="1" max="5" value="3" required></label></div><div class="row"><label>开始时间<input name="start" type="time" value="09:00" required></label><label>结束时间<input name="end" type="time" value="18:00" required></label></div><div class="row"><label>星期<select name="weekday"><option value="1">周一</option><option value="2">周二</option><option value="3">周三</option><option value="4">周四</option><option value="5">周五</option><option value="6" selected>周六</option><option value="7">周日</option></select></label><label>天气<select name="weather"><option value="sunny" selected>晴天</option><option value="rainy">雨天（最多 1 个户外景点）</option></select></label></div><fieldset class="must-visit"><legend>必去景点（可多选）</legend><div id="must-visit" class="muted">加载中…</div></fieldset><div class="row"><label>迭代上限<select name="max_iterations"><option value="1">1 轮 / 最多 6 次调用</option><option value="2">2 轮 / 最多 12 次调用</option><option value="3" selected>3 轮 / 最多 18 次调用</option></select></label><label>优先级<input name="priority" type="number" min="0" max="10" value="5" required></label></div><label class="check"><input type="checkbox" name="human_review" checked>自动验证后再由人工审批</label><label class="check consent"><input type="checkbox" name="confirm_paid" required>我确认本次调用真实 API，可能产生费用（Agent 每步最多与模型往返 3 次，每次输出最多 1024 token，整次运行上限 40000 token）</label><button class="primary" id="start-run" disabled>启动真实工作流 →</button><p class="muted">未授权时不启动。服务错误不自动重发；取消后已发送的请求仍可能计费。</p></form><div><div class="card"><div class="section-label">02 / 执行记录</div><div id="run-list" class="run-list">加载中…</div></div><div class="card overview" id="summary"><h2>等待选择任务</h2><p>这里显示实际调用次数、token 消耗、自动验证与审核结果。</p></div></div></div><div class="card"><div class="section-label">03 / 执行与反馈 Trace</div><div id="run-graph" class="dag-wrap"></div><div id="trace"><p class="muted">启动任务后，每秒刷新一次后端事件。</p></div></div></section><section id="panel-workflow" hidden><div class="card"><h2>工作流编排（YAML 定义）</h2><p>并行预取 MCP 工具 → Agent 通过 function calling 自主调用工具并循环规划 → 嵌套验证子流程 → 条件人工审批。具体调度和结果来自运行时，不预设成功。</p><div id="workflow-graph" class="dag-wrap">加载中…</div><p class="muted">定义文件位于 packages/application/workflows/，由安全加载器校验必填字段、依赖、唯一性与无环后交给通用 DAG 执行器；修改 YAML 后重启生效。</p><div id="workflow-yaml"></div><details><summary>解析后的结构（JSON）</summary><pre id="workflow-json"></pre></details><h3>MCP 工具服务</h3><div id="mcp-tools" class="muted">加载中…</div><h3>景点目录</h3><div id="catalog"></div></div></section><section id="panel-agents" hidden><div class="card"><h2>角色注册、发现与版本</h2><p>调度按角色发现每个 Agent 的最新版本，综合技能、当前负载和历史表现选择。已有版本不可覆盖。</p><div id="agents"></div><h3>注册一个新版本</h3><p class="muted">下列 JSON 为可编辑的配置，不会触发模型请求。Prompt 修改应递增版本。</p><textarea id="agent-json" class="code" spellcheck="false"></textarea><button id="register-agent" class="primary">注册版本</button></div></section><section id="panel-approvals" hidden><div class="card"><h2>人工最终审批</h2><p>只有程序校验和独立角色审核都通过的任务，才进入这里。拒绝不会自动产生新的收费请求。</p><div id="approvals"></div></div></section><section id="panel-settings" hidden><div class="grid"><form id="settings-form" class="card" autocomplete="off"><div class="section-label">模型配置</div><h2>API Key 与模型</h2><p class="muted">保存后写入项目根目录的 .env（已被 .gitignore 忽略），立即生效，无需重启。页面不会回显已保存的密钥。</p><label>API Key<input name="api_key" type="password" autocomplete="new-password" spellcheck="false" maxlength="512"></label><label class="check"><input type="checkbox" id="show-key">显示正在输入的密钥</label><label>接口地址（OpenAI 兼容，HTTPS）<input name="base_url" maxlength="300" placeholder="https://api.deepseek.com" required></label><label>模型名<input name="model" maxlength="256" placeholder="deepseek-flash" required></label><div class="row"><label>输出上限 / token<input name="max_tokens" type="number" min="1" max="8192" required></label><label>超时 / 秒<input name="timeout_seconds" type="number" min="1" max="180" step="1" required></label></div><p class="muted">工作流内每次调用的输出仍会被限制在 1024 token 以内，与启动任务时的费用确认一致。</p><button class="primary" id="save-settings">保存配置</button> <button type="button" id="test-settings">测试连接</button> <button type="button" id="clear-key" class="danger">清除密钥</button></form><div class="card"><div class="section-label">当前状态</div><div id="settings-status">加载中…</div><p class="muted">“测试连接”只请求服务商的 /models 接口验证地址与密钥，不调用对话接口，不消耗 token。</p></div></div></section><footer>结果可追溯 · 实际费用以服务商账单为准 · 此本地原型无公网用户认证，请勿公开暴露端口</footer></div>';

document.querySelectorAll("[data-panel]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("section[id^='panel-']").forEach(panel => panel.hidden = panel.id !== "panel-" + button.dataset.panel);
  document.querySelectorAll("[data-panel]").forEach(item => item.classList.toggle("selected", item === button));
}));
function minutes(value) { const [h,m] = value.split(":").map(Number); return h*60+m; }
document.querySelector("#run-form").addEventListener("submit", async event => {
  event.preventDefault();
  const data = new FormData(event.target);
  const button = document.querySelector("#start-run");
  button.disabled = true;
  try {
    const run = await api("/runs", {topic:data.get("topic"),budget:Number(data.get("budget")),min_stops:Number(data.get("min_stops")),day_start:minutes(data.get("start")),day_end:minutes(data.get("end")),max_iterations:Number(data.get("max_iterations")),priority:Number(data.get("priority")),weekday:Number(data.get("weekday")),weather:data.get("weather"),must_visit:data.getAll("must_visit"),human_review:data.has("human_review"),confirm_paid:data.has("confirm_paid")});
    activeRun = run.run_id;
    document.querySelector('[name="confirm_paid"]').checked = false;
    notice("任务已提交。运行期间可以观察候选 Agent、评分和校验反馈。");
    await refresh();
  } catch(error) { notice(error.message, true); }
  finally { button.disabled = !config?.ready; }
});
document.querySelector("#register-agent").addEventListener("click", async () => {
  try { await api("/agents", JSON.parse(document.querySelector("#agent-json").value)); notice("新版本已注册，后续调度将发现新版本。"); await loadAgents(); }
  catch(error) { notice(error.message, true); }
});
async function loadAgents() {
  agents = await api("/agents");
  document.querySelector("#agents").innerHTML = '<div class="agent-grid">' + agents.map(agent => '<article class="agent"><small>' + escape(agent.role) + '</small><h3>' + escape(agent.name) + '</h3><code>' + escape(agent.key) + '</code><p>' + escape(agent.skills.join(" · ")) + '</p><p>负载 ' + agent.load + '/' + agent.concurrency + ' · 样本 ' + agent.history.samples + '</p><p class="muted">记忆：运行反馈 + 历史表现</p><button data-copy="' + escape(agent.key) + '">复制为新版本</button></article>').join("") + '</div>';
  document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", () => {
    const agent = agents.find(item => item.key === button.dataset.copy);
    const {key,load,history,...definition} = agent;
    const version = definition.version.split(".").map(Number); version[2]++;
    definition.version = version.join(".");
    document.querySelector("#agent-json").value = JSON.stringify(definition,null,2);
  }));
  if (!agentTemplateReady && agents.length) {
    const {key,load,history,...example} = agents[0]; example.version = "1.0.1";
    document.querySelector("#agent-json").value = JSON.stringify(example,null,2);
    agentTemplateReady = true;
  }
}
function renderTrace(trace) {
  const signature = JSON.stringify([trace.run_id,trace.status,trace.calls,trace.spent_tokens,trace.events.at(-1)?.seq,trace.nodes.length]);
  if (signature === traceSignature) return;
  traceSignature = signature;
  currentTrace = trace;
  const result = trace.validation;
  document.querySelector("#summary").innerHTML = '<div class="summary-title"><h2>' + badge(trace.status) + '</h2><small>' + escape(trace.mode === "live" ? "真实模型运行" : "测试传输，非真实运行") + '</small></div><code class="run-id">' + escape(trace.run_id) + '</code><div class="metrics"><div><strong>' + trace.calls + '/' + trace.call_limit + '</strong><span>调用次数</span></div><div><strong>' + trace.spent_tokens + '</strong><span>已记录 token</span></div><div><strong>' + trace.iteration + '</strong><span>迭代轮次</span></div></div>' +
    (result ? '<p>程序核验：' + (result.passed ? '通过' : '未通过') + ' · 总预算 ' + result.total_cost + '/' + result.budget + ' 元</p>' : '<p class="muted">尚无程序核验结果</p>') +
    (trace.feedback.length ? '<div class="feedback">' + trace.feedback.map(escape).join('<br>') + '</div>' : '') +
    (trace.error ? '<p class="error">' + escape(trace.error) + '</p>' : '') +
    (trace.plan ? '<details open><summary>最新计划</summary><pre>' + pretty(trace.plan) + '</pre></details>' : '') +
    (['queued','running','awaiting_human'].includes(trace.status) ? '<button id="cancel-run" class="danger">取消本次任务</button>' : '') +
    (trace.resumable ? '<p class="muted">该任务被服务停止或重启中断。已完成的节点会从 checkpoint 恢复，不会重复调用模型。</p><button id="resume-run" class="primary">从断点继续</button>' : '');
  document.querySelector("#resume-run")?.addEventListener("click", async () => {
    if (!confirm("从断点继续会接着调用真实模型，可能产生费用。确定继续？")) return;
    try { await api('/runs/' + encodeURIComponent(activeRun) + '/resume', {confirm_paid: true}); notice("已从断点继续，已完成的节点不会重复执行。"); await refresh(); }
    catch(error) { notice(error.message, true); }
  });
  document.querySelector("#cancel-run")?.addEventListener("click", async () => {
    try { await api('/runs/' + encodeURIComponent(activeRun) + '/cancel', {}); notice("已申请取消；已发送请求可能已计费。"); await refresh(); }
    catch(error) { notice(error.message,true); }
  });
  if (workflowData) document.querySelector("#run-graph").innerHTML = renderDag(workflowData.definition, workflowData.subflows, trace);
  document.querySelector("#trace").innerHTML = trace.events.length ? '<ol class="timeline">' + trace.events.map(event => '<li><div><span class="event-label">' + escape(eventNames[event.kind] || event.kind) + '</span><time>' + escape(new Date(event.at).toLocaleTimeString()) + '</time><small>第 ' + (event.iteration || 1) + ' 轮</small></div><details ' + (['agent_selected','verification','feedback'].includes(event.kind) ? 'open' : '') + '><summary>' + escape(event.data.chosen || event.data.node || event.data.status || event.data.name || '查看详情') + '</summary><pre>' + pretty(event.data) + '</pre></details></li>').join('') + '</ol>' : '<p>等待执行事件…</p>';
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const runs = await api("/runs");
    document.querySelector("#run-list").innerHTML = runs.length ? runs.map(run => '<button class="run-item ' + (run.run_id === activeRun ? 'active' : '') + '" data-run="' + escape(run.run_id) + '"><span>' + escape(run.inputs.topic) + '</span>' + badge(run.status) + '<small>' + (run.resumable ? '可从断点继续 · ' : '') + escape(run.created_at.slice(0,19).replace('T',' ') + ' UTC') + '</small></button>').join('') : '<p class="muted">暂无任务。没有预置的成功记录。</p>';
    document.querySelectorAll("[data-run]").forEach(button => button.addEventListener("click", async () => { activeRun = button.dataset.run; await refresh(); }));
    if (!activeRun && runs.length) activeRun = runs[0].run_id;
    if (activeRun) renderTrace(await api('/runs/' + encodeURIComponent(activeRun) + '/trace'));
    if (!document.querySelector("#panel-agents").hidden) await loadAgents();
    const pending = runs.filter(run => run.status === 'awaiting_human');
    document.querySelector("#approvals").innerHTML = pending.length ? pending.map(run => '<article class="approval"><h3>' + escape(run.inputs.topic) + '</h3><code>' + escape(run.run_id) + '</code><details><summary>查看结果与独立审核</summary><pre>' + pretty({plan:run.plan,validation:run.validation,review:run.review}) + '</pre></details><label>审批意见<input maxlength="1000" data-comment="' + escape(run.run_id) + '"></label><button data-review="' + escape(run.run_id) + '" data-approved="true" class="primary">通过</button> <button data-review="' + escape(run.run_id) + '" data-approved="false" class="danger">拒绝</button></article>').join('') : '<p class="muted">暂无待审批任务。</p>';
    document.querySelectorAll("[data-review]").forEach(button => button.addEventListener("click", async () => {
      const id = button.dataset.review;
      try { await api('/runs/' + encodeURIComponent(id) + '/review', {approved:button.dataset.approved === 'true',comment:document.querySelector('[data-comment="' + id + '"]').value}); notice("审批已保存。"); await refresh(); }
      catch(error) { notice(error.message,true); }
    }));
  } catch(error) { notice("读取后端失败：" + error.message + "。不会使用模拟数据代替。",true); }
  finally { refreshing = false; }
}
function applyConfig(value) {
  config = value;
  document.querySelector("#connection").innerHTML = '<span class="dot ' + (config.ready ? 'ready' : '') + '"></span>' + escape(config.model) + '<small>' + escape(config.ready ? '模型配置就绪' : '模型配置待处理') + '</small>';
  document.querySelector("#start-run").disabled = !config.ready;
}
// --- 模型配置页面 ---
const settingsForm = document.querySelector("#settings-form");
function settingsPayload(extra) {
  const data = new FormData(settingsForm);
  return Object.assign({api_key:String(data.get("api_key") || "").trim(), base_url:String(data.get("base_url") || "").trim(), model:String(data.get("model") || "").trim(), max_tokens:Number(data.get("max_tokens")), timeout_seconds:Number(data.get("timeout_seconds"))}, extra || {});
}
function renderSettings(value) {
  const fields = settingsForm.elements;
  fields.namedItem("api_key").value = "";
  fields.namedItem("api_key").placeholder = value.key_configured ? "已保存 " + value.key_hint + "，留空表示不修改" : "粘贴你的 API Key，例如 sk-…";
  fields.namedItem("base_url").value = value.base_url;
  fields.namedItem("model").value = value.model;
  fields.namedItem("max_tokens").value = value.max_tokens;
  fields.namedItem("timeout_seconds").value = value.timeout_seconds;
  document.querySelector("#settings-status").innerHTML = '<p><span class="dot ' + (value.ready ? 'ready' : '') + '"></span>' + escape(value.ready ? '配置就绪，可以启动任务' : value.reason) + '</p><p class="muted">密钥：' + escape(value.key_configured ? value.key_hint : '未设置') + '<br>模型：' + escape(value.model) + '<br>地址：' + escape(value.base_url) + '<br>保存位置：' + escape(value.env_file ? '项目根目录 ' + value.env_file : '仅内存（未指定 .env）') + '</p>';
}
async function loadSettings() {
  try { renderSettings(await api("/settings/model")); }
  catch (error) { notice("读取模型配置失败：" + error.message, true); }
}
async function saveSettings(extra, message) {
  const result = await api("/settings/model", settingsPayload(extra), "PUT");
  renderSettings(result);
  applyConfig(await api("/config"));
  notice(message + (result.ready ? "" : " 当前状态：" + result.reason), !result.ready);
}
settingsForm.addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.querySelector("#save-settings");
  button.disabled = true;
  try { await saveSettings({}, "模型配置已保存并立即生效。"); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
document.querySelector("#test-settings").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  notice("正在测试连接（只请求 /models，不消耗 token）…");
  try {
    const result = await api("/settings/model/test", settingsPayload());
    const model = settingsPayload().model;
    const warning = result.model_listed === false ? "；但服务商返回的模型列表里没有 " + model + "，请检查模型名" : "";
    notice("连接成功，密钥有效" + warning + (settingsPayload().api_key ? "。别忘了点“保存配置”。" : "。"), result.model_listed === false);
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
document.querySelector("#clear-key").addEventListener("click", async () => {
  if (!confirm("清除已保存的 API Key？清除后无法启动新任务，直到重新填写。")) return;
  try { await saveSettings({api_key:"", clear_key:true}, "API Key 已清除。"); }
  catch (error) { notice(error.message, true); }
});
document.querySelector("#show-key").addEventListener("change", event => {
  settingsForm.elements.namedItem("api_key").type = event.target.checked ? "text" : "password";
});
document.querySelector('[data-panel="settings"]').addEventListener("click", () => { void loadSettings(); });

async function initialize() {
  try {
    applyConfig(await api("/config"));
    notice(config.ready ? "已连接本地后端。启动任务前请确认费用；模型是否实际成功以每次 Trace 为准。" : config.reason + "。请在左侧“模型配置”页面填写后保存。", !config.ready);
    if (!config.ready) document.querySelector('[data-panel="settings"]').click();
    workflowData = await api('/workflows/travel');
    document.querySelector("#workflow-json").textContent = JSON.stringify({definition: workflowData.definition, subflows: workflowData.subflows}, null, 2);
    document.querySelector("#workflow-graph").innerHTML = renderDag(workflowData.definition, workflowData.subflows, null);
    document.querySelector("#workflow-yaml").innerHTML = Object.entries(workflowData.source).map(([name, text]) => '<details' + (name === 'travel.yaml' ? ' open' : '') + '><summary>' + escape(name) + '</summary><pre>' + escape(text) + '</pre></details>').join('');
    const mcp = await api('/mcp/tools');
    document.querySelector("#mcp-tools").innerHTML = '<p class="muted">服务 ' + escape((mcp.server.serverInfo || {}).name) + ' · 协议 ' + escape(mcp.server.protocolVersion) + ' · 外部端点 POST ' + escape(mcp.endpoint) + ' · 已处理 ' + mcp.calls + ' 次 tools/call</p><div class="agent-grid">' + mcp.tools.map(tool => '<article class="agent"><small>MCP TOOL</small><h3>' + escape(tool.name) + '</h3><p>' + escape(tool.description) + '</p><details><summary>inputSchema</summary><pre>' + pretty(tool.inputSchema) + '</pre></details></article>').join('') + '</div>';
    const catalog = await api('/tools/catalog');
    const weekdayNames = "一二三四五六日";
    document.querySelector("#must-visit").innerHTML = catalog.items.map(item => '<label class="check"><input type="checkbox" name="must_visit" value="' + escape(item.id) + '">' + escape(item.name.replace("示例", "")) + '<small>' + (item.indoor ? "室内" : "户外") + (item.closed_on.length ? " · 周" + item.closed_on.map(day => weekdayNames[day - 1]).join("、") + "闭馆" : "") + '</small></label>').join('');
    document.querySelector("#must-visit").className = "must-grid";
    document.querySelector("#catalog").innerHTML = '<p class="muted">' + escape(catalog.source_note) + '</p><pre>' + pretty(catalog.items) + '</pre>';
    await loadAgents();
    await refresh();
    setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      // 编辑审批意见时不重绘表单，以免丢失用户输入。
      if (document.activeElement?.matches('[data-comment]')) return;
      void refresh();
    }, 1000);
  } catch(error) { notice("启动失败：" + error.message,true); }
}
void initialize();

// --- M12：知识库管理 + Agent 定义（追加在控制台初始化之后，复用既有样式） ---
const knowledgePanel = document.createElement("section");
knowledgePanel.id = "panel-knowledge";
knowledgePanel.hidden = true;
knowledgePanel.innerHTML = '<div class="grid"><form id="kb-form" class="card"><div class="section-label">知识库 / 新增</div><h2>上传文档并建索引</h2><p class="muted">服务端按段落切块，建立 BM25 与离线词形哈希向量索引；不调用外部 embedding。</p><label>标题<input name="title" maxlength="120" required></label><label>标签（逗号分隔）<input name="tags" maxlength="200"></label><label>来源<input name="source" maxlength="200" value="manual"></label><label>正文<textarea name="content" rows="12" maxlength="200000" required></textarea></label><button class="primary">保存并建索引</button></form><div><div class="card"><div class="section-label">知识库 / 检索</div><div class="row"><input id="kb-query" maxlength="500" placeholder="输入关键词，例如 预算 交通"><button id="kb-search">检索</button></div><div id="kb-hits" class="muted">尚未检索。</div></div><div class="card"><div class="section-label">知识库 / 文档</div><p id="kb-stats" class="muted">加载中…</p><div id="kb-list">加载中…</div></div></div></div>';
document.querySelector(".workspace").appendChild(knowledgePanel);

const knowledgeButton = document.createElement("button");
knowledgeButton.dataset.panel = "knowledge";
knowledgeButton.textContent = "知识库";
document.querySelector("aside nav").appendChild(knowledgeButton);
knowledgeButton.addEventListener("click", () => {
  document.querySelectorAll("section[id^='panel-']").forEach(panel => { panel.hidden = panel.id !== "panel-knowledge"; });
  document.querySelectorAll("[data-panel]").forEach(item => item.classList.toggle("selected", item === knowledgeButton));
  void loadKnowledge();
});

function knowledgeTags(value) {
  return value.split(/[,，]/).map(item => item.trim()).filter(Boolean);
}
function renderKnowledge(data) {
  const stats = data.stats;
  document.querySelector("#kb-stats").textContent = stats.documents + " 篇 / " + stats.chunks + " 块 / " + stats.characters + " 字符 · " + stats.indexes.join(" + ");
  const items = data.items;
  document.querySelector("#kb-list").innerHTML = items.length ? items.map(item => '<article class="agent"><small>' + escape(item.source) + '</small><h3>' + escape(item.title) + '</h3><code>' + escape(item.id) + '</code><p>' + escape(item.preview) + '</p><p class="muted">' + item.chunk_count + ' 块 · ' + item.char_count + ' 字符 · ' + escape(item.tags.join(" · ")) + '</p><button data-kb-view="' + escape(item.id) + '">查看切块</button> <button data-kb-delete="' + escape(item.id) + '" class="danger">删除</button></article>').join("") : '<p class="muted">知识库为空。没有预置内容。</p>';
  document.querySelectorAll("[data-kb-view]").forEach(button => button.addEventListener("click", async () => {
    try {
      const document_ = await api("/knowledge/" + encodeURIComponent(button.dataset.kbView));
      document.querySelector("#kb-hits").innerHTML = '<details open><summary>' + escape(document_.title) + ' · ' + document_.chunks.length + ' 块</summary>' + document_.chunks.map(chunk => '<pre>' + escape(chunk.id + " [" + chunk.start + "," + chunk.end + "]\n" + chunk.content) + '</pre>').join("") + '</details>';
    } catch (error) { notice(error.message, true); }
  }));
  document.querySelectorAll("[data-kb-delete]").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("删除该知识文档并重建索引？")) return;
    try { await api("/knowledge/" + encodeURIComponent(button.dataset.kbDelete), undefined, "DELETE"); notice("已删除并重建索引。"); await loadKnowledge(); }
    catch (error) { notice(error.message, true); }
  }));
}
async function loadKnowledge() {
  try { renderKnowledge(await api("/knowledge")); }
  catch (error) { notice("读取知识库失败：" + error.message + "。若刚更新代码，请重启后端。", true); }
}
document.querySelector("#kb-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const document_ = await api("/knowledge", {title: form.get("title"), content: form.get("content"), tags: knowledgeTags(String(form.get("tags") || "")), source: form.get("source") || "manual"});
    notice("已保存 " + document_.id + "，共 " + document_.chunks.length + " 块。");
    event.target.reset();
    document.querySelector("#kb-hits").textContent = "尚未检索。";
    await loadKnowledge();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
document.querySelector("#kb-search").addEventListener("click", async () => {
  const query = document.querySelector("#kb-query").value.trim();
  if (!query) { notice("请输入检索词。", true); return; }
  try {
    const data = await api("/knowledge/search", {query: query, top_k: 5});
    document.querySelector("#kb-hits").innerHTML = data.items.length ? data.items.map(item => '<article class="agent"><small>score ' + item.score.toFixed(3) + '</small><h3>' + escape(item.title) + '</h3><p>' + escape(item.content) + '</p><p class="muted">' + escape(item.tags.join(" · ")) + '</p></article>').join("") : '<p class="muted">没有召回结果。</p>';
  } catch (error) { notice(error.message, true); }
});

const knowledgeFileLabel = document.createElement("label");
knowledgeFileLabel.textContent = "选择文本文件（.txt / .md）";
const knowledgeFile = document.createElement("input");
knowledgeFile.type = "file";
knowledgeFile.id = "kb-file";
knowledgeFile.accept = ".txt,.md,text/plain,text/markdown";
knowledgeFileLabel.appendChild(knowledgeFile);
const knowledgeForm = document.querySelector("#kb-form");
knowledgeForm.insertBefore(knowledgeFileLabel, knowledgeForm.querySelector("label"));
knowledgeFile.addEventListener("change", async () => {
  const file = knowledgeFile.files ? knowledgeFile.files[0] : null;
  if (!file) return;
  if (file.size > 200000) { notice("文件超过 200000 字节上限。", true); knowledgeFile.value = ""; return; }
  try {
    const text = await file.text();
    knowledgeForm.querySelector('[name="content"]').value = text;
    const title = knowledgeForm.querySelector('[name="title"]');
    if (!title.value) title.value = file.name.replace(/\.[^.]+$/, "");
    notice("已读取 " + file.name + "，确认标题与标签后保存并建索引。");
  } catch (error) { notice("读取文件失败：" + error.message, true); }
});

const agentDefs = document.createElement("div");
agentDefs.className = "card";
agentDefs.id = "agent-defs";
agentDefs.innerHTML = '<div class="section-label">Agent 定义 / YAML</div><h2>内置团队</h2><p class="muted">打开本页时加载…</p>';
document.querySelector("#panel-agents").appendChild(agentDefs);
async function loadAgentCatalog() {
  try {
    const data = await api("/agent-catalog");
    document.querySelector("#agent-defs").innerHTML = '<div class="section-label">Agent 定义 / YAML</div><h2>内置团队 · ' + data.count + ' 个角色</h2><p class="muted">' + escape(data.note) + '</p><div class="agent-grid">' + data.items.map(spec => '<article class="agent"><small>' + escape(spec.role) + '</small><h3>' + escape(spec.name) + '</h3><code>' + escape(spec.id + "@" + spec.version) + '</code><p>' + escape(spec.description) + '</p><p class="muted">技能：' + escape(spec.skills.join(" · ")) + '</p><p class="muted">工具：' + escape(spec.tools.join(" · ")) + ' · 容量 ' + spec.capacity + ' · 温度 ' + spec.temperature + '</p><details><summary>提示词</summary><pre>' + escape(spec.prompt) + '</pre></details></article>').join("") + '</div>';
  } catch (error) { notice("读取 Agent 定义失败：" + error.message + "。若刚更新代码，请重启后端。", true); }
}
document.querySelector('[data-panel="agents"]').addEventListener("click", () => { void loadAgentCatalog(); });
