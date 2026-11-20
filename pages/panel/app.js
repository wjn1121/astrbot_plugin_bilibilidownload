/* B站视频下载 · 插件面板逻辑
 *
 * 三个约定必须遵守，否则面板会静默失效：
 * 1. 宿主桥接的主名是 AstrBotPluginView，AstrBotPluginPage 是保留别名，两个都要兼容；
 * 2. apiGet / apiPost 的第一个参数是**接口后缀**（如 "status"），不是完整路径，
 *    完整路径由宿主按插件名拼出来；
 * 3. index.html 里必须显式、提前引 bridge-sdk.js —— 宿主虽然会自动注入，
 *    但注入点在 </body> 前、也就是在 app.js 之后，那时 Bridge 还不存在。
 *
 * 结构：左栏是「概览 + 三个平台页 + 三个工具页」。平台页里的「调整」全部由
 * /settings 的控件元信息动态渲染，前端不重复写键名，新增调整项只改后端。
 */

const $ = (id) => document.getElementById(id);
const Bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const PLATFORM_LABEL = { bilibili: "哔哩哔哩", douyin: "抖音", xiaohongshu: "小红书", heybox: "小黑盒", kuaishou: "快手", weibo: "微博", wechat: "公众号" };
const CAP_LABEL = {
  video: "视频",
  images: "图集",
  summary: "总结",
  subtitle: "字幕",
  replies: "热评",
};

/* 左栏平台页 → 页面上的容器前缀 */
const PLATFORM_PAGES = {
  bilibili: { prefix: "bili", enableId: "biliEnabled" },
  douyin: { prefix: "dy", enableId: "dyEnabled" },
  xiaohongshu: { prefix: "xhs", enableId: "xhsEnabled" },
  heybox: { prefix: "hb", enableId: "hbEnabled" },
  kuaishou: { prefix: "ks", enableId: "ksEnabled" },
  weibo: { prefix: "wb", enableId: "wbEnabled" },
  wechat: { prefix: "wx", enableId: "wxEnabled" },
};

let lastStatus = null;
let lastSettings = null;
let probeBusy = false;
let qrTimer = null;

/* ── 小工具 ─────────────────────────────────────────────── */

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/** 宿主会把响应包一层 {success, data}，但不同版本也可能直接给 data，两种都兼容 */
function unwrap(payload) {
  if (payload && typeof payload === "object" && "success" in payload && "data" in payload) {
    return payload.data ?? {};
  }
  return payload || {};
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3600);
}

function fmtTime(seconds) {
  if (!seconds || seconds <= 0) return "—";
  const total = Math.floor(seconds);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  return `${m} 分`;
}

function sourceTag(source) {
  return source === "panel"
    ? '<span class="src panel">面板</span>'
    : '<span class="src config">配置</span>';
}

/* ── 概览 ───────────────────────────────────────────────── */

function renderStats(data) {
  const stats = data.stats || {};
  const card = data.card || {};
  const cards = [
    { label: "解析成功", value: stats.parse_ok ?? 0, hint: `失败 ${stats.parse_fail ?? 0}` },
    { label: "下载成功", value: stats.download_ok ?? 0, hint: `失败 ${stats.download_fail ?? 0}` },
    { label: "卡片出图", value: stats.card_ok ?? 0, hint: `失败 ${stats.card_fail ?? 0}` },
    { label: "运行时长", value: fmtTime(data.uptime), hint: `v${data.version ?? "?"}` },
  ];
  $("stats").innerHTML = cards
    .map(
      (item) => `<div class="stat">
        <span>${esc(item.label)}</span>
        <b>${esc(String(item.value))}</b>
        <i>${esc(item.hint)}</i>
      </div>`
    )
    .join("");

  if (card.disabled) {
    $("cardState").textContent = `图片卡片已熔断（连续失败 ${card.fail_streak} 次），本进程内改用纯文本；重载插件后恢复。`;
  } else if (!card.enabled) {
    $("cardState").textContent = "图片卡片已关闭（配置项 output_image）。";
  } else {
    $("cardState").textContent = "";
  }

  renderAttempts(stats.media_attempts || {});
  renderPlatformOverview(data.platforms || []);
  $("lastError").textContent = data.last_error
    ? `${data.last_error.at} · ${data.last_error.where}：${data.last_error.detail}`
    : "暂无错误记录";
}

