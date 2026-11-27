/* Live viewpoint hint for the phone viewfinder.
 *
 * About 1.4 times a second a small frame (640 px) goes to the server, which
 * answers per zone: fully in the frame or not, how many degrees the viewpoint
 * is from the typical viewpoint of the good samples, and which way (in the
 * frame) to move the phone. Only a hint: capturing is never blocked.
 *
 *   const g = ViewGuide({ video, container, url, pick, paused });
 *   g.start(); g.stop();
 *
 * pick(response) → the zone entry to show (or null); paused() → true while
 * the page is busy (capturing / uploading).
 */
(function () {
    const CSS = `
        .vg-hud { position: absolute; left: 8px; right: 8px; bottom: 8px; display: flex; gap: 8px; align-items: center;
            padding: 6px 10px; border-radius: 6px; background: rgba(0, 0, 0, .6); color: #fff; font-size: 13px;
            font-family: var(--mono, monospace); pointer-events: none; }
        .vg-hud .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; background: #6b7280; }
        .vg-hud.ok .dot { background: #2ea043; } .vg-hud.warn .dot { background: #d29922; } .vg-hud.bad .dot { background: #f85149; }
        .vg-arrow { position: absolute; left: 50%; top: 50%; width: 0; height: 0; pointer-events: none; }
        .vg-arrow::before { content: "➜"; position: absolute; transform: translate(-50%, -50%); font-size: 64px; line-height: 1;
            color: rgba(255, 255, 255, .85); text-shadow: 0 0 8px rgba(0, 0, 0, .8); }
        .vg-arrow.warn::before { color: rgba(210, 153, 34, .95); } .vg-arrow.bad::before { color: rgba(248, 81, 73, .95); }
        .vg-hidden { display: none !important; }`;
    const ARROWS = ['→', '↘', '↓', '↙', '←', '↖', '↑', '↗'];   // screen directions, y down

    window.ViewGuide = function ({ video, container, url, pick, paused, intervalMs = 700 }) {
        if (!document.getElementById('vg-style')) {
            const st = document.createElement('style');
            st.id = 'vg-style'; st.textContent = CSS;
            document.head.appendChild(st);
        }
        const hud = document.createElement('div');
        hud.className = 'vg-hud vg-hidden';
        hud.innerHTML = '<span class="dot"></span><span class="txt"></span>';
        const arrow = document.createElement('div');
        arrow.className = 'vg-arrow vg-hidden';
        container.appendChild(arrow);
        container.appendChild(hud);
        const canvas = document.createElement('canvas');
        let on = false, timer = null;

        // Same crop as the captured photo (what is visible on screen), smaller
        function frame() {
            const vw = video.videoWidth, vh = video.videoHeight;
            if (!vw || !vh) return null;
            const dispW = video.clientWidth || vw, dispH = video.clientHeight || vh;
            const videoRatio = vw / vh, dispRatio = dispW / dispH;
            let sx = 0, sy = 0, sw = vw, sh = vh;
            if (videoRatio > dispRatio) { sw = Math.round(vh * dispRatio); sx = Math.round((vw - sw) / 2); }
            else { sh = Math.round(vw / dispRatio); sy = Math.round((vh - sh) / 2); }
            const k = Math.min(1, 640 / Math.max(sw, sh));
            canvas.width = Math.round(sw * k); canvas.height = Math.round(sh * k);
            canvas.getContext('2d').drawImage(video, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
            return new Promise(res => canvas.toBlob(res, 'image/jpeg', 0.7));
        }

        function show(level, text, move) {
            hud.className = `vg-hud ${level}`;
            hud.querySelector('.txt').textContent = text;
            if (move && (level === 'warn' || level === 'bad')) {
                const deg = Math.atan2(move[1], move[0]) * 180 / Math.PI;
                arrow.className = `vg-arrow ${level}`;
                arrow.style.transform = `rotate(${deg}deg)`;
            } else arrow.className = 'vg-arrow vg-hidden';
        }

        function render(d) {
            if (!d.aligned) { show('none', 'Board not recognised — show more of it', null); return; }
            const z = pick(d);
            if (!z) { show('warn', 'No zone fully in the frame', null); return; }
            if (!z.in_frame) { show('warn', `${z.label}: not fully in the frame`, null); return; }
            if (z.view_dev == null) { show('ok', `${z.label}: in the frame`, null); return; }
            const [good, max] = d.limits || [10, 20];
            const dev = Math.round(z.view_dev);
            if (z.view_dev <= good) { show('ok', `${z.label}: angle OK (${dev}°)`, null); return; }
            const dir = ARROWS[((Math.round(Math.atan2(z.move[1], z.move[0]) / (Math.PI / 4)) % 8) + 8) % 8];
            show(z.view_dev <= max ? 'warn' : 'bad', `${z.label}: angle ${dev}° — move phone ${dir}`, z.move);
        }

        // → true when the server was busy with another phone's hint
        async function once() {
            const blob = await frame();
            if (!blob) return false;
            const fd = new FormData();
            fd.append('photo', blob, 'frame.jpg');
            const r = await fetch(url, { method: 'POST', body: fd });
            if (!r.ok) return false;
            const d = await r.json();
            if (d.busy) return true;
            if (on) render(d);
            return false;
        }

        async function loop() {
            if (!on) return;
            let busy = false;
            if (!document.hidden && !(paused && paused())) {
                try { busy = await once(); } catch (e) { /* network hiccup: try again next tick */ }
            }
            if (on) timer = setTimeout(loop, busy ? 250 : intervalMs);
        }

        return {
            start() { if (on) return; on = true; loop(); },
            stop() {
                on = false; clearTimeout(timer);
                hud.className = 'vg-hud vg-hidden'; arrow.className = 'vg-arrow vg-hidden';
            },
        };
    };
})();
