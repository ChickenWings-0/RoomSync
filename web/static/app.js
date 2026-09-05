const powerBtn = document.getElementById('powerToggle');
const statusBar = document.getElementById('statusBar');
const syncRoom = document.getElementById('syncRoom');
const syncPc = document.getElementById('syncPc');

const stripLeft = document.getElementById('strip-left');
const stripRight = document.getElementById('strip-right');
const stripBack = document.getElementById('strip-back');
const monitorPreview = document.getElementById('monitorPreview');

const $ = id => document.getElementById(id);

// The master switch. Seeded from /api/state rather than assumed: power is now
// engine state and it is persisted, so a page opened against a running RoomSync
// that was switched off must render as off. Assuming true here is what made the
// button lie for the first second after every reload.
let powerState = true;

// The two mode banks, described once. Everything about a bank apart from which
// hardware it addresses is identical, so both are driven by shared code.
const BANKS = {
    room: {
        grid: $('roomSourceGrid'), select: $('roomPatternSelect'),
        label: $('roomModeValue'), hint: $('roomHint'),
        blurb: 'Drives the three BLE strips.',
    },
    pc: {
        grid: $('pcSourceGrid'), select: $('pcPatternSelect'),
        label: $('pcModeValue'), hint: $('pcHint'),
        blurb: 'Drives OpenRGB, raw and unsmoothed. One zone, so spatial modes arrive as their average colour.',
    },
};

// The server's mode catalog, fetched once at load. This replaces the two
// hand-written constants that used to live here (MODE_LABELS and a COLOR_DRIVEN
// set mirroring the engine's own). Both could silently drift out of step with
// the backend; a mode's label and its capability flags now have exactly one
// source, and it is the same one the engine routes off.
let CATALOG = [];
const SPEC = {};                                  // mode -> spec
const modeState = {room: null, pc: null};

// ── Parameter blocks ─────────────────────────────────────────────
// Colour, speed and palette are the same widget three times: a Room field, a
// PC field, and a Link checkbox that makes an edit address both at once. Each
// block names the catalog flag that decides whether a target's mode actually
// reads it, so a field dims exactly when it stops doing anything.
const PARAMS = {
    color: {
        flag: 'color_driven',
        link: $('linkColors'),
        hint: $('colorHint'),
        noun: 'colour',
        fields: {room: $('roomColorField'), pc: $('pcColorField')},
        inputs: {room: $('roomColorPicker'), pc: $('pcColorPicker')},
        read: t => PARAMS.color.inputs[t].value,
        write: (t, v) => {
            PARAMS.color.inputs[t].value = v;
            $(t + 'ColorHex').textContent = String(v).toUpperCase();
        },
        // Both forms at once: the engine prefers the exact hex and falls back
        // to the channels, so neither end has to trust the other's parsing.
        // No REST fallback because there is no colour route — the wheel has
        // always been socket-only, and a wheel is useless without a live socket
        // anyway since it fires on every drag frame.
        send: (v, target) => push('SET_STATIC_COLOR', {hex: v, target, ...rgbFromHex(v)}),
    },
    brightness: {
        // No flag: every mode is brightness-scaled in _route, so neither
        // field ever dims and the hint stays a plain statement of fact.
        flag: null,
        link: $('linkBrightness'),
        hint: $('brightnessHint'),
        noun: 'brightness',
        fields: {room: $('roomBrightnessField'), pc: $('pcBrightnessField')},
        inputs: {room: $('roomBrightnessSlider'), pc: $('pcBrightnessSlider')},
        read: t => parseFloat(PARAMS.brightness.inputs[t].value),
        write: (t, v) => {
            PARAMS.brightness.inputs[t].value = v;
            $(t + 'BrightnessValue').textContent = Math.round(v * 100) + '%';
        },
        send: (v, target) => push('SET_BRIGHTNESS', {value: v, target},
                                  '/api/brightness', {value: v, target}),
    },
    speed: {
        flag: 'speed_driven',
        link: $('linkSpeed'),
        hint: $('speedHint'),
        noun: 'speed',
        fields: {room: $('roomSpeedField'), pc: $('pcSpeedField')},
        inputs: {room: $('roomSpeedSlider'), pc: $('pcSpeedSlider')},
        read: t => parseFloat(PARAMS.speed.inputs[t].value),
        write: (t, v) => {
            PARAMS.speed.inputs[t].value = v;
            $(t + 'SpeedValue').textContent = Number(v).toFixed(1) + '×';
        },
        send: (v, target) => push('SET_SPEED', {value: v, target},
                                  '/api/speed', {value: v, target}),
    },
    palette: {
        flag: 'palette_driven',
        link: $('linkPalette'),
        hint: $('paletteHint'),
        noun: 'palette',
        fields: {room: $('roomPaletteField'), pc: $('pcPaletteField')},
        inputs: {room: $('roomPaletteSelect'), pc: $('pcPaletteSelect')},
        read: t => PARAMS.palette.inputs[t].value,
        write: (t, v) => { PARAMS.palette.inputs[t].value = v; },
        send: (v, target) => push('SET_PALETTE', {name: v, target},
                                  '/api/palette', {name: v, target}),
    },
};

