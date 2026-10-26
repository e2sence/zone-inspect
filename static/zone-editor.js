/* ═══ Zone Editor — reusable component (Recipe Studio) ═══
 * Draw zones on a reference image, subzones inside a zone (optionally marked
 * critical), and up to two anchors per zone. Coordinates are normalized:
 * zones 0..1 of the image, subzones 0..1 of their parent zone.
 *
 *   const ed = ZoneEditor.create(rootEl, { onChange: zones => ... });
 *   ed.load(imageSrc, zones);   ed.getZones();
 */
(function () {
    const COLORS = ['#00e5ff', '#ff6b6b', '#ffd93d', '#6bff6b', '#c084fc', '#ff9f43', '#4ecdc4', '#ff78c5'];
    const SUB = '#ff6b6b';

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function create(root, opts = {}) {
        const onChange = opts.onChange || (() => {});
        root.innerHTML = `
            <div class="te-row">
                <div class="te-col-img">
                    <div class="zoom-bar">
                        <button class="zoom-btn" data-zoom="out" title="Zoom out">−</button>
                        <span class="zoom-level" data-role="zoom-level">100%</span>
                        <button class="zoom-btn" data-zoom="in" title="Zoom in">+</button>
                        <button class="zoom-btn" data-zoom="reset" title="Reset zoom">⟳</button>
                        <span style="font-size:10px;color:var(--muted);margin-left:4px">Ctrl+scroll = zoom</span>
                    </div>
                    <div class="te-canvas-wrap"><canvas></canvas></div>
                    <p data-role="draw-hint" style="font-size:11px;color:var(--muted);margin:0">
                        Draw rectangles on the image to add zones.
                    </p>
                    <div data-role="anchor-hint" class="te-hint hidden">
                        ⌖ Click on the image to place anchors (max 2).
                        <a href="#" data-act="anchor-done" style="color:var(--warn);margin-left:4px">Done</a>
                        <a href="#" data-act="anchor-clear" style="color:var(--danger);margin-left:4px">Clear</a>
                    </div>
                    <div data-role="subzone-hint" class="te-hint hidden" style="background:#ff6b6b18;border-color:${SUB};color:${SUB}">
                        ◻ Draw subzones inside the selected zone (max 10). Mark a subzone
                        <b>critical</b> when any deviation there is a defect: each critical
                        subzone needs its own defect sample.
                        <a href="#" data-act="subzone-done" style="color:${SUB};margin-left:4px">Done</a>
                        <a href="#" data-act="subzone-clear" style="color:var(--danger);margin-left:4px">Clear</a>
                        <div data-role="subzone-chips" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px"></div>
                    </div>
                </div>
                <div class="te-col-zones">
                    <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                        <label style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin:0">Zones</label>
                        <span data-role="zone-count" style="font-size:11px;color:var(--accent);font-family:var(--mono)"></span>
                    </div>
                    <ul class="te-zone-list" data-role="zone-list"></ul>
                </div>
            </div>`;

        const $ = sel => root.querySelector(sel);
        const canvas = $('canvas');
        const ctx = canvas.getContext('2d');
        const wrap = $('.te-canvas-wrap');
        let img = null, imgW = 0, imgH = 0, zoom = 1;
        let zones = [];
        let selected = -1, anchorMode = false, subzoneMode = false, hoveredSub = -1;
        let drawing = false, drawStart = null, dragIdx = -1;

        const changed = () => onChange(zones);

        function resize() {
            if (!img) return;
            const maxW = wrap.clientWidth || wrap.parentElement.clientWidth || 800;
            const base = maxW / imgW;
            canvas.width = Math.round(maxW * zoom);
            canvas.height = Math.round(imgH * base * zoom);
            canvas.style.width = canvas.width + 'px';
            canvas.style.height = canvas.height + 'px';
            $('[data-role=zoom-level]').textContent = Math.round(zoom * 100) + '%';
            draw();
        }

        function draw() {
            if (!img) return;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            zones.forEach((z, i) => {
                const color = COLORS[i % COLORS.length];
                const x = z.x * canvas.width, y = z.y * canvas.height;
                const w = z.w * canvas.width, h = z.h * canvas.height;
                ctx.strokeStyle = color;
                ctx.lineWidth = selected === i ? 3 : 2;
                ctx.strokeRect(x, y, w, h);
                ctx.fillStyle = color + '33';
                ctx.fillRect(x, y, w, h);
                if (selected === i) {
                    ctx.setLineDash([4, 3]); ctx.strokeStyle = '#fff'; ctx.lineWidth = 1;
                    ctx.strokeRect(x - 2, y - 2, w + 4, h + 4); ctx.setLineDash([]);
                }
                ctx.fillStyle = color; ctx.font = 'bold 12px sans-serif';
                ctx.fillText(z.label, x + 3, y + 14);
                (z.anchors || []).forEach((a, ai) => {
                    const ax = a.x * canvas.width, ay = a.y * canvas.height, r = 8;
                    ctx.strokeStyle = color; ctx.lineWidth = 2;
                    ctx.beginPath(); ctx.moveTo(ax - r, ay); ctx.lineTo(ax + r, ay); ctx.stroke();
                    ctx.beginPath(); ctx.moveTo(ax, ay - r); ctx.lineTo(ax, ay + r); ctx.stroke();
                    ctx.beginPath(); ctx.arc(ax, ay, r, 0, Math.PI * 2); ctx.stroke();
                    ctx.fillStyle = color; ctx.font = 'bold 10px monospace';
                    ctx.fillText('⌖' + (ai + 1), ax + r + 2, ay - 2);
                });
                (z.subzones || []).forEach((sz, si) => {
                    const sx = (z.x + sz.x * z.w) * canvas.width, sy = (z.y + sz.y * z.h) * canvas.height;
                    const sw = sz.w * z.w * canvas.width, sh = sz.h * z.h * canvas.height;
                    const hov = subzoneMode && selected === i && hoveredSub === si;
                    ctx.setLineDash(hov || sz.critical ? [] : [3, 3]);
                    ctx.strokeStyle = hov ? '#ff4444' : SUB;
                    ctx.lineWidth = hov ? 3 : (sz.critical ? 2.5 : 2);
                    ctx.strokeRect(sx, sy, sw, sh);
                    ctx.fillStyle = hov ? '#ff6b6b44' : '#ff6b6b22';
                    ctx.fillRect(sx, sy, sw, sh);
                    ctx.setLineDash([]);
                    ctx.fillStyle = hov ? '#ff4444' : SUB; ctx.font = 'bold 10px sans-serif';
                    ctx.fillText((sz.label || `S${si + 1}`) + (sz.critical ? ' !' : ''), sx + 2, sy + 10);
                });
            });
        }

        function setMode(kind, idx) {
            const turningOff = (kind === 'anchor' ? anchorMode : subzoneMode) && selected === idx;
            anchorMode = !turningOff && kind === 'anchor';
            subzoneMode = !turningOff && kind === 'subzone';
            selected = turningOff ? -1 : idx;
            hoveredSub = -1;
            $('[data-role=anchor-hint]').classList.toggle('hidden', !anchorMode);
            $('[data-role=subzone-hint]').classList.toggle('hidden', !subzoneMode);
            canvas.style.cursor = (anchorMode || subzoneMode) ? 'crosshair' : '';
            draw(); renderZones(); renderSubChips();
        }

        function renderZones() {
            const el = $('[data-role=zone-list]');
            $('[data-role=zone-count]').textContent = `(${zones.length})`;
            el.innerHTML = zones.map((z, i) => {
                const color = COLORS[i % COLORS.length];
                const anch = (z.anchors || []).length, szs = z.subzones || [];
                const crit = szs.filter(s => s.critical).length;
                const szOn = subzoneMode && selected === i, anOn = anchorMode && selected === i;
                return `<li class="te-zone-item${selected === i ? ' selected' : ''}" draggable="true" data-idx="${i}">
                    <span class="te-z-drag" title="Drag to reorder">⠿</span>
                    <span class="te-z-color" style="background:${color}"></span>
                    <input value="${esc(z.label)}" data-i="${i}" title="Rename zone">
                    <span class="te-z-anch${anOn ? ' active' : ''}" data-anch="${i}" title="Place anchors">⌖${anch}/2</span>
                    <span data-subz="${i}" title="Draw subzones" style="font-size:10px;cursor:pointer;padding:1px 4px;border-radius:3px;${szOn ? `background:#ff6b6b33;color:${SUB}` : 'opacity:.6'}">◻${szs.length}${crit ? ` · ${crit}!` : ''}</span>
                    <span class="te-z-del" data-del="${i}" title="Remove zone">&times;</span>
                </li>`;
            }).join('') || '<li style="font-size:11px;color:var(--muted);list-style:none">No zones yet: draw one on the image.</li>';

            el.querySelectorAll('input[data-i]').forEach(inp => {
                inp.addEventListener('change', () => { zones[+inp.dataset.i].label = inp.value.trim() || `Zone ${+inp.dataset.i + 1}`; draw(); changed(); });
                inp.addEventListener('focus', () => {
                    selected = +inp.dataset.i; draw();
                    el.querySelectorAll('.te-zone-item').forEach((li, k) => li.classList.toggle('selected', k === selected));
                });
            });
            el.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => {
                const i = +b.dataset.del;
                zones.splice(i, 1);
                if (selected === i) { selected = -1; anchorMode = subzoneMode = false; }
                else if (selected > i) selected--;
                draw(); renderZones(); renderSubChips(); changed();
            }));
            el.querySelectorAll('[data-anch]').forEach(b => b.addEventListener('click', () => setMode('anchor', +b.dataset.anch)));
            el.querySelectorAll('[data-subz]').forEach(b => b.addEventListener('click', () => setMode('subzone', +b.dataset.subz)));
            el.querySelectorAll('.te-zone-item').forEach(li => {
                li.addEventListener('dragstart', e => { dragIdx = +li.dataset.idx; li.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; });
                li.addEventListener('dragend', () => { li.classList.remove('dragging'); dragIdx = -1; });
                li.addEventListener('dragover', e => { e.preventDefault(); li.classList.add('drag-over'); });
                li.addEventListener('dragleave', () => li.classList.remove('drag-over'));
                li.addEventListener('drop', e => {
                    e.preventDefault(); li.classList.remove('drag-over');
                    const to = +li.dataset.idx;
                    if (dragIdx < 0 || dragIdx === to) return;
                    const [m] = zones.splice(dragIdx, 1);
                    zones.splice(to, 0, m);
                    selected = to; draw(); renderZones(); changed();
                });
            });
        }

        function renderSubChips() {
            const el = $('[data-role=subzone-chips]');
            if (!subzoneMode || !zones[selected]) { el.innerHTML = ''; return; }
            const szs = zones[selected].subzones || [];
            if (!szs.length) { el.innerHTML = '<span style="opacity:.7;font-size:11px">No subzones yet: draw inside the zone.</span>'; return; }
            el.innerHTML = szs.map((sz, i) => `
                <span data-chip="${i}" style="display:inline-flex;align-items:center;gap:4px;background:#ff6b6b22;border:1px solid #ff6b6b55;border-radius:4px;padding:2px 6px">
                    <input value="${esc(sz.label)}" data-label="${i}" style="width:40px;background:none;border:none;color:${SUB};font-size:11px;padding:0;font-weight:bold">
                    <label style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer" title="Any deviation here is a defect">
                        <input type="checkbox" data-crit="${i}" ${sz.critical ? 'checked' : ''}>critical</label>
                    <input value="${sz.sensitivity != null ? sz.sensitivity : ''}" data-sens="${i}" placeholder="sens" title="Strictness for this subzone (0..2). Empty = recipe default" style="width:40px;background:#ff6b6b11;border:1px dashed #ff6b6b55;border-radius:3px;color:${SUB};font-size:11px;padding:1px 3px;text-align:center">
                    <span data-rm="${i}" style="cursor:pointer;opacity:.7;font-size:13px" title="Delete">&times;</span>
                </span>`).join('');
            const sz = i => zones[selected].subzones[i];
            el.querySelectorAll('[data-label]').forEach(inp => inp.addEventListener('change', () => { sz(+inp.dataset.label).label = inp.value.trim() || `S${+inp.dataset.label + 1}`; draw(); changed(); }));
            el.querySelectorAll('[data-crit]').forEach(cb => cb.addEventListener('change', () => { sz(+cb.dataset.crit).critical = cb.checked; draw(); renderZones(); changed(); }));
            el.querySelectorAll('[data-sens]').forEach(inp => inp.addEventListener('change', () => {
                const v = inp.value.trim();
                sz(+inp.dataset.sens).sensitivity = v === '' ? null : Math.max(0, Math.min(2, parseFloat(v) || 0));
                inp.value = sz(+inp.dataset.sens).sensitivity ?? '';
                changed();
            }));
            el.querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', () => {
                zones[selected].subzones.splice(+b.dataset.rm, 1);
                zones[selected].subzones.forEach((s, j) => { if (/^S\d+$/.test(s.label)) s.label = `S${j + 1}`; });
                hoveredSub = -1; draw(); renderZones(); renderSubChips(); changed();
            }));
            el.querySelectorAll('[data-chip]').forEach(c => {
                c.addEventListener('mouseenter', () => { hoveredSub = +c.dataset.chip; draw(); });
                c.addEventListener('mouseleave', () => { hoveredSub = -1; draw(); });
            });
        }

        const pos = e => {
            const r = canvas.getBoundingClientRect();
            return { x: (e.clientX - r.left) / canvas.width, y: (e.clientY - r.top) / canvas.height };
        };

        canvas.addEventListener('mousedown', e => {
            if (!img) return;
            if (anchorMode && zones[selected]) {
                const p = pos(e);
                const a = zones[selected].anchors = zones[selected].anchors || [];
                if (a.length >= 2) a.shift();
                a.push(p);
                draw(); renderZones(); changed();
                return;
            }
            drawStart = pos(e); drawing = true;
        });
        canvas.addEventListener('mousemove', e => {
            if (!drawing) return;
            const cur = pos(e);
            draw();
            ctx.strokeStyle = subzoneMode ? SUB : '#fff'; ctx.lineWidth = 1.5; ctx.setLineDash([6, 3]);
            ctx.strokeRect(drawStart.x * canvas.width, drawStart.y * canvas.height,
                (cur.x - drawStart.x) * canvas.width, (cur.y - drawStart.y) * canvas.height);
            ctx.setLineDash([]);
        });
        window.addEventListener('mouseup', e => {
            if (!drawing) return;
            drawing = false;
            const end = pos(e);
            let x = Math.min(drawStart.x, end.x), y = Math.min(drawStart.y, end.y);
            let w = Math.abs(end.x - drawStart.x), h = Math.abs(end.y - drawStart.y);
            if (w < 0.02 || h < 0.02) { draw(); return; }
            if (subzoneMode && zones[selected]) {
                const pz = zones[selected];
                pz.subzones = pz.subzones || [];
                if (pz.subzones.length >= 10) { window.toast && toast('Max 10 subzones per zone', 'warn'); draw(); return; }
                const x1 = Math.max(x, pz.x), y1 = Math.max(y, pz.y);
                const x2 = Math.min(x + w, pz.x + pz.w), y2 = Math.min(y + h, pz.y + pz.h);
                if (x2 - x1 < 0.01 || y2 - y1 < 0.01) { window.toast && toast('Subzone must be inside the zone', 'warn'); draw(); return; }
                pz.subzones.push({ x: (x1 - pz.x) / pz.w, y: (y1 - pz.y) / pz.h, w: (x2 - x1) / pz.w, h: (y2 - y1) / pz.h,
                    label: `S${pz.subzones.length + 1}`, critical: false, sensitivity: null });
                draw(); renderZones(); renderSubChips(); changed();
                return;
            }
            x = Math.max(0, x); y = Math.max(0, y);
            w = Math.min(w, 1 - x); h = Math.min(h, 1 - y);
            zones.push({ x, y, w, h, label: `Zone ${zones.length + 1}`, anchors: [], subzones: [] });
            selected = zones.length - 1;
            draw(); renderZones(); changed();
        });

        root.querySelectorAll('[data-act]').forEach(a => a.addEventListener('click', e => {
            e.preventDefault();
            const act = a.dataset.act;
            if (act === 'anchor-done' || act === 'subzone-done') { setMode(act.startsWith('anchor') ? 'anchor' : 'subzone', selected); return; }
            if (!zones[selected]) return;
            if (act === 'anchor-clear') zones[selected].anchors = [];
            if (act === 'subzone-clear') { zones[selected].subzones = []; hoveredSub = -1; }
            draw(); renderZones(); renderSubChips(); changed();
        }));

        wrap.addEventListener('wheel', e => {
            if (!img || (!e.ctrlKey && !e.metaKey)) return;
            e.preventDefault();
            const old = zoom;
            zoom = Math.round(Math.max(1, Math.min(8, zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15))) * 100) / 100;
            if (zoom === old) return;
            const r = wrap.getBoundingClientRect();
            const vx = e.clientX - r.left, vy = e.clientY - r.top;
            const nx = (vx + wrap.scrollLeft) / canvas.width, ny = (vy + wrap.scrollTop) / canvas.height;
            resize();
            wrap.scrollLeft = nx * canvas.width - vx;
            wrap.scrollTop = ny * canvas.height - vy;
        }, { passive: false });
        root.querySelectorAll('[data-zoom]').forEach(b => b.addEventListener('click', () => {
            if (!img) return;
            const z = b.dataset.zoom;
            zoom = z === 'in' ? Math.min(8, zoom * 1.3) : z === 'out' ? Math.max(1, zoom / 1.3) : 1;
            if (z === 'reset') { wrap.scrollLeft = 0; wrap.scrollTop = 0; }
            resize();
        }));
        window.addEventListener('resize', () => resize());

        return {
            load(src, initialZones) {
                zones = JSON.parse(JSON.stringify(initialZones || []));
                selected = -1; anchorMode = subzoneMode = false; hoveredSub = -1; zoom = 1;
                renderZones();
                img = new Image();
                img.onload = () => { imgW = img.naturalWidth; imgH = img.naturalHeight; resize(); };
                img.src = src;
            },
            getZones: () => JSON.parse(JSON.stringify(zones)),
            redraw: () => resize(),
        };
    }

    window.ZoneEditor = { create };
})();