function renderAttempts(rows) {
  // 后端给的是 [{name, used, ok, detail, at}]：三条下载路径各自的最近一次结果
  if (!Array.isArray(rows) || !rows.length) {
    $("attempts").innerHTML = '<span class="hint">还没有下载记录</span>';
    return;
  }
  $("attempts").innerHTML = rows
    .map((row) => {
      if (!row.used) {
        return `<span class="pill">${esc(row.name || "")} · 未用过</span>`;
      }
      const cls = row.ok ? "ok" : "bad";
      const tail = row.ok ? "最近一次成功" : esc(row.detail || "最近一次失败");
      return `<span class="pill ${cls}">${esc(row.name || "")} · ${tail}</span>`;
    })
    .join("");
}

function renderPlatformOverview(rows) {
  const enabledSet = new Set(rows.filter((row) => row.enabled).map((row) => row.name));
  $("platformOverview").innerHTML = rows
    .map((row) => {
      const caps = (row.capabilities || [])
        .map((cap) => `<span class="cap">${esc(CAP_LABEL[cap] || cap)}</span>`)
        .join("");
      const on = enabledSet.has(row.name);
      return `<button class="platform ${on ? "" : "off"}" type="button" data-goto="${esc(row.name)}">
        <b>${esc(row.display || row.name)}</b>
        <div class="caps">${caps || '<span class="cap">—</span>'}</div>
        <span class="state ${on ? "on" : "off"}">${on ? "已启用" : "未启用"}</span>
      </button>`;
    })
    .join("");

  document.querySelectorAll("[data-goto]").forEach((node) => {
    node.addEventListener("click", () => switchTab(node.dataset.goto));
  });
}

/* ── 平台页：状态 / 启用开关 / 调整项 ───────────────────── */

function platformRow(name) {
  return (lastStatus?.platforms || []).find((item) => item.name === name) || null;
}

function platformUsable(name) {
  // 后端 describe_all() 的 enabled 表示「在启用列表里且模块加载成功」
  return Boolean(platformRow(name)?.enabled);
}

