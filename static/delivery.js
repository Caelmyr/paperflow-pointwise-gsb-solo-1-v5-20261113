'use strict';
// 容量约束下的 JPEG 交付：方案、1:1 解码核对、确认与下载。
// 依赖 app.js 中的全局 api / busy / modal / toast / escapeHtml / state。
(function () {
  const delivery = {plan: null, activePage: null, family: 'page', zoom: 1};
  const families = {
    page: '整页',
    text: '文字边缘',
    color: '细彩线与色晕',
    gradient: '照片渐变',
  };
  const familyHints = {
    page: '整页 1:1；确认全貌、页数、尺寸与整体观感。',
    text: '文字边缘窗口：核对细笔画的边缘是否发虚、发灰或出现锯齿。',
    color: '细彩线窗口：核对红字与浅色彩线是否褪色、串色，周围是否出现色晕。',
    gradient: '照片渐变窗口：核对平滑区域是否出现分块、带纹或色阶断层。',
  };

  function bytes(value) {
    if (value === null || value === undefined) return '未知';
    if (value >= 1048576) return (value / 1048576).toFixed(2) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(1) + ' KB';
    return value + ' B';
  }
  function pct(value, digits = 2) { return value === null || value === undefined ? '—' : (value * 100).toFixed(digits) + '%'; }
  function metric(name, value) {
    if (value === null || value === undefined) return '—';
    if (name === 'psnr') return value.toFixed(2) + ' dB';
    if (name === 'text_edge_contrast' || name === 'thin_color_retention' || name === 'gradient_ssim' || name === 'text_edge_ssim' || name === 'faint_line_ssim') return value.toFixed(4);
    return value.toFixed(2);
  }
  const metricNames = {
    text_edge_ssim: '文字边缘相似度', text_edge_contrast: '文字边缘对比度', faint_line_ssim: '浅色线相似度',
    thin_color_retention: '细彩线色度保留', thin_color_mae: '细彩线色度偏差', halo_p95: '彩线色晕(95%)',
    gradient_ssim: '渐变结构相似度', gradient_mae: '渐变亮度偏差', blockiness: '平滑块效应',
    colored_chroma_mae: '彩色区色度偏差', psnr: '整体信噪比',
  };
  const contentNames = {text: '文字页', color: '含细彩线', photo: '照片渐变', plain: '空白/低细节'};
  const statusNames = {ready: '可交付', infeasible_protection: '保护档位无法实现', infeasible_budget: '容量低于保护底线'};

  function currentPlan() { return delivery.plan; }

  async function startDelivery(comparedTo) {
    const options = await api('/deliveries/options');
    const doc = state.document;
    const pages = doc.pages.length;
    const known = delivery.lastOptions || {};
    const defaultBudget = known.budget || Math.max(64 * 1024, Math.round(pages * 150 * 1024));
    const profileOptions = options.profiles.map(p =>
      `<option value="${p.profile}" ${p.profile === (known.profile || 'balanced') ? 'selected' : ''}>${escapeHtml(p.label)} — ${escapeHtml(p.description)}</option>`).join('');
    const profileCards = options.profiles.map(p => `<div class="delivery-profile" data-profile="${p.profile}"><b>${escapeHtml(p.label)}</b><small>${escapeHtml(p.description)}</small></div>`).join('');
    modal('JPEG 容量交付', `
      <div class="delivery-intro help-text">
        <p>按当前页序交付 <b>${pages}</b> 页。每页在原像素尺度生成多档 JPEG，并解码后分别测量<b>文字边缘</b>、<b>细彩线</b>与<b>照片渐变</b>的损失，再在容量目标内分配质量。</p>
        <p class="delivery-guarantee">${options.guarantees.map(g => '✓ ' + escapeHtml(g.check)).join('<br>')}</p>
      </div>
      <form id="deliveryForm" class="modal-form delivery-form">
        <label>整批总容量目标（KB）<input name="budget_kb" type="number" min="8" max="1048576" step="1" value="${Math.max(8, Math.round(defaultBudget / 1024))}" required></label>
        <label>保护档位<select name="profile">${profileOptions}</select></label>
        <div class="field-row">
          <label>质量底线<select name="floor_quality">${options.quality_ladder.map(q => `<option value="${q}" ${q === (known.floor_quality || 30) ? 'selected' : ''}>${q}</option>`).join('')}</select></label>
          <label>色度采样<select name="chroma_sampling">
            <option value="guard" ${(known.chroma_sampling || 'guard') === 'guard' ? 'selected' : ''}>保护细彩线（推荐）</option>
            <option value="4:4:4" ${known.chroma_sampling === '4:4:4' ? 'selected' : ''}>整批强制 4:4:4</option>
          </select></label>
        </div>
        <p class="hint">质量底线是最低允许档位；保护档位决定每页可接受损失阈值。降到容量目标需要继续压缩时，系统会显示缺口，而不会越过各页保护下限。</p>
        <span class="delivery-profiles">${profileCards}</span>
        <button type="submit" class="primary">生成方案并核对</button>
      </form>
      <div id="deliveryExisting" class="delivery-existing"></div>
      <p class="help-text">生成方案只读当前处理结果；来源图、人工文字和既有导出都不改动。方案生成后需核对 1:1 解码候选，确认后才能下载。</p>
    `);
    const existing = await api('/documents/' + doc.id + '/deliveries').catch(() => []);
    if (existing.length) {
      $('deliveryExisting').innerHTML = `<h3>本单元已有方案</h3>` + existing.slice(0, 5).map(item =>
        `<div class="task-item"><b>${escapeHtml(item.created_at)}</b> · ${escapeHtml(statusNames[item.status] || item.status)} · 容量 ${bytes(item.budget_bytes)} → JPEG ${bytes(item.jpeg_bytes)}<small>${item.page_count} 页${item.confirmed ? ' · 已确认' : ''}${item.input_changed ? ' · 输入已变化' : ''}${item.download_count ? ' · 已下载 ' + item.download_count + ' 次' : ''}</small><button data-open="${item.id}">打开方案</button></div>`).join('');
      document.querySelectorAll('[data-open]').forEach(button => button.onclick = () => openPlan(button.dataset.open));
    }
    $('deliveryForm').onsubmit = async event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const payload = {
        budget_bytes: Math.round(Number(data.get('budget_kb')) * 1024),
        profile: data.get('profile'),
        floor_quality: Number(data.get('floor_quality')),
        chroma_sampling: data.get('chroma_sampling'),
      };
      delivery.lastOptions = payload;
      await busy('正在编码候选并逐页测量损失…', async () => {
        const plan = await api('/documents/' + doc.id + '/deliveries', 'POST', {options: payload, compared_to: comparedTo || null});
        delivery.plan = plan;
        await renderPlan(plan);
      });
    };
  }

  async function openPlan(identifier) {
    await busy('正在读取交付方案…', async () => {
      delivery.plan = await api('/deliveries/' + identifier);
      await renderPlan(delivery.plan);
    });
  }

  async function renderPlan(plan) {
    delivery.plan = plan;
    delivery.activePage = delivery.activePage && plan.pages.some(p => p.page_id === delivery.activePage)
      ? delivery.activePage : plan.pages[0].page_id;
    const allocation = plan.allocation || {};
    const head = [];
    head.push(`<div class="delivery-summary ${plan.status === 'ready' ? '' : 'blocked'}">
      <div class="delivery-summary-line"><b>${escapeHtml(plan.status_label)}</b><span>整批容量目标 ${bytes(plan.options.budget_bytes)} · 已选 JPEG ${bytes(plan.jpeg_bytes)}${plan.jpeg_bytes ? ' · 余量 ' + bytes(plan.options.budget_bytes - plan.jpeg_bytes) : ''}</span></div>`);
    if (allocation.floor_bytes !== null && allocation.floor_bytes !== undefined)
      head.push(`<div class="delivery-summary-line small">所选保护档位下：最低可达 ${bytes(allocation.floor_bytes)} / 最高 ${bytes(allocation.ceiling_bytes)}${allocation.deficit_bytes ? ' · 缺口 ' + bytes(allocation.deficit_bytes) : ''}${allocation.headroom_bytes !== undefined && allocation.headroom_bytes !== null ? ' · 余量 ' + bytes(allocation.headroom_bytes) : ''}</div>`);
    else if (allocation.deficit_bytes !== null && allocation.deficit_bytes !== undefined)
      head.push(`<div class="delivery-summary-line small">所选保护档位下：整批最低仍需 ${bytes(allocation.selected_jpeg_bytes)}，缺口 ${bytes(allocation.deficit_bytes)}</div>`);
    head.push(`</div>`);

    if (plan.input_changed) head.push(`<p class="delivery-alert">${escapeHtml(plan.input_notice)}</p>`);
    if (plan.status === 'infeasible_protection') head.push(renderProtectionBlock(plan));
    else if (plan.status === 'infeasible_budget') head.push(renderBudgetBlock(plan));

    const scenarioRows = Object.values(plan.scenarios).map(sc =>
      `<tr class="${sc.profile === plan.options.profile ? 'active' : ''}${sc.floor_bytes === null ? ' unsat' : ''}"><td>${escapeHtml(sc.label)}</td><td>${bytes(sc.floor_bytes)}</td><td>${bytes(sc.ceiling_bytes)}</td><td>${sc.unsatisfiable_pages ? sc.unsatisfiable_pages + ' 页不可达' : (sc.fits_budget ? '容量内' : '超 ' + bytes(sc.deficit_bytes))}</td></tr>`).join('');
    head.push(`<div class="delivery-scenarios"><h3>各保护档位的整批容量区间</h3>
      <table class="modal-table delivery-table"><thead><tr><th>保护档位</th><th>底线总量</th><th>上限总量</th><th>与本容量目标</th></tr></thead><tbody>${scenarioRows}</tbody></table>
      <p class="hint">底线总量是每页刚好达到该档保护阈值时的最小容量；上限总量是每页取最高可用质量。若底线总量已超出目标，说明整批在保持该保护时无法达成容量。</p></div>`);

    if (plan.status === 'ready') {
      head.push(renderAllocation(plan));
      head.push(renderPageList(plan));
    } else {
      head.push(renderBlockedPages(plan));
    }

    const actions = [`<div class="delivery-actions">
      <button id="deliveryAdjust">调整预算 / 保护后重新比较</button>
      <button id="deliveryReplan">用当前设置重新生成</button>
      ${plan.status === 'ready' && !plan.confirmed ? '<button class="primary" id="deliveryConfirm">确认此编码结果</button>' : ''}
      ${plan.status === 'ready' && plan.confirmed ? '<button class="primary" id="deliveryDownload">下载已确认的编码结果</button>' : ''}
      <button id="deliveryClose">关闭</button>
    </div>`];
    if (plan.comparison && plan.comparison.available) actions.push(renderComparison(plan));
    actions.push(`<p class="help-text">${plan.invariants.map(g => escapeHtml(g.check)).join('；')}。</p>`);

    modal('JPEG 容量交付方案', head.join('') + actions.join(''));
    wire(plan);
    if (plan.status === 'ready') {
      const active = plan.pages.find(p => p.page_id === delivery.activePage) || plan.pages[0];
      delivery.activePage = active.page_id;
      await showPage(active);
    }
  }

  function renderProtectionBlock(plan) {
    const rows = plan.allocation.unsatisfiable_pages.map(page => `
      <div class="delivery-blocked">
        <div class="delivery-blocked-head"><b>第 ${page.number} 页 · ${escapeHtml(page.name)}</b><span>${escapeHtml(contentNames[page.content_kind] || '')} · 档位「${escapeHtml(page.profile_label)}」</span></div>
        <p>即使质量 ${page.lowest_quality}（${page.lowest_sampling}）仍达不到阈值：</p>
        <table class="modal-table delivery-table">${page.failures.map(f => `<tr><td>${escapeHtml(f.label)}</td><td>实测 ${metric(f.metric, f.value)}</td><td>要求${f.direction === 'min' ? '≥' : '≤'} ${metric(f.metric, f.threshold)}</td></tr>`).join('')}</table>
        <p class="hint">该页在其他档位的最低容量：${page.alternatives.map(a => `${escapeHtml(a.label)} ${a.feasible ? bytes(a.floor_bytes) : '仍不可达'}`).join(' · ')}</p>
      </div>`).join('');
    return `<p class="delivery-alert">${escapeHtml(plan.allocation.explanation)}</p>${rows}`;
  }

  function renderBudgetBlock(plan) {
    const alternatives = (plan.allocation.alternatives || []).map(a =>
      `<tr><td>${escapeHtml(a.label)}</td><td>${bytes(a.floor_bytes)}</td><td>${a.fits_budget ? '可在容量内实现' : '仍超 ' + bytes(a.deficit_bytes)}</td></tr>`).join('');
    return `<p class="delivery-alert">${escapeHtml(plan.allocation.explanation)}</p>
      <div class="delivery-alternatives"><h3>换成其他保护档位后的整批最低容量</h3>
      <table class="modal-table delivery-table"><thead><tr><th>保护档位</th><th>底线总量</th><th>结论</th></tr></thead><tbody>${alternatives || '<tr><td colspan="3">没有可比较的档位</td></tr>'}</tbody></table>
      <p class="hint">这些数字是把每页压到该档位保护下限的最小容量。选择更宽松的档位会降低保护，系统会明确列出收紧或放宽了哪些阈值。</p></div>`;
  }

  function renderAllocation(plan) {
    const steps = plan.allocation.steps || [];
    if (!steps.length) {
      return `<div class="delivery-allocation"><h3>容量分配</h3><p class="hint">当前容量目标允许每页保持其最高可用质量，没有页面被压缩。</p></div>`;
    }
    const rows = steps.map(step => `<tr><td>${step.number}</td><td>${escapeHtml(step.name)}</td><td>${step.from_quality} → ${step.to_quality}</td><td>${escapeHtml(step.from_sampling)} → ${escapeHtml(step.to_sampling)}</td><td>${bytes(step.bytes_saved)}</td><td>${escapeHtml(metricNames[step.binding_after] || '')} ${pct(step.margin_after)}</td></tr>`).join('');
    const affected = [...new Set(steps.map(s => s.number))].sort((a, b) => a - b);
    return `<div class="delivery-allocation"><h3>已生效的取舍（逐页降档）</h3>
      <p class="hint">为满足容量目标，以下 ${steps.length} 次降档作用在 ${affected.length} 页上；每次都是该页在当前余量下最省容量的一步，且仍不低于该页保护阈值。</p>
      <table class="modal-table delivery-table"><thead><tr><th>页</th><th>名称</th><th>质量</th><th>采样</th><th>释放</th><th>降档后最紧指标</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderPageList(plan) {
    const rows = plan.pages.map(page => {
      const s = page.selection;
      const binds = s ? `${escapeHtml(metricNames[s.binding] || '')} ${s.at_floor ? '（下限）' : ''}${s.borderline ? ' 临界' : ''}` : '';
      return `<tr data-page="${page.page_id}" class="delivery-page-row ${page.page_id === delivery.activePage ? 'active' : ''}">
        <td>${page.number}</td><td>${escapeHtml(page.name)}</td><td>${escapeHtml(contentNames[page.content_kind] || page.content_kind)}</td>
        <td>${s ? s.quality : '—'}</td><td>${s ? escapeHtml(s.sampling) : '—'}</td><td>${s ? bytes(s.bytes) : '—'}</td>
        <td>${s ? escapeHtml(page.profile_label) : ''}</td><td>${binds}</td><td>${s ? pct(s.margin) : '—'}</td></tr>`;
    }).join('');
    return `<div class="delivery-pages"><h3>逐页结果（点击行在下方 1:1 核对）</h3>
      <p class="hint">“最紧指标”是当前页距离保护阈值最近的测量项；标（下限）表示已降到该页允许的最低档位，标“临界”表示余量低于 10%。</p>
      <table class="modal-table delivery-table delivery-page-table"><thead><tr><th>页</th><th>名称</th><th>识别内容</th><th>质量</th><th>采样</th><th>字节</th><th>保护档位</th><th>最紧指标</th><th>余量</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderBlockedPages(plan) {
    const blocked = plan.allocation.blocked_pages || [];
    if (!blocked.length) return '';
    const rows = blocked.map(page => `<tr><td>${page.number}</td><td>${escapeHtml(page.name)}</td><td>${escapeHtml(contentNames[page.content_kind] || '')}</td><td>${page.floor_quality} · ${escapeHtml(page.floor_sampling)}</td><td>${bytes(page.floor_bytes)}</td><td>${escapeHtml(metricNames[page.binding] || '')} ${pct(page.margin)}</td></tr>`).join('');
    return `<div class="delivery-blocked-summary"><h3>当前容量目标无法容纳的保护底线</h3>
      <p class="hint">每页以下限档位计仍无法装进目标容量。这些是各页不能继续下调之处，没有被“暗中”压到阈值以下。</p>
      <table class="modal-table delivery-table"><thead><tr><th>页</th><th>名称</th><th>识别内容</th><th>下限质量·采样</th><th>该页下限容量</th><th>最紧指标</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderComparison(plan) {
    const comparison = plan.comparison;
    const contextRows = [
      ['总容量目标', comparison.budget],
      ['保护档位', comparison.protection],
      ['质量底线', comparison.floor_quality],
      ['色度采样', comparison.chroma_sampling],
    ].map(([name, item]) => `<tr class="${item.changed ? 'changed' : ''}"><td>${name}</td><td>${escapeHtml(String(item.before))}</td><td>${escapeHtml(String(item.after))}</td><td>${item.changed ? '已调整' : '未变'}</td></tr>`).join('');
    const rows = comparison.pages.map(row => `<tr class="${row.added ? 'changed' : ''}">
      <td>${row.number}</td><td>${escapeHtml(row.name)}</td>
      <td>${row.before_quality === null ? '—' : row.before_quality + ' ' + escapeHtml(row.before_sampling)}</td>
      <td>${row.after_quality === null ? '—' : row.after_quality + ' ' + escapeHtml(row.after_sampling)}</td>
      <td>${bytes(row.before_bytes)} → ${bytes(row.after_bytes)}</td>
      <td>${row.before_margin === null ? '—' : pct(row.before_margin)} → ${row.after_margin === null ? '—' : pct(row.after_margin)}</td>
      <td>${row.after_at_floor ? '已到下限' : ''}${row.after_at_ceiling ? '在最高档' : ''}${row.input_changed ? ' 输入已变化' : ''}</td></tr>`).join('');
    const deficitText = (comparison.deficit_bytes.before === null && comparison.deficit_bytes.after === null) ? '' :
      `<p class="hint">缺口：调整前 ${bytes(comparison.deficit_bytes.before)} → 调整后 ${bytes(comparison.deficit_bytes.after)}；实际交付 JPEG 总量 ${bytes(comparison.jpeg_bytes.before)} → ${bytes(comparison.jpeg_bytes.after)}。${comparison.using_floor_estimates ? '方案未达成容量时，逐页数字改用该档位保护下限估算，便于比较需要放宽多少。' : ''}</p>`;
    return `<div class="delivery-comparison"><h3>与上一方案的比较</h3>
      ${comparison.input_changed ? '<p class="delivery-alert">页面输入已变化，以下比较包含输入差异。</p>' : ''}
      <table class="modal-table delivery-table"><thead><tr><th>设置</th><th>上一方案</th><th>本方案</th><th>状态</th></tr></thead><tbody>${contextRows}</tbody></table>
      ${deficitText}
      <table class="modal-table delivery-table"><thead><tr><th>页</th><th>名称</th><th>之前</th><th>现在</th><th>字节</th><th>余量</th><th>备注</th></tr></thead><tbody>${rows}</tbody></table>
      ${comparison.removed_pages.length ? `<p class="hint">上一方案含 ${comparison.removed_pages.length} 页不在本批：${comparison.removed_pages.map(p => escapeHtml(p.name)).join('、')}（未纳入本次交付）。</p>` : ''}
    </div>`;
  }

  async function showPage(page) {
    if (!page) return;
    delivery.activePage = page.page_id;
    const plan = delivery.plan;
    const host = $('deliveryCheck');
    if (!host) return;
    host.innerHTML = `<div class="delivery-check-head">
      <h3>1:1 解码核对 · 第 ${page.number} 页</h3>
      <div class="row">
        ${Object.entries(families).map(([key, label]) => `<button data-family="${key}" class="family-button ${delivery.family === key ? 'active' : ''}">${label}</button>`).join('')}
      </div>
      <p class="hint">${familyHints[delivery.family]}</p>
    </div><div id="deliveryCheckBody" class="delivery-check-body">正在解码候选…</div>`;
    host.querySelectorAll('[data-family]').forEach(button => button.onclick = () => {
      delivery.family = button.dataset.family;
      showPage(page).catch(error => toast(error.message, true));
    });
    let data;
    if (delivery.family !== 'page' && !page.regions[delivery.family]) {
      $('deliveryCheckBody').innerHTML = '<p class="hint">此页没有该类型内容的显著窗口；可改用整页，或在预览中手动定位。</p>';
      return;
    }
    data = await api(`/deliveries/${plan.id}/preview?page_id=${page.page_id}&family=${delivery.family}`);
    const s = data.candidate;
    const metricRows = Object.entries(data.window_metrics).filter(([key]) => data.window_applicable.includes(key)).map(([key, value]) =>
      `<tr><td>${escapeHtml(metricNames[key] || key)}</td><td>${metric(key, value)}</td></tr>`).join('');
    const failRows = data.window_failures.length ? data.window_failures.map(f =>
      `<tr class="failed"><td>${escapeHtml(f.label)}</td><td>${metric(f.metric, f.value)}</td><td>${f.direction === 'min' ? '≥' : '≤'} ${metric(f.metric, f.threshold)}</td></tr>`).join('')
      : '<tr><td colspan="3">窗口内所有适用指标均达标。</td></tr>';
    $('deliveryCheckBody').innerHTML = `
      <div class="delivery-preview">
        <div class="delivery-preview-pane"><span class="delivery-preview-label">源图同位置（参考，不是交付内容）</span><img alt="源图参考窗口" src="${data.images.source}&t=${Date.now()}"></div>
        <div class="delivery-preview-pane candidate"><span class="delivery-preview-label">实际交付 JPEG 解码 · 质量 ${s.quality} · ${escapeHtml(s.sampling)}</span><img alt="交付候选解码窗口" src="${data.images.candidate}&t=${Date.now()}"></div>
        <div class="delivery-preview-pane difference"><span class="delivery-preview-label">差异图（放大 8 倍便于观察）</span><img alt="差异图" src="${data.images.difference}&t=${Date.now()}"></div>
      </div>
      <p class="hint">${escapeHtml(data.note)} 窗口 [x=${data.window[0]}, y=${data.window[1]}, ${data.window[2]}×${data.window[3]}]，SHA-256 ${s.sha256.slice(0, 16)}…，${bytes(s.bytes)}。</p>
      <div class="delivery-check-metrics">
        <table class="modal-table delivery-table"><thead><tr><th>本窗口实测指标（1:1）</th><th>数值</th></tr></thead><tbody>${metricRows || '<tr><td colspan="2">窗口内容过少，未计算指标</td></tr>'}</tbody></table>
        <table class="modal-table delivery-table"><thead><tr><th>本窗口阈值核对</th><th>实测</th><th>阈值</th></tr></thead><tbody>${failRows}</tbody></table>
      </div>
      <p class="hint">本窗口只反映所选位置；整页的判定依据是整页指标。整页最紧指标：${escapeHtml(metricNames[data.page_binding] || data.page_binding || '')}，余量 ${pct(data.page_margin)}，${data.page_failures.length ? '低于阈值 ' + data.page_failures.map(f => escapeHtml(f.label)).join('、') : '全部达标'}。</p>
      <div class="delivery-check-actions">
        <label>手动定位 x <input id="deliveryX" type="number" value="${data.window[0]}"></label>
        <label>y <input id="deliveryY" type="number" value="${data.window[1]}"></label>
        <button id="deliveryLocate">按坐标查看</button>
      </div>`;
    $('deliveryLocate').onclick = () => {
      const x = Math.round(Number($('deliveryX').value)), y = Math.round(Number($('deliveryY').value));
      api(`/deliveries/${plan.id}/preview?page_id=${page.page_id}&family=${delivery.family}&x=${x}&y=${y}`)
        .then(result => { $('deliveryCheckBody').innerHTML = ''; delivery.plan = plan; renderCheckWindow(page, result); })
        .catch(error => toast(error.message, true));
    };
  }

  function renderCheckWindow(page, data) {
    const host = $('deliveryCheckBody');
    const s = data.candidate;
    const metricRows = Object.entries(data.window_metrics).filter(([key]) => data.window_applicable.includes(key)).map(([key, value]) =>
      `<tr><td>${escapeHtml(metricNames[key] || key)}</td><td>${metric(key, value)}</td></tr>`).join('');
    const failRows = data.window_failures.length ? data.window_failures.map(f =>
      `<tr class="failed"><td>${escapeHtml(f.label)}</td><td>${metric(f.metric, f.value)}</td><td>${f.direction === 'min' ? '≥' : '≤'} ${metric(f.metric, f.threshold)}</td></tr>`).join('')
      : '<tr><td colspan="3">窗口内所有适用指标均达标。</td></tr>';
    host.innerHTML = `
      <div class="delivery-preview">
        <div class="delivery-preview-pane"><span class="delivery-preview-label">源图同位置（参考）</span><img alt="源图参考窗口" src="${data.images.source}&t=${Date.now()}"></div>
        <div class="delivery-preview-pane candidate"><span class="delivery-preview-label">实际交付 JPEG 解码 · 质量 ${s.quality}</span><img alt="交付候选解码窗口" src="${data.images.candidate}&t=${Date.now()}"></div>
        <div class="delivery-preview-pane difference"><span class="delivery-preview-label">差异图（×8）</span><img alt="差异图" src="${data.images.difference}&t=${Date.now()}"></div>
      </div>
      <p class="hint">窗口 [x=${data.window[0]}, y=${data.window[1]}, ${data.window[2]}×${data.window[3]}]，SHA-256 ${s.sha256.slice(0, 16)}…。${escapeHtml(data.note)}</p>
      <div class="delivery-check-metrics">
        <table class="modal-table delivery-table"><tbody>${metricRows}</tbody></table>
        <table class="modal-table delivery-table"><thead><tr><th>窗口阈值核对</th><th>实测</th><th>阈值</th></tr></thead><tbody>${failRows}</tbody></table>
      </div>
      <div class="delivery-check-actions"><button id="deliveryBack">返回按内容窗口核对</button></div>`;
    $('deliveryBack').onclick = () => showPage(page).catch(error => toast(error.message, true));
  }

  function wire(plan) {
    const adjust = $('deliveryAdjust');
    if (adjust) adjust.onclick = () => startDelivery(plan.id).catch(error => toast(error.message, true));
    const replan = $('deliveryReplan');
    if (replan) replan.onclick = () => busy('正在用当前设置重新编码并比较…', async () => {
      const fresh = await api(`/deliveries/${plan.id}/replan`, 'POST', {});
      delivery.plan = fresh;
      await renderPlan(fresh);
    });
    const confirm = $('deliveryConfirm');
    if (confirm) confirm.onclick = () => busy('正在核对暂存编码并确认…', async () => {
      const result = await api(`/deliveries/${plan.id}/confirm`, 'POST', {});
      delivery.plan = result;
      await renderPlan(result);
      toast('已确认此编码结果；下载将只使用这些通过校验的 JPEG');
    });
    const download = $('deliveryDownload');
    if (download) download.onclick = () => busy('正在校验并打包已确认的编码结果…', async () => {
      const verification = await api(`/deliveries/${plan.id}/verify`);
      if (!verification.valid) throw Error('校验未通过：' + verification.errors.join('；'));
      const anchor = document.createElement('a');
      anchor.href = `/api/deliveries/${plan.id}/download`;
      anchor.download = '';
      document.body.append(anchor); anchor.click(); anchor.remove();
      toast(`已下载确认过的 ${plan.page_count} 页 JPEG（共 ${bytes(verification.jpeg_bytes)}）`);
    });
    const close = $('deliveryClose');
    if (close) close.onclick = () => $('modal').close();
    document.querySelectorAll('[data-page]').forEach(row => row.onclick = () => {
      document.querySelectorAll('[data-page]').forEach(item => item.classList.toggle('active', item === row));
      const page = plan.pages.find(item => item.page_id === row.dataset.page);
      showPage(page).catch(error => toast(error.message, true));
    });
    // The 1:1 check pane lives right under the page table.
    const pages = document.querySelector('.delivery-pages');
    if (pages && plan.status === 'ready' && !$('deliveryCheck')) {
      const check = document.createElement('div');
      check.id = 'deliveryCheck';
      check.className = 'delivery-check';
      pages.after(check);
    }
  }

  function attach() {
    if (!$('capacityDeliveryTool')) return;
    $('capacityDeliveryTool').onclick = () => {
      if (!state.document) { toast('请先选择文档', true); return; }
      startDelivery(null).catch(error => toast(error.message, true));
    };
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach);
  else attach();

  window.PaperFlowDelivery = {start: startDelivery, open: openPlan, current: currentPlan};
})();