// The live socket, hoisted so the controls can push commands up it. Colour
// wheels and sliders fire continuously while dragged; one HTTP round trip per
// event is a fetch storm, so anything draggable goes over the socket instead.
let socket = null;

function sendCommand(type, payload = {}) {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({type, payload}));
        return true;
    }
    return false;
}

// Socket first, REST as the fallback during a reconnect. Every control now
// goes through this, including the palette selector — it used to be the one
// control that skipped the socket, for no reason anyone recorded.
function push(type, payload, restUrl, restBody) {
    if (sendCommand(type, payload)) return;
    if (!restUrl || !restBody) return;
    fetch(restUrl, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(restBody),
    }).catch(() => {});
}

function rgbFromHex(hex) {
    const h = hex.replace('#', '');
    return {
        r: parseInt(h.slice(0, 2), 16),
        g: parseInt(h.slice(2, 4), 16),
        b: parseInt(h.slice(4, 6), 16),
    };
}

function hexFromRgb([r, g, b]) {
    return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('');
}

// Render the power state without sending anything. The click handler and the
// state fetch both need exactly this, and having only the handler know how to
// draw it is why a restored 'off' used to come back with a lit button.
function setPowerUI(on) {
    powerState = on;
    powerBtn.classList.toggle('on', on);
    if (!on) {
        [stripLeft, stripRight, stripBack].forEach(el => {
            el.style.backgroundColor = '#000';
            el.style.boxShadow = 'none';
        });
        monitorPreview.style.borderColor = '#000';
        monitorPreview.style.boxShadow = 'none';
    }
}

function paint(el, r, g, b) {
    el.style.backgroundColor = `rgb(${r},${g},${b})`;
    el.style.boxShadow = `0 0 20px rgba(${r},${g},${b},0.6)`;
}

// ── Catalog ──────────────────────────────────────────────────────

async function loadCatalog() {
    const res = await fetch('/api/modes');
    if (!res.ok) throw new Error('catalog unavailable');
    CATALOG = (await res.json()).modes || [];
    CATALOG.forEach(s => { SPEC[s.mode] = s; });
    Object.keys(BANKS).forEach(buildBank);
}

function option(value, text) {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = text;
    return o;
}

// Build one bank from the catalog, filtered by that target's capability flag.
// Sources stay a button grid — they are what you switch between constantly.
// Patterns go into a grouped dropdown, because sixteen flat buttons is not a
// control surface. The Room bank never receives a PC-only option at all.
function buildBank(target) {
    const bank = BANKS[target];
    const usable = s => (target === 'room' ? s.room_ok : s.pc_ok);

    bank.grid.innerHTML = '';
    CATALOG.filter(s => s.group === 'source' && usable(s)).forEach(s => {
        const btn = document.createElement('button');
        btn.className = 'mode-btn';
        btn.dataset.mode = s.mode;
        btn.textContent = s.label;

        // available=false means this MACHINE cannot feed the mode — no audio
        // loopback, no screen capture. Greyed out and disabled rather than
        // hidden: a mode that silently vanishes reads as a bug, and the
        // tooltip is the only place the user is ever told why.
        if (s.available === false) {
            btn.classList.add('unavailable');
            btn.disabled = true;
            btn.title = s.unavailable_reason || 'Not available on this machine.';
        } else {
            btn.title = s.hint || '';
        }
        bank.grid.appendChild(btn);
    });

    bank.select.innerHTML = '';
    bank.select.appendChild(option('', 'None — using a source'));
    [['room', 'Room-safe'], ['pc', 'PC only']].forEach(([group, caption]) => {
        const list = CATALOG.filter(s => s.group === group && usable(s));
        if (!list.length) return;
        const og = document.createElement('optgroup');
        og.label = caption;
        list.forEach(s => {
            // Same rule as the source buttons. A disabled <option> stays
            // visible in the list and cannot be picked, which is exactly the
            // behaviour we want — the mode exists, just not here.
            const unavailable = s.available === false;
            const o = option(s.mode, unavailable ? s.label + ' (unavailable)' : s.label);
            o.disabled = unavailable;
            o.title = unavailable
                ? (s.unavailable_reason || 'Not available on this machine.')
                : (s.hint || '');
            og.appendChild(o);
        });
        bank.select.appendChild(og);
    });
}