function renderPlatformPage(name) {
  const page = PLATFORM_PAGES[name];
  if (!page) return;
  const row = platformRow(name);
  const on = platformUsable(name);

  const caps = (row?.capabilities || []).map((cap) => CAP_LABEL[cap] || cap).join(" / ");
  const capsEl = $(`${page.prefix}Caps`);
  if (capsEl) capsEl.textContent = caps ? `能力：${caps}` : "";

  const enableEl = $(page.enableId);
  if (enableEl) enableEl.checked = on;

  const stateEl = $(`${page.prefix}State`);
  if (stateEl) {
    const rows = [
      ["状态", on ? "已启用" : "未启用（不会参与解析）"],
      ["能力", caps || "—"],
      ["平台标识", name],
    ];
    if (row?.error) rows.push(["加载错误", row.error]);
    stateEl.innerHTML = rows
      .map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`)
      .join("");
  }

  // 左栏小圆点：未启用的平台标灰
  const dot = $(`dot${name[0].toUpperCase()}${name.slice(1)}`);
  if (dot) dot.hidden = on;

  renderTune(name);
}

/** 按 /settings 的元信息渲染某一个平台页的调整控件 */
function renderTune(name) {
  const page = PLATFORM_PAGES[name];
  const box = $(`${page.prefix}Tune`);
  if (!box || !lastSettings) return;

  // group 可以是字符串，也可以是多个平台名组成的数组（如「图集」两项对抖音和
  // 小红书都生效）。"common" 表示每个平台页都显示。
  const groupsOf = (item) =>
    Array.isArray(item.group) ? item.group : [item.group].filter(Boolean);
  const items = (lastSettings.items || []).filter((item) => {
    const groups = groupsOf(item);
    return groups.includes(name) || groups.includes("common");
  });
  if (!items.length) {
    box.innerHTML = '<p class="hint">这个平台没有独占的调整项。</p>';
    return;
  }

  box.innerHTML = items
    .map((item) => {
      const id = `tune_${name}_${item.key}`;
      const restore =
        item.source === "panel"
          ? `<button class="mini" type="button" data-restore="${esc(item.key)}" data-page="${esc(name)}">恢复</button>`
          : "";
      if (item.kind === "bool") {
        return `<label class="tune-row" for="${id}">
          <span class="tune-label">${esc(item.label)}</span>
          <span class="tune-ctl">
            ${sourceTag(item.source)}
            <input type="checkbox" id="${id}" data-key="${esc(item.key)}" ${item.value ? "checked" : ""} />
            ${restore}
          </span>
        </label>`;
      }
      return `<label class="tune-row" for="${id}">
        <span class="tune-label">${esc(item.label)}${item.unit ? `<i>（${esc(item.unit)}）</i>` : ""}</span>
        <span class="tune-ctl">
          ${sourceTag(item.source)}
          <input type="number" id="${id}" data-key="${esc(item.key)}" value="${esc(String(item.value ?? ""))}" />
          ${restore}
        </span>
      </label>`;
    })
    .join("");

  box.querySelectorAll("[data-restore]").forEach((node) => {
    node.addEventListener("click", async () => {
      const values = {};
      values[node.dataset.restore] = null; // null = 删掉覆盖，退回配置值
      await saveSettings(node.dataset.page, values);
    });
  });
}

/** 读一个平台页上所有控件，返回 {key: value} */
function collectTune(name) {
  const page = PLATFORM_PAGES[name];
  const box = $(`${page.prefix}Tune`);
  const values = {};
  if (!box) return values;
  box.querySelectorAll("[data-key]").forEach((node) => {
    const key = node.dataset.key;
    if (node.type === "checkbox") {
      values[key] = node.checked;
    } else {
      const raw = node.value.trim();
      if (raw === "") return;
      values[key] = Number(raw);
    }
  });
  return values;
}

async function saveSettings(name, values) {
  if (!values || !Object.keys(values).length) {
    toast("没有需要保存的改动");
    return false;
  }
  try {
    const payload = await Bridge.apiPost("settings/set", { values });
    const body = payload || {};
    if (body.success) {
      toast(body.message || "已保存");
      await loadSettings();
      await loadStatus();
      return true;
    }
    toast(body.message || "保存失败");
  } catch (error) {
    toast(`保存失败：${error}`);
  }
  return false;
}

async function togglePlatform(name, enabled) {
  // 顺序取 PLATFORM_PAGES 的键：这里以前写死了三个平台，
  // 新加的平台会被 filter 掉，表现为「点启用提示保存失败 / 刷新后仍未启用」
  const order = Object.keys(PLATFORM_PAGES);
  const current = (lastSettings?.platforms || (lastStatus?.platforms || []).filter((r) => r.enabled).map((r) => r.name)).slice();
  const next = new Set(current.length ? current : order);
  if (enabled) next.add(name);
  else next.delete(name);
  await saveSettings(name, { enabled_platforms: order.filter((item) => next.has(item)) });
}

/* ── 凭据：抖音接口 / 小红书 Cookie / B 站 SESSDATA ─────── */

async function saveCredential(platform, key, value) {
  try {
    const payload = await Bridge.apiPost("credentials/set", { platform, key, value });
    const body = payload || {};
    if (body.success) {
      toast(body.message || "已保存");
      await loadCredentials();
      await loadStatus();
      return true;
    }
    toast(body.message || "保存失败");
  } catch (error) {
    toast(`保存失败：${error}`);
  }
  return false;
}

async function testCredential(platform, button) {
  if (button) button.disabled = true;
  try {
    const payload = await Bridge.apiPost("credentials/test", { platform });
    const body = payload || {};
    toast(body.message || (body.success ? "验证通过" : "验证失败"));
  } catch (error) {
    toast(`测试失败：${error}`);
  } finally {
    if (button) button.disabled = false;
  }
}

function renderCredentials(data) {
  const platforms = data.platforms || {};
  const badge = (platform, key, targetId) => {
    const entry = platforms?.[platform]?.[key] || {};
    const el = $(targetId);
    if (!el) return;
    if (entry.configured) {
      el.textContent = `已配置 · 尾号 ${entry.tail || "****"}`;
      el.className = "badge on";
    } else {
      el.textContent = "未配置";
      el.className = "badge";
    }
  };
  badge("heybox", "cookie", "hbCookieBadge");
  badge("bilibili", "sessdata", "biliLoginBadge");
  badge("douyin", "api_url", "dyApiBadge");
  badge("xiaohongshu", "cookie", "xhsCookieBadge");

  const bili = platforms?.bilibili?.sessdata || {};
  const hint = $("biliLoginHint");
  if (hint) {
    hint.textContent = bili.configured
      ? "已登录：字幕增强可用，AI 总结能说清视频里讲了什么。"
      : "不登录也能解析，只是 AI 总结只能靠标题/简介/热评推断。";
  }
}

/* ── B 站扫码登录 ───────────────────────────────────────── */

function stopQrPolling() {
  if (qrTimer) {
    clearInterval(qrTimer);
    qrTimer = null;
  }
}

function showQrState(state) {
  const stateEl = $("biliQrState");
  const labels = {
    waiting: "等待扫码…",
    scanned: "已扫码，请在手机上确认",
    success: "✅ 登录成功",
    expired: "二维码已过期，请重新获取",
    failed: "❌ 登录失败",
    cancelled: "已取消",
    idle: "",
  };
  if (stateEl) stateEl.textContent = state.message || labels[state.state] || "";

  if (state.state === "success") {
    stopQrPolling();
    $("biliQrBox").hidden = true;
    toast(state.message || "B 站登录成功");
    loadCredentials();
    loadStatus();
  } else if (["expired", "failed", "cancelled"].includes(state.state)) {
    stopQrPolling();
    $("biliQrBox").hidden = true;
    if (state.message) toast(state.message);
  } else if (state.qr) {
    $("biliQrImg").src = state.qr;
    $("biliQrBox").hidden = false;
  }
}

async function startBiliQr() {
  stopQrPolling();
  $("biliQrBox").hidden = false;
  $("biliQrState").textContent = "正在获取二维码…";
  try {
    const payload = await Bridge.apiPost("bili/qrcode", {});
    const body = payload || {};
    const state = body.data || {};
    if (state.state === "failed") {
      $("biliQrBox").hidden = true;
      toast(state.message || "获取二维码失败");
      return;
    }
    showQrState(state);
    qrTimer = setInterval(async () => {
      try {
        const pollPayload = await Bridge.apiGet("bili/qrcode", {});
        showQrState(unwrap(pollPayload));
      } catch (error) {
        stopQrPolling();
        toast(`扫码状态获取失败：${error}`);
      }
    }, 2000);
  } catch (error) {
    $("biliQrBox").hidden = true;
    toast(`获取二维码失败：${error}`);
  }
}

async function cancelBiliQr() {
  stopQrPolling();
  $("biliQrBox").hidden = true;
  try {
    await Bridge.apiPost("bili/qrcode", {});
  } catch (error) {
    /* 取消失败不影响界面 */
  }
}

async function biliLogout() {
  stopQrPolling();
  try {
    const payload = await Bridge.apiPost("bili/logout", {});
    const body = payload || {};
    toast(body.message || "已清除登录态");
    await loadCredentials();
    await loadStatus();
  } catch (error) {
    toast(`退出失败：${error}`);
  }
}

/* ── 试解析 ─────────────────────────────────────────────── */

async function runProbe() {
  if (probeBusy) return;
  const text = $("probeInput").value.trim();
  if (!text) {
    toast("先粘贴一条链接");
    return;
  }

  probeBusy = true;
  $("probeBtn").disabled = true;
  $("probeHint").textContent = "正在解析…";
  try {
    const payload = await Bridge.apiPost("probe", {
      text,
      summary: $("probeSummary").checked,
    });
    const body = payload || {};
    if (!body.success) {
      $("probeHint").textContent = body.message || "解析失败";
      $("probeResult").hidden = true;
      return;
    }
    renderProbe(body.data || {});
  } catch (error) {
    $("probeHint").textContent = `解析失败：${error}`;
  } finally {
    probeBusy = false;
    $("probeBtn").disabled = false;
    setTimeout(() => {
      $("probeHint").textContent =
        "每次试解析都会真的请求一次站方接口，所以做了 5 秒限流。勾选「AI 总结」会调一次当前会话的模型（几百 token，走你自己的模型额度），只想看能不能解析就取消勾选。";
    }, 6000);
  }
}

function renderProbe(data) {
  $("probeResult").hidden = false;
  $("probeMs").textContent = data.parse_ms ? `${data.parse_ms} ms` : "";

  const rows = [
    ["平台", data.platform_display || data.platform || ""],
    ["标题", data.title || ""],
    ["作者", data.author || ""],
    ["时长", data.duration ? `${data.duration} 秒` : ""],
    ["内容ID", data.media_id || ""],
    [
      "媒体",
      (data.items || [])
        .map((i) => `${i.kind}${i.size_hint ? `(${(i.size_hint / 1048576).toFixed(1)}MB)` : ""}`)
        .join(" · "),
    ],
    ["标签", (data.tags || []).join(" / ")],
  ].filter(([, value]) => value);
  $("probeMeta").innerHTML = rows
    .map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`)
    .join("");

  const summaryBox = $("probeSummaryBox");
  if (data.summary) {
    summaryBox.hidden = false;
    $("probeBasis").textContent = data.summary_basis || "AI 总结";
    $("probeSummaryText").textContent = data.summary;
  } else {
    summaryBox.hidden = true;
  }

  const notes = [];
  if (data.summary_note) notes.push(data.summary_note);
  if (data.card_note) notes.push(data.card_note);
  $("probeNotes").textContent = notes.join("；");

  const cardBox = $("probeCardBox");
  if (data.card) {
    cardBox.hidden = false;
    $("probeCard").src = data.card;
  } else {
    cardBox.hidden = true;
  }
}

