        /* ═══ Template Editor Logic ═══ */
        (function () {
            const COLORS = ['#00e5ff', '#ff6b6b', '#ffd93d', '#6bff6b', '#c084fc', '#ff9f43', '#4ecdc4', '#ff78c5'];
            let teId = null;       // template id being edited
            let teZones = [];      // working copy of zones
            let teImg = null;      // Image object
            let teImgW = 0, teImgH = 0;
            let teVersions = [];
            let teDrawing = false, teDrawStart = null;
            let teSelectedZone = -1;
            let teAnchorMode = false;
            let teSubzoneMode = false;
            let teHoveredSubzone = -1;
            let teDragIdx = -1;
            let teZoom = 1;

            const overlay = document.getElementById('tpl-editor-overlay');
            const canvas = document.getElementById('te-canvas');
            const ctx = canvas.getContext('2d');

            // ─── Open editor ───
            window.openTemplateEditor = async function (tid) {
                try {
                    const r = await fetch(`/api/templates/${tid}/detail`);
                    const d = await r.json();
                    if (d.error) { toast(d.error, 'error'); return; }

                    teId = tid;
                    teZones = JSON.parse(JSON.stringify(d.zones)); // deep copy
                    teVersions = d.versions || [];
                    teSelectedZone = -1;
                    teAnchorMode = false;
                    teSubzoneMode = false;
                    teHoveredSubzone = -1;
                    teZoom = 1;

                    document.getElementById('te-name').value = d.name;
                    document.getElementById('te-mask').value = d.barcode_mask || '';
                    document.getElementById('te-title').textContent = `Edit Template — v${d.version}`;

                    teImg = new Image();
                    teImg.onload = () => {
                        teImgW = teImg.naturalWidth;
                        teImgH = teImg.naturalHeight;
                        teResizeCanvas();
                        teRenderZones();
                        teRenderVersions();
                    };
                    teImg.src = 'data:image/jpeg;base64,' + d.image_b64;

                    overlay.classList.remove('hidden');
                } catch (e) { toast(e.message, 'error'); }
            };

            function teClose() {
                overlay.classList.add('hidden');
                teId = null;
                teZones = [];
                teAnchorMode = false;
                teSubzoneMode = false;
                teHoveredSubzone = -1;
                teZoom = 1;
            }

            // ─── Resize canvas with zoom ───
            function teResizeCanvas() {
                const wrap = document.querySelector('.te-canvas-wrap');
                const maxW = wrap.clientWidth || wrap.parentElement.clientWidth;
                const baseScale = maxW / teImgW;
                const w = Math.round(maxW * teZoom);
                const h = Math.round(teImgH * baseScale * teZoom);
                canvas.width = w;
                canvas.height = h;
                canvas.style.width = w + 'px';
                canvas.style.height = h + 'px';
                teDrawCanvas();
                const zl = document.getElementById('te-zoom-level');
                if (zl) zl.textContent = Math.round(teZoom * 100) + '%';
            }

            // ─── Draw canvas ───
            function teDrawCanvas() {
                if (!teImg) return;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.drawImage(teImg, 0, 0, canvas.width, canvas.height);

                teZones.forEach((z, i) => {
                    const color = COLORS[i % COLORS.length];
                    const x = z.x * canvas.width, y = z.y * canvas.height;
                    const w = z.w * canvas.width, h = z.h * canvas.height;
                    ctx.strokeStyle = color;
                    ctx.lineWidth = (teSelectedZone === i) ? 3 : 2;
                    ctx.strokeRect(x, y, w, h);
                    ctx.fillStyle = color + '33';
                    ctx.fillRect(x, y, w, h);
                    if (teSelectedZone === i) {
                        ctx.setLineDash([4, 3]);
                        ctx.strokeStyle = '#fff';
                        ctx.lineWidth = 1;
                        ctx.strokeRect(x - 2, y - 2, w + 4, h + 4);
                        ctx.setLineDash([]);
                    }
                    ctx.fillStyle = color;
                    ctx.font = 'bold 12px sans-serif';
                    ctx.fillText(z.label, x + 3, y + 14);

                    // Anchors
                    (z.anchors || []).forEach((a, ai) => {
                        const ax = a.x * canvas.width, ay = a.y * canvas.height;
                        const r = 8;
                        ctx.strokeStyle = color; ctx.lineWidth = 2;
                        ctx.beginPath(); ctx.moveTo(ax - r, ay); ctx.lineTo(ax + r, ay); ctx.stroke();
                        ctx.beginPath(); ctx.moveTo(ax, ay - r); ctx.lineTo(ax, ay + r); ctx.stroke();
                        ctx.beginPath(); ctx.arc(ax, ay, r, 0, Math.PI * 2); ctx.stroke();
                        ctx.fillStyle = color; ctx.font = 'bold 10px var(--mono)';
                        ctx.fillText('\u2316' + (ai + 1), ax + r + 2, ay - 2);
                    });
                    // Subzones
                    (z.subzones || []).forEach((sz, si) => {
                        const sx = (z.x + sz.x * z.w) * canvas.width;
                        const sy = (z.y + sz.y * z.h) * canvas.height;
                        const sw = sz.w * z.w * canvas.width;
                        const sh = sz.h * z.h * canvas.height;
                        const isHov = (teSubzoneMode && teSelectedZone === i && teHoveredSubzone === si);
                        ctx.setLineDash(isHov ? [] : [3, 3]);
                        ctx.strokeStyle = isHov ? '#ff4444' : '#ff6b6b';
                        ctx.lineWidth = isHov ? 3 : 2;
                        ctx.strokeRect(sx, sy, sw, sh);
                        ctx.fillStyle = isHov ? '#ff6b6b44' : '#ff6b6b22';
                        ctx.fillRect(sx, sy, sw, sh);
                        ctx.setLineDash([]);
                        ctx.fillStyle = isHov ? '#ff4444' : '#ff6b6b';
                        ctx.font = 'bold 10px sans-serif';
                        ctx.fillText(sz.label || `S${si + 1}`, sx + 2, sy + 10);
                    });
                });
            }

            // ─── Zone list ───
            function teRenderZones() {
                const el = document.getElementById('te-zone-list');
                document.getElementById('te-zone-count').textContent = `(${teZones.length})`;
                el.innerHTML = '';
                teZones.forEach((z, i) => {
                    const color = COLORS[i % COLORS.length];
                    const anchCount = (z.anchors || []).length;
                    const anchActive = (teAnchorMode && teSelectedZone === i);
                    const li = document.createElement('li');
                    li.className = 'te-zone-item' + (teSelectedZone === i ? ' selected' : '');
                    li.draggable = true;
                    li.dataset.idx = i;
                    const szCount = (z.subzones || []).length;
                    const szActive = (teSubzoneMode && teSelectedZone === i);
                    li.innerHTML = `
                    <span class="te-z-drag" title="Drag to reorder">⠿</span>
                    <span class="te-z-color" style="background:${color}"></span>
                    <input value="${z.label}" data-i="${i}" title="Rename zone">
                    <span class="te-z-anch${anchActive ? ' active' : ''}" data-i="${i}" title="Place anchors">\u2316${anchCount}/2</span>
                    <span style="font-size:10px;cursor:pointer;padding:1px 4px;border-radius:3px;${szActive ? 'background:#ff6b6b33;color:#ff6b6b' : 'opacity:.5'}" data-te-subz="${i}" title="Draw subzones (strict)">◻${szCount}/10</span>
                    <span class="te-z-del" data-i="${i}" title="Remove zone">&times;</span>`;
                    el.appendChild(li);
                });

                // Rename
                el.querySelectorAll('input').forEach(inp => {
                    inp.addEventListener('change', () => {
                        teZones[+inp.dataset.i].label = inp.value;
                        teDrawCanvas();
                    });
                    inp.addEventListener('focus', () => {
                        teSelectedZone = +inp.dataset.i;
                        teDrawCanvas();
                        // Highlight selected item without full re-render (preserves focus)
                        el.querySelectorAll('.te-zone-item').forEach((li, idx) => {
                            li.classList.toggle('selected', idx === teSelectedZone);
                        });
                    });
                });
                // Delete
                el.querySelectorAll('.te-z-del').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const ri = +btn.dataset.i;
                        teZones.splice(ri, 1);
                        if (teSelectedZone === ri) teSelectedZone = -1;
                        else if (teSelectedZone > ri) teSelectedZone--;
                        teDrawCanvas();
                        teRenderZones();
                    });
                });
                // Anchor mode toggle
                el.querySelectorAll('.te-z-anch').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const idx = +btn.dataset.i;
                        teSubzoneMode = false; teHoveredSubzone = -1;
                        document.getElementById('te-subzone-hint').classList.add('hidden');
                        if (teAnchorMode && teSelectedZone === idx) {
                            teAnchorMode = false; teSelectedZone = -1;
                        } else {
                            teAnchorMode = true; teSelectedZone = idx;
                        }
                        document.getElementById('te-anchor-hint').classList.toggle('hidden', !teAnchorMode);
                        canvas.style.cursor = teAnchorMode ? 'crosshair' : '';
                        teDrawCanvas();
                        teRenderZones();
                    });
                });
                // Subzone mode toggle
                el.querySelectorAll('[data-te-subz]').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const idx = +btn.dataset.teSubz;
                        teAnchorMode = false; teHoveredSubzone = -1;
                        document.getElementById('te-anchor-hint').classList.add('hidden');
                        if (teSubzoneMode && teSelectedZone === idx) {
                            teSubzoneMode = false; teSelectedZone = -1;
                        } else {
                            teSubzoneMode = true; teSelectedZone = idx;
                        }
                        document.getElementById('te-subzone-hint').classList.toggle('hidden', !teSubzoneMode);
                        canvas.style.cursor = teSubzoneMode ? 'crosshair' : '';
                        teDrawCanvas();
                        teRenderZones();
                        teRenderSubzoneChips();
                    });
                });

                // Drag & drop reorder
                el.querySelectorAll('.te-zone-item').forEach(li => {
                    li.addEventListener('dragstart', e => {
                        teDragIdx = +li.dataset.idx;
                        li.classList.add('dragging');
                        e.dataTransfer.effectAllowed = 'move';
                    });
                    li.addEventListener('dragend', () => {
                        li.classList.remove('dragging');
                        teDragIdx = -1;
                        el.querySelectorAll('.te-zone-item').forEach(x => x.classList.remove('drag-over'));
                    });
                    li.addEventListener('dragover', e => {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = 'move';
                        li.classList.add('drag-over');
                    });
                    li.addEventListener('dragleave', () => li.classList.remove('drag-over'));
                    li.addEventListener('drop', e => {
                        e.preventDefault();
                        li.classList.remove('drag-over');
                        const toIdx = +li.dataset.idx;
                        if (teDragIdx < 0 || teDragIdx === toIdx) return;
                        const [moved] = teZones.splice(teDragIdx, 1);
                        teZones.splice(toIdx, 0, moved);
                        teSelectedZone = toIdx;
                        teDrawCanvas();
                        teRenderZones();
                    });
                });
            }

            // ─── Version list ───
            function teRenderVersions() {
                const el = document.getElementById('te-ver-list');
                el.innerHTML = '';
                teVersions.forEach(v => {
                    const item = document.createElement('div');
                    item.className = 'te-ver-item' + (v.current ? ' current' : '');
                    const dateStr = v.date ? v.date.slice(0, 16).replace('T', ' ') : '';
                    const extra = v.name ? ` — ${v.name}` : '';
                    const zc = v.zone_count != null ? `, ${v.zone_count} zones` : '';
                    item.innerHTML = `<span class="te-v-badge">v${v.version}</span>
                    <span>${dateStr}${extra}${zc}</span>
                    ${v.current ? '<span style="color:var(--accent);font-size:10px">current</span>' : ''}`;
                    if (!v.current) {
                        item.style.cursor = 'pointer';
                        item.addEventListener('click', () => teRestoreVersion(v.version));
                    }
                    el.appendChild(item);
                });
            }

            async function teRestoreVersion(ver) {
                if (!confirm(`Restore to v${ver}? This will create a new version.`)) return;
                try {
                    const r = await fetch(`/api/templates/${teId}/restore/${ver}`, { method: 'POST' });
                    const d = await r.json();
                    if (d.error) { toast(d.error, 'error'); return; }
                    toast(`Restored → v${d.version}`, 'success');
                    teClose();
                    loadTemplateList();
                } catch (e) { toast(e.message, 'error'); }
            }

            // ─── Subzone chip list in editor ───
            function teRenderSubzoneChips() {
                const el = document.getElementById('te-subzone-chips');
                if (!el) return;
                if (!teSubzoneMode || teSelectedZone < 0 || !teZones[teSelectedZone]) { el.innerHTML = ''; return; }
                const szs = teZones[teSelectedZone].subzones || [];
                if (!szs.length) { el.innerHTML = '<span style="opacity:.6;font-size:11px">No subzones yet — draw on canvas</span>'; return; }
                el.innerHTML = szs.map((sz, i) => `
                    <span style="display:inline-flex;align-items:center;gap:3px;background:#ff6b6b22;border:1px solid #ff6b6b55;border-radius:4px;padding:2px 6px" data-te-sz-chip="${i}">
                        <input value="${sz.label}" data-te-sz-label="${i}" style="width:36px;background:none;border:none;color:#ff6b6b;font-size:11px;padding:0;font-weight:bold;cursor:text">
                        <input value="${sz.sensitivity != null ? sz.sensitivity : ''}" data-te-sz-sens="${i}" placeholder="sns" title="Per-subzone sensitivity (0..2). Empty = use global" style="width:42px;background:#ff6b6b11;border:1px dashed #ff6b6b55;border-radius:3px;color:#ff6b6b;font-size:12px;padding:1px 3px;text-align:center;cursor:text">
                        <span data-te-sz-rm="${i}" style="cursor:pointer;opacity:.7;font-size:13px" title="Delete">&times;</span>
                    </span>`).join('');
                el.querySelectorAll('[data-te-sz-label]').forEach(inp => {
                    inp.addEventListener('change', () => {
                        const si = +inp.dataset.teSzLabel;
                        if (teZones[teSelectedZone] && teZones[teSelectedZone].subzones[si]) {
                            teZones[teSelectedZone].subzones[si].label = inp.value;
                            teDrawCanvas();
                        }
                    });
                });
                el.querySelectorAll('[data-te-sz-sens]').forEach(inp => {
                    inp.addEventListener('change', () => {
                        const si = +inp.dataset.teSzSens;
                        if (teZones[teSelectedZone] && teZones[teSelectedZone].subzones[si]) {
                            const v = inp.value.trim();
                            teZones[teSelectedZone].subzones[si].sensitivity = v === '' ? null : Math.max(0, Math.min(2, parseFloat(v) || 0));
                            inp.value = teZones[teSelectedZone].subzones[si].sensitivity != null ? teZones[teSelectedZone].subzones[si].sensitivity : '';
                        }
                    });
                });
                el.querySelectorAll('[data-te-sz-rm]').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const si = +btn.dataset.teSzRm;
                        if (teZones[teSelectedZone] && teZones[teSelectedZone].subzones) {
                            teZones[teSelectedZone].subzones.splice(si, 1);
                            teZones[teSelectedZone].subzones.forEach((s, j) => { if (s.label.match(/^S\d+$/)) s.label = `S${j+1}`; });
                            teHoveredSubzone = -1;
                            teDrawCanvas(); teRenderZones(); teRenderSubzoneChips();
                        }
                    });
                });
                el.querySelectorAll('[data-te-sz-chip]').forEach(chip => {
                    chip.addEventListener('mouseenter', () => { teHoveredSubzone = +chip.dataset.teSzChip; teDrawCanvas(); });
                    chip.addEventListener('mouseleave', () => { teHoveredSubzone = -1; teDrawCanvas(); });
                });
            }

            // ─── Canvas: draw new zones / subzones ───
            canvas.addEventListener('mousedown', e => {
                if (teAnchorMode && teSelectedZone >= 0 && teSelectedZone < teZones.length) {
                    const rect = canvas.getBoundingClientRect();
                    const ax = (e.clientX - rect.left) / canvas.width;
                    const ay = (e.clientY - rect.top) / canvas.height;
                    if (!teZones[teSelectedZone].anchors) teZones[teSelectedZone].anchors = [];
                    if (teZones[teSelectedZone].anchors.length >= 2) teZones[teSelectedZone].anchors.shift();
                    teZones[teSelectedZone].anchors.push({ x: ax, y: ay });
                    teDrawCanvas();
                    teRenderZones();
                    return;
                }
                if (teSubzoneMode && teSelectedZone >= 0 && teSelectedZone < teZones.length) {
                    const rect = canvas.getBoundingClientRect();
                    teDrawStart = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
                    teDrawing = true;
                    return;
                }
                const rect = canvas.getBoundingClientRect();
                teDrawStart = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
                teDrawing = true;
            });
            canvas.addEventListener('mousemove', e => {
                if (!teDrawing) return;
                const rect = canvas.getBoundingClientRect();
                const cur = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
                teDrawCanvas();
                const x = teDrawStart.x * canvas.width, y = teDrawStart.y * canvas.height;
                const w = (cur.x - teDrawStart.x) * canvas.width, h = (cur.y - teDrawStart.y) * canvas.height;
                ctx.strokeStyle = teSubzoneMode ? '#ff6b6b' : '#fff'; ctx.lineWidth = 1.5;
                ctx.setLineDash([6, 3]);
                ctx.strokeRect(x, y, w, h);
                ctx.setLineDash([]);
            });
            canvas.addEventListener('mouseup', e => {
                if (!teDrawing) return;
                teDrawing = false;
                const rect = canvas.getBoundingClientRect();
                const end = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
                let x = Math.min(teDrawStart.x, end.x), y = Math.min(teDrawStart.y, end.y);
                let w = Math.abs(end.x - teDrawStart.x), h = Math.abs(end.y - teDrawStart.y);
                if (w < 0.02 || h < 0.02) { teDrawCanvas(); return; }

                // Subzone mode — create subzone clipped to parent
                if (teSubzoneMode && teSelectedZone >= 0 && teSelectedZone < teZones.length) {
                    const pz = teZones[teSelectedZone];
                    if (!pz.subzones) pz.subzones = [];
                    if (pz.subzones.length >= 10) { toast('Max 10 subzones per zone', 'warn'); teDrawCanvas(); return; }
                    const cx1 = Math.max(x, pz.x), cy1 = Math.max(y, pz.y);
                    const cx2 = Math.min(x + w, pz.x + pz.w), cy2 = Math.min(y + h, pz.y + pz.h);
                    const cw = cx2 - cx1, ch = cy2 - cy1;
                    if (cw < 0.01 || ch < 0.01) { toast('Subzone outside zone', 'warn'); teDrawCanvas(); return; }
                    const szx = (cx1 - pz.x) / pz.w, szy = (cy1 - pz.y) / pz.h;
                    const szw = cw / pz.w, szh = ch / pz.h;
                    pz.subzones.push({ x: szx, y: szy, w: szw, h: szh, label: `S${pz.subzones.length + 1}`, sensitivity: null });
                    teDrawCanvas(); teRenderZones(); teRenderSubzoneChips();
                    return;
                }

                x = Math.max(0, x); y = Math.max(0, y);
                w = Math.min(w, 1 - x); h = Math.min(h, 1 - y);
                teZones.push({ x, y, w, h, label: `Zone ${teZones.length + 1}`, anchors: [], subzones: [] });
                teSelectedZone = teZones.length - 1;
                teDrawCanvas();
                teRenderZones();
            });

            // ─── Anchor hint buttons ───
            document.getElementById('te-anchor-done').addEventListener('click', e => {
                e.preventDefault();
                teAnchorMode = false; teSelectedZone = -1;
                document.getElementById('te-anchor-hint').classList.add('hidden');
                canvas.style.cursor = '';
                teDrawCanvas(); teRenderZones();
            });
            document.getElementById('te-anchor-clear').addEventListener('click', e => {
                e.preventDefault();
                if (teSelectedZone >= 0 && teZones[teSelectedZone]) {
                    teZones[teSelectedZone].anchors = [];
                    teDrawCanvas(); teRenderZones();
                }
            });

            // ─── Subzone hint buttons ───
            document.getElementById('te-subzone-done').addEventListener('click', e => {
                e.preventDefault();
                teSubzoneMode = false; teSelectedZone = -1; teHoveredSubzone = -1;
                document.getElementById('te-subzone-hint').classList.add('hidden');
                canvas.style.cursor = '';
                teDrawCanvas(); teRenderZones();
            });
            document.getElementById('te-subzone-clear').addEventListener('click', e => {
                e.preventDefault();
                if (teSelectedZone >= 0 && teZones[teSelectedZone]) {
                    teZones[teSelectedZone].subzones = [];
                    teHoveredSubzone = -1;
                    teDrawCanvas(); teRenderZones(); teRenderSubzoneChips();
                }
            });

            // ─── Save ───
            document.getElementById('te-save').addEventListener('click', async () => {
                const name = document.getElementById('te-name').value.trim();
                const mask = document.getElementById('te-mask').value.trim();
                if (!name) { toast('Enter template name', 'error'); return; }
                if (!teZones.length) { toast('At least one zone required', 'error'); return; }
                try {
                    const r = await fetch(`/api/templates/${teId}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name, barcode_mask: mask, zones: teZones }),
                    });
                    const d = await r.json();
                    if (d.error) { toast(d.error, 'error'); return; }
                    toast(`Template saved → v${d.version}`, 'success');
                    teClose();
                    loadTemplateList();
                } catch (e) { toast(e.message, 'error'); }
            });

            // ─── Close / cancel ───
            document.getElementById('te-close').addEventListener('click', teClose);
            document.getElementById('te-cancel').addEventListener('click', teClose);
            overlay.addEventListener('click', e => { if (e.target === overlay) teClose(); });

            // ─── Canvas Zoom ───
            const teWrap = document.querySelector('.te-canvas-wrap');
            teWrap.addEventListener('wheel', e => {
                if (!teImg) return;
                if (!e.ctrlKey && !e.metaKey) return;
                e.preventDefault();
                const oldZoom = teZoom;
                const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
                teZoom = Math.max(1, Math.min(8, teZoom * factor));
                teZoom = Math.round(teZoom * 100) / 100;
                if (teZoom === oldZoom) return;
                const wrapRect = teWrap.getBoundingClientRect();
                const vx = e.clientX - wrapRect.left;
                const vy = e.clientY - wrapRect.top;
                const nx = (vx + teWrap.scrollLeft) / canvas.width;
                const ny = (vy + teWrap.scrollTop) / canvas.height;
                teResizeCanvas();
                teWrap.scrollLeft = nx * canvas.width - vx;
                teWrap.scrollTop = ny * canvas.height - vy;
            }, { passive: false });
            document.getElementById('te-zoom-in').addEventListener('click', () => {
                if (!teImg) return;
                teZoom = Math.min(8, teZoom * 1.3);
                teResizeCanvas();
            });
            document.getElementById('te-zoom-out').addEventListener('click', () => {
                if (!teImg) return;
                teZoom = Math.max(1, teZoom / 1.3);
                teResizeCanvas();
            });
            document.getElementById('te-zoom-reset').addEventListener('click', () => {
                if (!teImg) return;
                teZoom = 1;
                teWrap.scrollLeft = 0; teWrap.scrollTop = 0;
                teResizeCanvas();
            });
        })();