// ── Mode selection ───────────────────────────────────────────────
// The button grid and the dropdown are two views of ONE value, so selecting in
// either clears the other. Paint first: the engine is authoritative, but a mode
// click should feel instant rather than waiting on a round trip.

function setModeUI(target, mode) {
    const spec = SPEC[mode];
    if (!spec) return;
    modeState[target] = mode;

    const bank = BANKS[target];
    bank.grid.querySelectorAll('.mode-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
    });
    bank.select.value = spec.group === 'source' ? '' : mode;
    bank.label.textContent = spec.label;
    // A mode restored from disk (or set before a device was unplugged) can be
    // the ACTIVE one and unavailable at the same time. Say so in the bank's
    // hint rather than pretending it is running normally — the strips are
    // rendering black and the user deserves the reason.
    bank.hint.textContent = spec.available === false
        ? bank.blurb + ' ⚠ ' + (spec.unavailable_reason || 'Not available on this machine.')
        : (spec.hint ? bank.blurb + ' ' + spec.hint : bank.blurb);

    updateDerivedUI();
}

function selectMode(target, mode) {
    const spec = SPEC[mode];
    if (!spec || mode === modeState[target]) return;
    // The engine refuses these too, and the REST route returns a 400 — this is
    // the third guard rather than the only one, and it exists so a click on a
    // stale page does nothing visible instead of painting a mode that is about
    // to be rejected and then silently snapping back on the next state poll.
    if (spec.available === false) {
        statusBar.textContent = spec.unavailable_reason || 'That mode is not available here.';
        return;
    }
    setModeUI(target, mode);
    push('SET_MODE', {mode, target}, '/api/mode', {mode, target});
}

Object.keys(BANKS).forEach(target => {
    BANKS[target].grid.addEventListener('click', e => {
        const btn = e.target.closest('.mode-btn');
        if (btn) selectMode(target, btn.dataset.mode);
    });
    BANKS[target].select.addEventListener('change', e => {
        // The empty option means "I want a source instead" — it is not a mode,
        // so it re-selects the first source rather than sending nothing and
        // leaving the dropdown disagreeing with the engine.
        const mode = e.target.value ||
            (CATALOG.find(s => s.group === 'source' && s.available !== false &&
                (target === 'room' ? s.room_ok : s.pc_ok)) || {}).mode;
        if (mode) selectMode(target, mode);
    });
});

// ── Parameter blocks ─────────────────────────────────────────────

Object.entries(PARAMS).forEach(([name, block]) => {
    Object.keys(block.fields).forEach(target => {
        block.inputs[target].addEventListener('input', () => {
            const value = block.read(target);
            if (block.link.checked) {
                // Linked: mirror the other field locally and send ONE command
                // with target 'both'. Two separate commands could land on
                // different ticks and briefly split the outputs apart.
                block.write('room', value);
                block.write('pc', value);
                block.send(value, 'both');
            } else {
                block.write(target, value);
                block.send(value, target);
            }
        });
    });

    // Re-linking adopts the value of the field you linked FROM — the room's —
    // rather than leaving the two disagreeing while the box claims they are
    // linked. Unchecking changes nothing; it only stops the mirroring.
    block.link.addEventListener('change', () => {
        if (!block.link.checked) return;
        const value = block.read('room');
        block.write('pc', value);
        block.send(value, 'both');
    });
});