/* ── 最近 ───────────────────────────────────────────────── */

async function loadHistory() {
  try {
    const payload = await Bridge.apiGet("history", {
      platform: $("historyPlatform").value || "",
      limit: 50,
    });
    const data = unwrap(payload);
    renderHistory(data.entries || [], data.total ?? 0);
  } catch (error) {
    $("historyList").innerHTML = '<p class="hint">加载失败</p>';
  }
}

function renderHistory(entries, total) {
  $("historyMeta").textContent = `共 ${total} 条，显示最近 ${entries.length} 条`;
  if (!entries.length) {
    $("historyList").innerHTML = '<p class="hint">还没有解析记录</p>';
    return;
  }
  $("historyList").innerHTML = entries
    .map((row) => {
      const time = row.at ? new Date(row.at * 1000).toLocaleString("zh-CN") : "";
      return `<div class="history-row">
        <span class="dot ${row.ok ? "ok" : "bad"}"></span>
        <span class="hist-platform">${esc(PLATFORM_LABEL[row.platform] || row.platform || "")}</span>
        <span class="hist-title">${esc(row.title || row.media_id || "")}</span>
        <span class="hist-result">${row.ok ? (row.card_ok ? "出图" : "文本") : esc(row.detail || "失败")}</span>
        <span class="hist-ms">${row.ms ? row.ms + "ms" : ""}</span>
        <span class="hist-time">${esc(time)}</span>
        ${row.summary
          ? `<span class="hist-summary" title="${esc(row.summary)}">${esc(row.summary)}</span>`
          : ""}
      </div>`;
    })
    .join("");
}

