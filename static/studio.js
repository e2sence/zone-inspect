/* ═══ Recipe Studio ═══
 * Recipe list → recipe (versions) → draft wizard:
 * 1 Details · 2 Reference · 3 Zones · 4 Good samples · 5 Defect samples ·
 * 6 Calibrate · 7 Publish.
 */
(function () {
    const ROLE = (window.STUDIO || {}).role;
    const MIN_GOOD = 15, MIN_DEFECT = 2;
    const DEFECT_TYPES = [['missing', 'Missing'], ['wrong_part', 'Wrong part'], ['shifted', 'Shifted'],
        ['rotated', 'Rotated'], ['contamination', 'Contamination'], ['damage', 'Damage'], ['other', 'Other']];
    const PHONES = ['iPhone 12', 'iPhone 13', 'iPhone 14', 'Xiaomi 14', 'Xiaomi 15', 'Xiaomi 16'];

    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    let rec = null;        // recipe header + versions
    let draft = null;      // draft version (with summary/check)
    let samples = [];
    let step = 1;
    let zonesDirty = false;
    let pollTimer = null;

    function toast(msg, type = 'info') {
        const t = document.createElement('div');
        t.className = 'toast ' + type;
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 3500);
    }
    window.toast = toast;

    async function api(url, opts = {}) {
        const init = { method: opts.method || 'GET' };
        if (opts.json !== undefined) {
            init.headers = { 'Content-Type': 'application/json' };
            init.body = JSON.stringify(opts.json);
        } else if (opts.form) {
            init.body = opts.form;
        }
        const r = await fetch(url, init);
        if (r.status === 401) { location.href = '/login'; throw new Error('Unauthorized'); }
        let d = {};
        try { d = await r.json(); } catch (e) { /* non-JSON */ }
        if (!r.ok) { const err = new Error(d.error || `Request failed (${r.status})`); err.data = d; err.status = r.status; throw err; }
        return d;
    }

    // Two-step confirmation on the button itself (no browser dialogs)
    function confirmClick(btn, label, action) {
        if (btn.dataset.armed) {
            delete btn.dataset.armed;
            btn.textContent = btn.dataset.orig;
            action();
            return;
        }
        btn.dataset.orig = btn.textContent;
        btn.dataset.armed = '1';
        btn.textContent = label;
        setTimeout(() => { if (btn.dataset.armed) { delete btn.dataset.armed; btn.textContent = btn.dataset.orig; } }, 4000);
    }

    // ─── Recipe list ─────────────────────────────────────────────────────
    async function showList() {
        stopPoll();
        rec = null; draft = null;
        $('view-recipe').classList.add('hidden');
        $('view-list').classList.remove('hidden');
        history.replaceState(null, '', '/studio');
        const tbody = $('recipe-table').querySelector('tbody');
        try {
            const d = await api('/api/recipes');
            $('recipe-count').textContent = `· ${d.recipes.length}`;
            if (!d.recipes.length) {
                tbody.innerHTML = '<tr><td colspan="6" class="st-muted">No recipes yet. Create the first one with “New recipe”.</td></tr>';
                return;
            }
            tbody.innerHTML = d.recipes.map(r => `
                <tr class="clickable" data-id="${esc(r.id)}">
                    <td class="name">${esc(r.name)}${r.barcode_mask ? ` <span class="st-muted">${esc(r.barcode_mask)}*</span>` : ''}</td>
                    <td>${r.active_version ? `<span class="chip ok">v${r.active_version}</span>` : '<span class="st-muted">—</span>'}</td>
                    <td>${r.draft_version ? `<span class="chip acc">v${r.draft_version}</span>` : '<span class="st-muted">—</span>'}</td>
                    <td>${r.zone_count}</td>
                    <td>${r.samples.good} / ${r.samples.defect}${r.samples.good || r.samples.defect ? '' : ' <span class="chip">no samples</span>'}</td>
                    <td>${esc(r.group) || '<span class="st-muted">all</span>'}</td>
                </tr>`).join('');
            tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.addEventListener('click', () => openRecipe(tr.dataset.id)));
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="6" class="st-muted">${esc(e.message)}</td></tr>`;
        }
    }

    $('btn-new-recipe').addEventListener('click', () => {
        $('new-recipe-form').classList.toggle('hidden');
        $('nr-name').focus();
    });
    $('nr-cancel').addEventListener('click', () => $('new-recipe-form').classList.add('hidden'));
    $('new-recipe-form').addEventListener('submit', async e => {
        e.preventDefault();
        const body = { name: $('nr-name').value.trim(), barcode_mask: $('nr-mask').value.trim() };
        if ($('nr-group')) body.group = $('nr-group').value.trim();
        try {
            const d = await api('/api/recipes', { method: 'POST', json: body });
            $('new-recipe-form').reset();
            $('new-recipe-form').classList.add('hidden');
            toast('Draft v1 created', 'success');
            openRecipe(d.recipe_id, 2);
        } catch (err) { toast(err.message, 'error'); }
    });

    // ─── Recipe ──────────────────────────────────────────────────────────
    async function openRecipe(id, startStep) {
        stopPoll();
        try {
            rec = await api(`/api/recipes/${id}`);
        } catch (e) { toast(e.message, 'error'); return; }
        history.replaceState(null, '', `/studio#${id}`);
        $('view-list').classList.add('hidden');
        $('view-recipe').classList.remove('hidden');
        $('rc-title').textContent = rec.name;
        $('rc-badges').innerHTML =
            (rec.active_version ? `<span class="chip ok">Live v${rec.active_version}</span>` : '<span class="chip">Not published</span>') +
            (rec.draft_version ? `<span class="chip acc">Draft v${rec.draft_version}</span>` : '') +
            (rec.group ? `<span class="chip">${esc(rec.group)}</span>` : '');
        $('btn-open-draft').classList.toggle('hidden', !!rec.draft_version || !rec.active_version);
        renderVersions();
        if (rec.draft_version) {
            $('no-draft').classList.add('hidden');
            $('draft-area').classList.remove('hidden');
            await loadDraft(startStep);
        } else {
            draft = null;
            $('draft-area').classList.add('hidden');
            $('no-draft').classList.remove('hidden');
        }
    }

    function fmtDate(s) { return s ? String(s).slice(0, 16).replace('T', ' ') : ''; }

    function renderVersions() {
        const el = $('version-list');
        el.innerHTML = rec.versions.map(v => {
            const chip = v.status === 'active' ? '<span class="chip ok">live</span>'
                : v.status === 'draft' ? '<span class="chip acc">draft</span>' : '<span class="chip">archived</span>';
            const when = v.status === 'draft' ? `created ${fmtDate(v.created_at)}${v.created_by ? ' · ' + esc(v.created_by) : ''}`
                : `published ${fmtDate(v.published_at)}${v.published_by ? ' · ' + esc(v.published_by) : ''}`;
            const legacy = v.migrated ? '<span class="st-muted">from old template</span>' : '';
            const btn = v.status === 'archived' ? `<button data-rollback="${v.version}">Put back on the line</button>` : '';
            return `<li><div class="top"><b>v${v.version}</b>${chip}</div><span class="st-muted">${when}</span>${legacy}${btn}</li>`;
        }).join('');
        el.querySelectorAll('[data-rollback]').forEach(b => b.addEventListener('click', () =>
            confirmClick(b, `Confirm: v${b.dataset.rollback} goes live`, async () => {
                try {
                    await api(`/api/recipes/${rec.id}/rollback/${b.dataset.rollback}`, { method: 'POST' });
                    toast(`v${b.dataset.rollback} is live again`, 'success');
                    openRecipe(rec.id);
                } catch (e) { toast(e.message, 'error'); }
            })));
    }

    $('back-to-list').addEventListener('click', e => { e.preventDefault(); showList(); });
    $('btn-open-draft').addEventListener('click', async () => {
        try {
            const d = await api(`/api/recipes/${rec.id}/draft`, { method: 'POST', json: {} });
            toast(`Draft v${d.version} opened`, 'success');
            openRecipe(rec.id, 1);
        } catch (e) { toast(e.message, 'error'); }
    });
    $('btn-delete-recipe').addEventListener('click', e => confirmClick(e.currentTarget,
        'Click again: delete recipe, versions and samples', async () => {
            try {
                await api(`/api/templates/${rec.id}`, { method: 'DELETE' });
                toast('Recipe deleted', 'info');
                showList();
            } catch (err) { toast(err.message, 'error'); }
        }));

    // ─── Draft ───────────────────────────────────────────────────────────
    const editor = ZoneEditor.create($('zone-editor'), {
        onChange: () => { zonesDirty = true; $('zones-dirty').textContent = 'Unsaved changes'; }
    });

    async function loadDraft(startStep) {
        try {
            draft = await api(`/api/recipes/${rec.id}/draft`);
        } catch (e) { toast(e.message, 'error'); return; }
        zonesDirty = false;
        $('zones-dirty').textContent = '';
        fillDetails();
        renderReference(draft.image_b64 ? 'data:image/jpeg;base64,' + draft.image_b64 : null, draft.ref_quality);
        if (draft.image_b64) editor.load('data:image/jpeg;base64,' + draft.image_b64, draft.zones);
        await refreshSamples();
        if (startStep) step = startStep;
        else step = firstIncompleteStep();
        renderAll();
    }

    function applyState(state) {
        if (!state || !draft) return;
        if (state.summary) draft.summary = state.summary;
        if (state.check) draft.check = state.check;
        if ('calibration_state' in state) draft.calibration_state = state.calibration_state;
        if (state.calibration) draft.calibration = state.calibration;
    }

    async function refreshSamples() {
        try {
            samples = (await api(`/api/recipes/${rec.id}/samples`)).samples;
        } catch (e) { samples = []; }
    }

    async function refreshState() {
        try { applyState(await api(`/api/recipes/${rec.id}/draft/state`)); } catch (e) { /* keep last */ }
    }

    function stepDone(n) {
        if (!draft) return false;
        const s = draft.summary || { zones: [] };
        switch (n) {
            case 1: return !!draft.name;
            case 2: return !!draft.ref_image && (draft.ref_quality || {}).level !== 'bad';
            case 3: return (draft.zones || []).length > 0;
            case 4: return s.zones.length > 0 && s.zones.every(z => z.good >= MIN_GOOD);
            case 5: return s.zones.length > 0 && s.zones.every(z => z.defect >= MIN_DEFECT &&
                z.critical_subzones.every(c => c.defects >= 1));
            case 6: { const c = draft.calibration_state || {}; return !!(c.present && c.fresh && !c.missed && !(c.skipped || []).length); }
            case 7: return false;
            default: return false;
        }
    }
    function firstIncompleteStep() {
        for (const n of [1, 2, 3, 4, 5, 6]) if (!stepDone(n)) return n;
        return 7;
    }

    const STEP_NAMES = ['Details', 'Reference', 'Zones', 'Good samples', 'Defect samples', 'Calibrate', 'Publish'];
    function renderTabs() {
        $('step-tabs').innerHTML = STEP_NAMES.map((n, i) => {
            const k = i + 1;
            const cls = k === step ? 'cur' : stepDone(k) ? 'done' : '';
            return `<button type="button" class="${cls}" data-step="${k}">${k} ${n}</button>`;
        }).join('');
        $('step-tabs').querySelectorAll('button').forEach(b => b.addEventListener('click', () => goStep(+b.dataset.step)));
        document.querySelectorAll('.st-step').forEach(el => el.classList.toggle('active', +el.dataset.step === step));
    }

    function goStep(n) {
        if (step === 3 && zonesDirty && n !== 3) toast('Zones have unsaved changes', 'warn');
        step = n;
        renderAll();
        if (n === 3) setTimeout(() => editor.redraw(), 0);
    }

    function renderAll() {
        renderTabs();
        renderSamplesStep(4, 'good');
        renderSamplesStep(5, 'defect');
        renderCalibration();
        renderPublish();
        if (step === 4 || step === 5) startPoll(); else stopPoll();
        if (step === 6) loadCalibration();
    }

    // 1 · Details
    function fillDetails() {
        $('d-name').value = draft.name || '';
        $('d-mask').value = draft.barcode_mask || '';
        $('d-notes').value = draft.operator_notes || '';
        $('d-zsens').value = draft.zone_sensitivity ?? '';
        $('d-ssens').value = draft.subzone_sensitivity ?? '';
        document.querySelectorAll('input[name=d-plan]').forEach(r => { r.checked = r.value === (draft.shooting_plan || 'zones'); });
    }
    $('details-form').addEventListener('submit', async e => {
        e.preventDefault();
        const num = v => v.trim() === '' ? null : parseFloat(v);
        const body = {
            name: $('d-name').value.trim(), barcode_mask: $('d-mask').value.trim(),
            operator_notes: $('d-notes').value.trim(),
            shooting_plan: (document.querySelector('input[name=d-plan]:checked') || {}).value || 'zones',
            zone_sensitivity: num($('d-zsens').value), subzone_sensitivity: num($('d-ssens').value),
        };
        try {
            const d = await api(`/api/recipes/${rec.id}/draft`, { method: 'PUT', json: body });
            Object.assign(draft, body);
            applyState(d);
            $('rc-title').textContent = body.name;
            toast('Details saved', 'success');
            renderAll();
        } catch (err) { toast(err.message, 'error'); }
    });

    // 2 · Reference
    function renderReference(src, quality) {
        $('ref-preview').classList.toggle('hidden', !src);
        $('ref-empty').classList.toggle('hidden', !!src);
        if (src) $('ref-preview').src = src;
        const q = quality;
        $('ref-quality').innerHTML = !q ? '' :
            `<div class="st-row"><span class="chip ${q.level === 'ok' ? 'ok' : q.level === 'warn' ? 'warn' : 'bad'}">Quality: ${q.level.toUpperCase()}</span>
             <span class="st-muted">${q.width}×${q.height} · sharpness ${q.sharpness}</span></div>
             ${q.issues.length ? `<ul class="st-list ${q.level === 'bad' ? 'bad' : 'warn'}">${q.issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul>` : ''}`;
        const staleN = (draft && draft.summary && draft.summary.stale) || 0;
        $('ref-warning').textContent = staleN ? `${staleN} sample(s) were aligned to the previous reference. Re-align them in step 4.` : '';
    }

    async function uploadReference(blob, name) {
        const fd = new FormData();
        fd.append('image', blob, name || 'reference.jpg');
        try {
            const d = await api(`/api/recipes/${rec.id}/draft/reference`, { method: 'POST', form: fd });
            draft.ref_image = `v${draft.version}/ref.jpg`;
            draft.ref_quality = d.quality;
            draft.ref_size = [d.width, d.height];
            applyState(d);
            const src = 'data:image/jpeg;base64,' + d.image_b64;
            renderReference(src, d.quality);
            editor.load(src, editor.getZones().length ? editor.getZones() : draft.zones);
            toast(d.quality.level === 'bad' ? 'Reference saved, but it failed the quality check' : 'Reference saved',
                d.quality.level === 'bad' ? 'warn' : 'success');
            await refreshSamples();
            renderAll();
        } catch (e) { toast(e.message, 'error'); }
    }
    $('ref-file').addEventListener('change', e => {
        const f = e.target.files[0];
        if (f) uploadReference(f, f.name);
        e.target.value = '';
    });
    $('ref-blend').addEventListener('change', async e => {
        const files = [...e.target.files];
        e.target.value = '';
        if (files.length < 2) { toast('Select at least 2 photos to blend', 'warn'); return; }
        const fd = new FormData();
        files.forEach(f => fd.append('images', f, f.name));
        toast(`Blending ${files.length} photos…`, 'info');
        try {
            const d = await api('/api/auto_blend', { method: 'POST', form: fd });
            const blob = await (await fetch('data:image/jpeg;base64,' + d.image_b64)).blob();
            await uploadReference(blob, 'blended.jpg');
        } catch (err) { toast(err.message, 'error'); }
    });

    // 3 · Zones
    $('btn-save-zones').addEventListener('click', async () => {
        const zones = editor.getZones();
        try {
            const d = await api(`/api/recipes/${rec.id}/draft`, { method: 'PUT', json: { zones } });
            draft.zones = zones;
            applyState(d);
            zonesDirty = false;
            $('zones-dirty').textContent = '';
            toast(`${zones.length} zone(s) saved`, 'success');
            renderAll();
        } catch (e) { toast(e.message, 'error'); }
    });

    // 4 + 5 · Samples
    function zoneLabel(i) { return ((draft.zones || [])[i] || {}).label || `Zone ${i + 1}`; }

    function renderSamplesStep(n, kind) {
        const el = document.querySelector(`.st-step[data-step="${n}"]`);
        if (!draft) { el.innerHTML = ''; return; }
        const s = draft.summary || { zones: [], devices: [], boards: [] };
        const good = kind === 'good';
        const list = samples.filter(x => x.kind === kind);
        const rows = s.zones.map(z => {
            const have = good ? z.good : z.defect, need = good ? MIN_GOOD : MIN_DEFECT;
            const pct = Math.min(100, Math.round(100 * have / need));
            const crit = good ? '' : z.critical_subzones.map(c =>
                `<span class="chip ${c.defects ? 'ok' : 'bad'}">${esc(c.label)} critical: ${c.defects}/1</span>`).join(' ');
            return `<div class="st-zone-row"><span>${esc(z.label)}</span>
                <span class="st-progress${have >= need ? '' : ' partial'}"><i style="width:${pct}%"></i></span>
                <span class="chip ${have >= need ? 'ok' : 'warn'}">${have} / ${need}</span></div>${crit ? `<div class="st-row">${crit}</div>` : ''}`;
        }).join('') || '<p class="st-muted">Define zones in step 3 first.</p>';
        const devs = (good ? s.good_devices : s.defect_devices) || [];
        const devices = devs.length ? devs.map(d => `<span class="chip">${esc(d)}</span>`).join(' ')
            + (good && devs.length < 2 ? ' <span class="chip warn">2 phones needed</span>' : '')
            : '<span class="chip warn">no phone named yet</span>';
        const small = s.zones.filter(z => z.smallest && z.smallest.px_in_photos !== null && z.smallest.px_in_photos < 48)
            .map(z => `${esc(z.label)}${z.smallest.name !== 'zone' ? ' / ' + esc(z.smallest.name) : ''} ≈ ${z.smallest.px_in_photos} px`);
        const stale = s.stale ? `<div class="st-confirm">${s.stale} sample(s) were aligned to an older reference and are not counted.
            <button class="btn btn-sm" data-realign>Re-align now</button></div>` : '';
        const intro = good
            ? `Photograph known-good boards the way operators will: hand-held, normal light, from at least
               ${2} different phones and ${3}+ boards. Each photo is aligned to the reference automatically.`
            : `Photograph boards with known defects: real rejects or prepared ones (missing part, marker dot,
               tape strip, wrong part). Then open each photo and draw a box around the defect.`;

        el.innerHTML = `
            <h3 class="st-h3">${n} · ${good ? 'Good samples' : 'Defect samples'}</h3>
            <p class="st-hint" style="margin-bottom:10px">${intro}</p>
            <div class="st-capture">
                <div class="st-panel st-qr">
                    <button class="btn btn-sm" data-qr>Connect a phone</button>
                    <div data-qr-box class="hidden"><img alt="QR code for the phone camera"><p class="st-hint" data-qr-url style="word-break:break-all;max-width:160px"></p></div>
                </div>
                <div class="st-panel">
                    <div class="st-dev">
                        <span>Upload from computer:</span>
                        <input list="phone-list" data-device placeholder="Phone model" size="12">
                        <input data-board placeholder="Board" size="7">
                        <label class="btn btn-sm st-file">Choose photos<input type="file" accept="image/*" multiple data-upload></label>
                    </div>
                    ${rows}
                    ${small.length && good ? `<p class="st-hint" style="color:var(--warn)">Too small in the photos: ${small.join(', ')}. Shoot closer or make it larger (48+ px).</p>` : ''}
                    <div class="st-row"><span class="st-muted">Phones${good ? ' (good samples)' : ''}:</span> ${devices}
                        ${good ? `<span class="st-muted">· boards: ${s.boards.length}</span>` : ''}</div>
                </div>
            </div>
            ${stale}
            <div class="st-row st-between" style="margin:12px 0 6px"><span class="st-muted">${list.length} photo(s)</span>
                <button class="btn btn-sm" data-next>${good ? 'Next: defect samples' : 'Next: publish'}</button></div>
            <div class="st-thumbs">${list.map(sampleCard).join('') || '<p class="st-muted">No photos yet.</p>'}</div>
            <datalist id="phone-list">${PHONES.map(p => `<option value="${p}">`).join('')}</datalist>`;

        el.querySelector('[data-qr]').addEventListener('click', () => connectPhone(el));
        el.querySelector('[data-upload]').addEventListener('change', e => uploadSamples(e, kind, el));
        el.querySelector('[data-next]').addEventListener('click', () => goStep(good ? 5 : 7));
        const re = el.querySelector('[data-realign]');
        if (re) re.addEventListener('click', realign);
        el.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => sampleAction(b)));
        const dev = el.querySelector('[data-device]');
        try { dev.value = localStorage.getItem('zi_studio_device') || ''; } catch (e) { /* ignore */ }
    }

    // Short label for the first quality issue (full text in the tooltip)
    function qualityLabel(q) {
        const first = (q.issues || [])[0] || '';
        const map = [[/^Low resolution/, 'low res'], [/^Blurry/, 'blurry'], [/^Slightly soft/, 'soft focus'],
            [/^Too dark/, 'dark'], [/^Overexposed/, 'overexposed'], [/^Glare/, 'glare']];
        const hit = map.find(([re]) => re.test(first));
        return hit ? hit[1] : 'quality';
    }

    function sampleCard(x) {
        const q = x.quality || {};
        const chips = [];
        if (x.stale) chips.push('<span class="chip warn">old reference</span>');
        else if (!x.aligned) chips.push('<span class="chip bad">not aligned</span>');
        else chips.push(`<span class="chip ok">${x.visible_zones.length ? x.visible_zones.map(i => esc(zoneLabel(i))).join(', ') : 'no full zone'}</span>`);
        if (q.level === 'bad' || q.level === 'warn')
            chips.push(`<span class="chip ${q.level}" title="${esc((q.issues || []).join('; '))}">${qualityLabel(q)}</span>`);
        if (x.kind === 'defect') {
            const located = (x.defects || []).filter(d => d.zone !== null && d.zone !== undefined);
            chips.push(x.defects && x.defects.length
                ? `<span class="chip acc">${located.map(d => esc(zoneLabel(d.zone)) + (d.subzone !== null && d.subzone !== undefined ? '/' + esc(((draft.zones[d.zone] || {}).subzones || [])[d.subzone]?.label || 'S' + (d.subzone + 1)) : '')).join(', ') || 'outside zones'}</span>`
                : '<span class="chip bad">not labelled</span>');
        }
        return `<div class="st-thumb${x.excluded ? ' excluded' : ''}">
            <img src="/api/samples/${esc(x.id)}/image?size=thumb" alt="Sample photo" loading="lazy">
            <div class="meta"><div class="chips">${chips.join('')}</div>
                <span class="st-muted">${esc(x.device || 'phone not named')}${x.board ? ' · board ' + esc(x.board) : ''}</span></div>
            <div class="acts">
                ${x.kind === 'defect' ? `<button data-act="label" data-id="${esc(x.id)}">Label</button>` : ''}
                <button data-act="exclude" data-id="${esc(x.id)}" title="${x.excluded ? 'Count this photo again' : 'Keep the photo but do not count it'}">${x.excluded ? 'Include' : 'Exclude'}</button>
                <button data-act="move" data-id="${esc(x.id)}" title="${x.kind === 'good' ? 'This board has a defect' : 'This board is good'}">${x.kind === 'good' ? '→ Defect' : '→ Good'}</button>
                <button data-act="delete" data-id="${esc(x.id)}">Delete</button>
            </div></div>`;
    }

    async function uploadSamples(e, kind, el) {
        const files = [...e.target.files];
        e.target.value = '';
        if (!files.length) return;
        const device = el.querySelector('[data-device]').value.trim();
        const board = el.querySelector('[data-board]').value.trim();
        try { localStorage.setItem('zi_studio_device', device); } catch (err) { /* ignore */ }
        if (!device) toast('Tip: name the phone so the 2-phone rule can be checked', 'warn');
        const fd = new FormData();
        files.forEach(f => fd.append('photo', f, f.name));
        fd.append('kind', kind); fd.append('device', device); fd.append('board', board);
        toast(`Uploading ${files.length} photo(s)…`, 'info');
        try {
            const d = await api(`/api/recipes/${rec.id}/samples`, { method: 'POST', form: fd });
            applyState(d);
            const bad = d.samples.filter(x => x.error || !x.aligned).length;
            toast(bad ? `${d.samples.length - bad} added, ${bad} could not be aligned` : `${d.samples.length} added`, bad ? 'warn' : 'success');
            await refreshSamples();
            renderAll();
        } catch (err) { toast(err.message, 'error'); }
    }

    async function sampleAction(b) {
        const s = samples.find(x => x.id === b.dataset.id);
        if (!s) return;
        const act = b.dataset.act;
        if (act === 'label') { openLabel(s); return; }
        if (act === 'delete') {
            confirmClick(b, 'Confirm', async () => {
                try { await api(`/api/samples/${s.id}`, { method: 'DELETE' }); await refreshSamples(); await refreshState(); renderAll(); }
                catch (e) { toast(e.message, 'error'); }
            });
            return;
        }
        const body = act === 'exclude' ? { excluded: !s.excluded } : { kind: s.kind === 'good' ? 'defect' : 'good' };
        try {
            const d = await api(`/api/samples/${s.id}`, { method: 'PATCH', json: body });
            applyState(d);
            await refreshSamples();
            renderAll();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function realign() {
        toast('Re-aligning samples…', 'info');
        try {
            const d = await api(`/api/recipes/${rec.id}/samples/realign`, { method: 'POST' });
            applyState(d);
            await refreshSamples();
            toast(`${d.realigned} sample(s) re-aligned`, 'success');
            renderAll();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function connectPhone(el) {
        try {
            const d = await api(`/api/recipes/${rec.id}/sample_qr`, { method: 'POST' });
            document.querySelectorAll('[data-qr-box]').forEach(box => {
                box.classList.remove('hidden');
                box.querySelector('img').src = 'data:image/png;base64,' + d.qr_b64;
                box.querySelector('[data-qr-url]').textContent = d.url;
            });
            toast('Scan the QR code with the phone', 'info');
        } catch (e) { toast(e.message, 'error'); }
    }

    function startPoll() {
        if (pollTimer) return;
        pollTimer = setInterval(async () => {
            if (!draft || document.hidden || (step !== 4 && step !== 5)) return;
            const before = samples.length;
            await refreshSamples();
            if (samples.length !== before) { await refreshState(); renderSamplesStep(step, step === 4 ? 'good' : 'defect'); renderTabs(); }
        }, 4000);
    }
    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

    // 6 · Calibrate
    let calibPoll = null;
    let calibMargin = null;      // slider value (not yet saved)

    async function loadCalibration() {
        try {
            const d = await api(`/api/recipes/${rec.id}/calibrate`);
            applyState(d);
            draft.calibration = d.calibration;
            const running = d.job && d.job.state === 'running';
            showCalibRun(running ? d.job : null);
            $('calib-job').textContent = d.job && d.job.state === 'error' ? d.job.message : '';
            $('btn-calibrate').disabled = running;
            if (running && !calibPoll) calibPoll = setInterval(loadCalibration, 1000);
            if (!running && calibPoll) {
                clearInterval(calibPoll); calibPoll = null;
                if (d.job.state === 'done') toast('Calibration finished', 'success');
                if (d.job.state === 'error') toast(d.job.message, 'error');
            }
            renderCalibration();
            renderTabs();
            renderPublish();
        } catch (e) { toast(e.message, 'error'); }
    }

    $('btn-calibrate').addEventListener('click', async () => {
        try {
            await api(`/api/recipes/${rec.id}/calibrate`, { method: 'POST' });
            showCalibRun({ progress: 0, message: 'Starting', elapsed: 0 });
            $('calib-job').textContent = '';
            $('btn-calibrate').disabled = true;
            if (!calibPoll) calibPoll = setInterval(loadCalibration, 1000);
        } catch (e) { toast(e.message, 'error'); }
    });

    const fmtSecs = s => s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;

    function showCalibRun(job) {
        $('calib-run').classList.toggle('hidden', !job);
        if (!job) return;
        const p = Math.max(0, Math.min(1, job.progress || 0));
        $('calib-run-pct').textContent = `${Math.round(p * 100)}%`;
        $('calib-run-bar').style.width = `${Math.max(p * 100, 2)}%`;
        $('calib-run-msg').textContent = `${job.message}…`;
        const el = job.elapsed || 0;
        // estimate only once there is enough progress to extrapolate from
        const left = p > 0.08 && p < 1 ? el * (1 - p) / p : null;
        $('calib-run-time').textContent = `${fmtSecs(el)} elapsed` + (left !== null ? ` · about ${fmtSecs(left)} left` : '');
    }

    // Same rules as calibration.apply_margin, for live feedback while sliding
    function outcomes(cal, m) {
        const band = 1.25;
        return cal.zones.map(z => {
            const tOk = Math.max(...z.good_scores) * m, tDef = tOk * band;
            const subs = z.subzones.map((sz, j) => {
                const sOk = Math.max(...sz.good_scores) * m;
                const mine = z.defects.filter(d => d.subzone === j);
                return { ...sz, t_ok: sOk, t_def: sOk * band, fa: sz.good_scores.filter(x => x > sOk).length,
                    n: mine.length, caught: mine.filter(d => d.sub_scores[j] > sOk).length,
                    dscores: mine.map(d => d.sub_scores[j]) };
            });
            return { ...z, t_ok: tOk, t_def: tDef, fa: z.good_scores.filter(x => x > tOk).length,
                caught: z.defects.filter(d => d.score > tOk).length,
                atDefect: z.defects.filter(d => d.score > tDef).length,
                inBox: z.defects.filter(d => d.in_box).length,
                minMargin: z.defects.length ? Math.min(...z.defects.map(d => d.score)) / tOk : null, subs };
        });
    }

    function dist(goods, defs, tOk, tDef) {
        const top = Math.max(tDef, ...goods, ...defs) * 1.08 || 1;
        const x = v => `${Math.min(99.5, 100 * v / top).toFixed(2)}%`;
        return `<div class="cal-dist">${goods.map(v => `<i class="g" style="left:${x(v)}"></i>`).join('')}
            ${defs.map(v => `<i class="d" style="left:${x(v)}"></i>`).join('')}
            <span class="t ok" style="left:${x(tOk)}"></span><span class="t def" style="left:${x(tDef)}"></span></div>`;
    }

    function renderCalibration() {
        if (!draft) return;
        const c = draft.calibration_state || {};
        $('calib-status').innerHTML = !c.present ? '<span class="chip warn">Not calibrated</span>'
            : !c.fresh ? '<span class="chip warn">Out of date</span>'
            : c.missed ? '<span class="chip bad">Misses defects</span>' : '<span class="chip ok">Up to date</span>';
        $('btn-calibrate').textContent = c.present ? 'Recalibrate' : 'Calibrate';
        const cal = draft.calibration;
        if (!cal || !cal.zones) { $('calib-report').innerHTML = ''; return; }
        const m = calibMargin ?? cal.margin;
        const zs = outcomes(cal, m);
        const totFa = zs.reduce((a, z) => a + z.fa, 0), totGood = zs.reduce((a, z) => a + z.good_scores.length, 0);
        const totDef = zs.reduce((a, z) => a + z.defects.length, 0), totCaught = zs.reduce((a, z) => a + z.caught, 0);
        const critMiss = zs.some(z => z.subs.some(sz => sz.critical && sz.caught < sz.n));
        const ok = totCaught === totDef && !critMiss;
        const rows = zs.map(z => `
            <tr><td class="name">${esc(z.label)}</td><td>${z.good_scores.length}</td><td>${z.defects.length}</td>
                <td>${dist(z.good_scores, z.defects.map(d => d.score), z.t_ok, z.t_def)}</td>
                <td><span class="chip ${z.fa ? 'warn' : 'ok'}">${z.fa} / ${z.good_scores.length}</span></td>
                <td><span class="chip ${z.caught === z.defects.length ? 'ok' : 'bad'}">${z.caught} / ${z.defects.length}</span>
                    ${z.atDefect !== z.caught ? `<span class="st-muted">${z.atDefect} as DEFECT</span>` : ''}</td>
                <td>${z.minMargin ? '×' + z.minMargin.toFixed(2) : '—'}</td>
                <td>${z.defects.length ? `${z.inBox} / ${z.defects.length}` : '—'}</td></tr>
            ${z.subs.map(sz => `<tr class="cal-sub"><td>${esc(sz.label)}${sz.critical ? ' <span class="chip warn">critical</span>' : ''}</td>
                <td>${sz.good_scores.length}</td><td>${sz.n}</td><td>${dist(sz.good_scores, sz.dscores, sz.t_ok, sz.t_def)}</td>
                <td><span class="chip ${sz.fa ? 'warn' : 'ok'}">${sz.fa} / ${sz.good_scores.length}</span></td>
                <td>${sz.n ? `<span class="chip ${sz.caught === sz.n ? 'ok' : (sz.critical ? 'bad' : 'warn')}">${sz.caught} / ${sz.n}</span>` : '—'}</td>
                <td></td><td></td></tr>`).join('')}`).join('');
        const skipped = (cal.skipped || []).map(sk => `<li>${esc(sk.label)}: ${esc(sk.reason)}</li>`).join('');
        const changed = calibMargin !== null && Math.abs(calibMargin - cal.margin) > 1e-6;
        $('calib-report').innerHTML = `
            <div class="st-row" style="margin:8px 0">
                <span class="chip ${totFa ? 'warn' : 'ok'}">False alarms on good samples: ${totFa} / ${totGood}</span>
                <span class="chip ${ok ? 'ok' : 'bad'}">Defects caught: ${totCaught} / ${totDef}</span>
                <span class="st-muted">calibrated ${fmtDate(cal.created_at)}${cal.created_by ? ' · ' + esc(cal.created_by) : ''}</span>
            </div>
            ${skipped ? `<ul class="st-list bad">${skipped}</ul>` : ''}
            <div class="cal-slider">
                <span class="st-muted">Stricter</span>
                <input type="range" id="cal-margin" min="${(draft.margin_range || [0.9, 1.8])[0]}" max="${(draft.margin_range || [0.9, 1.8])[1]}" step="0.01" value="${m}" aria-label="Strictness">
                <span class="st-muted">Looser</span>
                <b>OK limit = worst good × ${m.toFixed(2)}</b>
                <button class="btn btn-sm" id="cal-save" ${changed ? '' : 'disabled'}>Save strictness</button>
            </div>
            ${!ok ? '<p class="st-hint" style="color:var(--danger)">At this strictness some defect samples pass as OK. Move the slider towards Stricter, add good samples so the normal variation is clearer, or check the defect labels.</p>' : ''}
            <div class="cal-legend"><span class="lg">good samples (each vs the others)</span><span class="ld">defect samples</span>
                <span class="lo">OK limit</span><span class="lw">DEFECT limit (WARN between)</span></div>
            <div class="st-table-wrap"><table class="data-table st-table"><thead><tr>
                <th>Zone</th><th>Good</th><th>Defect</th><th>Scores</th><th>False alarms</th><th>Caught</th><th>Margin</th><th>Found in box</th>
            </tr></thead><tbody>${rows}</tbody></table></div>`;
        $('cal-margin').addEventListener('input', e => { calibMargin = parseFloat(e.target.value); renderCalibration(); });
        $('cal-save').addEventListener('click', async () => {
            try {
                const d = await api(`/api/recipes/${rec.id}/draft/calibration`, { method: 'PUT', json: { margin: calibMargin } });
                draft.calibration = d.calibration;
                applyState(d);
                calibMargin = null;
                toast('Strictness saved', 'success');
                renderCalibration(); renderTabs(); renderPublish();
            } catch (err) { toast(err.message, 'error'); }
        });
    }

    // 7 · Publish
    function renderPublish() {
        if (!draft) return;
        const c = draft.check || { blockers: [], warnings: [], ok: false };
        $('publish-check').innerHTML = `
            ${c.ok ? '<p class="chip ok" style="margin-bottom:8px">Ready to publish</p>' : ''}
            ${c.blockers.length ? `<p class="st-muted">Must be fixed before publishing:</p><ul class="st-list bad">${c.blockers.map(b => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
            ${c.warnings.length ? `<p class="st-muted" style="margin-top:6px">Worth checking:</p><ul class="st-list warn">${c.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
            <p class="st-hint" style="margin:10px 0">Publishing makes v${draft.version} live for ${rec.group ? 'group ' + esc(rec.group) : 'all groups'} immediately.
            ${rec.active_version ? `v${rec.active_version} is archived and can be put back from the version list.` : ''}</p>`;
        $('btn-publish').disabled = !c.ok;
        const force = $('btn-publish-force');
        if (force) force.classList.toggle('hidden', c.ok);
        $('publish-confirm').classList.add('hidden');
    }

    async function publish(force) {
        try {
            await api(`/api/recipes/${rec.id}/publish`, { method: 'POST', json: { force: !!force } });
            toast(`v${draft.version} is live`, 'success');
            openRecipe(rec.id);
        } catch (e) {
            if (e.data && e.data.check) { applyState(e.data); renderPublish(); }
            toast(e.message, 'error');
        }
    }
    $('btn-publish').addEventListener('click', e => confirmClick(e.currentTarget, `Confirm: publish v${draft.version}`, () => publish(false)));
    if ($('btn-publish-force')) $('btn-publish-force').addEventListener('click', e =>
        confirmClick(e.currentTarget, 'Confirm: publish with unmet requirements', () => publish(true)));
    $('btn-discard-draft').addEventListener('click', e => confirmClick(e.currentTarget, 'Confirm: discard draft', async () => {
        try {
            await api(`/api/recipes/${rec.id}/draft`, { method: 'DELETE' });
            toast('Draft discarded', 'info');
            openRecipe(rec.id);
        } catch (err) { toast(err.message, 'error'); }
    }));

    // ─── Defect labelling ────────────────────────────────────────────────
    const lb = { sample: null, img: null, boxes: [], drawing: null, scale: 1 };
    const lbCanvas = $('lb-canvas'), lbCtx = lbCanvas.getContext('2d');

    async function openLabel(s) {
        if (!s.aligned) { toast('Align this sample to the current reference first', 'warn'); return; }
        lb.sample = s;
        lb.boxes = (s.defects || []).map(d => ({ box: { ...d.box }, type: d.type || 'other' }));
        $('lb-title').textContent = `· ${s.device || ''} ${s.board ? '· board ' + s.board : ''}`;
        $('label-overlay').classList.remove('hidden');
        // Clear the previous photo until this one has loaded
        lbCanvas.width = 0; lbCanvas.height = 0;
        $('lb-list').innerHTML = '<li class="st-muted">Loading the aligned photo…</li>';
        $('lb-save').disabled = true;
        const img = new Image();
        lb.img = null;
        img.onerror = () => { toast('Could not load the aligned photo', 'error'); closeLabel(); };
        img.onload = () => {
            if (lb.sample !== s) return;   // another sample was opened meanwhile
            lb.img = img;
            $('lb-save').disabled = false;
            const maxW = Math.min($('label-overlay').querySelector('.st-label-dlg').clientWidth - 40, lb.img.naturalWidth);
            lb.scale = maxW / lb.img.naturalWidth;
            lbCanvas.width = Math.round(lb.img.naturalWidth * lb.scale);
            lbCanvas.height = Math.round(lb.img.naturalHeight * lb.scale);
            drawLabel(); renderLabelList();
        };
        img.src = `/api/samples/${s.id}/aligned?t=${Date.now()}`;
    }

    function drawLabel(tmp) {
        if (!lb.img) return;
        const W = lbCanvas.width, H = lbCanvas.height;
        lbCtx.drawImage(lb.img, 0, 0, W, H);
        lbCtx.lineWidth = 1; lbCtx.setLineDash([4, 3]);
        (draft.zones || []).forEach(z => {
            lbCtx.strokeStyle = 'rgba(0,229,255,.7)';
            lbCtx.strokeRect(z.x * W, z.y * H, z.w * W, z.h * H);
            lbCtx.fillStyle = 'rgba(0,229,255,.9)'; lbCtx.font = '11px monospace';
            lbCtx.fillText(z.label, z.x * W + 3, z.y * H + 12);
            (z.subzones || []).forEach(sz => {
                lbCtx.strokeStyle = sz.critical ? 'rgba(255,107,107,.9)' : 'rgba(255,107,107,.5)';
                lbCtx.strokeRect((z.x + sz.x * z.w) * W, (z.y + sz.y * z.h) * H, sz.w * z.w * W, sz.h * z.h * H);
            });
        });
        lbCtx.setLineDash([]);
        lb.boxes.forEach((b, i) => {
            lbCtx.strokeStyle = '#f85149'; lbCtx.lineWidth = 2.5;
            lbCtx.strokeRect(b.box.x * W, b.box.y * H, b.box.w * W, b.box.h * H);
            lbCtx.fillStyle = '#f85149'; lbCtx.font = 'bold 12px monospace';
            lbCtx.fillText(String(i + 1), b.box.x * W + 3, b.box.y * H + 13);
        });
        if (tmp) { lbCtx.strokeStyle = '#fff'; lbCtx.setLineDash([6, 3]); lbCtx.strokeRect(tmp.x * W, tmp.y * H, tmp.w * W, tmp.h * H); lbCtx.setLineDash([]); }
    }

    function renderLabelList() {
        $('lb-list').innerHTML = lb.boxes.length ? lb.boxes.map((b, i) => `
            <li><b>${i + 1}</b>
                <select data-type="${i}">${DEFECT_TYPES.map(([v, t]) => `<option value="${v}"${v === b.type ? ' selected' : ''}>${t}</option>`).join('')}</select>
                <span class="st-muted">${zoneForBox(b.box)}</span>
                <button class="btn btn-sm btn-outline" data-rm="${i}">Remove</button></li>`).join('')
            : '<li class="st-muted">Draw a box around the defect on the photo.</li>';
        $('lb-list').querySelectorAll('[data-type]').forEach(s => s.addEventListener('change', () => { lb.boxes[+s.dataset.type].type = s.value; }));
        $('lb-list').querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', () => { lb.boxes.splice(+b.dataset.rm, 1); drawLabel(); renderLabelList(); }));
    }

    // Same rule as the server (recipes.locate_box), for immediate feedback
    function zoneForBox(box) {
        const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
        let best = -1;
        (draft.zones || []).forEach((z, i) => {
            if (z.x <= cx && cx <= z.x + z.w && z.y <= cy && cy <= z.y + z.h &&
                (best < 0 || z.w * z.h < draft.zones[best].w * draft.zones[best].h)) best = i;
        });
        if (best < 0) return 'outside all zones: move the box into a zone';
        const z = draft.zones[best], rx = (cx - z.x) / z.w, ry = (cy - z.y) / z.h;
        const sz = (z.subzones || []).find(s => s.x <= rx && rx <= s.x + s.w && s.y <= ry && ry <= s.y + s.h);
        return `${esc(z.label)}${sz ? ' / ' + esc(sz.label) + (sz.critical ? ' (critical)' : '') : ''}`;
    }

    const lbPos = e => { const r = lbCanvas.getBoundingClientRect(); return { x: (e.clientX - r.left) / lbCanvas.width, y: (e.clientY - r.top) / lbCanvas.height }; };
    lbCanvas.addEventListener('mousedown', e => { if (lb.img) lb.drawing = lbPos(e); });
    lbCanvas.addEventListener('mousemove', e => {
        if (!lb.drawing) return;
        const p = lbPos(e), a = lb.drawing;
        drawLabel({ x: Math.min(a.x, p.x), y: Math.min(a.y, p.y), w: Math.abs(p.x - a.x), h: Math.abs(p.y - a.y) });
    });
    window.addEventListener('mouseup', e => {
        if (!lb.drawing) return;
        const p = lbPos(e), a = lb.drawing;
        lb.drawing = null;
        const box = { x: Math.max(0, Math.min(a.x, p.x)), y: Math.max(0, Math.min(a.y, p.y)), w: Math.abs(p.x - a.x), h: Math.abs(p.y - a.y) };
        box.w = Math.min(box.w, 1 - box.x); box.h = Math.min(box.h, 1 - box.y);
        if (box.w < 0.005 || box.h < 0.005) { drawLabel(); return; }
        lb.boxes.push({ box, type: 'missing' });
        drawLabel(); renderLabelList();
    });
    function closeLabel() { $('label-overlay').classList.add('hidden'); lb.sample = null; lb.img = null; }
    $('lb-close').addEventListener('click', closeLabel);
    $('lb-cancel').addEventListener('click', closeLabel);
    $('lb-save').addEventListener('click', async () => {
        if (!lb.sample) return;
        try {
            const d = await api(`/api/samples/${lb.sample.id}`, { method: 'PATCH', json: { defects: lb.boxes } });
            applyState(d);
            closeLabel();
            await refreshSamples();
            toast('Labels saved', 'success');
            renderAll();
        } catch (e) { toast(e.message, 'error'); }
    });

    // ─── Start ───────────────────────────────────────────────────────────
    const initial = location.hash.slice(1);
    if (initial) openRecipe(initial); else showList();
})();
