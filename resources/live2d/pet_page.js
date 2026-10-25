/* DeskPet Live2D 页面控制器。
   职责：把画布交给渲染器；驱动待机动画（眨眼/呼吸/看向鼠标）；把鼠标事件回传给 Python。
   模型没有动作与表情资源时也能演出，全部走参数；有资源时接口如实返回可用状态。 */
(function () {
  'use strict';

  var canvas = document.getElementById('pet');
  var r = null;
  var scale = 1;
  var lastError = null;
  var ready = false;
  var maskInfo = null;
  var maskAt = 0, maskGX = 0, maskGY = 0;
  var DRAG_SLOP = 4;
  var drag = null;
  var debug = { backend: 'webgl', core: null };
  /* 帧率上限。WebView2 里的 rAF 不跟着显示器走（实测能跑到两百多），不限速就是白烧
     CPU，所以自己定一个稳的。60fps 配 25 次/秒的光标已经连着走；以前卡 30fps，帧间隔
     在 30 和 40 毫秒之间来回，光标匀速划过时反而看着一格一格。 */
  var FRAME_MS = 1000 / 60;
  var raf = 0, lastFrame = 0, frames = 0, fpsAt = 0, fps = 0, fpsFrames = 0;
  var params = {};
  var gaze = { tx: 0, ty: 0, x: 0, y: 0 };
  var pointerKnown = false;
  var emotion = { name: 'neutral', w: 0, target: 0 };
  var react = { name: '', t0: 0, len: 0 };
var reducedMotion = false;   // 减少动态效果：不摆不呼吸，眨眼留着
  var REACT_COOLDOWN_MS = 350;   // 同一个反应两次之间至少隔这么久
  var blink = { next: 2, phase: 0, active: false, count: 0 };

  /* 头部运动怎么来的：指针跟随 + 待机轻摆，两路都先合成目标角度，再让弹簧收敛。
     一阶平滑是「起步最快、尾巴拖长」，配上几赫兹的光标更新就是一顿一顿；弹簧从
     零速度起步、到目标前减速，手快手慢都接着走。 */
  var follow = { x: 0, y: 0, vx: 0, vy: 0 };
  var swayPhase = 0;             // 待机摆动相位，按 dt 累积，停顿后接着摆
  var FOLLOW_STIFF = 26;         // 弹簧劲度
  var FOLLOW_DAMP = 10.5;        // 阻尼：2*sqrt(26) 附近，不过冲
  var SWAY_SECONDS = 7.2;        // 左右摆一个来回的秒数
  var SWAY_YAW = 0.06;           // 待机摆动幅度（占参数范围的比例）
  var SWAY_NOD = 0.03;
  var SWAY_ROLL = 0.05;
  var POINTER_YAW = 0.50;        // 指针左右 → 头左右
  var POINTER_PITCH = 0.34;      // 指针上下 → 头上下
  var BODY_YAW = 0.50;           // 身体跟着头转多少
  var BODY_ROLL = 0.06;

  var EMOTIONS = {
    neutral: {},
    happy: { mouth: 0.9, eyeSmile: 0.8, cheek: 0.9, brow: 0.25, mouthOpen: 0.2 },
    excited: { mouth: 1.0, eyeSmile: 0.9, cheek: 1.0, brow: 0.4, mouthOpen: 0.5 },
    sad: { mouth: -0.8, brow: -0.45, eyeOpen: -0.3 },
    angry: { mouth: -0.6, brow: -0.6, cheek: 0.35, eyeOpen: 0.12 },
    tired: { eyeOpen: -0.5, brow: -0.3, mouth: -0.2 },
    thinking: { eyeOpen: -0.25, brow: -0.2, gazeX: -0.45, gazeY: -0.3 },
  };

  var EV_URL = '/ev';
  function emit(name, payload) {
    try {
      fetch(EV_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, payload: payload === undefined ? null : payload }),
      }).catch(function () { });
    } catch (e) { }
  }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function fitCanvas() {
    var w = Math.max(1, Math.floor(window.innerWidth));
    var h = Math.max(1, Math.floor(window.innerHeight));
    var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    return [canvas.width, canvas.height];
  }
  function hitCss(x, y) {
    if (!maskInfo) { return false; }
    var w = canvas.clientWidth || 1, h = canvas.clientHeight || 1;
    var col = Math.floor(x / w * maskInfo.cols);
    var row = Math.floor(y / h * maskInfo.rows);
    if (col < 0 || row < 0 || col >= maskInfo.cols || row >= maskInfo.rows) { return false; }
    return maskInfo.bits[row * maskInfo.cols + col] === 1;
  }
  function decodeMask(raw) {
    var bin = atob(raw.data);
    var bits = new Uint8Array(raw.cols * raw.rows);
    for (var i = 0; i < bits.length; i++) {
      bits[i] = (bin.charCodeAt(i >> 3) >> (i & 7)) & 1;
    }
    maskInfo = { cols: raw.cols, rows: raw.rows, bits: bits };
    return maskInfo;
  }
  function rebuildMask() {
    if (!r || !ready) { return null; }
    try {
      var m = decodeMask(r.mask(6));
      maskAt = nowMs();
      maskGX = gaze.x;
      maskGY = gaze.y;
      return m;
    } catch (e) { return null; }
  }
  function nowMs() {
    try { return performance.now(); } catch (e) { return Date.now(); }
  }

  /* ---------- 参数读写：只动模型真的有的参数 ---------- */
  function set(id, value) {
    var p = params[id];
    if (!p) { return false; }
    r.setParameter(id, clamp(value, p.min, p.max));
    return true;
  }
  function base(id) { var p = params[id]; return p ? p.default : 0; }
  function up(id, frac) { var p = params[id]; return p ? (p.max - p.default) * frac : 0; }
  function down(id, frac) { var p = params[id]; return p ? (p.min - p.default) * frac : 0; }
  function toward(id, frac) { return frac >= 0 ? up(id, frac) : down(id, -frac); }
  function readParams() {
    params = {};
    try {
      var list = r.parameters() || [];
      for (var i = 0; i < list.length; i++) {
        params[list[i].id] = { min: list[i].min, max: list[i].max, default: list[i].default };
      }
    } catch (e) { params = {}; }
    return Object.keys(params).length;
  }

  /* ---------- 待机与反应 ---------- */
  function followStep(tx, ty, dt) {
    // 掉帧时步长会变大，大步长会把弹簧算炸，所以拆成小步长多算几遍。
    var n = Math.max(1, Math.ceil(dt / 0.02));
    var h = dt / n;
    for (var i = 0; i < n; i++) {
      var ax = FOLLOW_STIFF * (tx - follow.x) - FOLLOW_DAMP * follow.vx;
      follow.vx += ax * h;
      follow.x += follow.vx * h;
      var ay = FOLLOW_STIFF * (ty - follow.y) - FOLLOW_DAMP * follow.vy;
      follow.vy += ay * h;
      follow.y += follow.vy * h;
    }
  }

  function step(ts, dt) {
    var t = ts / 1000;
    // 眨眼
    if (blink.active) {
      blink.phase += dt / 0.13;
      if (blink.phase >= 2) { blink.active = false; blink.phase = 0; blink.count++; blink.next = 2.5 + Math.random() * 4; }
    } else {
      blink.next -= dt;
      if (blink.next <= 0) { blink.active = true; blink.phase = 0; }
    }
    var lid = blink.active ? (1 - Math.abs(blink.phase - 1)) : 0;
    // 看向鼠标；鼠标不动时让它自己慢慢飘。开了减少动态效果就不飘了。
    var calm = reducedMotion;
    var t1 = pointerKnown ? gaze.tx : (calm ? 0 : 0.3 * Math.sin(t / 7.3));
    var t2 = pointerKnown ? gaze.ty : (calm ? 0 : 0.18 * Math.sin(t / 9.1));
    followStep(t1, t2, dt);
    gaze.x = follow.x;
    gaze.y = follow.y;
    // 情绪权重渐入
    emotion.w += (emotion.target - emotion.w) * Math.min(1, dt * 2.4);
    var e = EMOTIONS[emotion.name] || {};
    var w = emotion.w;
    // 反应包络
    var rx = 0, ry = 0, rz = 0, extraBlink = 0, extraCheek = 0, extraMouth = 0;
    if (react.name && react.len > 0) {
      var u = (nowMs() - react.t0) / react.len;
      if (u >= 1) { react = { name: '', t0: 0, len: 0 }; } else {
        if (react.name === 'click') {
          ry = Math.sin(Math.PI * u) * -0.55;        // 点一下：点头
          extraBlink = Math.sin(Math.PI * u) * 0.75;
          extraCheek = Math.sin(Math.PI * u) * 0.8;
          extraMouth = Math.sin(Math.PI * u) * 0.35;
        } else if (react.name === 'double_click') {
          rx = Math.sin(Math.PI * 3 * u) * 0.45;     // 双击：摇头
          rz = Math.sin(Math.PI * 2 * u) * 0.25;
          extraBlink = Math.sin(Math.PI * u) * 0.3;
        } else if (react.name === 'hover') {
          rz = Math.sin(Math.PI * u) * 0.10;   // 靠近只是轻轻歪一下，不抢视线
        } else if (react.name === 'long_press') {
          ry = Math.sin(Math.PI * u) * -0.30;        // 长按：慢慢低头
          rz = Math.sin(Math.PI * 2 * u) * 0.30;
          extraCheek = Math.sin(Math.PI * u) * 0.6;
        } else if (react.name === 'drag_end') {
          rx = Math.sin(Math.PI * u) * 0.40;         // 放下：身体歪一下
          extraBlink = -0.25 * Math.sin(Math.PI * u);
        }
      }
    }
    var breath = calm ? 0.5 : (0.5 + 0.5 * Math.sin(2 * Math.PI * t / 3.6));
    if (!calm) { swayPhase += dt; }
    var unit = 2 * Math.PI / SWAY_SECONDS;
    var sway = calm ? 0 : Math.sin(swayPhase * unit);
    var nod = calm ? 0 : Math.sin(swayPhase * unit / 1.35 + 0.8);
    var yaw = POINTER_YAW * gaze.x + 0.10 * (e.gazeX || 0) * w + rx + SWAY_YAW * sway;
    set('ParamBreath', base('ParamBreath') + up('ParamBreath', breath));
    set('ParamAngleX', base('ParamAngleX') + toward('ParamAngleX', yaw));
    set('ParamAngleY', base('ParamAngleY') + toward('ParamAngleY', -POINTER_PITCH * gaze.y + ry + SWAY_NOD * nod));
    set('ParamAngleZ', base('ParamAngleZ') + toward('ParamAngleZ', SWAY_ROLL * sway + rz));
    set('ParamBodyAngleX', base('ParamBodyAngleX') + toward('ParamBodyAngleX', BODY_YAW * yaw));
    set('ParamBodyAngleZ', base('ParamBodyAngleZ') + toward('ParamBodyAngleZ', BODY_ROLL * sway));
    set('Parambodyupper', base('Parambodyupper') + up('Parambodyupper', 0.05 * breath));
    set('ParamHairFront', base('ParamHairFront') + toward('ParamHairFront', 0.16 * Math.sin(t / 2.3)));
    set('ParamHairSide', base('ParamHairSide') + toward('ParamHairSide', 0.16 * Math.sin(t / 2.1 + 1.2)));
    set('ParamHairBack', base('ParamHairBack') + toward('ParamHairBack', 0.14 * Math.sin(t / 3.1 + 0.4)));
    set('ParamEyeBallX', base('ParamEyeBallX') + up('ParamEyeBallX', 0.7 * gaze.x));
    set('ParamEyeBallY', base('ParamEyeBallY') + toward('ParamEyeBallY', -0.6 * gaze.y));
    var lidFrac = clamp(lid + extraBlink, 0, 1);
    set('ParamEyeLOpen', base('ParamEyeLOpen') + down('ParamEyeLOpen', lidFrac));
    set('ParamEyeROpen', base('ParamEyeROpen') + down('ParamEyeROpen', lidFrac));
    set('ParamEyeLSmile', base('ParamEyeLSmile') + up('ParamEyeLSmile', (e.eyeSmile || 0) * w));
    set('ParamEyeRSmile', base('ParamEyeRSmile') + up('ParamEyeRSmile', (e.eyeSmile || 0) * w));
    if (e.eyeOpen) {
      set('ParamEyeLOpen', base('ParamEyeLOpen') + toward('ParamEyeLOpen', e.eyeOpen * w));
      set('ParamEyeROpen', base('ParamEyeROpen') + toward('ParamEyeROpen', e.eyeOpen * w));
    }
    set('ParamMouthForm', base('ParamMouthForm') + toward('ParamMouthForm', (e.mouth || 0) * w));
    set('ParamMouthOpenY', base('ParamMouthOpenY') + up('ParamMouthOpenY', (e.mouthOpen || 0) * w + extraMouth));
    set('ParamCheek', base('ParamCheek') + up('ParamCheek', (e.cheek || 0) * w + extraCheek));
    set('ParamBrowLY', base('ParamBrowLY') + toward('ParamBrowLY', (e.brow || 0) * w));
    set('ParamBrowRY', base('ParamBrowRY') + toward('ParamBrowRY', (e.brow || 0) * w));
    // 姿势漂了就把碰撞掩码重算，保证点得中
    if (ready && nowMs() - maskAt > 2500 && (Math.abs(gaze.x - maskGX) > 0.22 || Math.abs(gaze.y - maskGY) > 0.22)) {
      rebuildMask();
      emit('mask_ready', null);
    }
  }

  function tick(ts) {
    raf = window.requestAnimationFrame(tick);
    if (ts - lastFrame < FRAME_MS) { return; }
    var dt = lastFrame ? Math.min(0.25, (ts - lastFrame) / 1000) : 0.033;
    lastFrame = ts;
    frames++;
    fpsFrames++;
    if (ts - fpsAt >= 1000) { fps = Math.round(fpsFrames * 1000 / (ts - fpsAt)); fpsAt = ts; fpsFrames = 0; }
    if (!r || !ready) { return; }
    try { step(ts, dt); r.draw(); } catch (err) { lastError = String((err && err.message) || err); }
  }
  function startLoop() { if (!raf) { lastFrame = 0; raf = window.requestAnimationFrame(tick); } }
  function stopLoop() { if (raf) { window.cancelAnimationFrame(raf); raf = 0; } }

  var Pet = {
    load: function (url, opts) {
      try {
        if (!window.Live2DCubismCore) { throw new Error('没有加载 Live2D 运行库'); }
        if (r) { r.unload(); r = null; }
        ready = false;
        lastError = null;
        params = {};
        maskInfo = null;
        pointerKnown = false;
        if (opts && opts.scale) { scale = clamp(Number(opts.scale) || 1, 0.2, 4); }
        fitCanvas();
        r = PetRender.create(canvas, { viewScale: scale });
        var info = window.Live2DCubismCore.Version;
        debug.core = info ? String(info.csmGetVersion()) : null;
        return r.load(url).then(function (st) {
          ready = st.status === 'ready';
          lastError = ready ? null : (st.error || '模型加载失败');
          r.setView({ viewScale: scale });
          if (ready) { readParams(); }
          r.draw();
          rebuildMask();
          startLoop();
          var out = Pet.status();
          emit(ready ? 'ready' : 'error', out);
          return out;
        });
      } catch (e) {
        ready = false;
        lastError = String((e && e.message) || e);
        var bad = Pet.status();
        emit('error', bad);
        return Promise.resolve(bad);
      }
    },
    unload: function () {
      stopLoop();
      try { if (r) { r.unload(); r = null; } } catch (e) { }
      blink.count = 0;
      ready = false;
      maskInfo = null;
      emit('unloaded', null);
      return true;
    },
    setScale: function (value) {
      scale = clamp(Number(value) || 1, 0.2, 4);
      if (r) { r.setView({ viewScale: scale }); r.draw(); rebuildMask(); }
      return scale;
    },
    /* 表情与动作：模型没这份资源就如实说没有，不假装播过。 */
    setExpression: function (name) {
      if (!ready || !r) { return { ok: false, error: '模型还没加载' }; }
      var st = r.status();
      var list = st.expressions || [];
      if (!list.length) { return { ok: false, error: '这个模型没有表情资源' }; }
      if (list.indexOf(String(name)) < 0) { return { ok: false, error: '模型里没有表情：' + name }; }
      return { ok: false, error: '表情解析还没接上（接口已预留）', declared: String(name) };
    },
    playMotion: function (group, index) {
      if (!ready || !r) { return { ok: false, error: '模型还没加载' }; }
      var st = r.status();
      var list = st.motionGroups || [];
      if (!list.length) { return { ok: false, error: '这个模型没有动作资源' }; }
      if (list.indexOf(String(group)) < 0) { return { ok: false, error: '模型里没有这组动作：' + group }; }
      return { ok: false, error: '动作播放还没接上（接口已预留）', declared: String(group), index: Number(index) || 0 };
    },
    /* 情绪走参数，不依赖表情文件：这是看得见的反应。 */
    setEmotion: function (name) {
      var wanted = String(name || '').trim();
      if (!wanted) { return { ok: false, error: '没有给情绪' }; }
      if (!ready || !r) { return { ok: false, error: '模型还没加载' }; }
      if (!EMOTIONS[wanted]) {
        emotion.name = 'neutral';
        emotion.target = 0;
        return { ok: false, error: '渲染器没有这个情绪：' + wanted, emotion: wanted };
      }
      emotion.name = wanted;
      emotion.target = wanted === 'neutral' ? 0 : 1;
      return { ok: true, emotion: wanted, animated: true };
    },
    /* 鼠标位置（归一化 -1..1），来自宿主进程轮询的全局光标。 */
    setPointer: function (x, y) {
      var px = Number(x), py = Number(y);
      if (!isFinite(px) || !isFinite(py)) { pointerKnown = false; return false; }
      gaze.tx = clamp(px, -1, 1);
      gaze.ty = clamp(py, -1, 1);
      pointerKnown = true;
      return true;
    },
    clearPointer: function () { pointerKnown = false; return true; },
    /* 被点、被双击、被靠近时的小反应，不需要任何动作资源。 */
    react: function (name) {
      var len = 0.34;
      if (name === 'double_click') { len = 0.42; }
      else if (name === 'hover') { len = 0.8; }
      else if (name === 'long_press') { len = 0.6; }
      else if (name === 'drag_end') { len = 0.45; }
      var key = String(name || 'click');
      var at = nowMs();
      // 同一个动作还在播、或者刚播完，就别被鼠标抖动连着重启，否则看着像在抽搐。
      if (react.name === key && at - react.t0 < react.len + REACT_COOLDOWN_MS) {
        return { ok: true, reaction: key, skipped: '同一个动作还没到下一次' };
      }
      react = { name: key, t0: at, len: len * 1000 };
      return { ok: true, reaction: react.name };
    },
    status: function () {
      var base = r ? r.status() : { status: 'idle' };
      return {
        status: base.status, ready: ready, error: lastError,
        url: base.url || '', viewScale: scale,
        drawables: base.drawables || 0, parameters: base.parameters || 0,
        textures: base.textures || 0, masks: base.masks || 0,
        expressions: base.expressions || [], motions: base.motionGroups || [],
        canvas: base.canvas || [canvas.width, canvas.height],
        boxSize: base.boxSize || null,
        backend: debug.backend, core: debug.core,
        host: [window.innerWidth, window.innerHeight],
        animating: !!raf, fps: fps, emotion: emotion.name,
        gaze: [Math.round(gaze.x * 100) / 100, Math.round(gaze.y * 100) / 100],
        pointer: pointerKnown,
      };
    },
    /* 当前参数真值，只给开发者面板和自检查询，不发给模型。 */
    samples: function () {
      var out = {};
      if (!r || !ready) { return out; }
      try {
        var list = r.parameters() || [];
        for (var i = 0; i < list.length; i++) {
          out[list[i].id] = Math.round(list[i].value * 1000) / 1000;
        }
      } catch (e) { }
      return out;
    },
    metrics: function () {
      return {
        ready: ready, animating: !!raf, fps: fps, frames: frames,
        params: Object.keys(params).length,
        emotion: emotion.name, blink: blink.active, blinks: blink.count,
        pointer: pointerKnown, mask: maskInfo ? [maskInfo.cols, maskInfo.rows] : null,
        reactions: react.name || null,
        motion: reducedMotion ? 'reduced' : 'full',
      };
    },
    /* 减少动态效果：摆动和呼吸停下来，眨眼保留，不然看着像卡住了。 */
    setMotion: function (enabled) {
      reducedMotion = !enabled;
      return { ok: true, motion: reducedMotion ? 'reduced' : 'full' };
    },
    redraw: function () { if (r) { r.draw(); } return true; },
    mask: function () { return maskInfo ? { cols: maskInfo.cols, rows: maskInfo.rows } : null; },
    maskRaw: function () { return r && ready ? r.mask(6) : null; },
    /* 把当前画面导成 PNG：开发者面板和自检脚本用它核对角色到底长什么样。 */
    capture: function () { return r && ready ? (r.capture() || '') : ''; },
    hit: hitCss,
    start: function () { startLoop(); return true; },
    stop: function () { stopLoop(); return true; },
  };

  function onResize() {
    fitCanvas();
    if (r) { r.draw(); rebuildMask(); }
  }

  canvas.addEventListener('mousedown', function (ev) {
    if (ev.button !== 0 || !hitCss(ev.clientX, ev.clientY)) { return; }
    drag = { sx: ev.screenX, sy: ev.screenY, moved: false, sent: false };
    ev.preventDefault();
  });
  /* 视线只认宿主推来的全局光标（视场是窗口宽的 POINTER_FIELD 倍）。页面自己也收得到
     鼠标事件时不能再喂一遍：两套换算不一样，光标一进窗口头就会蹿一下。 */
  window.addEventListener('mousemove', function (ev) {
    if (!drag) { return; }
    var dx = ev.screenX - drag.sx, dy = ev.screenY - drag.sy;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) <= DRAG_SLOP) { return; }
    if (!drag.moved) { drag.moved = true; emit('drag_begin', null); }
    emit('drag', { dx: dx, dy: dy });
  });
  window.addEventListener('mouseup', function (ev) {
    if (!drag) { return; }
    var moved = drag.moved;
    drag = null;
    emit(moved ? 'drag_end' : 'click', null);
    if (!moved) { Pet.react('click'); }
  });
  window.addEventListener('contextmenu', function (ev) {
    ev.preventDefault();
    if (hitCss(ev.clientX, ev.clientY)) { emit('right_click', null); }
  });
  canvas.addEventListener('dblclick', function (ev) {
    if (hitCss(ev.clientX, ev.clientY)) { emit('double_click', null); Pet.react('double_click'); }
  });
  canvas.addEventListener('mouseenter', function (ev) {
    if (hitCss(ev.clientX, ev.clientY)) { Pet.react('hover'); }
  });
  window.addEventListener('resize', onResize);

  window.Pet = Pet;
  if (window.__AUTO_URL) { Pet.load(window.__AUTO_URL); }
})();