// A field is live when THAT target's mode declares it reads this parameter.
// Both flags come from the catalog, so this can never disagree with what the
// engine's _render_key actually consults.
function updateDerivedUI() {
    Object.values(PARAMS).forEach(block => {
        if (!block.flag) return;   // always live on both targets
        const driven = [];
        Object.keys(block.fields).forEach(target => {
            const spec = SPEC[modeState[target]];
            const live = !!(spec && spec[block.flag]);
            block.fields[target].classList.toggle('inactive', !live);
            if (live) driven.push(target === 'room' ? 'Room' : 'PC');
        });
        block.hint.textContent = driven.length
            ? `Driving the ${driven.join(' + ')} ${block.noun}.`
            : `Idle — neither output's mode reads a ${block.noun}.`;
        block.hint.classList.toggle('inactive', driven.length === 0);
    });
}

// ── Presets & Hex ────────────────────────────────────────────────

const PRESETS_STORAGE_KEY = 'roomsync_color_presets';
const DEFAULT_PRESETS = ['#FF0000', '#00FF00', '#0000FF', '#FFFFFF'];
const hexInput = $('hexInput');
const applyHexBtn = $('applyHexBtn');
const savePresetBtn = $('savePresetBtn');
const presetContainer = $('presetContainer');

function getPresets() {
    try {
        const stored = localStorage.getItem(PRESETS_STORAGE_KEY);
        if (stored) return JSON.parse(stored);
    } catch (e) {}
    return [...DEFAULT_PRESETS];
}

function savePresets(presets) {
    localStorage.setItem(PRESETS_STORAGE_KEY, JSON.stringify(presets));
}

function applyHexColor(hex) {
    if (!/^#[0-9A-Fa-f]{6}$/i.test(hex)) {
        statusBar.textContent = 'Invalid hex color (use #RRGGBB)';
        return;
    }
    
    if (PARAMS.color.link.checked) {
        PARAMS.color.write('room', hex);
        PARAMS.color.write('pc', hex);
        PARAMS.color.send(hex, 'both');
    } else {
        PARAMS.color.write('room', hex);
        PARAMS.color.send(hex, 'room');
    }
}

function renderPresets() {
    const presets = getPresets();
    presetContainer.innerHTML = '';
    
    presets.forEach((hex, index) => {
        const circle = document.createElement('div');
        circle.className = 'preset-circle';
        circle.style.backgroundColor = hex;
        circle.title = hex;
        
        circle.addEventListener('click', () => {
            hexInput.value = hex;
            applyHexColor(hex);
        });
        
        circle.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            if (confirm(`Delete preset ${hex}?`)) {
                presets.splice(index, 1);
                savePresets(presets);
                renderPresets();
            }
        });
        
        presetContainer.appendChild(circle);
    });
}

applyHexBtn.addEventListener('click', () => {
    let hex = hexInput.value.trim();
    if (!hex.startsWith('#')) hex = '#' + hex;
    if (hex.length === 4) hex = '#' + hex[1]+hex[1] + hex[2]+hex[2] + hex[3]+hex[3];
    hex = hex.toUpperCase();
    hexInput.value = hex;
    applyHexColor(hex);
});

savePresetBtn.addEventListener('click', () => {
    let hex = hexInput.value.trim();
    if (!hex.startsWith('#')) hex = '#' + hex;
    if (hex.length === 4) hex = '#' + hex[1]+hex[1] + hex[2]+hex[2] + hex[3]+hex[3];
    hex = hex.toUpperCase();
    
    if (!/^#[0-9A-Fa-f]{6}$/i.test(hex)) {
        statusBar.textContent = 'Invalid hex color (use #RRGGBB)';
        return;
    }
    
    const presets = getPresets();
    if (!presets.includes(hex)) {
        presets.push(hex);
        savePresets(presets);
        renderPresets();
    }
});

renderPresets();

// ── State ────────────────────────────────────────────────────────