/* ── 缓存 ───────────────────────────────────────────────── */

async function loadCache() {
  try {
    const payload = await Bridge.apiGet("cache", {});
    const data = unwrap(payload);
    renderCache(data.caches || []);
  } catch (error) {
    $("caches").innerHTML = '<p class="hint">加载失败</p>';
  }
}

function renderCache(caches) {
  if (!caches.length) {
    $("caches").innerHTML = '<p class="hint">还没有缓存条目（解析过一次之后再看）</p>';
    return;
  }
  $("caches").innerHTML = caches
    .map(
      (item) => `<div class="stat">
        <span>${esc(item.platform || "")} · ${esc(item.name || "")}</span>
        <b>${item.size ?? 0} / ${item.max_items ?? "?"}</b>
        <i>命中 ${item.hits ?? 0} · 未命中 ${item.misses ?? 0}</i>
      </div>`
    )
    .join("");
}

async function clearCache() {
  try {
    const payload = await Bridge.apiPost("cache/clear", {});
    const body = payload || {};
    toast(body.message || "缓存已清空");
    await loadCache();
  } catch (error) {
    toast(`清空失败：${error}`);
  }
}

/* ── 卡片外观（实时预览） ───────────────────────────────── */

const LOOK_DEFAULTS = {
  theme: "light",
  accent: "#00AEEC",
  layout: "standard",
  width: 760,
  radius: 10,
  watermark: "",
  brand: true,
  cover: true,
  time: true,
  stats: true,
  qr: false,
};

