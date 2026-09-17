"use strict";
let token = "", serviceSelection = "", session = null, selected = null, current = null, nextCursor = null, items = [], timer = null;
const $ = id => document.getElementById(id);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const states = {QUEUED:"正在收集证据",RECOMMENDED:"有待核对建议",NEEDS_ATTENTION:"需要人工处理",OBSERVING:"正在验证恢复",RECOVERED:"已验证恢复"};
const when = t => t ? new Date(t * 1000).toLocaleString("zh-CN", {hour12:false}) : "—";
const duration = n => n === null || n === undefined ? "—" : n < 60 ? `${n.toFixed(1)} 秒` : `${(n/60).toFixed(1)} 分钟`;
async function api(path, body) {
  const result = await fetch(path, {method:body ? "POST" : "GET",headers:{Authorization:`Bearer ${token}`, ...(serviceSelection ? {"X-MTTR-Service":serviceSelection} : {}), ...(body ? {"Content-Type":"application/json"} : {})},body:body ? JSON.stringify(body) : undefined});
  const data = await result.json();
  if (!result.ok) throw new Error(data.error || `请求失败 ${result.status}`);
  return data;
}
function showError(e) { $("error").textContent = e.message; }
function badge(i) { return `<span class="badge ${i.state === "NEEDS_ATTENTION" ? "attention" : ""}">${esc(states[i.state] || i.state)}</span>`; }
function renderList() {
  $("incidents").innerHTML = items.length ? items.map(i => `<button class="incident ${i.id===selected?"selected":""}" data-id="${esc(i.id)}">${badge(i)}<strong>${esc(i.summary)}</strong><span class="muted">${esc(i.scope.service)} · ${esc(when(i.detected_at))}</span></button>`).join("") : '<p class="muted">尚未收到告警。配置 Alertmanager 接收地址后，事件会自动出现在这里。</p>';
  document.querySelectorAll("[data-id]").forEach(b => b.onclick = () => select(b.dataset.id).catch(showError));
  $("more").hidden = !nextCursor;
}
async function reload() {
  const [list, metrics] = await Promise.all([api("/api/incidents"), api("/api/metrics")]);
  items = list.items; nextCursor = list.next_cursor; renderList();
  $("measurement").innerHTML = `<div class="metrics"><div class="metric"><span>真实事件</span><strong>${metrics.incidents}</strong></div><div class="metric"><span>尚未验证恢复</span><strong>${metrics.unresolved}</strong></div><div class="metric"><span>已恢复事件平均 TTR</span><strong>${duration(metrics.ttr_completed_only.mean_seconds)}</strong></div><div class="metric"><span>平均形成建议时间</span><strong>${duration(metrics.phases.time_to_recommendation.mean_seconds)}</strong></div><div class="metric"><span>建议接受 / 拒绝</span><strong>${metrics.reviews.accepted} / ${metrics.reviews.rejected}</strong></div></div><p>演练事件 ${metrics.excluded_demo_incidents} 起，已排除。TTR 从首次告警到验证恢复；尚未恢复事件单独列出。没有对照基线时，不据此宣称 MTTR 降低。</p>`;
  if (selected) await select(selected, false);
  else if (items.length) await select(items[0].id);
}
async function select(id, redraw=true) {
  const result = await api(`/api/incidents/${id}`); selected = id; current = result.incident;
  if (redraw) renderList();
  const i = current, r = i.recommendation, m = i.snapshot?.metrics;
  const canOperate = session.role === "operator";
  const expired = r && Date.now()/1000 >= r.expires_at;
  $("detail").innerHTML = `${badge(i)}<h2>${esc(i.summary)}</h2><div class="meta"><span>Owner：${esc(i.owner)}</span><span>接手人：${esc(i.assignment?.subject || "待接手")}</span><span>${esc(i.mode)}</span></div><p class="muted">${esc([i.scope.tenant,i.scope.environment,i.scope.cluster,i.scope.namespace,i.scope.service].join(" / "))}</p><div class="links">${Object.entries(i.links).map(([name,url])=>`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>`).join("")}</div>
  ${canOperate && !i.assignment ? '<div class="actions"><button id="ack">我来接手</button></div>' : ""}
  ${i.attention_reason?`<p class="notice">${esc(i.attention_reason)}</p>`:""}
  ${r?`<div class="recommendation"><h3>候选处置：灰度回退 ${esc(r.expected_revision)} → ${esc(r.target_revision)}</h3><p>影响范围 ${esc(r.traffic_percent)}% · 有效至 ${esc(when(r.expires_at))}${expired?" · 已过期，请重新取证":""}</p><p>${esc(r.instructions)}</p><p class="muted">${i.review?`审核：${esc(i.review.subject)} · ${i.review.decision==="accept"?"接受":"拒绝"} · ${esc(i.review.reason)}`:"此建议尚待值班人员核对。"}</p></div>`:""}
  ${m?`<h3>本次取证</h3><p>灰度错误率 ${(m.error_rate*100).toFixed(2)}% · 稳定组 ${(m.control_error_rate*100).toFixed(2)}% · P99 ${m.p99_ms.toFixed(0)} ms · ${m.qps.toFixed(1)} QPS</p>`:""}
  ${i.findings.length?`<ul>${i.findings.map(f=>`<li><strong>${esc(f.agent)}</strong>：${esc(f.verdict)} — ${esc(f.summary)}</li>`).join("")}</ul>`:""}
  <h3>人工处置步骤</h3><ol>${i.runbook.map(s=>`<li>${esc(s)}</li>`).join("")}</ol>
  ${i.state==="OBSERVING"?`<p class="notice">连续健康窗口 ${i.healthy_streak || 0} / ${i.policy.consecutive_windows}；每窗口 ${i.policy.window_seconds} 秒。系统只读取监控，不会替你执行回滚。</p>`:""}
  ${i.observations.length?`<h3>恢复观测</h3><ul>${i.observations.slice(-5).map(o=>`<li>${esc(when(o.at))} · ${o.healthy?"符合恢复条件":esc(o.reasons.join("；"))} · 版本 ${esc(o.revision)}</li>`).join("")}</ul>`:""}
  ${i.state==="RECOVERED"?`<p class="recommendation">恢复已通过独立观测验证，TTR ${duration(i.recovery_seconds)}。${i.mode==="DEMO"?"这是演练数据，不纳入真实效果指标。":""}</p>`:""}
  ${canOperate && ["RECOMMENDED","NEEDS_ATTENTION"].includes(i.state)?`<div class="form"><h3>记录处置</h3><label for="reason">核对结论或实际操作说明</label><textarea id="reason" placeholder="记录版本、操作单号、人工判断依据；不要填写密码或个人数据。"></textarea><div class="actions">${r && !i.review && i.state==="RECOMMENDED"?`<button id="accept" ${expired?"disabled":""}>接受建议（不执行）</button><button id="reject" class="secondary">拒绝建议</button>`:""}<button id="refresh" class="secondary">重新收集证据</button></div><label for="target">已在发布平台完成处置后，填写预期运行版本</label><input id="target" value="${esc(r?.target_revision || "")}" placeholder="例如 r41"><div class="actions"><button id="observe">记录已人工处置，开始验证</button></div><p class="muted">此操作记录你的说明并启动监控采集。恢复时间以监控验证通过为准。</p></div>`:""}
  ${canOperate && session.features?.includes("pause") && i.state === "OBSERVING" ? '<section class="form"><h3>人工接管</h3><label for="pause-reason">暂停自动观察的原因</label><textarea id="pause-reason"></textarea><button id="pause" class="secondary">暂停观察，交由人工处理</button></section>' : ""}
  ${canOperate && session.features?.includes("handoff") && i.state !== "RECOVERED" ? `<section class="form"><h3>值班交接</h3><label for="assignee">交接给同服务的值班人员</label><select id="assignee">${session.operators.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join("")}</select><label for="handoff-reason">交接说明</label><textarea id="handoff-reason"></textarea><button id="handoff" class="secondary">记录交接</button><p class="muted">交接记录不会向对方发送通知，请使用团队现有联系渠道。</p></section>` : ""}
  <h3>事件时间线</h3><ul class="timeline">${result.events.slice(-30).map(e=>`<li><time>${esc(when(e.at))}</time>${esc(e.kind)}</li>`).join("")}</ul><details><summary>查看证据与来源</summary><pre>${esc(JSON.stringify(i.evidence,null,2))}</pre></details><button id="export" class="secondary">导出事件与最近审计记录</button>`;
  if ($("ack")) $("ack").onclick = () => operate("ack", {}).catch(showError);
  for (const decision of ["accept","reject"]) if ($(decision)) $(decision).onclick = () => operate("review",{decision,plan_hash:r.hash,reason:$("reason").value}).catch(showError);
  if ($("refresh")) $("refresh").onclick = () => operate("refresh",{reason:$("reason").value}).catch(showError);
  if ($("observe")) $("observe").onclick = () => operate("observe",{reason:$("reason").value,target_revision:$("target").value}).catch(showError);
  if ($("pause")) $("pause").onclick = () => operate("pause",{reason:$("pause-reason").value}).catch(showError);
  if ($("handoff")) $("handoff").onclick = () => operate("handoff",{reason:$("handoff-reason").value,assignee:$("assignee").value,expected_assignee:i.assignment?.subject || null}).catch(showError);
  $("export").onclick = () => {const blob=new Blob([JSON.stringify(result,null,2)],{type:"application/json"});const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=`incident-${id}.json`;a.click();URL.revokeObjectURL(url);};
}
async function operate(action, body) {
  $("error").textContent="";
  const id=selected;
  await api(`/api/incidents/${id}/${action}`,{...body,request_id:crypto.randomUUID()});
  await reload();
}
$("login-form").onsubmit=async e=>{e.preventDefault();try{token=$("token").value;serviceSelection=$("service-selection").value.trim();session=await api("/api/session");$("token").value="";$("login").hidden=true;$("workspace").hidden=false;$("identity").textContent=`${session.subject} · ${session.service}`;$("mode").textContent=session.mode==="DEMO"?"本地演练 · 所有数据均为合成数据":"只读试点 · 人工处置，独立验证恢复";$("error").textContent="";await reload();timer=setInterval(()=>{if(!document.querySelector("textarea:focus, input:focus, select:focus") && ![...document.querySelectorAll("textarea")].some(t=>t.value))reload().catch(showError);},15000);}catch(err){token="";showError(err);}};
$("reload").onclick=()=>reload().catch(showError);
$("logout").onclick=()=>{token="";clearInterval(timer);location.reload();};
$("more").onclick=async()=>{try{const result=await api(`/api/incidents?before=${nextCursor}`);items.push(...result.items);nextCursor=result.next_cursor;renderList();}catch(e){showError(e);}};
