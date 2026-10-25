/* DeskPet Live2D 渲染器：只依赖 Live2D Cubism Core（官方），做静态渲染。
   预留：参数、表情、动作、点击命中、捕获画布。 */
(function () {
  'use strict';

  const VERT = ['attribute vec2 a_pos;', 'attribute vec2 a_uv;', 'uniform mat4 u_mvp;',
    'varying vec2 v_uv;', 'void main() {', '  v_uv = a_uv;',
    '  gl_Position = u_mvp * vec4(a_pos, 0.0, 1.0);', '}'].join('\n');
  // u_clip > 0.5 是"画裁剪形状"那一遍：只留真正画到的地方，透明像素直接丢掉。
  const FRAG = ['precision mediump float;', 'varying vec2 v_uv;', 'uniform sampler2D u_tex;',
    'uniform float u_opacity;', 'uniform float u_solid;', 'uniform vec4 u_color;',
    'uniform float u_clip;',
    'void main() {', '  vec4 c = texture2D(u_tex, v_uv);',
    '  if (u_clip > 0.5 && c.a * u_opacity <= 0.01) discard;',
    '  vec4 base = (u_solid > 0.5) ? u_color : vec4(c.rgb, c.a);',
    '  gl_FragColor = base * u_opacity;', '}'].join('\n');

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      throw new Error('着色器编译失败: ' + gl.getShaderInfoLog(sh));
    }
    return sh;
  }

  /* Cubism 官方语义：renderOrders[i] 表示"第 i 个元素画在第几个槽位"，
     所以要按槽位升序排出绘制顺序（与 Cubism Web Framework 的 drawObjectLoop 一致）。
     数组长度是 drawableCount + offscreenCount，这里只画 drawable。 */
  function sortDrawOrder(raw, drawableCount) {
    const total = raw.length;
    const seq = new Array(total);
    for (let i = 0; i < total; i++) seq[i] = i;
    seq.sort(function (a, b) { return raw[a] - raw[b]; });
    const out = [];
    for (let n = 0; n < total; n++) {
      if (seq[n] < drawableCount) out.push(seq[n]);
    }
    return out;
  }

  /* 某个元素要用哪些元素当裁剪形状。Core 给的是"每个元素自带一串"，
     老版本给的是平铺的一串，两种都认。 */
  function maskIndices(table, index, from, to) {
    if (!table) return null;
    const own = table[index];
    if (own && typeof own.length === 'number') return own;
    const out = [];
    for (let n = from; n < to; n++) out.push(table[n]);
    return out;
  }

  function create(canvas, opts) {
    opts = opts || {};
    const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, preserveDrawingBuffer: true, antialias: true, stencil: true })
      || canvas.getContext('experimental-webgl', { alpha: true, premultipliedAlpha: true, preserveDrawingBuffer: true });
    if (!gl) throw new Error('这台机器没有可用的 WebGL');

    const program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error('着色器链接失败: ' + gl.getProgramInfoLog(program));
    }
    const aPos = gl.getAttribLocation(program, 'a_pos');
    const aUv = gl.getAttribLocation(program, 'a_uv');
    const uMvp = gl.getUniformLocation(program, 'u_mvp');
    const uTex = gl.getUniformLocation(program, 'u_tex');
    const uOpacity = gl.getUniformLocation(program, 'u_opacity');
    const uSolid = gl.getUniformLocation(program, 'u_solid');
    const uColor = gl.getUniformLocation(program, 'u_color');
    const uClip = gl.getUniformLocation(program, 'u_clip');

    const state = {
      status: 'idle', error: null, moc: null, model: null, textures: [], slots: [],
      box: null, boxSize: [0, 0], canvasInfo: null, masks: 0, frames: 0, url: '',
      drawables: 0, parameters: 0, expressions: [], motionGroups: [],
    };

    function setError(message) {
      state.status = 'error';
      state.error = String(message);
      return false;
    }

    function loadImage(url) {
      return new Promise(function (resolve, reject) {
        const img = new Image();
        img.onload = function () { resolve(img); };
        img.onerror = function () { reject(new Error('贴图读不出来: ' + url)); };
        img.src = url;
      });
    }

    function makeTexture(image) {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, opts.flipUV === undefined ? true : !!opts.flipUV);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      return tex;
    }

    function measureBox() {
      const d = state.model.drawables;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (let i = 0; i < d.count; i++) {
        const vp = d.vertexPositions[i];
        for (let k = 0; k < vp.length; k += 2) {
          if (vp[k] < minX) minX = vp[k];
          if (vp[k] > maxX) maxX = vp[k];
          if (vp[k + 1] < minY) minY = vp[k + 1];
          if (vp[k + 1] > maxY) maxY = vp[k + 1];
        }
      }
      if (!isFinite(minX) || maxX <= minX) return null;
      state.box = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
      state.boxSize = [maxX - minX, maxY - minY];
      return state.box;
    }

    function mvpMatrix() {
      const box = state.box;
      const w = canvas.width, h = canvas.height;
      const viewScale = opts.viewScale || 1;
      const fit = Math.min(w / box.w, h / box.h) * viewScale;
      const flip = opts.flipY ? -1 : 1;
      const sx = (2 * fit) / w, sy = ((2 * fit) / h) * flip;
      const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
      const ox = opts.offsetX || 0, oy = opts.offsetY || 0;
      return new Float32Array([
        sx, 0, 0, 0,
        0, sy, 0, 0,
        0, 0, 1, 0,
        -cx * sx + ox, -cy * sy + oy, 0, 1,
      ]);
    }

    function uploadSlot(i) {
      const d = state.model.drawables;
      let slot = state.slots[i];
      if (!slot) {
        slot = state.slots[i] = {
          pos: gl.createBuffer(), uv: gl.createBuffer(), idx: gl.createBuffer(), indexCount: 0,
        };
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, slot.pos);
      gl.bufferData(gl.ARRAY_BUFFER, d.vertexPositions[i], gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, slot.uv);
      gl.bufferData(gl.ARRAY_BUFFER, d.vertexUvs[i], gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, slot.idx);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, d.indices[i], gl.STATIC_DRAW);
      slot.indexCount = d.indexCounts[i];
      return slot;
    }


    /* 裁剪（Cubism 的 clipping mask）：带 mask 的元素只画在 mask 形状里面，
       多个 mask 取交集。用模板缓冲做，和官方框架的语义一致；没有 mask 的
       元素照旧直接画。 */
    function paint(i) {
      const d = state.model.drawables;
      const texture = state.textures[d.textureIndices[i]];
      if (!texture) return false;
      const slot = uploadSlot(i);
      if ((d.constantFlags[i] & 2) !== 0) gl.blendFuncSeparate(gl.ONE, gl.ONE, gl.ONE, gl.ONE);
      else gl.blendFuncSeparate(gl.ONE, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.uniform1i(uTex, 0);
      gl.uniform1f(uOpacity, d.opacities[i]);
      gl.uniform1f(uSolid, opts.debugSolid ? 1 : 0);
      if (opts.debugSolid) { const h = (i * 0.618) % 1; gl.uniform4f(uColor, ((h*3)%1)*0.7+0.3, ((h*7)%1)*0.7+0.3, ((h*11)%1)*0.7+0.3, 1); }
      gl.bindBuffer(gl.ARRAY_BUFFER, slot.pos);
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, slot.uv);
      gl.enableVertexAttribArray(aUv);
      gl.vertexAttribPointer(aUv, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, slot.idx);
      gl.drawElements(gl.TRIANGLES, slot.indexCount, gl.UNSIGNED_SHORT, 0);
      return true;
    }

    function drawFrame() {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.colorMask(true, true, true, true);
      gl.stencilMask(0xFF);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.STENCIL_BUFFER_BIT);
      if (!state.model) return 0;
      const d = state.model.drawables;
      const order = sortDrawOrder(state.model.getRenderOrders(), d.count);
      const mvp = mvpMatrix();
      const offsets = new Int32Array(d.count + 1);
      for (let i = 0; i < d.count; i++) offsets[i + 1] = offsets[i] + d.maskCounts[i];
      const maskTable = d.masks;
      gl.useProgram(program);
      gl.uniformMatrix4fv(uMvp, false, mvp);
      gl.enable(gl.BLEND);
      gl.disable(gl.CULL_FACE);
      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.STENCIL_TEST);
      gl.uniform1f(uClip, 0);
      let drawn = 0;
      for (let n = 0; n < order.length; n++) {
        const i = order[n];
        const opacity = d.opacities[i];
        if (!(opacity > 0)) continue;
        const flags = d.dynamicFlags[i];
        if (Live2DCubismCore.DynamicFlags && Live2DCubismCore.DynamicFlags.hasIsVisibleBit && !Live2DCubismCore.DynamicFlags.hasIsVisibleBit(flags)) continue;
        const refs = maskIndices(maskTable, i, offsets[i], offsets[i + 1]);
        if (!refs || !refs.length) {
          if (paint(i)) drawn++;
          continue;
        }
        // 把 mask 的形状先画进模板缓冲（不写颜色），再在模板里画元素本身。
        gl.enable(gl.STENCIL_TEST);
        gl.colorMask(false, false, false, false);
        gl.stencilMask(0xFF);
        gl.clear(gl.STENCIL_BUFFER_BIT);
        gl.uniform1f(uClip, 1);
        for (let m = 0; m < refs.length; m++) {
          gl.stencilFunc(m === 0 ? gl.ALWAYS : gl.EQUAL, 1, 0xFF);
          gl.stencilOp(gl.KEEP, gl.REPLACE, gl.REPLACE);
          paint(refs[m]);
        }
        gl.uniform1f(uClip, 0);
        gl.colorMask(true, true, true, true);
        gl.stencilFunc(gl.EQUAL, 1, 0xFF);
        gl.stencilOp(gl.KEEP, gl.KEEP, gl.KEEP);
        if (paint(i)) drawn++;
        gl.disable(gl.STENCIL_TEST);
      }
      state.frames++;
      return drawn;
    }

    async function load(url) {
      try {
        state.status = 'loading';
        state.url = url;
        const response = await fetch(url);
        if (!response.ok) throw new Error('模型文件读不出来: ' + response.status);
        const setting = await response.json();
        const refs = setting.FileReferences || {};
        const base = url.slice(0, url.lastIndexOf('/') + 1);
        const mocUrl = base + (refs.Moc || refs.moc);
        const bytes = await (await fetch(mocUrl)).arrayBuffer();
        const moc = Live2DCubismCore.Moc.fromArrayBuffer(bytes);
        if (!moc) throw new Error('Core 读不了这个 moc3（版本不匹配或文件损坏）');
        const model = Live2DCubismCore.Model.fromMoc(moc);
        if (!model) throw new Error('moc3 解析失败');
        state.moc = moc;
        state.model = model;
        state.canvasInfo = model.canvasinfo;
        state.drawables = model.drawables.count;
        state.parameters = model.parameters.count;
        let masks = 0;
        for (let i = 0; i < model.drawables.count; i++) masks += model.drawables.maskCounts[i];
        state.masks = masks;
        const textures = refs.Textures || refs.textures || [];
        state.textures = [];
        for (let i = 0; i < textures.length; i++) {
          state.textures.push(makeTexture(await loadImage(base + textures[i])));
        }
        if (refs.Expressions) state.expressions = refs.Expressions.map(function (e) { return e.Name || e.File; });
        if (refs.Motions) state.motionGroups = Object.keys(refs.Motions);
        model.update();
        if (!measureBox()) throw new Error('模型没有任何可画的顶点');
        state.status = 'ready';
        state.error = null;
        return api.status();
      } catch (err) {
        setError(err && err.message ? err.message : err);
        return api.status();
      }
    }

    function unload() {
      state.slots.forEach(function (slot) {
        gl.deleteBuffer(slot.pos); gl.deleteBuffer(slot.uv); gl.deleteBuffer(slot.idx);
      });
      state.textures.forEach(function (tex) { gl.deleteTexture(tex); });
      state.slots = [];
      state.textures = [];
      if (state.model) { try { state.model.release(); } catch (e) {} }
      if (state.moc) { try { state.moc._release(); } catch (e) {} }
      state.model = null;
      state.moc = null;
      state.status = 'idle';
      state.frames = 0;
      return true;
    }

    function scan() {
      const w = canvas.width, h = canvas.height;
      const px = new Uint8Array(w * h * 4);
      gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
      let solid = 0, minX = w, maxX = -1, minY = h, maxY = -1;
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
          if (px[(y * w + x) * 4 + 3] > 8) {
            solid++;
            if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
          }
        }
      }
      const corners = [px[3], px[(w - 1) * 4 + 3], px[(h - 1) * w * 4 + 3], px[((h - 1) * w + w - 1) * 4 + 3]];
      return { solid: solid, box: [minX, minY, maxX, maxY], corners: corners, size: [w, h] };
    }

    const api = {
      load: load,
      unload: unload,
      status: function () {
        return {
          status: state.status, error: state.error, url: state.url,
          drawables: state.drawables, parameters: state.parameters,
          textures: state.textures.length, masks: state.masks,
          canvasInfo: state.canvasInfo, boxSize: state.boxSize,
          expressions: state.expressions, motionGroups: state.motionGroups,
          frames: state.frames, canvas: [canvas.width, canvas.height],
        };
      },
      resize: function (w, h) {
        if (w > 0) canvas.width = Math.floor(w);
        if (h > 0) canvas.height = Math.floor(h);
        return [canvas.width, canvas.height];
      },
      mask: function (block) {
        block = Math.max(2, Math.floor(block || 6));
        const w = canvas.width, h = canvas.height;
        const px = new Uint8Array(w * h * 4);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
        const cols = Math.ceil(w / block), rows = Math.ceil(h / block);
        const bits = new Uint8Array(Math.ceil(cols * rows / 8));
        for (let by = 0; by < rows; by++) {
          const gy0 = Math.max(0, h - (by + 1) * block), gy1 = Math.min(h, h - by * block);
          for (let bx = 0; bx < cols; bx++) {
            let on = 0;
            for (let y = gy0; y < gy1 && on === 0; y++) {
              for (let x = bx * block; x < Math.min(w, (bx + 1) * block); x++) {
                if (px[(y * w + x) * 4 + 3] > 16) { on = 1; break; }
              }
            }
            const idx = by * cols + bx;
            if (on) bits[idx >> 3] |= (1 << (idx & 7));
          }
        }
        let s = '';
        for (let i = 0; i < bits.length; i++) s += String.fromCharCode(bits[i]);
        return { block: block, cols: cols, rows: rows, w: w, h: h, data: btoa(s) };
      },
      diag: function () {
        if (!state.model) return null;
        const d = state.model.drawables;
        const out = [];
        for (let i = 0; i < d.count; i++) {
          const vp = d.vertexPositions[i];
          let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
          for (let k = 0; k < vp.length; k += 2) {
            if (vp[k] < minX) minX = vp[k]; if (vp[k] > maxX) maxX = vp[k];
            if (vp[k + 1] < minY) minY = vp[k + 1]; if (vp[k + 1] > maxY) maxY = vp[k + 1];
          }
          out.push({
            i: i, id: d.ids[i], order: d.drawOrders[i], tex: d.textureIndices[i],
            op: d.opacities[i], constFlags: d.constantFlags[i], dynFlags: d.dynamicFlags[i],
            vCount: vp.length / 2, iCount: d.indexCounts[i], maskN: d.maskCounts[i],
            bbox: [Math.round(minX*1000)/1000, Math.round(minY*1000)/1000, Math.round(maxX*1000)/1000, Math.round(maxY*1000)/1000],
          });
        }
        return out;
      },
      drawFrame: drawFrame,
      draw: function () { try { if (state.model) state.model.update(); } catch (e) {} return drawFrame(); },
      scan: scan,
      capture: function () { return canvas.toDataURL('image/png'); },
      setView: function (o) { opts = Object.assign({}, opts, o || {}); return api.status(); },
      setParameter: function (id, value) {
        if (!state.model) return false;
        state.model.parameters.values[state.model.parameters.ids.indexOf(id)] = value;
        return true;
      },
      setParameterByIndex: function (index, value) {
        if (!state.model) return false;
        state.model.parameters.values[index] = value;
        return true;
      },
      parameters: function () {
        if (!state.model) return [];
        return state.model.parameters.ids.map(function (id, i) {
          const p = state.model.parameters;
          return { id: id, value: p.values[i], min: p.minimumValues[i], max: p.maximumValues[i], default: p.defaultValues[i] };
        });
      },
    };
    return api;
  }

  window.PetRender = {
    create: create,
    sortDrawOrder: sortDrawOrder,
    masksOf: maskIndices,
    version: 'deskpet-live2d-1',
  };
})();