let lookState = { ...LOOK_DEFAULTS };
let lookTimer = null;

function normalizeAccent(value) {
  const text = String(value || "").trim();
  // 统一小写：input[type=color] 会把值规范化成小写，两边保持一致才不会来回跳
  return /^#[0-9a-fA-F]{6}$/.test(text) ? text.toLowerCase() : LOOK_DEFAULTS.accent;
}

function setSeg(containerId, dataKey, value) {
  document.querySelectorAll(`#${containerId} button`).forEach((node) => {
    node.classList.toggle("on", node.dataset[dataKey] === value);
  });
}

function syncLookControls() {
  setSeg("lookTheme", "theme", lookState.theme);
  setSeg("lookLayout", "layout", lookState.layout);
  $("lookAccent").value = lookState.accent;
  $("lookAccentText").value = lookState.accent;
  $("lookWidth").value = String(lookState.width);
  $("lookWidthValue").textContent = String(lookState.width);
  $("lookRadius").value = String(lookState.radius);
  $("lookRadiusValue").textContent = String(lookState.radius);
  $("lookWatermark").value = lookState.watermark;
  $("lookBrand").checked = lookState.brand;
  $("lookCover").checked = lookState.cover;
  $("lookTime").checked = lookState.time;
  $("lookStats").checked = lookState.stats;
  $("lookQr").checked = lookState.qr;
}

/** 从 /settings 的回显值恢复控件（首次加载、保存之后都会走这里） */
function syncLookFromSettings() {
  if (!lastSettings) return;
  const byKey = {};
  (lastSettings.items || []).forEach((item) => { byKey[item.key] = item.value; });
  const radius = Number(byKey.card_radius);
  lookState = {
    theme: byKey.card_theme === "dark" ? "dark" : "light",
    accent: normalizeAccent(byKey.card_accent),
    layout: byKey.card_layout === "compact" ? "compact" : "standard",
    width: Number(byKey.card_width) || LOOK_DEFAULTS.width,
    radius: Number.isFinite(radius) ? radius : LOOK_DEFAULTS.radius,
    watermark: String(byKey.card_watermark || ""),
    brand: byKey.card_brand !== false,
    cover: byKey.card_show_cover !== false,
    time: byKey.card_show_time !== false,
    stats: byKey.card_show_stats !== false,
    qr: byKey.card_qr === true,
  };
  syncLookControls();
}

function scheduleLookPreview() {
  clearTimeout(lookTimer);
  lookTimer = setTimeout(refreshLookPreview, 180);
}

function updateLookStatus(previewWidth) {
  const themeLabel = lookState.theme === "dark" ? "深色" : "浅色";
  const layoutLabel = lookState.layout === "compact" ? "紧凑" : "标准";
  let text = `主题 ${themeLabel} · 版式 ${layoutLabel} · 实际发送 ${lookState.width}px · 圆角 ${lookState.radius}px`;
  // 预览按容器宽度排版，比设置值窄是正常的，写清楚免得以为宽度滑块没生效
  if (previewWidth && Math.abs(previewWidth - lookState.width) > 2) {
    text += ` · 预览按 ${previewWidth}px 排版`;
  }
  $("lookStatus").textContent = text;
}

