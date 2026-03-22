        /* ─── Auth guard: redirect to /login on 401 ─── */
        const _origFetch = window.fetch;
        window.fetch = function (...args) {
            return _origFetch.apply(this, args).then(resp => {
                if (resp.status === 401) { window.location.href = '/login'; }
                return resp;
            });
        };

        /* ─── State ─── */
        let sessionId = null;
        let refImg = null;       // Image object
        let zones = [];          // {x,y,w,h,label} normalized 0..1
        let currentTemplateName = null;
        let currentTemplateId = null;
        let currentBarcodeMask = null;
        let zonePreviews = [];   // base64
        let checkedZones = new Set();
        let zoneDefectStatus = {};  // {zoneIndex: 'ok'|'warn'|'defect'}
        let zoneResults = {};       // {zoneIndex: {ssim, defect_pct, defect_count, score, status}}
        let userDecisions = {};     // {zoneIndex: 'ok'|'warn'|'defect'} user overrides
        let userSubDecisions = {};  // {zoneIndex: {subIndex: 'ok'|'warn'|'defect'}} user overrides for subzones
        let resultSaved = false;        // true after onAllZonesComplete saves — prevents Close & Save duplicate
        let lastResultId = null;        // result_id from last save — for updating existing record
        let autoAccept = true;           // auto-advance when all zones OK
        let currentViewZone = null;      // zone index currently shown in match-result
        let drawing = false;
        let drawStart = null;
        let anchorMode = false;  // true = clicking places anchors instead of drawing zones
        let selectedZone = -1;   // which zone is selected for anchor placement
        let subzoneMode = false; // true = drawing subzones inside selected zone
        let hoveredSubzone = -1;  // index of hovered subzone chip (for canvas highlight)
        let canvasZoom = 1;       // zoom level for Step 2 canvas

        // Chart instances
        let chartScores = null;
        let chartDefects = null;
        let chartSsim = null;

        const COLORS = ['#f59e0b', '#6366f1', '#22c55e', '#ef4444', '#ec4899', '#14b8a6', '#f97316', '#8b5cf6', '#06b6d4', '#84cc16'];

        /* ─── Utils ─── */
        function toast(msg, type = 'info') {
            const t = document.createElement('div');
            t.className = 'toast ' + type;
            t.textContent = msg;
            document.body.appendChild(t);
            setTimeout(() => t.remove(), 3000);
        }

        function goStep(n) {
            [1, 2, 3].forEach(i => {
                document.getElementById('panel-' + i).classList.toggle('hidden', i !== n);
                const step = document.getElementById('step-' + i);
                step.classList.toggle('active', i === n);
                step.classList.toggle('done', i < n);
                if (i < 3) document.getElementById('line-' + i).classList.toggle('done', i < n);
            });
        }

        /* ─── Upload step click → full reset to step 1 ─── */
        document.getElementById('step-1').addEventListener('click', () => {
            if (!sessionId) return;  // already on step 1
            // if (!confirm('Вернуться к загрузке? Текущая сессия будет сброшена.')) return;
            if (!confirm('Go back to upload? Current session will be reset.')) return;
            fetch(`/api/session/${sessionId}/reset`, { method: 'POST' }).catch(() => {});
            sessionId = null;
            refImg = null;
            zones = [];
            currentTemplateName = null;
            currentTemplateId = null;
            currentBarcodeMask = null;
            zonePreviews = [];
            checkedZones = new Set();
            zoneDefectStatus = {};
            zoneResults = {};
            userDecisions = {};
            userSubDecisions = {};
            resultSaved = false;
            lastResultId = null;
            currentSerial = null;
            goStep(1);
            toast('Session reset', 'info');
        });

        /* ─── Dropzone helper ─── */
        function setupDZ(dzId, inputId, onFile) {
            const dz = document.getElementById(dzId);
            const inp = document.getElementById(inputId);
            dz.addEventListener('click', () => inp.click());
            dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
            dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
            dz.addEventListener('drop', e => {
                e.preventDefault(); dz.classList.remove('dragover');
                if (e.dataTransfer.files.length) {
                    // For multiple file input, use DataTransfer to set files
                    const dt = new DataTransfer();
                    for (const f of e.dataTransfer.files) dt.items.add(f);
                    inp.files = dt.files;
                    onFile(inp.files);
                }
            });
            inp.addEventListener('change', () => { if (inp.files.length) onFile(inp.files); });
        }

        /* ═══════════════════════════════════════════════════════════════════════════ */
        /*  STEP 1: Upload reference                                                  */
        /* ═══════════════════════════════════════════════════════════════════════════ */
        let refFiles = [];
        let loadedImages = [];  // {img, offsetX, offsetY, scale}
        let stitchedBlob = null;

        setupDZ('dz-ref', 'file-ref', files => {
            refFiles = Array.from(files);
            stitchedBlob = null;
            const label = refFiles.length === 1
                ? refFiles[0].name
                : `${refFiles.length} images selected`;
            document.querySelector('#dz-ref .dropzone-label').textContent = label;
            loadRefImages(refFiles);
        });

        async function loadRefImages(files) {
            loadedImages = [];
            const panel = document.getElementById('stitch-panel');
            const editArea = document.getElementById('stitch-edit-area');
            const resultWrap = document.getElementById('stitch-result-wrap');
            const controls = document.getElementById('stitch-controls');
            const btnUpload = document.getElementById('btn-upload');

            resultWrap.classList.add('hidden');
            editArea.classList.remove('hidden');

            if (files.length <= 1) {
                panel.classList.add('hidden');
                btnUpload.disabled = files.length === 0;
                if (files.length === 1) {
                    const img = new Image();
                    img.src = URL.createObjectURL(files[0]);
                    await new Promise(r => img.onload = r);
                    loadedImages = [{ img, offsetX: 0, offsetY: 0, scale: 1 }];
                    stitchedBlob = files[0];
                }
                return;
            }

            for (const f of files) {
                const img = new Image();
                img.src = URL.createObjectURL(f);
                await new Promise(r => img.onload = r);
                loadedImages.push({ img, offsetX: 0, offsetY: 0, scale: 1 });
            }

            // Build per-image controls (skip first — it's the base)
            controls.innerHTML = '';
            loadedImages.forEach((item, i) => {
                if (i === 0) return;
                const div = document.createElement('div');
                div.className = 'stitch-img-item';
                div.innerHTML = `
                    <div class="ctrl-title">Img ${i + 1}</div>
                    <div class="ctrl-row">
                        <label>X</label>
                        <input type="range" min="-500" max="500" value="0" data-param="offsetX" data-idx="${i}">
                        <span class="ctrl-val" data-for="offsetX-${i}">0</span>
                    </div>
                    <div class="ctrl-row">
                        <label>Y</label>
                        <input type="range" min="-500" max="500" value="0" data-param="offsetY" data-idx="${i}">
                        <span class="ctrl-val" data-for="offsetY-${i}">0</span>
                    </div>
                    <div class="ctrl-row">
                        <label>S</label>
                        <input type="range" min="50" max="200" value="100" data-param="scale" data-idx="${i}">
                        <span class="ctrl-val" data-for="scale-${i}">100%</span>
                    </div>`;
                div.querySelectorAll('input[type=range]').forEach(range => {
                    range.addEventListener('input', () => {
                        const idx = +range.dataset.idx;
                        const param = range.dataset.param;
                        const v = +range.value;
                        if (param === 'scale') {
                            loadedImages[idx].scale = v / 100;
                            div.querySelector(`[data-for="scale-${idx}"]`).textContent = v + '%';
                        } else {
                            loadedImages[idx][param] = v;
                            div.querySelector(`[data-for="${param}-${idx}"]`).textContent = v;
                        }
                        drawBlendPreview();
                    });
                });
                controls.appendChild(div);
            });

            panel.classList.remove('hidden');
            btnUpload.disabled = true;
            document.getElementById('stitch-title').textContent = 'Image Blending (averaging)';
            drawBlendPreview();
        }

        /* Draw overlay preview using globalAlpha averaging */
        function drawBlendPreview() {
            if (loadedImages.length < 2) return;
            const base = loadedImages[0];
            const baseW = base.img.naturalWidth;
            const baseH = base.img.naturalHeight;

            const canvas = document.getElementById('stitch-canvas');
            const previewScale = Math.min(600 / baseW, 1);
            canvas.width = Math.round(baseW * previewScale);
            canvas.height = Math.round(baseH * previewScale);
            const ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Draw each image with alpha = 1/(i+1) for proper averaging
            loadedImages.forEach((item, i) => {
                ctx.globalAlpha = 1 / (i + 1);
                const s = item.scale * previewScale;
                const w = item.img.naturalWidth * s;
                const h = item.img.naturalHeight * s;
                const x = item.offsetX * previewScale;
                const y = item.offsetY * previewScale;
                ctx.drawImage(item.img, x, y, w, h);
            });
            ctx.globalAlpha = 1;
        }

        /* Build full-resolution averaged image using pixel data */
        function buildBlendedCanvas() {
            const base = loadedImages[0];
            const W = base.img.naturalWidth;
            const H = base.img.naturalHeight;
            const N = loadedImages.length;

            // Accumulate pixel sums
            const sum = new Float32Array(W * H * 3);
            const count = new Float32Array(W * H);

            const tmpCanvas = document.createElement('canvas');
            tmpCanvas.width = W; tmpCanvas.height = H;
            const tmpCtx = tmpCanvas.getContext('2d');

            loadedImages.forEach(item => {
                tmpCtx.clearRect(0, 0, W, H);
                const s = item.scale;
                const w = item.img.naturalWidth * s;
                const h = item.img.naturalHeight * s;
                tmpCtx.drawImage(item.img, item.offsetX, item.offsetY, w, h);
                const data = tmpCtx.getImageData(0, 0, W, H).data;
                for (let p = 0; p < W * H; p++) {
                    const r = data[p * 4], g = data[p * 4 + 1], b = data[p * 4 + 2], a = data[p * 4 + 3];
                    if (a > 0) {
                        sum[p * 3] += r;
                        sum[p * 3 + 1] += g;
                        sum[p * 3 + 2] += b;
                        count[p]++;
                    }
                }
            });

            // Build averaged result
            const outCanvas = document.createElement('canvas');
            outCanvas.width = W; outCanvas.height = H;
            const outCtx = outCanvas.getContext('2d');
            const outData = outCtx.createImageData(W, H);
            for (let p = 0; p < W * H; p++) {
                const c = count[p] || 1;
                outData.data[p * 4] = Math.round(sum[p * 3] / c);
                outData.data[p * 4 + 1] = Math.round(sum[p * 3 + 1] / c);
                outData.data[p * 4 + 2] = Math.round(sum[p * 3 + 2] / c);
                outData.data[p * 4 + 3] = 255;
            }
            outCtx.putImageData(outData, 0, 0);
            return outCanvas;
        }

        // "Объединить" button — build averaged image, show result
        document.getElementById('btn-stitch').addEventListener('click', () => {
            if (loadedImages.length < 2) return;
            const c = buildBlendedCanvas();
            c.toBlob(blob => {
                stitchedBlob = blob;
                document.getElementById('stitch-edit-area').classList.add('hidden');
                const resultWrap = document.getElementById('stitch-result-wrap');
                const resultImg = document.getElementById('stitch-result-img');
                const resultInfo = document.getElementById('stitch-result-info');
                resultImg.src = URL.createObjectURL(blob);
                resultInfo.textContent = `${c.width}×${c.height} px · ${(blob.size / 1024 / 1024).toFixed(2)} MB`;
                resultWrap.classList.remove('hidden');
                document.getElementById('stitch-title').textContent = '✅ Final image (averaged)';
                document.getElementById('btn-upload').disabled = false;
                toast('Blended! Click Upload to continue', 'success');
            }, 'image/jpeg', 0.92);
        });

        // "Подстроить заново" — go back to edit
        document.getElementById('btn-stitch-redo').addEventListener('click', () => {
            stitchedBlob = null;
            document.getElementById('stitch-result-wrap').classList.add('hidden');
            document.getElementById('stitch-edit-area').classList.remove('hidden');
            document.getElementById('stitch-title').textContent = 'Image Blending (averaging)';
            document.getElementById('btn-upload').disabled = true;
        });

        // "Auto-align" — send all images to backend for SIFT alignment + averaging
        document.getElementById('btn-auto-blend').addEventListener('click', async () => {
            if (refFiles.length < 2) return;
            const btn = document.getElementById('btn-auto-blend');
            const spin = document.getElementById('spin-auto-blend');
            btn.disabled = true; spin.classList.remove('hidden');

            try {
                const fd = new FormData();
                refFiles.forEach(f => fd.append('images', f));

                const r = await fetch('/api/auto_blend', { method: 'POST', body: fd });
                const d = await r.json();
                if (d.error) {
                    toast(d.error + (d.log ? '\n' + d.log.join('\n') : ''), 'error');
                    return;
                }

                // Convert base64 result to blob
                const byteStr = atob(d.image_b64);
                const ab = new Uint8Array(byteStr.length);
                for (let i = 0; i < byteStr.length; i++) ab[i] = byteStr.charCodeAt(i);
                stitchedBlob = new Blob([ab], { type: 'image/jpeg' });

                // Show result
                document.getElementById('stitch-edit-area').classList.add('hidden');
                const resultWrap = document.getElementById('stitch-result-wrap');
                const resultImg = document.getElementById('stitch-result-img');
                const resultInfo = document.getElementById('stitch-result-info');
                resultImg.src = 'data:image/jpeg;base64,' + d.image_b64;
                resultInfo.innerHTML = `${d.width}×${d.height} px<br><small style="color:var(--muted)">${d.log.join('<br>')}</small>`;
                resultWrap.classList.remove('hidden');
                document.getElementById('stitch-title').textContent = '✅ Final image (auto-aligned)';
                document.getElementById('btn-upload').disabled = false;
                toast('Auto-alignment complete!', 'success');
            } catch (e) { toast(e.message, 'error'); }
            finally { btn.disabled = false; spin.classList.add('hidden'); }
        });

        document.getElementById('btn-upload').addEventListener('click', async () => {
            if (!stitchedBlob && !refFiles.length) return;
            const btn = document.getElementById('btn-upload');
            const spin = document.getElementById('spin-upload');
            btn.disabled = true; spin.classList.remove('hidden');

            try {
                const blob = stitchedBlob || refFiles[0];
                const fd = new FormData();
                fd.append('image', blob, 'stitched.jpg');

                const r = await fetch('/api/session', { method: 'POST', body: fd });
                const d = await r.json();
                if (d.error) { toast(d.error, 'error'); return; }

                sessionId = d.session_id;
                refImg = new Image();
                refImg.onload = () => { canvasZoom = 1; goStep(2); requestAnimationFrame(initCanvas); };
                refImg.src = 'data:image/jpeg;base64,' + d.image_b64;
            } catch (e) { toast(e.message, 'error'); }
            finally { btn.disabled = false; spin.classList.add('hidden'); }
        });

        /* ═══════════════════════════════════════════════════════════════════════════ */
        /*  STEP 2: Draw zones on canvas                                              */
        /* ═══════════════════════════════════════════════════════════════════════════ */
        function initCanvas() {
            const c = document.getElementById('ref-canvas');
            const card = document.getElementById('panel-2');
            const availW = card.clientWidth - 32;
            const baseScale = availW / refImg.naturalWidth;
            const w = Math.round(refImg.naturalWidth * baseScale * canvasZoom);
            const h = Math.round(refImg.naturalHeight * baseScale * canvasZoom);
            c.width = w;
            c.height = h;
            c.style.width = w + 'px';
            c.style.height = h + 'px';
            drawCanvas();
            const zl = document.getElementById('zoom-level');
            if (zl) zl.textContent = Math.round(canvasZoom * 100) + '%';
        }
        window.addEventListener('resize', () => { if (refImg && refImg.complete) initCanvas(); });

        /* ─── Canvas Zoom ─── */
        (function() {
            const wrap = document.getElementById('canvas-wrap');
            wrap.addEventListener('wheel', e => {
                if (!refImg || !refImg.complete) return;
                if (!e.ctrlKey && !e.metaKey) return;
                e.preventDefault();
                const oldZoom = canvasZoom;
                const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
                canvasZoom = Math.max(1, Math.min(8, canvasZoom * factor));
                canvasZoom = Math.round(canvasZoom * 100) / 100;
                if (canvasZoom === oldZoom) return;
                const wrapRect = wrap.getBoundingClientRect();
                const vx = e.clientX - wrapRect.left;
                const vy = e.clientY - wrapRect.top;
                const c = document.getElementById('ref-canvas');
                const nx = (vx + wrap.scrollLeft) / c.width;
                const ny = (vy + wrap.scrollTop) / c.height;
                initCanvas();
                wrap.scrollLeft = nx * c.width - vx;
                wrap.scrollTop = ny * c.height - vy;
            }, { passive: false });
            document.getElementById('zoom-in').addEventListener('click', () => {
                if (!refImg) return;
                canvasZoom = Math.min(8, canvasZoom * 1.3);
                initCanvas();
            });
            document.getElementById('zoom-out').addEventListener('click', () => {
                if (!refImg) return;
                canvasZoom = Math.max(1, canvasZoom / 1.3);
                initCanvas();
            });
            document.getElementById('zoom-reset').addEventListener('click', () => {
                if (!refImg) return;
                canvasZoom = 1;
                const wrap = document.getElementById('canvas-wrap');
                wrap.scrollLeft = 0; wrap.scrollTop = 0;
                initCanvas();
            });
        })();

        function drawCanvas() {
            const c = document.getElementById('ref-canvas');
            const ctx = c.getContext('2d');
            ctx.clearRect(0, 0, c.width, c.height);
            ctx.drawImage(refImg, 0, 0, c.width, c.height);

            zones.forEach((z, i) => {
                const color = COLORS[i % COLORS.length];
                const x = z.x * c.width, y = z.y * c.height;
                const w = z.w * c.width, h = z.h * c.height;
                ctx.strokeStyle = color;
                ctx.lineWidth = (selectedZone === i) ? 3 : 2;
                ctx.strokeRect(x, y, w, h);
                ctx.fillStyle = color + '33';
                ctx.fillRect(x, y, w, h);
                // Selected highlight
                if (selectedZone === i) {
                    ctx.setLineDash([4, 3]);
                    ctx.strokeStyle = '#fff';
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x - 2, y - 2, w + 4, h + 4);
                    ctx.setLineDash([]);
                }
                // Label
                ctx.fillStyle = color;
                ctx.font = 'bold 13px sans-serif';
                ctx.fillText(z.label, x + 4, y + 16);

                // Per-zone anchors
                const za = z.anchors || [];
                za.forEach((a, ai) => {
                    const ax = a.x * c.width, ay = a.y * c.height;
                    const r = 10;
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 2;
                    ctx.beginPath(); ctx.moveTo(ax - r, ay); ctx.lineTo(ax + r, ay); ctx.stroke();
                    ctx.beginPath(); ctx.moveTo(ax, ay - r); ctx.lineTo(ax, ay + r); ctx.stroke();
                    ctx.beginPath(); ctx.arc(ax, ay, r, 0, Math.PI * 2); ctx.stroke();
                    ctx.fillStyle = color;
                    ctx.font = 'bold 11px var(--mono)';
                    ctx.fillText('\u2316' + (ai + 1), ax + r + 3, ay - 3);
                });

                // Per-zone subzones (coords relative to parent zone)
                const szs = z.subzones || [];
                szs.forEach((sz, si) => {
                    const sx = (z.x + sz.x * z.w) * c.width;
                    const sy = (z.y + sz.y * z.h) * c.height;
                    const sw = sz.w * z.w * c.width;
                    const sh = sz.h * z.h * c.height;
                    const isHovered = (subzoneMode && selectedZone === i && hoveredSubzone === si);
                    ctx.setLineDash(isHovered ? [] : [3, 3]);
                    ctx.strokeStyle = isHovered ? '#ff4444' : '#ff6b6b';
                    ctx.lineWidth = isHovered ? 3 : 2;
                    ctx.strokeRect(sx, sy, sw, sh);
                    ctx.fillStyle = isHovered ? '#ff6b6b44' : '#ff6b6b22';
                    ctx.fillRect(sx, sy, sw, sh);
                    ctx.setLineDash([]);
                    ctx.fillStyle = isHovered ? '#ff4444' : '#ff6b6b';
                    ctx.font = 'bold 10px sans-serif';
                    ctx.fillText(sz.label || `S${si + 1}`, sx + 2, sy + 11);
                });
            });
        }

        function renderZoneChips() {
            const el = document.getElementById('zone-chips');
            el.innerHTML = '';
            zones.forEach((z, i) => {
                const chip = document.createElement('div');
                chip.className = 'zone-chip' + (selectedZone === i ? ' selected' : '');
                chip.style.borderColor = COLORS[i % COLORS.length];
                if (selectedZone === i) chip.style.background = COLORS[i % COLORS.length] + '22';
                const anchorCount = (z.anchors || []).length;
                const anchorActive = (anchorMode && selectedZone === i);
                const szCount = (z.subzones || []).length;
                const szActive = (subzoneMode && selectedZone === i);
                chip.innerHTML = `<span class="dot" style="background:${COLORS[i % COLORS.length]};cursor:pointer" data-sel="${i}"></span>
            <input class="zone-label-input" value="${z.label}" data-i="${i}">
            <span style="font-size:12px;cursor:pointer;padding:2px 4px;border-radius:4px;${anchorActive ? 'background:var(--warn-dim);color:var(--warn)' : 'opacity:.5'}" data-anch="${i}" title="Click to place anchors">\u2316${anchorCount}/2</span>
            <span style="font-size:11px;cursor:pointer;padding:2px 4px;border-radius:4px;${szActive ? 'background:#ff6b6b33;color:#ff6b6b' : 'opacity:.5'}" data-subz="${i}" title="Draw subzones (strict check)">◻${szCount}/10</span>
            <span class="remove" data-i="${i}">&times;</span>`;
                el.appendChild(chip);
            });
            el.querySelectorAll('[data-sel]').forEach(btn => {
                btn.addEventListener('click', () => selectZone(+btn.dataset.sel));
            });
            el.querySelectorAll('[data-anch]').forEach(btn => {
                btn.addEventListener('click', () => toggleAnchorMode(+btn.dataset.anch));
            });
            el.querySelectorAll('[data-subz]').forEach(btn => {
                btn.addEventListener('click', () => toggleSubzoneMode(+btn.dataset.subz));
            });
            el.querySelectorAll('.zone-label-input').forEach(inp => {
                inp.addEventListener('change', () => {
                    zones[+inp.dataset.i].label = inp.value;
                    drawCanvas();
                });
            });
            el.querySelectorAll('.remove').forEach(btn => {
                btn.addEventListener('click', () => {
                    const ri = +btn.dataset.i;
                    zones.splice(ri, 1);
                    if (selectedZone === ri) selectedZone = -1;
                    else if (selectedZone > ri) selectedZone--;
                    zones.forEach((z, i) => { if (z.label.match(/^Zone \d+$/)) z.label = `Zone ${i + 1}`; });
                    drawCanvas(); renderZoneChips();
                });
            });
            document.getElementById('btn-save-zones').disabled = zones.length === 0;
        }

        // Mouse drawing
        const canvas = document.getElementById('ref-canvas');
        canvas.addEventListener('mousedown', e => {
            // Anchor placement mode — place anchors for selected zone
            if (anchorMode && selectedZone >= 0 && selectedZone < zones.length) {
                const rect = canvas.getBoundingClientRect();
                const ax = (e.clientX - rect.left) / canvas.width;
                const ay = (e.clientY - rect.top) / canvas.height;
                if (!zones[selectedZone].anchors) zones[selectedZone].anchors = [];
                if (zones[selectedZone].anchors.length >= 2) zones[selectedZone].anchors.shift();
                zones[selectedZone].anchors.push({ x: ax, y: ay });
                drawCanvas();
                renderZoneChips();
                return;
            }
            // Subzone mode — draw inside parent zone
            if (subzoneMode && selectedZone >= 0 && selectedZone < zones.length) {
                const rect = canvas.getBoundingClientRect();
                drawStart = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
                drawing = true;
                return;
            }
            const rect = canvas.getBoundingClientRect();
            drawStart = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
            drawing = true;
        });
        canvas.addEventListener('mousemove', e => {
            if (!drawing) return;
            const rect = canvas.getBoundingClientRect();
            const cur = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
            drawCanvas();
            const ctx = canvas.getContext('2d');
            const x = drawStart.x * canvas.width, y = drawStart.y * canvas.height;
            const w = (cur.x - drawStart.x) * canvas.width, h = (cur.y - drawStart.y) * canvas.height;
            ctx.strokeStyle = subzoneMode ? '#ff6b6b' : '#fff';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([6, 3]);
            ctx.strokeRect(x, y, w, h);
            ctx.setLineDash([]);
        });
        canvas.addEventListener('mouseup', e => {
            if (!drawing) return;
            drawing = false;
            const rect = canvas.getBoundingClientRect();
            const end = { x: (e.clientX - rect.left) / canvas.width, y: (e.clientY - rect.top) / canvas.height };
            let x = Math.min(drawStart.x, end.x), y = Math.min(drawStart.y, end.y);
            let w = Math.abs(end.x - drawStart.x), h = Math.abs(end.y - drawStart.y);
            // Ignore tiny rects (accidental clicks)
            if (w < 0.02 || h < 0.02) { drawCanvas(); return; }

            if (subzoneMode && selectedZone >= 0 && selectedZone < zones.length) {
                // Create subzone clipped to parent zone
                const pz = zones[selectedZone];
                if (!pz.subzones) pz.subzones = [];
                if (pz.subzones.length >= 10) { toast('Максимум 10 подзон на зону', 'warn'); drawCanvas(); return; }
                // Clamp to parent zone bounds
                const cx1 = Math.max(x, pz.x), cy1 = Math.max(y, pz.y);
                const cx2 = Math.min(x + w, pz.x + pz.w), cy2 = Math.min(y + h, pz.y + pz.h);
                const cw = cx2 - cx1, ch = cy2 - cy1;
                if (cw < 0.01 || ch < 0.01) { toast('Подзона вне зоны', 'warn'); drawCanvas(); return; }
                // Convert to parent-relative coords (0..1)
                const szx = (cx1 - pz.x) / pz.w;
                const szy = (cy1 - pz.y) / pz.h;
                const szw = cw / pz.w;
                const szh = ch / pz.h;
                pz.subzones.push({ x: szx, y: szy, w: szw, h: szh, label: `S${pz.subzones.length + 1}`, sensitivity: null });
                drawCanvas(); renderZoneChips(); renderSubzoneChips();
                return;
            }

            // Clamp
            x = Math.max(0, x); y = Math.max(0, y);
            w = Math.min(w, 1 - x); h = Math.min(h, 1 - y);
            zones.push({ x, y, w, h, label: `Zone ${zones.length + 1}`, anchors: [], subzones: [] });
            drawCanvas();
            renderZoneChips();
        });

        // Anchor placement — click ⌖ on zone chip to toggle
        function toggleAnchorMode(idx) {
            subzoneMode = false;  // turn off subzone mode
            if (anchorMode && selectedZone === idx) {
                anchorMode = false;
                selectedZone = -1;
            } else {
                anchorMode = true;
                selectedZone = idx;
            }
            document.getElementById('anchor-hint').classList.toggle('hidden', !anchorMode);
            document.getElementById('subzone-hint').classList.add('hidden');
            canvas.style.cursor = anchorMode ? 'crosshair' : '';
            drawCanvas();
            renderZoneChips();
        }
        // Subzone drawing — click ◻ on zone chip to toggle
        function toggleSubzoneMode(idx) {
            anchorMode = false;  // turn off anchor mode
            hoveredSubzone = -1;
            if (subzoneMode && selectedZone === idx) {
                subzoneMode = false;
                selectedZone = -1;
            } else {
                subzoneMode = true;
                selectedZone = idx;
            }
            document.getElementById('subzone-hint').classList.toggle('hidden', !subzoneMode);
            document.getElementById('anchor-hint').classList.add('hidden');
            canvas.style.cursor = subzoneMode ? 'crosshair' : '';
            drawCanvas();
            renderZoneChips();
            renderSubzoneChips();
        }
        function renderSubzoneChips() {
            const el = document.getElementById('subzone-chips');
            if (!el) return;
            if (!subzoneMode || selectedZone < 0 || !zones[selectedZone]) { el.innerHTML = ''; return; }
            const szs = zones[selectedZone].subzones || [];
            if (!szs.length) { el.innerHTML = '<span style="opacity:.6;font-size:11px">No subzones yet — draw on canvas</span>'; return; }
            el.innerHTML = szs.map((sz, i) => `
                <span style="display:inline-flex;align-items:center;gap:3px;background:#ff6b6b22;border:1px solid #ff6b6b55;border-radius:4px;padding:2px 6px" data-sz-chip="${i}">
                    <input value="${sz.label}" data-sz-label="${i}" style="width:36px;background:none;border:none;color:#ff6b6b;font-size:11px;padding:0;font-weight:bold;cursor:text">
                    <input value="${sz.sensitivity != null ? sz.sensitivity : ''}" data-sz-sens="${i}" placeholder="sns" title="Per-subzone sensitivity (0..2). Empty = use global" style="width:42px;background:#ff6b6b11;border:1px dashed #ff6b6b55;border-radius:3px;color:#ff6b6b;font-size:12px;padding:1px 3px;text-align:center;cursor:text">
                    <span data-sz-rm="${i}" style="cursor:pointer;opacity:.7;font-size:13px" title="Delete subzone">&times;</span>
                </span>`).join('');
            el.querySelectorAll('[data-sz-label]').forEach(inp => {
                inp.addEventListener('change', () => {
                    const si = +inp.dataset.szLabel;
                    if (zones[selectedZone] && zones[selectedZone].subzones[si]) {
                        zones[selectedZone].subzones[si].label = inp.value;
                        drawCanvas();
                    }
                });
            });
            el.querySelectorAll('[data-sz-sens]').forEach(inp => {
                inp.addEventListener('change', () => {
                    const si = +inp.dataset.szSens;
                    if (zones[selectedZone] && zones[selectedZone].subzones[si]) {
                        const v = inp.value.trim();
                        zones[selectedZone].subzones[si].sensitivity = v === '' ? null : Math.max(0, Math.min(2, parseFloat(v) || 0));
                        inp.value = zones[selectedZone].subzones[si].sensitivity != null ? zones[selectedZone].subzones[si].sensitivity : '';
                    }
                });
            });
            el.querySelectorAll('[data-sz-rm]').forEach(btn => {
                btn.addEventListener('click', () => {
                    const si = +btn.dataset.szRm;
                    if (zones[selectedZone] && zones[selectedZone].subzones) {
                        zones[selectedZone].subzones.splice(si, 1);
                        zones[selectedZone].subzones.forEach((s, j) => { if (s.label.match(/^S\d+$/)) s.label = `S${j + 1}`; });
                        hoveredSubzone = -1;
                        drawCanvas(); renderZoneChips(); renderSubzoneChips();
                    }
                });
            });
            el.querySelectorAll('[data-sz-chip]').forEach(chip => {
                chip.addEventListener('mouseenter', () => { hoveredSubzone = +chip.dataset.szChip; drawCanvas(); });
                chip.addEventListener('mouseleave', () => { hoveredSubzone = -1; drawCanvas(); });
            });
        }
        // Hint bar buttons (delegated after render)
        document.getElementById('anchor-hint').addEventListener('click', e => {
            if (e.target.id === 'anchor-hint-done') {
                e.preventDefault();
                anchorMode = false;
                selectedZone = -1;
                document.getElementById('anchor-hint').classList.add('hidden');
                canvas.style.cursor = '';
                drawCanvas(); renderZoneChips();
            } else if (e.target.id === 'anchor-hint-clear') {
                e.preventDefault();
                if (selectedZone >= 0 && zones[selectedZone]) zones[selectedZone].anchors = [];
                drawCanvas(); renderZoneChips();
            }
        });
        document.getElementById('subzone-hint').addEventListener('click', e => {
            if (e.target.id === 'subzone-hint-done') {
                e.preventDefault();
                subzoneMode = false;
                selectedZone = -1;
                hoveredSubzone = -1;
                document.getElementById('subzone-hint').classList.add('hidden');
                canvas.style.cursor = '';
                drawCanvas(); renderZoneChips();
            } else if (e.target.id === 'subzone-hint-clear') {
                e.preventDefault();
                if (selectedZone >= 0 && zones[selectedZone]) zones[selectedZone].subzones = [];
                hoveredSubzone = -1;
                renderSubzoneChips();
                drawCanvas(); renderZoneChips();
            }
        });
        function selectZone(idx) {
            selectedZone = (selectedZone === idx) ? -1 : idx;
            hoveredSubzone = -1;
            if (!anchorMode && !subzoneMode) { drawCanvas(); renderZoneChips(); return; }
            // If in anchor/subzone mode, switch to this zone
            drawCanvas(); renderZoneChips();
            if (subzoneMode) renderSubzoneChips();
        }

        // Touch support
        canvas.addEventListener('touchstart', e => {
            e.preventDefault();
            const t = e.touches[0];
            canvas.dispatchEvent(new MouseEvent('mousedown', { clientX: t.clientX, clientY: t.clientY }));
        }, { passive: false });
        canvas.addEventListener('touchmove', e => {
            e.preventDefault();
            const t = e.touches[0];
            canvas.dispatchEvent(new MouseEvent('mousemove', { clientX: t.clientX, clientY: t.clientY }));
        }, { passive: false });
        canvas.addEventListener('touchend', e => {
            e.preventDefault();
            const t = e.changedTouches[0];
            canvas.dispatchEvent(new MouseEvent('mouseup', { clientX: t.clientX, clientY: t.clientY }));
        }, { passive: false });

        document.getElementById('btn-clear-zones').addEventListener('click', () => {
            zones = []; drawCanvas(); renderZoneChips();
        });
        document.getElementById('btn-undo-zone').addEventListener('click', () => {
            zones.pop(); drawCanvas(); renderZoneChips();
        });

        // Save zones
        document.getElementById('btn-save-zones').addEventListener('click', async () => {
            if (!zones.length) return;
            try {
                const r = await fetch(`/api/session/${sessionId}/zones`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ zones }),
                });
                const d = await r.json();
                if (d.error) { toast(d.error, 'error'); return; }
                zonePreviews = d.previews;
                checkedZones = new Set();
                zoneDefectStatus = {};
                goStep(3);
                renderStep3();
                drawMiniRef();
                generateMobileQR();
                // Sync auto-accept to session
                fetch(`/api/session/${sessionId}/auto_accept`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_accept: autoAccept })
                }).catch(() => {});
            } catch (e) { toast(e.message, 'error'); }
        });

        /* ═══════════════════════════════════════════════════════════════════════════ */
        /*  STEP 3: Check zones                                                        */
        /* ═══════════════════════════════════════════════════════════════════════════ */
        function _buildSubzoneHtml(subzones, zoneIdx) {
            if (!subzones || !subzones.length) return '';
            let html = '<div style="margin-top:8px;border-top:1px solid #333;padding-top:6px"><p style="color:#ff6b6b;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Subzone checks (strict)</p>';
            for (let si = 0; si < subzones.length; si++) {
                const sz = subzones[si];
                const usd = (userSubDecisions[zoneIdx] || {})[si];
                const effective = usd || sz.status;
                const vc = effective === 'ok' ? 'verdict-ok' : effective === 'warn' ? 'verdict-warn' : 'verdict-defect';
                const overridden = usd && usd !== sz.status;
                let calcLabel = overridden
                    ? ` <span style="font-size:10px;color:var(--muted);text-decoration:line-through;margin-left:4px">${sz.status.toUpperCase()}</span>`
                    : '';
                html += `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap">
                    <span class="verdict-badge ${vc}" style="font-size:11px;padding:2px 8px">${sz.label}: ${effective.toUpperCase()}</span>${calcLabel}
                    <select class="zone-decision sz-decision" data-zone="${zoneIdx}" data-sz="${si}" style="font-size:10px">
                        <option value="ok"${effective === 'ok' ? ' selected' : ''}>OK</option>
                        <option value="warn"${effective === 'warn' ? ' selected' : ''}>WARN</option>
                        <option value="defect"${effective === 'defect' ? ' selected' : ''}>DEFECT</option>
                    </select>
                    <span style="font-size:11px;color:var(--muted)">SSIM ${(sz.ssim * 100).toFixed(1)}% · Defect ${sz.defect_pct.toFixed(1)}%${sz.sensitivity != null ? ` · sens ${sz.sensitivity.toFixed(2)}` : ''}</span>
                </div>`;
            }
            html += '<div class="sz-thumbs" style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">';
            for (let ti = 0; ti < subzones.length; ti++) {
                const sz = subzones[ti];
                html += `<div style="text-align:center;cursor:pointer" data-sz-thumb="${ti}"><p style="font-size:10px;color:#ff6b6b">${sz.label}</p><img src="data:image/png;base64,${sz.vis_defects_b64}" style="width:80px;height:80px;object-fit:contain;border:1px solid #333;border-radius:4px"></div>`;
            }
            html += '</div></div>';
            return html;
        }

        /* ─── Inspection lightbox: full-size image view with decisions ─── */
        function openInspectLightbox(zoneIdx, subzoneIdx) {
            const zr = zoneResults[zoneIdx];
            if (!zr) return;
            const modal = document.getElementById('inspect-lightbox');
            const header = document.getElementById('ilb-header');
            const body = document.getElementById('ilb-body');
            const zLabel = zr.best_zone_label || zones[zoneIdx]?.label || `Zone ${zoneIdx}`;

            if (subzoneIdx != null && zr.subzones && zr.subzones[subzoneIdx]) {
                // Subzone lightbox
                const sz = zr.subzones[subzoneIdx];
                const usd = (userSubDecisions[zoneIdx] || {})[subzoneIdx];
                const effective = usd || sz.status;
                header.innerHTML = `
                    <span class="ilb-title">${zLabel} / ${sz.label}</span>
                    <span style="font-size:11px;color:var(--muted)">SSIM ${(sz.ssim*100).toFixed(1)}% · Defect ${sz.defect_pct.toFixed(1)}%</span>
                    <select class="ilb-decision" id="ilb-sz-decision">
                        <option value="ok"${effective==='ok'?' selected':''}>OK</option>
                        <option value="warn"${effective==='warn'?' selected':''}>WARN</option>
                        <option value="defect"${effective==='defect'?' selected':''}>DEFECT</option>
                    </select>`;
                body.innerHTML = `
                    <div class="ilb-img-col"><p>Defect map</p><img src="data:image/png;base64,${sz.vis_defects_b64}"></div>
                    <div class="ilb-img-col"><p>Reference</p><img src="data:image/png;base64,${sz.reference_b64 || zonePreviews[zoneIdx]}"></div>
                    <div class="ilb-img-col"><p>Extracted</p><img src="data:image/png;base64,${sz.extracted_b64 || ''}"></div>`;
                document.getElementById('ilb-sz-decision').addEventListener('change', (e) => {
                    const val = e.target.value;
                    if (!userSubDecisions[zoneIdx]) userSubDecisions[zoneIdx] = {};
                    if (val === sz.status) delete userSubDecisions[zoneIdx][subzoneIdx];
                    else userSubDecisions[zoneIdx][subzoneIdx] = val;
                    if (!Object.keys(userSubDecisions[zoneIdx]).length) delete userSubDecisions[zoneIdx];
                    resultSaved = false;
                    renderStep3();
                });
            } else {
                // Zone-level lightbox: all images
                const uDec = userDecisions[zoneIdx];
                const effective = uDec || zr.status;
                const sensTag = zr.zone_sensitivity != null
                    ? ` · Zone: ${zr.zone_sensitivity.toFixed(2)}`
                    : '';
                header.innerHTML = `
                    <span class="ilb-title">${zLabel}</span>
                    <span style="font-size:11px;color:var(--muted)">SSIM ${(zr.ssim*100).toFixed(1)}% · Defect ${zr.defect_pct.toFixed(2)}% · ${zr.defect_count} regions · Match ${(zr.score*100).toFixed(1)}%${sensTag}</span>
                    <select class="ilb-decision" id="ilb-zone-decision">
                        <option value="ok"${effective==='ok'?' selected':''}>OK</option>
                        <option value="warn"${effective==='warn'?' selected':''}>WARN</option>
                        <option value="defect"${effective==='defect'?' selected':''}>DEFECT</option>
                    </select>`;
                let imgs = `
                    <div class="ilb-img-col"><p>Input</p><img src="data:image/jpeg;base64,${zr.photo_b64}"></div>
                    <div class="ilb-img-col"><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[zoneIdx]}"></div>`;
                if (zr.extracted_b64) imgs += `<div class="ilb-img-col"><p>Extracted</p><img src="data:image/png;base64,${zr.extracted_b64}"></div>`;
                if (zr.vis_defects_b64) imgs += `<div class="ilb-img-col"><p>Defect map</p><img src="data:image/png;base64,${zr.vis_defects_b64}"></div>`;
                if (zr.vis_heatmap_b64) imgs += `<div class="ilb-img-col"><p>Heatmap</p><img src="data:image/png;base64,${zr.vis_heatmap_b64}"></div>`;
                body.innerHTML = imgs;
                document.getElementById('ilb-zone-decision').addEventListener('change', (e) => {
                    const val = e.target.value;
                    if (val === zoneDefectStatus[zoneIdx]) delete userDecisions[zoneIdx];
                    else userDecisions[zoneIdx] = val;
                    resultSaved = false;
                    renderStep3();
                });
            }
            modal.classList.remove('hidden');
        }
        function closeInspectLightbox() {
            document.getElementById('inspect-lightbox').classList.add('hidden');
            if (currentViewZone != null) showZoneResult(currentViewZone);
        }

        function showZoneResult(zoneIdx) {
            const zr = zoneResults[zoneIdx];
            if (!zr) return;
            currentViewZone = zoneIdx;
            const res = document.getElementById('match-result');
            res.classList.remove('hidden');
            res.querySelectorAll('.defect-panel').forEach(p => p.remove());
            const title = document.getElementById('match-title');
            const label = zr.best_zone_label || zones[zoneIdx]?.label || `Zone ${zoneIdx}`;
            const verdictClass = zr.status === 'ok' ? 'verdict-ok' : zr.status === 'warn' ? 'verdict-warn' : 'verdict-defect';
            if (zr.status === 'defect') {
                title.textContent = `DEFECT // ${label}`;
                title.style.color = 'var(--danger)';
            } else if (zr.status === 'warn') {
                title.textContent = `WARN // ${label}`;
                title.style.color = 'var(--warn)';
            } else {
                title.textContent = `OK // ${label} (${(zr.score * 100).toFixed(1)}%)`;
                title.style.color = 'var(--success)';
            }
            const body = document.getElementById('match-body');
            if (zr.extracted_b64) {
                body.innerHTML = `
                    <div><p>Input</p><img src="data:image/jpeg;base64,${zr.photo_b64}"></div>
                    <div><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[zoneIdx]}"></div>
                    <div><p>Extracted</p><img src="data:image/png;base64,${zr.extracted_b64}"></div>
                    <div><p>Defect map</p><img src="data:image/png;base64,${zr.vis_defects_b64}"></div>
                    <div><p>Heatmap</p><img src="data:image/png;base64,${zr.vis_heatmap_b64}"></div>`;
                const defPanel = document.createElement('div');
                defPanel.className = 'defect-panel';
                const sensHtml2 = zr.zone_sensitivity != null
                    ? `<div class="sens-tag">Zone: ${zr.zone_sensitivity.toFixed(2)}</div>`
                    : '';
                defPanel.innerHTML = `
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <span class="verdict-badge ${verdictClass}">${zr.verdict || zr.status.toUpperCase()}</span>
                        <div class="defect-stats">
                            <div class="stat"><div class="num">${(zr.ssim * 100).toFixed(1)}%</div><div class="lbl">SSIM</div></div>
                            <div class="stat"><div class="num">${zr.defect_pct.toFixed(2)}%</div><div class="lbl">Defect area</div></div>
                            <div class="stat"><div class="num">${zr.defect_count}</div><div class="lbl">Regions</div></div>
                            <div class="stat"><div class="num">${(zr.score * 100).toFixed(1)}%</div><div class="lbl">Match</div></div>
                        </div>
                        ${sensHtml2}
                    </div>` + _buildSubzoneHtml(zr.subzones, zoneIdx);
                res.appendChild(defPanel);
            } else {
                body.innerHTML = `
                    <div><p>Input</p><img src="data:image/jpeg;base64,${zr.photo_b64}"></div>
                    <div><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[zoneIdx]}"></div>
                    <div></div><div></div><div></div>`;
            }
            // Re-bind subzone decision selects
            res.querySelectorAll('.sz-decision').forEach(sel => {
                sel.addEventListener('change', (e) => {
                    const zi = parseInt(e.target.dataset.zone);
                    const si = parseInt(e.target.dataset.sz);
                    const val = e.target.value;
                    if (!userSubDecisions[zi]) userSubDecisions[zi] = {};
                    const origStatus = zoneResults[zi]?.subzones?.[si]?.status;
                    if (val === origStatus) delete userSubDecisions[zi][si];
                    else userSubDecisions[zi][si] = val;
                    if (!Object.keys(userSubDecisions[zi]).length) delete userSubDecisions[zi];
                    resultSaved = false;
                    renderStep3();
                    showZoneResult(zi);
                });
            });
            if (zr.all_scores) {
                const scDiv = document.getElementById('all-scores');
                scDiv.innerHTML = '<p style="color:var(--muted);font-size:12px;margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px">All zone scores:</p>';
                zr.all_scores.forEach((sc, i) => {
                    const pctVal = (sc * 100).toFixed(1);
                    const color = i === zoneIdx ? 'var(--accent)' : 'var(--muted)';
                    scDiv.innerHTML += `<div class="score-bar">
                        <span style="font-size:12px;min-width:50px;color:${color}">${zones[i].label}</span>
                        <div class="bar-bg"><div class="bar-fg" style="width:${pctVal}%;background:${color}"></div></div>
                        <span class="score-val">${pctVal}%</span></div>`;
                });
            }
            res.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }

        function renderStep3() {
            // Progress bar
            const pct = zones.length ? Math.round(checkedZones.size / zones.length * 100) : 0;
            const fill = document.getElementById('progress-fill');
            fill.style.width = pct + '%';
            fill.textContent = `${checkedZones.size}/${zones.length}`;

            // Zone status chips
            const el = document.getElementById('zone-status');
            el.innerHTML = '';
            zones.forEach((z, i) => {
                const chip = document.createElement('div');
                const isChecked = checkedZones.has(i);
                const dStatus = zoneDefectStatus[i];
                const uDec = userDecisions[i];  // explicit zone-level override
                // Effective: zone override > worst-of-subzone-overrides > calculated
                let effective;
                if (uDec) {
                    effective = uDec;
                } else if (zoneResults[i]?.subzones?.length && userSubDecisions[i]) {
                    const _sr = {ok: 0, warn: 1, defect: 2};
                    const _rs = ['ok', 'warn', 'defect'];
                    let w = 0;
                    const subs = zoneResults[i].subzones;
                    for (let si = 0; si < subs.length; si++) {
                        const se = (userSubDecisions[i] || {})[si] || subs[si].status;
                        w = Math.max(w, _sr[se] ?? 0);
                    }
                    effective = _rs[w] || dStatus;
                } else {
                    effective = dStatus;
                }
                let cls = 'zone-chip';
                if (isChecked) {
                    if (effective === 'ok') cls += ' defect-ok';
                    else if (effective === 'warn') cls += ' defect-warn';
                    else if (effective === 'defect') cls += ' defect-bad';
                    else cls += ' checked';
                    cls += ' clickable';
                }
                chip.className = cls;
                const overridden = effective && effective !== dStatus;
                let chipHtml = `<span class="dot"></span>${z.label}`;
                if (isChecked && dStatus) {
                    // Show: calc status (if overridden — crossed out) + accepted
                    if (overridden) {
                        chipHtml += ` <span class="calc-status">${dStatus.toUpperCase()}</span>`;
                        chipHtml += ` <span class="accepted-status ${effective}">${effective.toUpperCase()}</span>`;
                    } else {
                        chipHtml += ` <span class="accepted-status ${effective}">${effective.toUpperCase()}</span>`;
                    }
                    chipHtml += `<select class="zone-decision" data-zone="${i}">`;
                    for (const opt of ['ok','warn','defect']) {
                        const sel = effective === opt ? ' selected' : '';
                        chipHtml += `<option value="${opt}"${sel}>${opt.toUpperCase()}</option>`;
                    }
                    chipHtml += `</select>`;
                }
                chip.innerHTML = chipHtml;
                if (isChecked && zoneResults[i]) {
                    chip.addEventListener('click', (e) => {
                        if (e.target.closest('.zone-decision')) return;
                        showZoneResult(i);
                    });
                }
                el.appendChild(chip);
            });
            // Bind decision selects
            el.querySelectorAll('.zone-decision').forEach(sel => {
                sel.addEventListener('change', (e) => {
                    const zi = parseInt(e.target.dataset.zone);
                    const val = e.target.value;
                    console.log('[ZONE-DECISION] zone=', zi, 'val=', val, 'origStatus=', zoneDefectStatus[zi]);
                    if (val === zoneDefectStatus[zi]) delete userDecisions[zi];
                    else userDecisions[zi] = val;
                    console.log('[ZONE-DECISION] userDecisions=', JSON.stringify(userDecisions));
                    resultSaved = false;
                    renderStep3();
                });
            });

            // Complete banner — managed by onAllZonesComplete(), just update serial
            if (checkedZones.size >= zones.length && zones.length > 0) {
                const sib = document.getElementById('serial-in-banner');
                if (currentSerial) {
                    sib.textContent = `Serial: ${currentSerial}`;
                    sib.classList.remove('hidden');
                }
            }
        }

        function drawMiniRef() {
            const c = document.getElementById('ref-canvas-mini');
            if (!refImg) return;
            const maxW = (c.parentElement ? c.parentElement.clientWidth : 260) - 4;
            const scale = Math.min(maxW / refImg.naturalWidth, 1.0);
            c.width = refImg.naturalWidth * scale;
            c.height = refImg.naturalHeight * scale;
            const ctx = c.getContext('2d');

            // Draw reference image lightened (dimmed)
            ctx.drawImage(refImg, 0, 0, c.width, c.height);
            ctx.fillStyle = 'rgba(10, 12, 16, 0.45)';
            ctx.fillRect(0, 0, c.width, c.height);

            zones.forEach((z, i) => {
                const dStatus = zoneDefectStatus[i];
                const isChecked = checkedZones.has(i);
                let color;
                if (dStatus === 'ok') color = '#22c55e';
                else if (dStatus === 'warn') color = '#f59e0b';
                else if (dStatus === 'defect') color = '#ef4444';
                else if (isChecked) color = '#22c55e';
                else color = '#3b82f6';

                const x = z.x * c.width, y = z.y * c.height;
                const w = z.w * c.width, h = z.h * c.height;

                if (!isChecked) {
                    // Unchecked: bright cutout — redraw original ref in this zone
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x, y, w, h);
                    ctx.clip();
                    ctx.drawImage(refImg, 0, 0, c.width, c.height);
                    ctx.restore();
                    // Bright border + pulsing highlight
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 3;
                    ctx.strokeRect(x, y, w, h);
                    ctx.fillStyle = color + '30';
                    ctx.fillRect(x, y, w, h);
                } else {
                    // Checked: dim fill with status color
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x, y, w, h);
                    ctx.fillStyle = color + '33';
                    ctx.fillRect(x, y, w, h);
                }

                ctx.fillStyle = isChecked ? color + 'aa' : '#fff';
                ctx.font = `bold ${isChecked ? 9 : 11}px sans-serif`;
                ctx.fillText(z.label, x + 3, y + 13);
                if (isChecked) {
                    const mark = dStatus === 'defect' ? '✗' : dStatus === 'warn' ? '⚡' : '✓';
                    ctx.fillStyle = color;
                    ctx.font = 'bold 14px sans-serif';
                    ctx.fillText(mark, x + w - 16, y + 15);
                }
            });
        }

        /* ─── Serial number detection ─── */
        let currentSerial = null;
        let mobileToken = null;
        let mobilePollTimer = null;

        function unlockZoneCheck() {
            const zca = document.getElementById('zone-check-area');
            zca.style.opacity = '1';
            zca.style.pointerEvents = 'auto';
        }

        // Generate QR code when entering step 3
        async function generateMobileQR() {
            if (!sessionId) return;
            try {
                const r = await fetch(`/api/session/${sessionId}/mobile_qr`, { method: 'POST' });
                const d = await r.json();
                if (d.error) return;
                mobileToken = d.token;

                const qrImg = document.getElementById('qr-image');
                qrImg.src = 'data:image/png;base64,' + d.qr_b64;
                qrImg.classList.remove('hidden');
                document.getElementById('qr-loading').classList.add('hidden');

                const linkWrap = document.getElementById('mobile-link-wrap');
                const link = document.getElementById('mobile-link');
                link.href = d.url;
                link.textContent = d.url;
                linkWrap.classList.remove('hidden');

                // Start polling for mobile updates
                startMobilePoll();
            } catch (e) {
                document.getElementById('qr-loading').innerHTML = '<span style="font-size:12px;color:var(--muted)">QR unavailable</span>';
            }
        }

        function startMobilePoll() {
            if (mobilePollTimer) return;
            mobilePollTimer = setInterval(pollMobileUpdates, 5000);
        }

        function stopMobilePoll() {
            if (mobilePollTimer) {
                clearInterval(mobilePollTimer);
                mobilePollTimer = null;
            }
        }

        async function pollMobileUpdates() {
            if (!sessionId) return;
            try {
                const r = await fetch(`/api/session/${sessionId}/mobile_photos`);
                if (!r.ok) {
                    if (r.status === 404) {
                        stopMobilePoll();
                        // Session lost (server restarted) — auto-recover
                        if (currentTemplateId) {
                            toast('Session lost — reconnecting...', 'info');
                            await loadTemplate(currentTemplateId);
                        } else {
                            toast('Session expired — please reload template', 'error');
                        }
                    }
                    return;
                }
                const d = await r.json();
                if (d.error) return;

                const statusEl = document.getElementById('mobile-poll-status');

                // Check if serial was set from mobile
                if (d.serial && !currentSerial) {
                    currentSerial = d.serial;
                    document.getElementById('serial-value').textContent = d.serial;
                    document.getElementById('serial-type').textContent = `(${d.serial_type})`;
                    document.getElementById('serial-result').classList.remove('hidden');
                    // Mask validation
                    const mw = document.getElementById('serial-mask-warn');
                    if (currentBarcodeMask && !d.serial.startsWith(currentBarcodeMask)) {
                        mw.classList.remove('hidden');
                        mw.textContent = `⚠ Expected ${currentBarcodeMask}*`;
                    } else { mw.classList.add('hidden'); }
                    unlockZoneCheck();
                    statusEl.textContent = '✅ Serial received from mobile';
                }

                // Auto-process mobile photos as they arrive
                if (d.count > 0) {
                    const banner = document.getElementById('mobile-photos-banner');
                    banner.classList.remove('hidden');
                    document.getElementById('mobile-photos-text').textContent = `${d.count} mobile photos — processing…`;
                    await processMobilePhotos();
                }
            } catch (e) { /* ignore poll errors */ }
        }

        // Process mobile photos one by one
        let mobileProcessing = false;
        async function processMobilePhotos() {
            if (mobileProcessing) return;
            mobileProcessing = true;
            const btn = document.getElementById('btn-process-mobile');
            btn.disabled = true;
            btn.textContent = 'Processing…';

            try {
                let hasMore = true;
                let idx = 0;
                while (hasMore) {
                    const zSens = document.getElementById('inp-zone-sens').value;
                    const sSens = document.getElementById('inp-subzone-sens').value;
                    const r = await fetch(`/api/session/${sessionId}/mobile_photos/next?zone_sensitivity=${encodeURIComponent(zSens)}&subzone_sensitivity=${encodeURIComponent(sSens)}`, { method: 'POST' });
                    if (r.status === 404) { hasMore = false; break; }
                    const d = await r.json();
                    if (d.error) { hasMore = false; break; }

                    d.checked_zones.forEach(i => checkedZones.add(i));
                    if (d.matched && d.defect) {
                        zoneDefectStatus[d.best_zone_index] = d.defect.status;
                        zoneResults[d.best_zone_index] = {
                            ssim: d.defect.ssim,
                            defect_pct: d.defect.defect_pct,
                            defect_count: d.defect.defect_count,
                            score: d.best_score,
                            status: d.defect.status,
                            subzones: d.defect.subzones || [],
                            photo_b64: d.photo_b64,
                            extracted_b64: d.defect.extracted_b64,
                            vis_defects_b64: d.defect.vis_defects_b64,
                            vis_heatmap_b64: d.defect.vis_heatmap_b64,
                            verdict: d.defect.verdict,
                            best_zone_label: d.best_zone_label,
                            all_scores: d.all_scores,
                            zone_sensitivity: d.defect.zone_sensitivity,
                            subzone_sensitivity: d.defect.subzone_sensitivity,
                        };
                    }

                    idx++;
                    currentViewZone = d.best_zone_index;
                    const res = document.getElementById('match-result');
                    res.classList.remove('hidden');
                    res.querySelectorAll('.defect-panel').forEach(p => p.remove());
                    const title = document.getElementById('match-title');
                    const prefix = `[MOB ${idx}] `;
                    if (d.matched) {
                        if (d.defect && d.defect.status === 'defect') {
                            title.textContent = `${prefix}DEFECT // ${d.best_zone_label}`;
                            title.style.color = 'var(--danger)';
                        } else if (d.defect && d.defect.status === 'warn') {
                            title.textContent = `${prefix}WARN // ${d.best_zone_label}`;
                            title.style.color = 'var(--warn)';
                        } else {
                            title.textContent = `${prefix}OK // ${d.best_zone_label} (${(d.best_score * 100).toFixed(1)}%)`;
                            title.style.color = 'var(--success)';
                        }
                    } else {
                        title.textContent = `${prefix}NO MATCH`;
                        title.style.color = 'var(--danger)';
                    }

                    const body = document.getElementById('match-body');
                    if (d.defect) {
                        const df = d.defect;
                        const verdictClass = df.status === 'ok' ? 'verdict-ok' : df.status === 'warn' ? 'verdict-warn' : 'verdict-defect';
                        body.innerHTML = `
                            <div><p>Input</p><img src="data:image/jpeg;base64,${d.photo_b64}"></div>
                            <div><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[d.best_zone_index]}"></div>
                            <div><p>Extracted</p><img src="data:image/png;base64,${df.extracted_b64}"></div>
                            <div><p>Defect map</p><img src="data:image/png;base64,${df.vis_defects_b64}"></div>
                            <div><p>Heatmap</p><img src="data:image/png;base64,${df.vis_heatmap_b64}"></div>`;
                        const defPanel = document.createElement('div');
                        defPanel.className = 'defect-panel';
                        defPanel.innerHTML = `
                            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                                <span class="verdict-badge ${verdictClass}">${df.verdict}</span>
                                <div class="defect-stats">
                                    <div class="stat"><div class="num">${(df.ssim * 100).toFixed(1)}%</div><div class="lbl">SSIM</div></div>
                                    <div class="stat"><div class="num">${df.defect_pct.toFixed(2)}%</div><div class="lbl">Defect area</div></div>
                                    <div class="stat"><div class="num">${df.defect_count}</div><div class="lbl">Count</div></div>
                                </div>
                            </div>` + _buildSubzoneHtml(df.subzones, d.best_zone_index);
                        res.appendChild(defPanel);
                    } else {
                        body.innerHTML = `
                            <div><p>Input</p><img src="data:image/jpeg;base64,${d.photo_b64}"></div>
                            <div><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[d.best_zone_index]}"></div>
                            <div></div><div></div><div></div>`;
                    }

                    renderStep3();
                    drawMiniRef();
                    updateStatsDashboard(null);
                    btn.textContent = `Processing… (${idx})`;

                    if (d.progress && d.progress.complete) {
                        await onAllZonesComplete();
                    }
                }

                document.getElementById('mobile-photos-banner').classList.add('hidden');
                if (idx > 0) toast(`Processed ${idx} mobile photos`, 'success');
            } catch (e) { toast(e.message, 'error'); }
            finally {
                mobileProcessing = false;
                btn.disabled = false;
                btn.textContent = 'Process';
            }
        }

        document.getElementById('btn-process-mobile').addEventListener('click', () => processMobilePhotos());

        setupDZ('dz-serial', 'file-serial', async files => {
            const file = files[0];
            const status = document.getElementById('serial-status');
            const spin = document.getElementById('spin-serial');
            const stxt = document.getElementById('serial-text');
            const sres = document.getElementById('serial-result');
            const serr = document.getElementById('serial-error');

            sres.classList.add('hidden');
            serr.classList.add('hidden');
            status.classList.remove('hidden');
            spin.classList.remove('hidden');
            stxt.textContent = 'Reading code…';

            try {
                const fd = new FormData();
                fd.append('photo', file);
                const r = await fetch(`/api/session/${sessionId}/serial`, { method: 'POST', body: fd });
                const d = await r.json();
                if (d.error) {
                    serr.textContent = d.error;
                    serr.classList.remove('hidden');
                    stxt.textContent = 'Error';
                    spin.classList.add('hidden');
                    return;
                }
                currentSerial = d.serial;
                document.getElementById('serial-value').textContent = d.serial;
                document.getElementById('serial-type').textContent = `(${d.type})`;
                sres.classList.remove('hidden');
                status.classList.add('hidden');

                // Mask validation
                const mw = document.getElementById('serial-mask-warn');
                if (currentBarcodeMask && !d.serial.startsWith(currentBarcodeMask)) {
                    mw.classList.remove('hidden');
                    mw.textContent = `⚠ Expected ${currentBarcodeMask}*`;
                } else { mw.classList.add('hidden'); }

                // Unlock zone-check-area
                unlockZoneCheck();
            } catch (e) {
                serr.textContent = e.message;
                serr.classList.remove('hidden');
                stxt.textContent = 'Error';
                spin.classList.add('hidden');
            }
        });

        let checkFiles = [];  // Array of files for batch processing
        setupDZ('dz-check', 'file-check', files => {
            checkFiles = Array.from(files);
            document.getElementById('btn-check').disabled = false;
            const label = checkFiles.length === 1
                ? checkFiles[0].name
                : `${checkFiles.length} photos selected`;
            document.querySelector('#dz-check .dropzone-label').textContent = label;
            document.getElementById('check-queue').textContent =
                checkFiles.length > 1 ? `Queue: ${checkFiles.length} photos` : '';
        });

        /* ─── Process one photo and show result ─── */
        async function processOnePhoto(file, fileIdx, totalFiles) {
            const fd = new FormData();
            fd.append('photo', file);
            fd.append('zone_sensitivity', document.getElementById('inp-zone-sens').value);
            fd.append('subzone_sensitivity', document.getElementById('inp-subzone-sens').value);
            const r = await fetch(`/api/session/${sessionId}/check`, { method: 'POST', body: fd });
            const d = await r.json();
            if (d.error) { toast(d.error, 'error'); return; }

            // Update checked zones
            d.checked_zones.forEach(i => checkedZones.add(i));
            if (d.matched && d.defect) {
                zoneDefectStatus[d.best_zone_index] = d.defect.status;
                zoneResults[d.best_zone_index] = {
                    ssim: d.defect.ssim,
                    defect_pct: d.defect.defect_pct,
                    defect_count: d.defect.defect_count,
                    score: d.best_score,
                    status: d.defect.status,
                    subzones: d.defect.subzones || [],
                    photo_b64: d.photo_b64,
                    extracted_b64: d.defect.extracted_b64,
                    vis_defects_b64: d.defect.vis_defects_b64,
                    vis_heatmap_b64: d.defect.vis_heatmap_b64,
                    verdict: d.defect.verdict,
                    best_zone_label: d.best_zone_label,
                    all_scores: d.all_scores,
                    zone_sensitivity: d.defect.zone_sensitivity,
                    subzone_sensitivity: d.defect.subzone_sensitivity,
                };
            }

            // Show result
            currentViewZone = d.best_zone_index;
            const res = document.getElementById('match-result');
            res.classList.remove('hidden');
            res.querySelectorAll('.defect-panel').forEach(p => p.remove());
            const title = document.getElementById('match-title');
            const prefix = totalFiles > 1 ? `[${fileIdx + 1}/${totalFiles}] ` : '';
            if (d.matched) {
                if (d.defect && d.defect.status === 'defect') {
                    title.textContent = `${prefix}DEFECT // ${d.best_zone_label}`;
                    title.style.color = 'var(--danger)';
                    toast(`${d.best_zone_label} — DEFECT`, 'error');
                } else if (d.defect && d.defect.status === 'warn') {
                    title.textContent = `${prefix}WARN // ${d.best_zone_label}`;
                    title.style.color = 'var(--warn)';
                    toast(`${d.best_zone_label} — WARN`, 'info');
                } else {
                    title.textContent = `${prefix}OK // ${d.best_zone_label} (${(d.best_score * 100).toFixed(1)}%)`;
                    title.style.color = 'var(--success)';
                    toast(`${d.best_zone_label} — OK`, 'success');
                }
            } else {
                title.textContent = `${prefix}NO MATCH // best: ${d.best_zone_label} (${(d.best_score * 100).toFixed(1)}%)`;
                title.style.color = 'var(--danger)';
                toast('No match found', 'error');
            }

            // Row 2: all 5 images in a single grid
            const body = document.getElementById('match-body');
            if (d.defect) {
                const df = d.defect;
                const verdictClass = df.status === 'ok' ? 'verdict-ok' : df.status === 'warn' ? 'verdict-warn' : 'verdict-defect';
                body.innerHTML = `
                    <div><p>Input</p><img src="data:image/jpeg;base64,${d.photo_b64}"></div>
                    <div><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[d.best_zone_index]}"></div>
                    <div><p>Extracted</p><img src="data:image/png;base64,${df.extracted_b64}"></div>
                    <div><p>Defect map</p><img src="data:image/png;base64,${df.vis_defects_b64}"></div>
                    <div><p>Heatmap</p><img src="data:image/png;base64,${df.vis_heatmap_b64}"></div>`;
                // Verdict + stats below images
                const defPanel = document.createElement('div');
                defPanel.className = 'defect-panel';
                const sensHtml = df.zone_sensitivity != null
                    ? `<div class="sens-tag">Zone: ${df.zone_sensitivity.toFixed(2)} · Subzone: ${df.subzone_sensitivity != null ? df.subzone_sensitivity.toFixed(2) : '?'}</div>`
                    : '';
                defPanel.innerHTML = `
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <span class="verdict-badge ${verdictClass}">${df.verdict}</span>
                        <div class="defect-stats">
                            <div class="stat"><div class="num">${(df.ssim * 100).toFixed(1)}%</div><div class="lbl">SSIM</div></div>
                            <div class="stat"><div class="num">${df.defect_pct.toFixed(2)}%</div><div class="lbl">Defect area</div></div>
                            <div class="stat"><div class="num">${df.defect_count}</div><div class="lbl">Regions</div></div>
                            <div class="stat"><div class="num">${(d.best_score * 100).toFixed(1)}%</div><div class="lbl">Match</div></div>
                        </div>
                        ${sensHtml}
                    </div>` + _buildSubzoneHtml(df.subzones, d.best_zone_index);
                res.appendChild(defPanel);
            } else {
                body.innerHTML = `
                    <div><p>Input</p><img src="data:image/jpeg;base64,${d.photo_b64}"></div>
                    <div><p>Reference</p><img src="data:image/jpeg;base64,${zonePreviews[d.best_zone_index]}"></div>
                    <div></div><div></div><div></div>`;
            }

            const scDiv = document.getElementById('all-scores');
            scDiv.innerHTML = '<p style="color:var(--muted);font-size:12px;margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px">All zone scores:</p>';
            d.all_scores.forEach((sc, i) => {
                const pct = (sc * 100).toFixed(1);
                const color = i === d.best_zone_index ? 'var(--accent)' : 'var(--muted)';
                scDiv.innerHTML += `<div class="score-bar">
            <span style="font-size:12px;min-width:50px;color:${color}">${zones[i].label}</span>
            <div class="bar-bg"><div class="bar-fg" style="width:${pct}%;background:${color}"></div></div>
            <span class="score-val">${pct}%</span></div>`;
            });

            renderStep3();
            drawMiniRef();
            updateStatsDashboard(d.all_scores);

            if (d.progress.complete) {
                await onAllZonesComplete();
            }
        }

        /* ─── Completion logic: save result, decide next action ─── */
        async function onAllZonesComplete() {
            // Save result to server (with user decision overrides)
            const payload = {};
            if (Object.keys(userDecisions).length) payload.user_decisions = userDecisions;
            if (Object.keys(userSubDecisions).length) payload.user_sub_decisions = userSubDecisions;
            const body = Object.keys(payload).length ? JSON.stringify(payload) : null;
            try {
                const sr = await fetch(`/api/session/${sessionId}/save_result`, {
                    method: 'POST',
                    headers: body ? { 'Content-Type': 'application/json' } : {},
                    body
                });
                const sd = await sr.json();
                console.log('Result saved:', sd);
                if (sd.result_id) lastResultId = sd.result_id;
                loadHistory();
                resultSaved = true;
            } catch (e) { console.warn('Save failed:', e); }

            // Check overall using effective decisions (user override > subzone overrides > auto)
            const allOk = zones.every((_, i) => {
                if (userDecisions[i]) return userDecisions[i] === 'ok';
                if (zoneResults[i]?.subzones?.length && userSubDecisions[i]) {
                    return zoneResults[i].subzones.every((sz, si) => ((userSubDecisions[i] || {})[si] || sz.status) === 'ok');
                }
                return zoneDefectStatus[i] === 'ok';
            });
            const banner = document.getElementById('complete-banner');
            const bannerTitle = document.getElementById('complete-banner-title');
            const bannerSub = document.getElementById('complete-banner-sub');
            const sib = document.getElementById('serial-in-banner');
            banner.classList.remove('hidden');

            if (currentSerial) {
                sib.textContent = `Serial: ${currentSerial}`;
                sib.classList.remove('hidden');
            }

            if (allOk) {
                if (autoAccept) {
                    // Auto-advance: new board in 3s
                    banner.style.borderColor = 'var(--success)';
                    banner.style.background = 'var(--success-dim)';
                    bannerTitle.textContent = 'ALL ZONES OK ✓';
                    bannerTitle.style.color = 'var(--success)';
                    bannerSub.textContent = 'Result saved. Starting new board in 3s...';
                    toast('All zones OK — result saved', 'success');
                    document.getElementById('inspection-action-btns').classList.add('hidden');
                    setTimeout(() => startNewBoard(), 3000);
                } else {
                    // Manual mode — show banner but wait for user action
                    banner.style.borderColor = 'var(--success)';
                    banner.style.background = 'var(--success-dim)';
                    bannerTitle.textContent = 'ALL ZONES OK ✓';
                    bannerTitle.style.color = 'var(--success)';
                    bannerSub.textContent = 'Result saved. Auto-accept OFF — review and click Skip/Close & Save.';
                    toast('All zones OK — manual review mode', 'info');
                    document.getElementById('inspection-action-btns').classList.remove('hidden');
                }
            } else {
                // Has issues — show action buttons
                banner.style.borderColor = 'var(--warn)';
                banner.style.background = 'var(--warn-dim)';
                bannerTitle.textContent = 'INSPECTION COMPLETE — ISSUES FOUND';
                bannerTitle.style.color = 'var(--warn)';
                bannerSub.textContent = 'Result saved. Review zones and choose action.';
                toast('Inspection complete — issues found', 'info');
                document.getElementById('inspection-action-btns').classList.remove('hidden');
            }
        }

        async function startNewBoard() {
            // Fire server reset in background (don't block UI)
            fetch(`/api/session/${sessionId}/reset`, { method: 'POST' }).catch(() => {});

            // Reset frontend state immediately
            checkedZones = new Set();
            zoneDefectStatus = {};
            zoneResults = {};
            userDecisions = {};
            userSubDecisions = {};
            resultSaved = false;
            lastResultId = null;
            currentSerial = null;

            // Reset serial UI
            document.getElementById('serial-result').classList.add('hidden');
            document.getElementById('serial-value').textContent = '';
            document.getElementById('serial-type').textContent = '';
            document.getElementById('serial-error').classList.add('hidden');
            document.getElementById('serial-status').classList.add('hidden');
            document.getElementById('serial-mask-warn').classList.add('hidden');
            document.getElementById('serial-in-banner').textContent = '';
            document.getElementById('serial-in-banner').classList.add('hidden');
            document.getElementById('file-serial').value = '';
            document.querySelector('#dz-serial .dropzone-label').textContent = 'Serial number photo';

            // Lock zone-check-area again
            const zca = document.getElementById('zone-check-area');
            zca.style.opacity = '0.3';
            zca.style.pointerEvents = 'none';

            // Hide banners and results
            document.getElementById('complete-banner').classList.add('hidden');
            document.getElementById('match-result').classList.add('hidden');
            document.getElementById('stats-panel').classList.add('hidden');
            document.getElementById('inspection-action-btns').classList.add('hidden');
            document.getElementById('mobile-photos-banner').classList.add('hidden');
            document.getElementById('mobile-poll-status').textContent = '';

            renderStep3();
            drawMiniRef();
            toast('Ready for new board — scan serial number', 'info');
        }

        /* ─── Check button: process all selected photos sequentially ─── */
        document.getElementById('btn-check').addEventListener('click', async () => {
            if (!checkFiles.length) return;
            const btn = document.getElementById('btn-check');
            const spin = document.getElementById('spin-check');
            const queueDiv = document.getElementById('check-queue');
            btn.disabled = true; spin.classList.remove('hidden');

            const files = [...checkFiles];
            try {
                for (let i = 0; i < files.length; i++) {
                    if (files.length > 1) {
                        queueDiv.textContent = `Processing ${i + 1} of ${files.length}...`;
                    }
                    await processOnePhoto(files[i], i, files.length);
                }
            } catch (e) { toast(e.message, 'error'); }
            finally {
                btn.disabled = false; spin.classList.add('hidden');
                checkFiles = [];
                document.getElementById('file-check').value = '';
                document.querySelector('#dz-check .dropzone-label').textContent = 'Drop zone photos here (multiple OK)';
                queueDiv.textContent = '';
            }
        });

        document.getElementById('btn-reset-checks').addEventListener('click', async () => {
            if (!sessionId) return;
            await startNewBoard();
        });

        // Auto-accept toggle
        document.getElementById('chk-auto-accept').addEventListener('change', (e) => {
            autoAccept = e.target.checked;
            if (sessionId) fetch(`/api/session/${sessionId}/auto_accept`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ auto_accept: autoAccept })
            }).catch(() => {});
        });

        // Delegated handler for subzone decision selects in match-result panel
        document.getElementById('match-result').addEventListener('change', (e) => {
            if (!e.target.classList.contains('sz-decision')) return;
            const zi = parseInt(e.target.dataset.zone);
            const si = parseInt(e.target.dataset.sz);
            const val = e.target.value;
            const origStatus = (zoneResults[zi]?.subzones || [])[si]?.status;
            console.log('[SZ-DECISION] zone=', zi, 'sub=', si, 'val=', val, 'origStatus=', origStatus);
            if (!userSubDecisions[zi]) userSubDecisions[zi] = {};
            if (val === origStatus) delete userSubDecisions[zi][si];
            else userSubDecisions[zi][si] = val;
            if (!Object.keys(userSubDecisions[zi]).length) delete userSubDecisions[zi];
            console.log('[SZ-DECISION] userSubDecisions=', JSON.stringify(userSubDecisions), 'userDecisions=', JSON.stringify(userDecisions));
            resultSaved = false;
            // Update the label next to the dropdown
            const row = e.target.closest('div');
            const badge = row?.querySelector('.verdict-badge');
            const calcSpan = row?.querySelector('.calc-status');
            if (badge) {
                const effective = val;
                const vc = effective === 'ok' ? 'verdict-ok' : effective === 'warn' ? 'verdict-warn' : 'verdict-defect';
                badge.className = `verdict-badge ${vc}`;
                const szLabel = (zoneResults[zi]?.subzones || [])[si]?.label || `S${si+1}`;
                badge.textContent = `${szLabel}: ${effective.toUpperCase()}`;
            }
            if (calcSpan) {
                if (val !== origStatus) {
                    calcSpan.style.display = '';
                } else {
                    calcSpan.style.display = 'none';
                }
            } else if (val !== origStatus) {
                // Insert calc-status span after badge
                if (badge) {
                    const cs = document.createElement('span');
                    cs.className = 'calc-status';
                    cs.style.fontSize = '10px';
                    cs.style.color = 'var(--muted)';
                    cs.style.textDecoration = 'line-through';
                    cs.style.marginLeft = '4px';
                    cs.textContent = (origStatus || '').toUpperCase();
                    badge.after(cs);
                }
            }
            // Update zone chips to reflect subzone override effect
            renderStep3();
        });

        // Delegated click handler for images in match-result → open lightbox
        document.getElementById('match-result').addEventListener('click', (e) => {
            const img = e.target.closest('#match-body img, .defect-panel img');
            if (!img || currentViewZone == null) return;
            if (!zoneResults[currentViewZone]) return;
            // Check if it's a subzone thumbnail (has data-sz-thumb on parent div)
            const szThumbDiv = img.closest('[data-sz-thumb]');
            if (szThumbDiv) {
                const szIdx = parseInt(szThumbDiv.dataset.szThumb);
                openInspectLightbox(currentViewZone, szIdx);
                return;
            }
            // Zone-level image click
            openInspectLightbox(currentViewZone, null);
        });

        // Close & Save — save whatever we have now, return to SN
        document.getElementById('btn-close-save').addEventListener('click', async () => {
            const btn = document.getElementById('btn-close-save');
            if (!sessionId) return;
            console.log('[CLOSE-SAVE] checkedZones=', [...checkedZones], 'lastResultId=', lastResultId, 'resultSaved=', resultSaved);
            console.log('[CLOSE-SAVE] userDecisions=', JSON.stringify(userDecisions), 'userSubDecisions=', JSON.stringify(userSubDecisions));
            if (checkedZones.size === 0) {
                toast('No zones checked — nothing to save', 'warn');
                return;
            }
            btn.disabled = true;
            const origText = btn.textContent;
            btn.textContent = 'Saving...';
            // If already saved, update existing record; otherwise create new
            if (lastResultId) {
                // update_result doesn't depend on session state, safe to fire-and-forget
                const payload2 = { result_id: lastResultId };
                if (Object.keys(userDecisions).length) payload2.user_decisions = userDecisions;
                if (Object.keys(userSubDecisions).length) payload2.user_sub_decisions = userSubDecisions;
                fetch(`/api/session/${sessionId}/update_result`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload2)
                }).then(r => r.json()).then(sd => {
                    console.log('Result updated:', sd);
                    loadHistory();
                    toast('Decisions updated', 'success');
                }).catch(e => toast('Update failed: ' + e.message, 'error'));
                startNewBoard();
            } else {
                // save_result reads session.checked — must complete BEFORE reset
                const payload2 = {};
                if (Object.keys(userDecisions).length) payload2.user_decisions = userDecisions;
                if (Object.keys(userSubDecisions).length) payload2.user_sub_decisions = userSubDecisions;
                const body = Object.keys(payload2).length ? JSON.stringify(payload2) : null;
                const savedCount = checkedZones.size;
                const totalCount = zones.length;
                try {
                    const resp = await fetch(`/api/session/${sessionId}/save_result`, {
                        method: 'POST',
                        headers: body ? { 'Content-Type': 'application/json' } : {},
                        body
                    });
                    const sd = await resp.json();
                    console.log('Partial result saved:', sd);
                    loadHistory();
                    toast(`Result saved (${savedCount}/${totalCount} zones)`, 'success');
                } catch(e) { toast('Save failed: ' + e.message, 'error'); }
                startNewBoard();
            }
            btn.disabled = false;
            btn.textContent = origText;
        });

        // Skip Board — save result as-is, move to new board
        document.getElementById('btn-skip-board').addEventListener('click', async () => {
            const btn = document.getElementById('btn-skip-board');
            if (!sessionId) return;
            btn.disabled = true;
            btn.textContent = 'Skipping...';
            toast('Board skipped — starting new board', 'info');
            await startNewBoard();
            btn.disabled = false;
            btn.textContent = 'Skip Board';
        });

        // Retry Failed Zones — reset only non-OK zones
        document.getElementById('btn-retry-board').addEventListener('click', async () => {
            const btn = document.getElementById('btn-retry-board');
            if (!sessionId) return;
            btn.disabled = true;
            btn.textContent = 'Retrying...';
            // Clear only non-OK zones from checked
            const toRetry = [];
            zones.forEach((_, i) => {
                if (zoneDefectStatus[i] && zoneDefectStatus[i] !== 'ok') {
                    toRetry.push(i);
                    checkedZones.delete(i);
                    delete zoneDefectStatus[i];
                    delete zoneResults[i];
                }
            });
            // Clear only non-OK zones on server (no board_seq bump → mobile stays in zone mode)
            try {
                await fetch(`/api/session/${sessionId}/retry_failed`, { method: 'POST' });
            } catch (e) { /* ok */ }

            document.getElementById('complete-banner').classList.add('hidden');
            document.getElementById('inspection-action-btns').classList.add('hidden');
            document.getElementById('match-result').classList.add('hidden');

            renderStep3();
            drawMiniRef();
            toast(`Retrying ${toRetry.length} zone(s)`, 'info');
            btn.disabled = false;
            btn.textContent = 'Retry Failed Zones';
        });

        /* ═══════════════════════════════════════════════════════════════════════════ */
        /*  STATISTICS DASHBOARD                                                       */
        /* ═══════════════════════════════════════════════════════════════════════════ */
        const chartColors = {
            ok: 'rgba(46, 160, 67, .8)',
            warn: 'rgba(210, 153, 34, .8)',
            defect: 'rgba(248, 81, 73, .8)',
            neutral: 'rgba(59, 130, 246, .6)',
            border_ok: '#2ea043',
            border_warn: '#d29922',
            border_defect: '#f85149',
            border_neutral: '#3b82f6',
            grid: 'rgba(33, 38, 45, .8)',
            text: '#6e7681',
        };

        const chartDefaults = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: chartColors.text, font: { family: "'JetBrains Mono'", size: 8 } }, grid: { color: chartColors.grid } },
                y: { ticks: { color: chartColors.text, font: { family: "'JetBrains Mono'", size: 8 } }, grid: { color: chartColors.grid }, beginAtZero: true }
            }
        };

        function updateStatsDashboard(allScores) {
            const panel = document.getElementById('stats-panel');
            if (checkedZones.size === 0) { panel.classList.add('hidden'); return; }
            panel.classList.remove('hidden');

            // Summary counters
            let okCount = 0, warnCount = 0, defectCount = 0;
            let ssimSum = 0, defPctSum = 0, ssimCount = 0;
            Object.values(zoneResults).forEach(r => {
                if (r.status === 'ok') okCount++;
                else if (r.status === 'warn') warnCount++;
                else if (r.status === 'defect') defectCount++;
                if (r.ssim !== undefined) { ssimSum += r.ssim; ssimCount++; }
                if (r.defect_pct !== undefined) defPctSum += r.defect_pct;
            });
            document.querySelector('#stat-total .stat-value').textContent = checkedZones.size;
            document.querySelector('#stat-ok .stat-value').textContent = okCount;
            document.querySelector('#stat-warn .stat-value').textContent = warnCount;
            document.querySelector('#stat-defect .stat-value').textContent = defectCount;
            document.querySelector('#stat-avg-ssim .stat-value').textContent = ssimCount > 0 ? (ssimSum / ssimCount * 100).toFixed(1) + '%' : '—';
            document.querySelector('#stat-avg-defect .stat-value').textContent = ssimCount > 0 ? (defPctSum / ssimCount).toFixed(1) + '%' : '—';

            // Labels & data
            const labels = zones.map((z, i) => z.label);
            const scoreData = allScores ? allScores.map(s => +(s * 100).toFixed(1)) : zones.map(() => 0);
            const defData = zones.map((_, i) => zoneResults[i] ? +zoneResults[i].defect_pct.toFixed(2) : 0);
            const ssimData = zones.map((_, i) => zoneResults[i] ? +(zoneResults[i].ssim * 100).toFixed(1) : 0);
            const barColors = zones.map((_, i) => {
                const s = zoneDefectStatus[i];
                return s === 'ok' ? chartColors.ok : s === 'warn' ? chartColors.warn : s === 'defect' ? chartColors.defect : chartColors.neutral;
            });
            const barBorders = zones.map((_, i) => {
                const s = zoneDefectStatus[i];
                return s === 'ok' ? chartColors.border_ok : s === 'warn' ? chartColors.border_warn : s === 'defect' ? chartColors.border_defect : chartColors.border_neutral;
            });

            // ── Scores chart ──
            if (chartScores) chartScores.destroy();
            chartScores = new Chart(document.getElementById('chart-scores'), {
                type: 'bar',
                data: { labels, datasets: [{ data: scoreData, backgroundColor: barColors, borderColor: barBorders, borderWidth: 1 }] },
                options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, max: 100, ticks: { ...chartDefaults.scales.y.ticks, callback: v => v + '%' } } } }
            });

            // ── Defect % chart ──
            if (chartDefects) chartDefects.destroy();
            const defBarColors = defData.map(v => v < 6.5 ? chartColors.ok : v < 17 ? chartColors.warn : chartColors.defect);
            const defBarBorders = defData.map(v => v < 6.5 ? chartColors.border_ok : v < 17 ? chartColors.border_warn : chartColors.border_defect);
            chartDefects = new Chart(document.getElementById('chart-defects'), {
                type: 'bar',
                data: { labels, datasets: [{ data: defData, backgroundColor: defBarColors, borderColor: defBarBorders, borderWidth: 1 }] },
                options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => v + '%' } } } }
            });

            // ── SSIM chart ──
            if (chartSsim) chartSsim.destroy();
            const ssimBarColors = ssimData.map(v => v >= 45 ? chartColors.ok : v >= 30 ? chartColors.warn : chartColors.defect);
            const ssimBarBorders = ssimData.map(v => v >= 45 ? chartColors.border_ok : v >= 30 ? chartColors.border_warn : chartColors.border_defect);
            chartSsim = new Chart(document.getElementById('chart-ssim'), {
                type: 'bar',
                data: { labels, datasets: [{ data: ssimData, backgroundColor: ssimBarColors, borderColor: ssimBarBorders, borderWidth: 1 }] },
                options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, max: 100, ticks: { ...chartDefaults.scales.y.ticks, callback: v => v + '%' } } } }
            });

            // ── Data table ──
            const tbody = document.getElementById('stats-tbody');
            tbody.innerHTML = '';
            zones.forEach((z, i) => {
                const r = zoneResults[i];
                const tr = document.createElement('tr');
                if (!r) {
                    tr.innerHTML = `<td>${z.label}</td><td class="td-muted">—</td><td class="td-muted">—</td><td class="td-muted">—</td><td class="td-muted">—</td><td class="td-muted">—</td>`;
                } else {
                    const sc = r.status === 'ok' ? 'td-ok' : r.status === 'warn' ? 'td-warn' : 'td-danger';
                    const statusText = r.status === 'ok' ? 'OK' : r.status === 'warn' ? 'WARN' : 'DEFECT';
                    tr.innerHTML = `<td>${z.label}</td><td class="${sc}">${statusText}</td><td>${(r.score * 100).toFixed(1)}%</td><td>${(r.ssim * 100).toFixed(1)}%</td><td>${r.defect_pct.toFixed(2)}%</td><td>${r.defect_count}</td>`;
                }
                tbody.appendChild(tr);
            });
        }

        /* ═══════════════════════════════════════════════════════════════════════════ */
        /*  TEMPLATES                                                                      */
        /* ═══════════════════════════════════════════════════════════════════════════ */
        async function loadTemplateList() {
            try {
                const r = await fetch('/api/templates');
                const d = await r.json();
                const el = document.getElementById('tpl-list');
                if (!d.templates.length) {
                    el.innerHTML = '<p style="color:var(--muted);font-size:12px">No saved templates. Upload a board and define zones to create one.</p>';
                    return;
                }
                el.innerHTML = '';
                d.templates.forEach(t => {
                    const card = document.createElement('div');
                    card.className = 'tpl-card';
                    const maskBadge = t.barcode_mask ? `<span style="background:var(--accent-dim);color:var(--accent);padding:0 4px;border-radius:2px;font-size:11px;font-family:var(--mono)">${t.barcode_mask}*</span>` : '';
                    const verBadge = t.version > 1 ? `<span style="color:var(--muted);font-size:11px">v${t.version}</span>` : '';
                    card.innerHTML = `
                <div style="display:flex;gap:4px;align-items:center"><span class="tpl-name">${t.name}</span>${maskBadge}${verBadge}</div>
                <span class="tpl-meta">Zones: ${t.zone_count} • ${t.created ? t.created.slice(0, 10) : ''}</span>
                <div class="tpl-actions">
                    <button class="tpl-btn load" data-id="${t.id}">Load</button>
                    <button class="tpl-btn edit" data-id="${t.id}" data-name="${t.name}" data-mask="${t.barcode_mask || ''}">Edit</button>
                    <button class="tpl-btn del" data-id="${t.id}">Delete</button>
                </div>`;
                    el.appendChild(card);
                });
                // Event delegation
                el.querySelectorAll('.tpl-btn.load').forEach(btn => {
                    btn.addEventListener('click', e => { e.stopPropagation(); loadTemplate(btn.dataset.id); });
                });
                el.querySelectorAll('.tpl-btn.edit').forEach(btn => {
                    btn.addEventListener('click', e => {
                        e.stopPropagation();
                        editTemplate(btn.dataset.id, btn.dataset.name, btn.dataset.mask);
                    });
                });
                el.querySelectorAll('.tpl-btn.del').forEach(btn => {
                    btn.addEventListener('click', async e => {
                        e.stopPropagation();
                        if (!confirm('Delete template?')) return;
                        await fetch(`/api/templates/${btn.dataset.id}`, { method: 'DELETE' });
                        toast('Template deleted', 'info');
                        loadTemplateList();
                    });
                });
            } catch (e) { console.error(e); }
        }

        async function editTemplate(tid, currentName, currentMask) {
            openTemplateEditor(tid);
        }

        async function loadTemplate(tid) {
            try {
                const r = await fetch(`/api/templates/${tid}`);
                const d = await r.json();
                if (d.error) { toast(d.error, 'error'); return; }

                sessionId = d.session_id;
                zones = d.zones;
                zonePreviews = d.previews;
                checkedZones = new Set();
                zoneDefectStatus = {};
                currentTemplateName = d.template_name || null;
                currentTemplateId = tid;
                currentBarcodeMask = d.barcode_mask || null;

                currentTemplateName = d.template_name || null;
                currentTemplateId = tid;
                currentBarcodeMask = d.barcode_mask || null;

                // Load reference image
                refImg = new Image();
                refImg.onload = () => {
                    // Go directly to step 3 (zones are already set)
                    goStep(3);
                    renderStep3();
                    requestAnimationFrame(drawMiniRef);
                    generateMobileQR();
                    // Sync auto-accept to session
                    fetch(`/api/session/${sessionId}/auto_accept`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ auto_accept: autoAccept })
                    }).catch(() => {});
                    // Show recipe badge
                    if (currentTemplateName) {
                        document.getElementById('recipe-badge-name').textContent = currentTemplateName;
                        document.getElementById('recipe-badge-mask').textContent = currentBarcodeMask ? `(mask: ${currentBarcodeMask}*)` : '';
                        document.getElementById('recipe-badge').classList.remove('hidden');
                    }
                    toast(`Template "${d.template_name}" loaded — ${zones.length} zones`, 'success');
                };
                refImg.src = 'data:image/jpeg;base64,' + d.image_b64;
            } catch (e) { toast(e.message, 'error'); }
        }

        // Save template button
        document.getElementById('btn-save-tpl').addEventListener('click', async () => {
            const name = document.getElementById('tpl-name-input').value.trim();
            if (!name) { toast('Enter template name', 'error'); return; }
            if (!sessionId || !zones.length) { toast('Define zones first', 'error'); return; }
            try {
                const mask = document.getElementById('tpl-mask-input').value.trim();
                const r = await fetch('/api/templates', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: sessionId, name, zones, barcode_mask: mask || null }),
                });
                const d = await r.json();
                if (d.error) { toast(d.error, 'error'); return; }
                toast(`Template "${name}" saved!`, 'success');
                document.getElementById('tpl-name-input').value = '';
            } catch (e) { toast(e.message, 'error'); }
        });

        // Enable save-template button when zones exist and name is typed
        document.getElementById('tpl-name-input').addEventListener('input', () => {
            document.getElementById('btn-save-tpl').disabled = !document.getElementById('tpl-name-input').value.trim() || !zones.length;
        });

        // Load template list on page load
        loadTemplateList();

        // ═══════════════════════════════════════════════════════════════════
        //  INSPECTION HISTORY
        // ═══════════════════════════════════════════════════════════════════

        // Toggle panel open/close on header click
        document.getElementById('history-header').addEventListener('click', () => {
            document.getElementById('history-panel').classList.toggle('collapsed');
        });

        const HISTORY_PAGE_SIZE = 20;
        let _historyTotal = 0;
        let _searchQuery = '';
        let _searchTimer = null;

        async function loadHistory(append) {
            try {
                const offset = append ? _historyData.length : 0;
                let url = `/api/results?limit=${HISTORY_PAGE_SIZE}&offset=${offset}`;
                if (_searchQuery) url += `&q=${encodeURIComponent(_searchQuery)}`;
                const r = await fetch(url);
                const d = await r.json();
                const items = d.results || [];
                _historyTotal = d.total || items.length;
                if (append) {
                    _historyData = _historyData.concat(items);
                    appendHistoryRows(items);
                } else {
                    _historyData = items;
                    renderHistory(items);
                }
                updateHistoryCount();
                updateLoadMoreBtn();
            } catch (e) { console.warn('History load error:', e); }
        }

        function updateHistoryCount() {
            const countEl = document.getElementById('history-count');
            if (_historyData.length < _historyTotal) {
                countEl.textContent = `(${_historyData.length} of ${_historyTotal})`;
            } else if (_historyTotal > 0) {
                countEl.textContent = `(${_historyTotal})`;
            } else {
                countEl.textContent = '';
            }
        }

        function updateLoadMoreBtn() {
            let btn = document.getElementById('history-load-more');
            const remaining = _historyTotal - _historyData.length;
            const hasMore = remaining > 0;
            if (!btn) {
                btn = document.createElement('button');
                btn.id = 'history-load-more';
                btn.className = 'btn btn-outline btn-sm';
                btn.style.cssText = 'width:100%;margin-top:6px;padding:6px;font-size:12px';
                btn.addEventListener('click', () => loadHistory(true));
                document.getElementById('history-list').after(btn);
            }
            btn.textContent = `Load more (${remaining} remaining)`;
            btn.classList.toggle('hidden', !hasMore);
        }

        async function clearHistory() {
            if (!confirm('Delete ALL inspection history? This cannot be undone.')) return;
            try {
                await fetch('/api/results', { method: 'DELETE' });
                loadHistory();
            } catch (e) { console.warn('Clear failed:', e); }
        }

        let _historyData = [];
        let _historyCharts = {};

        function _buildHistoryRow(r, q) {
            const serial = r.serial || '';
            const tplName = r.template_name || '';

            const row = document.createElement('div');
            row.className = 'history-row';
            const ts = r.timestamp ? r.timestamp.slice(11, 19) : '';
            const date = r.timestamp ? r.timestamp.slice(5, 10) : '';
            const ov = r.overall_status || 'ok';
            const _zones = r.zones || [];
            const zCount = `${r.zones_checked || 0}/${r.zones_total || 0}`;
            const _sr = {ok: 0, warn: 1, defect: 2};
            const _rs = ['ok', 'warn', 'defect'];
            const _eff = z => {
                if (z.user_decision) return z.user_decision;
                if (z.subzones && z.subzones.length) {
                    let w = 0;
                    for (const sz of z.subzones) { w = Math.max(w, _sr[sz.user_decision || sz.status] ?? 0); }
                    return _rs[w] || z.status;
                }
                return z.status;
            };
            const _ok = _zones.filter(z => _eff(z) === 'ok').length;
            const _def = _zones.filter(z => _eff(z) === 'defect').length;
            const _wrn = _zones.filter(z => _eff(z) === 'warn').length;
            const _avg = _zones.length ? (_zones.reduce((s, z) => s + (z.score || 0), 0) / _zones.length * 100).toFixed(1) : '—';
            const _maxD = _zones.length ? Math.max(..._zones.map(z => z.defect_pct || 0)).toFixed(2) : '—';
            // highlight matching parts from search query
            const qParts = q ? q.split('/').map(p => p.trim()).filter(Boolean) : [];
            const serialDisp = _hl(serial || '—', qParts[0] || '');
            const tplDisp = tplName ? `<span style="color:var(--accent);font-size:11px;margin-left:2px">[${_hl(tplName, qParts[1] || '')}]</span>` : '';
            const opDisp = r.operator ? `<span style="color:#8b949e;font-size:11px;margin-left:4px">${_hl(r.operator, qParts[2] || '')}</span>` : '';
            // Check if any zone/subzone has user overrides
            const _hasAnyOverride = _zones.some(z =>
                z.user_decision ||
                (z.subzones && z.subzones.some(sz => sz.user_decision))
            );
            row.innerHTML = `
                <div class="hr-summary">
                    <span class="hr-time">${date} ${ts}</span>
                    <span class="hr-serial">${serialDisp}${tplDisp}${opDisp}</span>
                    <span class="hr-zones-inline">${zCount}</span>
                    <span class="hr-badge ${ov}">${ov}</span>${_hasAnyOverride ? '<span class="hd-override-icon" title="Has user decision overrides">✎</span>' : ''}
                    <span class="hr-inline-stats">
                        <span class="is-sep">|</span>
                        <span class="is-val is-ok">${_ok}</span>ok
                        <span class="is-val is-def">${_def}</span>def
                        ${_wrn ? `<span class="is-val is-wrn">${_wrn}</span>wrn` : ''}
                        <span class="is-sep">|</span>
                        avg <span class="is-val">${_avg}%</span>
                        maxD <span class="is-val">${_maxD}%</span>
                    </span>
                    <span class="hr-spacer"></span>
                    <span class="hr-actions">
                        <button class="hr-btn-print" title="Print passport" onclick="event.stopPropagation();printPassport(this)">Print</button>
                        <span class="hr-expand">&#9654;</span>
                    </span>
                </div>
                <div class="history-detail">
                    <div class="hd-stats-row"></div>
                    <div class="hd-chart-wrap">
                        <div class="hd-chart-box"><canvas class="hd-pie-canvas"></canvas></div>
                        <div class="hd-score-bars"></div>
                    </div>
                    <div class="hd-zones-grid"></div>
                </div>`;
            row._resultData = r;
            row.querySelector('.hr-summary').addEventListener('click', () => toggleHistoryDetail(row, r, q));
            return row;
        }

        function renderHistory(results) {
            const el = document.getElementById('history-list');
            const q = _searchQuery;
            if (!results.length) {
                el.innerHTML = _searchQuery
                    ? '<p style="color:var(--muted);font-size:12px">Nothing found</p>'
                    : '<p style="color:var(--muted);font-size:12px">No inspections saved yet</p>';
                return;
            }
            el.innerHTML = '';
            // destroy old charts
            Object.values(_historyCharts).forEach(c => c.destroy());
            _historyCharts = {};
            results.forEach(r => {
                const row = _buildHistoryRow(r, q);
                el.appendChild(row);
            });
        }

        function appendHistoryRows(newResults) {
            const el = document.getElementById('history-list');
            if (el.querySelector('p')) el.innerHTML = ''; // remove "Loading..."
            const q = _searchQuery;
            newResults.forEach(r => {
                const row = _buildHistoryRow(r, q);
                el.appendChild(row);
            });
        }

        function _hl(text, q) {
            if (!q || !text || q === '*') return text;
            // Convert wildcard pattern to regex for highlighting
            if (q.includes('*')) {
                const re = new RegExp('(' + q.split('*').map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*') + ')', 'i');
                return text.replace(re, '<span class="h-match">$1</span>');
            }
            const idx = text.toLowerCase().indexOf(q.toLowerCase());
            if (idx < 0) return text;
            return text.slice(0, idx) + '<span class="h-match">' + text.slice(idx, idx + q.length) + '</span>' + text.slice(idx + q.length);
        }

        document.getElementById('history-search').addEventListener('input', () => {
            clearTimeout(_searchTimer);
            _searchTimer = setTimeout(() => {
                _searchQuery = (document.getElementById('history-search').value || '').trim();
                loadHistory(false);
            }, 400);
        });

        function clearSearchInput() {
            const inp = document.getElementById('history-search');
            inp.value = '';
            _searchQuery = '';
            loadHistory(false);
        }

        function toggleHistoryDetail(row, r, q) {
            const wasExpanded = row.classList.contains('expanded');
            // destroy charts of previously expanded rows
            document.querySelectorAll('.history-row.expanded').forEach(el => {
                const cid = el._chartId;
                if (cid && _historyCharts[cid]) { _historyCharts[cid].destroy(); delete _historyCharts[cid]; }
                el.classList.remove('expanded');
            });
            if (wasExpanded) return;
            row.classList.add('expanded');

            const searchQ = q || (document.getElementById('history-search').value || '').trim().toLowerCase();
            const zones = r.zones || [];
            const _sr2 = {ok: 0, warn: 1, defect: 2};
            const _rs2 = ['ok', 'warn', 'defect'];
            const _zEff = z => {
                if (z.user_decision) return z.user_decision;
                if (z.subzones && z.subzones.length) {
                    let w = 0;
                    for (const sz of z.subzones) { w = Math.max(w, _sr2[sz.user_decision || sz.status] ?? 0); }
                    return _rs2[w] || z.status;
                }
                return z.status;
            };
            const okCount = zones.filter(z => _zEff(z) === 'ok').length;
            const defectCount = zones.filter(z => _zEff(z) === 'defect').length;
            const warnCount = zones.filter(z => _zEff(z) === 'warn').length;
            const unchkCount = zones.filter(z => { const e = _zEff(z); return !e || e === 'unchecked'; }).length;
            const avgScore = zones.length ? (zones.reduce((s, z) => s + (z.score || 0), 0) / zones.length) : 0;
            const maxDefect = zones.length ? Math.max(...zones.map(z => z.defect_pct || 0)) : 0;

            // Stats row
            const statsRow = row.querySelector('.hd-stats-row');
            const recipeStat = r.template_name ? `<div class="hd-stat-card"><div class="hd-stat-val" style="color:var(--accent);font-size:11px">${r.template_name}</div><div class="hd-stat-label">Recipe</div></div>` : '';
            // Extract sensitivity from first zone that has it
            const _sensZone = zones.find(z => z.zone_sensitivity != null);
            const _zSens = _sensZone ? _sensZone.zone_sensitivity.toFixed(2) : null;
            const _sSens = _sensZone && _sensZone.subzone_sensitivity != null ? _sensZone.subzone_sensitivity.toFixed(2) : null;
            const sensCard = _zSens ? `<div class="hd-stat-card"><div class="hd-stat-val" style="font-size:11px">Zone ${_zSens} / Sub ${_sSens || '?'}</div><div class="hd-stat-label">Sensitivity</div></div>` : '';
            statsRow.innerHTML = `
                ${recipeStat}
                <div class="hd-stat-card"><div class="hd-stat-val">${zones.length}</div><div class="hd-stat-label">Zones</div></div>
                <div class="hd-stat-card"><div class="hd-stat-val" style="color:var(--success)">${okCount}</div><div class="hd-stat-label">OK</div></div>
                <div class="hd-stat-card"><div class="hd-stat-val" style="color:var(--danger)">${defectCount}</div><div class="hd-stat-label">Defect</div></div>
                ${warnCount ? `<div class="hd-stat-card"><div class="hd-stat-val" style="color:var(--warn)">${warnCount}</div><div class="hd-stat-label">Warn</div></div>` : ''}
                <div class="hd-stat-card"><div class="hd-stat-val">${(avgScore * 100).toFixed(1)}%</div><div class="hd-stat-label">Avg Score</div></div>
                <div class="hd-stat-card"><div class="hd-stat-val">${maxDefect.toFixed(2)}%</div><div class="hd-stat-label">Max Defect</div></div>
                ${sensCard}`;

            // Pie chart
            const canvas = row.querySelector('.hd-pie-canvas');
            const chartId = 'chart_' + r.result_id;
            row._chartId = chartId;
            const ctx = canvas.getContext('2d');
            _historyCharts[chartId] = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: ['OK', 'Defect', 'Warn', 'Unchecked'],
                    datasets: [{
                        data: [okCount, defectCount, warnCount, unchkCount],
                        backgroundColor: ['#22c55e', '#ef4444', '#f59e0b', '#555']
                    }]
                },
                options: {
                    responsive: true, maintainAspectRatio: true,
                    plugins: { legend: { display: false } },
                    cutout: '55%'
                }
            });

            // Score bars
            const barsEl = row.querySelector('.hd-score-bars');
            barsEl.innerHTML = '';
            zones.forEach(z => {
                const pct = ((z.score || 0) * 100).toFixed(1);
                const eff = z.user_decision || z.status;
                const color = eff === 'ok' ? 'var(--success)' : eff === 'defect' ? 'var(--danger)' : 'var(--warn)';
                const labelHl = _hl(z.label || '', searchQ);
                barsEl.innerHTML += `
                    <div class="hd-score-bar">
                        <span class="hd-score-bar-label">${labelHl}</span>
                        <div class="hd-score-bar-track"><div class="hd-score-bar-fill" style="width:${pct}%;background:${color}"></div></div>
                        <span class="hd-score-bar-val">${pct}%</span>
                    </div>`;
            });

            // Zone cards with images
            const grid = row.querySelector('.hd-zones-grid');
            grid.innerHTML = '';
            const _statusRank = {ok: 0, warn: 1, defect: 2};
            const _rankStatus = ['ok', 'warn', 'defect'];
            // Helper: compute zone effective status considering subzone overrides
            function _zoneEff(z) {
                if (z.user_decision) return z.user_decision;
                if (z.subzones && z.subzones.length) {
                    let worst = 0;
                    for (const sz of z.subzones) {
                        const se = sz.user_decision || sz.status || 'unchecked';
                        worst = Math.max(worst, _statusRank[se] ?? 0);
                    }
                    return _rankStatus[worst] || z.status || 'unchecked';
                }
                return z.status || 'unchecked';
            }
            zones.forEach((z, i) => {
                const card = document.createElement('div');
                card.className = 'hd-zone-card';
                const hasImg = z.image && r.result_id;
                const imgSrc = hasImg ? `/api/results/${r.result_id}/image/${z.image}` : '';
                const refSrc = hasImg ? `/api/results/${r.result_id}/image/ref_${i}.jpg` : '';
                const effStatus = _zoneEff(z);
                const calcStatus = z.status || 'unchecked';
                const hasOverride = effStatus !== calcStatus;
                if (hasOverride) card.classList.add('has-override');
                const labelHl = _hl(z.label || '', searchQ);
                const zSensTag = z.zone_sensitivity != null
                    ? `<span class="sens-tag">Zone ${z.zone_sensitivity.toFixed(2)}</span>` : '';
                card.innerHTML = `
                    ${hasImg ? `<img src="${imgSrc}" alt="${z.label}" loading="lazy" style="cursor:pointer">` : '<div style="height:60px;background:var(--bg2);display:flex;align-items:center;justify-content:center;color:var(--muted)">no image</div>'}
                    <div class="hd-zone-info">
                        <span>${labelHl}</span>
                        ${hasOverride ? `<span class="hd-calc-badge ${calcStatus}">${calcStatus}</span><span class="hd-override-arrow">→</span>` : ''}
                        <span class="hr-badge ${effStatus}">${effStatus} ${z.defect_pct ? z.defect_pct.toFixed(1) + '%' : ''}</span>
                        ${hasOverride ? '<span class="hd-override-icon">✎</span>' : ''}
                        ${zSensTag}
                    </div>`;
                if (hasImg) {
                    card.style.cursor = 'pointer';
                    card.addEventListener('click', () => openZoneCompare(z.label || `Zone ${i + 1}`, imgSrc, refSrc, effStatus));
                }
                grid.appendChild(card);

                // Subzone images
                if (z.subzones && z.subzones.length) {
                    z.subzones.forEach(sz => {
                        const szCard = document.createElement('div');
                        szCard.className = 'hd-zone-card';
                        szCard.style.borderLeft = '3px solid #ff6b6b';
                        const szEff = sz.user_decision || sz.status || 'defect';
                        const szCalc = sz.status || 'defect';
                        const szOverride = sz.user_decision && sz.user_decision !== sz.status;
                        if (szOverride) szCard.classList.add('has-override');
                        const szDefImg = sz.image_defects ? `/api/results/${r.result_id}/image/${sz.image_defects}` : '';
                        const szRefImg = sz.image_reference ? `/api/results/${r.result_id}/image/${sz.image_reference}` : (sz.image_extracted ? `/api/results/${r.result_id}/image/${sz.image_extracted}` : '');
                        const szSensTag = z.subzone_sensitivity != null
                            ? `<span class="sens-tag">Subzone ${z.subzone_sensitivity.toFixed(2)}</span>` : '';
                        szCard.innerHTML = `
                            ${szDefImg ? `<img src="${szDefImg}" alt="${sz.label}" loading="lazy" style="cursor:pointer">` : '<div style="height:60px;background:#1a1a2e;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:10px">no img</div>'}
                            <div class="hd-zone-info">
                                <span style="font-size:11px;color:#ff6b6b">◻ ${sz.label || ''}</span>
                                ${szOverride ? `<span class="hd-calc-badge ${szCalc}">${szCalc}</span><span class="hd-override-arrow">→</span>` : ''}
                                <span class="hr-badge ${szEff}">${szEff} ${sz.defect_pct != null ? sz.defect_pct.toFixed(1) + '%' : ''}</span>
                                ${szOverride ? '<span class="hd-override-icon">✎</span>' : ''}
                                ${szSensTag}
                            </div>`;
                        if (szDefImg) {
                            szCard.style.cursor = 'pointer';
                            szCard.addEventListener('click', () => openZoneCompare(sz.label || 'Subzone', szDefImg, szRefImg, szEff));
                        }
                        grid.appendChild(szCard);
                    });
                }
            });
        }

        function openZoneCompare(label, capturedSrc, refSrc, status) {
            document.getElementById('zcm-title').textContent = label + ' — ' + status;
            document.getElementById('zcm-captured').src = capturedSrc;
            document.getElementById('zcm-reference').src = refSrc;
            document.getElementById('zone-compare-modal').classList.remove('hidden');
        }
        function closeZoneCompare() {
            document.getElementById('zone-compare-modal').classList.add('hidden');
            document.getElementById('zcm-captured').src = '';
            document.getElementById('zcm-reference').src = '';
        }

        function printPassport(btn) {
            const row = btn.closest('.history-row');
            const r = row._resultData;
            if (!r) return;
            const zones = r.zones || [];
            const _ppEff = z => z.user_decision || z.status;
            const okN = zones.filter(z => _ppEff(z) === 'ok').length;
            const defN = zones.filter(z => _ppEff(z) === 'defect').length;
            const warnN = zones.filter(z => _ppEff(z) === 'warn').length;
            const avgSc = zones.length ? (zones.reduce((s, z) => s + (z.score || 0), 0) / zones.length * 100).toFixed(1) : '—';
            const ts = r.timestamp ? r.timestamp.replace('T', ' ').slice(0, 19) : '';
            const ov = r.overall_status || 'ok';

            // preload zone images as base64
            const imgPromises = zones.map((z, i) => {
                if (!z.image || !r.result_id) return Promise.resolve(null);
                return fetch(`/api/results/${r.result_id}/image/${z.image}`)
                    .then(resp => { if (!resp.ok) throw new Error(resp.status); return resp.blob(); })
                    .then(blob => new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    }))
                    .catch(e => { console.warn('Image load failed zone', i, e); return null; });
            });

            // preload subzone images as base64
            const szImgPromises = zones.map((z, i) => {
                if (!z.subzones || !z.subzones.length || !r.result_id) return Promise.resolve([]);
                return Promise.all(z.subzones.map(sz => {
                    if (!sz.image_defects) return Promise.resolve(null);
                    return fetch(`/api/results/${r.result_id}/image/${sz.image_defects}`)
                        .then(resp => { if (!resp.ok) throw new Error(resp.status); return resp.blob(); })
                        .then(blob => new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result);
                            reader.onerror = reject;
                            reader.readAsDataURL(blob);
                        }))
                        .catch(() => null);
                }));
            });

            Promise.all([Promise.all(imgPromises), Promise.all(szImgPromises)]).then(([images, szImages]) => {
                const loadedCount = images.filter(Boolean).length;
                console.log(`Print: loaded ${loadedCount}/${zones.length} images`);

                const pp = document.getElementById('print-passport');
                pp.innerHTML = `
                    <div class="pp-header">
                        <div class="pp-header-left">
                            <div class="pp-logo">P</div>
                            <div class="pp-title">INSPECTION PASSPORT</div>
                        </div>
                        <div class="pp-doc-id">${r.result_id || ''}<br>${ts}</div>
                    </div>

                    <div class="pp-status-strip ${ov}">${ov.toUpperCase()}</div>

                    <div class="pp-meta">
                        <div class="pp-meta-cell"><span class="pp-meta-label">Serial</span><span class="pp-meta-val">${r.serial || '—'}</span></div>
                        <div class="pp-meta-cell"><span class="pp-meta-label">Type</span><span class="pp-meta-val">${r.serial_type || '—'}</span></div>
                        <div class="pp-meta-cell"><span class="pp-meta-label">Recipe</span><span class="pp-meta-val">${r.template_name || '—'}</span></div>
                        <div class="pp-meta-cell"><span class="pp-meta-label">Date</span><span class="pp-meta-val">${ts}</span></div>
                        <div class="pp-meta-cell"><span class="pp-meta-label">Operator</span><span class="pp-meta-val">${r.operator || '—'}</span></div>
                        <div class="pp-meta-cell"><span class="pp-meta-label">Zones</span><span class="pp-meta-val">${r.zones_checked || 0}/${r.zones_total || 0}</span></div>
                        <div class="pp-meta-cell"><span class="pp-meta-label">Session</span><span class="pp-meta-val">${r.session_id || '—'}</span></div>
                        ${(() => { const sz = zones.find(z => z.zone_sensitivity != null); return sz ? `<div class="pp-meta-cell"><span class="pp-meta-label">Sensitivity</span><span class="pp-meta-val">Zone: ${sz.zone_sensitivity.toFixed(2)} / Subzone: ${sz.subzone_sensitivity != null ? sz.subzone_sensitivity.toFixed(2) : '?'}</span></div>` : ''; })()}
                    </div>

                    <div class="pp-summary">
                        <div class="pp-summary-card"><div class="pp-sc-val pp-sc-ok">${okN}</div><div class="pp-sc-label">OK</div></div>
                        <div class="pp-summary-card"><div class="pp-sc-val pp-sc-def">${defN}</div><div class="pp-sc-label">Defect</div></div>
                        ${warnN ? `<div class="pp-summary-card"><div class="pp-sc-val pp-sc-wrn">${warnN}</div><div class="pp-sc-label">Warn</div></div>` : ''}
                        <div class="pp-summary-card"><div class="pp-sc-val">${avgSc}%</div><div class="pp-sc-label">Avg Score</div></div>
                    </div>

                    <div class="pp-section">Zone Details</div>
                    <table class="pp-zone-table">
                        <thead><tr><th>#</th><th>Zone</th><th>Status</th><th>Score</th><th>Bar</th><th>Defect%</th><th>Sens</th><th>Operator</th><th>Scanned</th></tr></thead>
                        <tbody>
                        ${zones.map((z, i) => {
                    const sc = z.score ? (z.score * 100).toFixed(1) : '0';
                    const st = z.status || 'unchecked';
                    const eff = z.user_decision || st;
                    const hasOvr = z.user_decision && z.user_decision !== z.status;
                    const bc = eff === 'ok' ? '#22c55e' : eff === 'defect' ? '#ef4444' : '#f59e0b';
                    const zts = z.checked_at ? z.checked_at.replace('T', ' ').slice(11, 19) : '—';
                    const zop = z.operator || r.operator || '—';
                    const statusHtml = hasOvr
                        ? `<span style="text-decoration:line-through;color:#888;font-size:9px;margin-right:4px">${st.toUpperCase()}</span><span class="pp-zt-status pp-zt-${eff}">${eff.toUpperCase()}</span>`
                        : `<span class="pp-zt-status pp-zt-${eff}">${eff.toUpperCase()}</span>`;
                    let rows = `<tr>
                                <td>${i + 1}</td>
                                <td style="font-weight:600">${z.label || ''}</td>
                                <td>${statusHtml}</td>
                                <td>${sc}%</td>
                                <td><div class="pp-bar-bg"><div class="pp-bar-fill" style="width:${sc}%;background:${bc}"></div></div></td>
                                <td>${z.defect_pct != null ? z.defect_pct.toFixed(2) + '%' : '—'}</td>
                                <td style="font-size:10px">${z.zone_sensitivity != null ? 'Zone ' + z.zone_sensitivity.toFixed(2) : '—'}</td>
                                <td>${zop}</td>
                                <td>${zts}</td>
                            </tr>`;
                    if (z.subzones && z.subzones.length) {
                        z.subzones.forEach(sz => {
                            const szSt = sz.status || 'unchecked';
                            const szEff = sz.user_decision || szSt;
                            const szOvr = sz.user_decision && sz.user_decision !== sz.status;
                            const szBc = szEff === 'ok' ? '#22c55e' : szEff === 'defect' ? '#ef4444' : '#f59e0b';
                            const szStatusHtml = szOvr
                                ? `<span style="text-decoration:line-through;color:#888;font-size:8px;margin-right:3px">${szSt.toUpperCase()}</span><span class="pp-zt-status pp-zt-${szEff}" style="font-size:9px">${szEff.toUpperCase()}</span>`
                                : `<span class="pp-zt-status pp-zt-${szEff}" style="font-size:9px">${szEff.toUpperCase()}</span>`;
                            rows += `<tr style="background:#ff6b6b08">
                                <td></td>
                                <td style="padding-left:18px;color:#ff6b6b;font-size:11px">◻ ${sz.label || ''}</td>
                                <td>${szStatusHtml}</td>
                                <td style="font-size:11px">${sz.ssim != null ? (sz.ssim * 100).toFixed(1) + '%' : '—'}</td>
                                <td><div class="pp-bar-bg"><div class="pp-bar-fill" style="width:${sz.ssim ? (sz.ssim * 100).toFixed(1) : 0}%;background:${szBc}"></div></div></td>
                                <td style="font-size:11px">${sz.defect_pct != null ? sz.defect_pct.toFixed(2) + '%' : '—'}</td>
                                <td style="font-size:10px;color:#ff6b6b">${z.subzone_sensitivity != null ? 'Subzone ' + z.subzone_sensitivity.toFixed(2) : '—'}</td>
                                <td colspan="2"></td>
                            </tr>`;
                        });
                    }
                    return rows;
                }).join('')}
                        </tbody>
                    </table>

                    ${images.some(img => img) ? `
                    <div class="pp-section">Zone Images</div>
                    <div class="pp-zone-images">
                        ${zones.map((z, i) => {
                    let html = '';
                    if (images[i]) {
                        const st = z.status || 'unchecked';
                        const eff = z.user_decision || st;
                        const hasOvr = z.user_decision && z.user_decision !== z.status;
                        const sc = z.score ? (z.score * 100).toFixed(1) : '0';
                        const zts = z.checked_at ? z.checked_at.replace('T', ' ').slice(11, 19) : '';
                        html += `<div class="pp-zone-img-card zic-${eff}">
                                <img src="${images[i]}" alt="${z.label}">
                                <div class="pp-zone-img-caption">
                                    <b>#${i + 1} ${z.label || ''}</b>
                                    ${hasOvr ? `<span style="text-decoration:line-through;color:#888;font-size:9px">${st}</span>` : ''}
                                    <span class="pp-zt-status pp-zt-${eff}">${eff}</span>
                                    <span>${sc}%</span>
                                    ${zts ? `<span class="pp-zts">${zts}</span>` : ''}
                                </div>
                            </div>`;
                    }
                    if (z.subzones && z.subzones.length && szImages[i]) {
                        z.subzones.forEach((sz, si) => {
                            const szImg = szImages[i][si];
                            if (!szImg) return;
                            const szSt = sz.status || 'unchecked';
                            const szEff = sz.user_decision || szSt;
                            const szOvr = sz.user_decision && sz.user_decision !== sz.status;
                            html += `<div class="pp-zone-img-card zic-${szEff}" style="border-left:3px solid #ff6b6b">
                                <img src="${szImg}" alt="${sz.label}">
                                <div class="pp-zone-img-caption">
                                    <b style="color:#ff6b6b">◻ ${sz.label || ''}</b>
                                    ${szOvr ? `<span style="text-decoration:line-through;color:#888;font-size:8px">${szSt}</span>` : ''}
                                    <span class="pp-zt-status pp-zt-${szEff}">${szEff}</span>
                                    <span>${sz.defect_pct != null ? sz.defect_pct.toFixed(1) + '%' : ''}</span>
                                </div>
                            </div>`;
                        });
                    }
                    return html;
                }).join('')}
                    </div>` : ''}

                    <div class="pp-footer">
                        <span>Zone Inspect — Quality Control</span>
                        <span>${new Date().toISOString().slice(0, 19).replace('T', ' ')}</span>
                    </div>`;

                // Wait for all images inside the passport to finish rendering
                const ppImgs = pp.querySelectorAll('img');
                if (ppImgs.length === 0) {
                    window.print();
                } else {
                    let remaining = ppImgs.length;
                    const onReady = () => { if (--remaining <= 0) window.print(); };
                    ppImgs.forEach(img => {
                        if (img.complete) { onReady(); }
                        else { img.onload = onReady; img.onerror = onReady; }
                    });
                }
            });
        }

        loadHistory();