async function fetchState() {
    try {
        const res = await fetch('/api/state');
        if (!res.ok) return;
        const data = await res.json();

        // Fall back to the legacy single "mode" key so a stale engine still
        // lands the page in a coherent state instead of blank.
        setModeUI('room', data.room_mode || data.mode);
        setModeUI('pc', data.pc_mode || data.mode);

        PARAMS.brightness.write('room', data.room_brightness ?? data.brightness ?? 0.85);
        PARAMS.brightness.write('pc', data.pc_brightness ?? data.brightness ?? 0.85);

        if (data.room_static_color) PARAMS.color.write('room', hexFromRgb(data.room_static_color));
        if (data.pc_static_color) PARAMS.color.write('pc', hexFromRgb(data.pc_static_color));
        PARAMS.speed.write('room', data.room_speed ?? data.speed ?? 1);
        PARAMS.speed.write('pc', data.pc_speed ?? data.speed ?? 1);
        PARAMS.palette.write('room', data.room_palette ?? data.palette ?? 'rainbow');
        PARAMS.palette.write('pc', data.pc_palette ?? data.palette ?? 'rainbow');

        // Link state is UI-only, so it is inferred from the engine rather than
        // stored there: if the two values already agree the box starts checked
        // (the fresh-boot case, since both seed from the same default). If they
        // differ, the user had unlinked them, and starting checked would
        // clobber one on the next drag.
        Object.values(PARAMS).forEach(block => {
            const a = block.read('room'), b = block.read('pc');
            block.link.checked = String(a).toLowerCase() === String(b).toLowerCase();
        });

        syncRoom.checked = data.sync_room !== false;
        syncPc.checked = data.sync_pc !== false;
        // Absent on an older engine, which is why this is an explicit !== false
        // rather than a truthiness test: a build that does not report power_on
        // is one where power was never off, so the default must be on.
        setPowerUI(data.power_on !== false);
        updateToggleUI();
    } catch (e) {
        console.error('Failed to fetch state', e);
    }
}

function connectWebSocket() {
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${wsProto}//${window.location.host}/ws/preview`);
    socket = ws;

    ws.onopen = () => {
        statusBar.textContent = 'Live Stream Active';
        statusBar.style.color = '#10b981';
    };

    ws.onmessage = (event) => {
        if (!powerState) return;

        const data = JSON.parse(event.data);

        if (data['OA10 30']) paint(stripLeft, ...data['OA10 30']);    // Left Wall
        if (data['OA10 20']) paint(stripRight, ...data['OA10 20']);   // Right Wall
        if (data['OA10 33']) paint(stripBack, ...data['OA10 33']);    // Back Wall

        // The PC target is a separate signal from the strips: it carries the
        // raw, unsmoothed colour, so it must be previewed separately or the
        // decoupling is invisible from here.
        if (data['pc']) {
            const [r, g, b] = data['pc'];
            monitorPreview.style.borderColor = `rgb(${r},${g},${b})`;
            monitorPreview.style.boxShadow = `inset 0 0 24px rgba(${r},${g},${b},0.55)`;
        }
    };

    ws.onclose = () => {
        socket = null;
        statusBar.textContent = 'Disconnected. Reconnecting...';
        statusBar.style.color = '#ef4444';
        setTimeout(connectWebSocket, 2000);
    };
}

powerBtn.addEventListener('click', async () => {
    const on = !powerState;
    // Paint first, ask second: the button must feel instant, and the engine is
    // authoritative anyway — the next /api/state corrects us if the POST failed.
    setPowerUI(on);

    // Stays on REST rather than moving to the socket. Unlike a slider this
    // fires once per click, and the route does something the socket cannot:
    // it drives the BLE workers' power line directly, without waiting for a
    // tick. The engine is kept in step by the SET_POWER command the route
    // puts on the bus itself.
    try {
        await fetch('/api/power', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({on}),
        });
    } catch (e) {
        console.error('Power toggle failed', e);
    }
});

[syncRoom, syncPc].forEach(box => {
    box.addEventListener('change', () => {
        updateToggleUI();
        sendCommand('SET_TARGETS', {room: syncRoom.checked, pc: syncPc.checked});
    });
});

function updateToggleUI() {
    syncRoom.closest('.toggle').classList.toggle('off', !syncRoom.checked);
    syncPc.closest('.toggle').classList.toggle('off', !syncPc.checked);
}

// Initial load. The catalog must land before the state does: setModeUI needs a
// spec to read a label and a group off, and the banks need their buttons before
// one of them can be marked active.
(async () => {
    try {
        await loadCatalog();
    } catch (e) {
        statusBar.textContent = 'Could not load the mode catalog.';
        statusBar.style.color = '#ef4444';
        console.error(e);
        return;
    }
    await fetchState();
    connectWebSocket();
})();