async function refreshLookPreview() {
  const host = $("lookPreviewHost");
  const wrap = $("lookFrameWrap");
  if (!host || !wrap) return;

  $("lookStatus").textContent = "渲染中…";
  try {
    // 按预览区实际宽度排版：卡片本身是响应式的（--u 随宽度走），
    // 不缩放、不用 iframe，所以不会有内部滚动条，也不用测量高度
    const previewWidth = Math.max(280, Math.floor(wrap.clientWidth - 24));
    const payload = await Bridge.apiGet("preview/card", {
      theme: lookState.theme,
      accent: lookState.accent,
      layout: lookState.layout,
      width: previewWidth,
      radius: lookState.radius,
      watermark: lookState.watermark,
      brand: lookState.brand ? 1 : 0,
      cover: lookState.cover ? 1 : 0,
      time: lookState.time ? 1 : 0,
      stats: lookState.stats ? 1 : 0,
      qr: lookState.qr ? 1 : 0,
      _: Date.now(),
    });
    const data = unwrap(payload) || {};
    if (!data.css || !data.body) {
      throw new Error(payload?.message || data.message || "预览失败");
    }

    const shadow = host.shadowRoot || host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${data.css}</style><div class="card-root">${data.body}</div>`;
    // 字号基准跟着预览宽度走：760 宽对应 20px，窄列等比缩，最低 13px
    const unit = Math.max(13, Math.min(20, previewWidth * 0.026));
    host.style.setProperty("--u", `${unit.toFixed(2)}px`);
    updateLookStatus(previewWidth);
  } catch (error) {
    $("lookStatus").textContent = `预览失败：${error.message || error}`;
  }
}

async function saveLook() {
  await saveSettings("appearance", {
    card_theme: lookState.theme,
    card_accent: lookState.accent,
    card_layout: lookState.layout,
    card_width: lookState.width,
    card_radius: lookState.radius,
    card_watermark: lookState.watermark,
    card_brand: lookState.brand,
    card_show_cover: lookState.cover,
    card_show_time: lookState.time,
    card_show_stats: lookState.stats,
    card_qr: lookState.qr,
  });
}

function bindAppearance() {
  document.querySelectorAll("#lookTheme button").forEach((node) => {
    node.addEventListener("click", () => {
      lookState.theme = node.dataset.theme === "dark" ? "dark" : "light";
      setSeg("lookTheme", "theme", lookState.theme);
      scheduleLookPreview();
    });
  });

  document.querySelectorAll("#lookLayout button").forEach((node) => {
    node.addEventListener("click", () => {
      lookState.layout = node.dataset.layout === "compact" ? "compact" : "standard";
      setSeg("lookLayout", "layout", lookState.layout);
      scheduleLookPreview();
    });
  });

  $("lookAccent").addEventListener("input", () => {
    lookState.accent = normalizeAccent($("lookAccent").value);
    $("lookAccentText").value = lookState.accent;
    scheduleLookPreview();
  });

  $("lookAccentText").addEventListener("input", () => {
    const raw = $("lookAccentText").value.trim();
    if (/^#[0-9a-fA-F]{6}$/.test(raw)) {
      lookState.accent = raw.toLowerCase();
      $("lookAccent").value = lookState.accent;
      scheduleLookPreview();
    }
  });

  $("lookWidth").addEventListener("input", () => {
    lookState.width = Number($("lookWidth").value) || LOOK_DEFAULTS.width;
    $("lookWidthValue").textContent = String(lookState.width);
    scheduleLookPreview();
  });

  $("lookRadius").addEventListener("input", () => {
    lookState.radius = Number($("lookRadius").value);
    $("lookRadiusValue").textContent = String(lookState.radius);
    scheduleLookPreview();
  });

  $("lookWatermark").addEventListener("input", () => {
    lookState.watermark = $("lookWatermark").value.trim();
    scheduleLookPreview();
  });

  [
    ["lookBrand", "brand"],
    ["lookCover", "cover"],
    ["lookTime", "time"],
    ["lookStats", "stats"],
    ["lookQr", "qr"],
  ].forEach(([id, key]) => {
    $(id).addEventListener("change", () => {
      lookState[key] = $(id).checked;
      scheduleLookPreview();
    });
  });

  $("lookSave").addEventListener("click", saveLook);
  $("lookRefresh").addEventListener("click", refreshLookPreview);

  // 宽度变了要重新排版（预览按容器宽度渲染，不是缩放），用同一个 debounce
  window.addEventListener("resize", () => {
    const pane = document.querySelector('[data-pane="appearance"]');
    if (pane && pane.classList.contains("on")) scheduleLookPreview();
  });
}

/* ── 加载 ───────────────────────────────────────────────── */

async function loadStatus() {
  try {
    const payload = await Bridge.apiGet("status", {});
    const data = unwrap(payload);
    lastStatus = data;
    $("version").textContent = `v${data.version || "?"} · 已运行 ${fmtTime(data.uptime)}`;
    renderStats(data);
    Object.keys(PLATFORM_PAGES).forEach(renderPlatformPage);
  } catch (error) {
    $("version").textContent = "状态加载失败，请重试";
    toast(`状态加载失败：${error}`);
  }
}

async function loadSettings() {
  try {
    const payload = await Bridge.apiGet("settings", {});
    lastSettings = unwrap(payload);
    Object.keys(PLATFORM_PAGES).forEach(renderTune);
    syncLookFromSettings();
  } catch (error) {
    toast(`调整项加载失败：${error}`);
  }
}

async function loadCredentials() {
  try {
    const payload = await Bridge.apiGet("credentials", {});
    renderCredentials(unwrap(payload));
  } catch (error) {
    /* 凭据状态读不到不影响其它页 */
  }
}

/* ── 导航 ───────────────────────────────────────────────── */

function switchTab(tab) {
  document.querySelectorAll(".nav-item").forEach((node) => {
    node.classList.toggle("on", node.dataset.tab === tab);
  });
  document.querySelectorAll(".pane").forEach((node) => {
    node.classList.toggle("on", node.dataset.pane === tab);
  });
  if (tab === "history") loadHistory();
  if (tab === "cache") loadCache();
  if (tab === "appearance") {
    // 先按最新的 /settings 对齐控件，再渲染一次预览（首次进入时 iframe 还是空的）
    loadSettings().then(refreshLookPreview);
  }
  if (PLATFORM_PAGES[tab]) {
    loadCredentials();
    loadSettings();
  }
}

function bindTabs() {
  document.querySelectorAll(".nav-item").forEach((node) => {
    node.addEventListener("click", () => switchTab(node.dataset.tab));
  });
}

function bindPlatformActions() {
  Object.entries(PLATFORM_PAGES).forEach(([name, page]) => {
    const el = $(page.enableId);
    if (el) el.addEventListener("change", () => togglePlatform(name, el.checked));
    const save = $(`${page.prefix}Save`);
    if (save) {
      save.addEventListener("click", () => saveSettings(name, collectTune(name)));
    }
  });

  $("biliQrStart").addEventListener("click", startBiliQr);
  $("biliQrCancel").addEventListener("click", cancelBiliQr);
  $("biliLogout").addEventListener("click", biliLogout);
  $("biliCheck").addEventListener("click", (event) => testCredential("bilibili", event.target));
  $("biliSessdataSave").addEventListener("click", async () => {
    const value = $("biliSessdata").value.trim();
    if (await saveCredential("bilibili", "sessdata", value)) {
      $("biliSessdata").value = "";
    }
  });

  $("hbCookieSave").addEventListener("click", async () => {
    const value = $("hbCookie").value.trim();
    if (await saveCredential("heybox", "cookie", value)) {
      $("hbCookie").value = "";
    }
  });

  $("dyApiSave").addEventListener("click", () => {
    saveCredential("douyin", "api_url", $("dyApi").value.trim());
  });
  $("dyApiTest").addEventListener("click", (event) => testCredential("douyin", event.target));

  $("xhsCookieSave").addEventListener("click", async () => {
    const value = $("xhsCookie").value.trim();
    if (await saveCredential("xiaohongshu", "cookie", value)) {
      $("xhsCookie").value = "";
    }
  });
  $("xhsCookieTest").addEventListener("click", (event) =>
    testCredential("xiaohongshu", event.target)
  );
}

/* ── 启动 ───────────────────────────────────────────────── */

function boot() {
  bindTabs();

  if (!Bridge) {
    // 走到这里几乎都不是"版本太低"：AstrBot 会把 bridge SDK 自动注入到 </body> 前，
    // 但那是在 app.js 之后执行。所以 index.html 里必须显式、提前引一次 bridge-sdk.js，
    // 否则这里拿到的永远是 undefined。文案按实际情况写，避免误导用户去升级。
    $("version").textContent = "页面桥接未加载";
    $("stats").innerHTML =
      `<div class="stat bad"><span>桥接未加载</span><b>请强制刷新（Ctrl+F5）</b></div>`;
    document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    return;
  }

  bindPlatformActions();
  bindAppearance();

  $("refresh").addEventListener("click", async () => {
    await loadStatus();
    await loadSettings();
    await loadCredentials();
    toast("已刷新");
  });
  $("probeBtn").addEventListener("click", runProbe);
  $("probeInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runProbe();
  });
  $("historyPlatform").addEventListener("change", loadHistory);
  $("clearCache").addEventListener("click", clearCache);

  Bridge.ready().then(async () => {
    await loadStatus();
    await loadSettings();
    await loadCredentials();
  });
}

boot();
