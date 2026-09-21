const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('q');
const btn = document.getElementById('go');
const fileInput = document.getElementById('img');
const preview = document.getElementById('preview');
const pvimg = document.getElementById('pvimg');
let pendingB64 = null;  // base64 of attached image, no data: prefix

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

document.getElementById('pick').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  const f = fileInput.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    pendingB64 = r.result.split(',')[1];
    pvimg.src = r.result;
    preview.hidden = false;
  };
  r.readAsDataURL(f);
});
document.getElementById('pvx').addEventListener('click', () => {
  pendingB64 = null; fileInput.value = ''; preview.hidden = true;
});

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const q = input.value.trim();
  if (!q && !pendingB64) return;
  log.appendChild(el('div', 'msg user', q + (pendingB64 ? '  📎[ảnh]' : '')));
  if (pendingB64) {
    const uimg = document.createElement('img');
    uimg.className = 'srcimg'; uimg.style.maxWidth = '160px';
    uimg.src = 'data:image/jpeg;base64,' + pendingB64;
    log.appendChild(uimg);
  }
  input.value = '';
  const b64 = pendingB64;
  pendingB64 = null; fileInput.value = ''; preview.hidden = true;
  btn.disabled = true;

  const bot = el('div', 'msg');
  const body = el('div');
  bot.appendChild(body);
  log.appendChild(bot);
  bot.scrollIntoView();

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q, top_k: 8, image: b64}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') continue;
        const obj = JSON.parse(data);
        if (obj.delta) body.textContent += obj.delta;
        if (obj.text || obj.media) {
          const box = el('div', 'srcs');

          function srcLink(s) {
            const label = s.kind + (s.timestamp != null ? ` @ ${s.timestamp.toFixed(1)}s` : '') +
              ' · ' + (s.excerpt || s.title || s.url).slice(0, 60);
            const a = el('a', 'src ' + s.kind, label);
            a.href = s.url; a.target = '_blank';
            return a;
          }

          const mediaWithImg = (obj.media || []).filter(s => s.media_id);
          if ((obj.text || []).length || mediaWithImg.length) {
            box.appendChild(el('span', null, 'Văn bản: '));
            for (const s of obj.text || []) {
              if (s.media_id && (s.kind === 'image' || s.kind === 'video_frame')) {
                const img = document.createElement('img');
                img.className = 'srcimg';
                img.src = '/media/' + s.media_id;
                img.alt = (s.excerpt || s.kind).slice(0, 80);
                img.loading = 'lazy';
                box.appendChild(img);
              }
              box.appendChild(srcLink(s));
            }
            if (mediaWithImg.length) {
              box.appendChild(el('span', null, 'Ảnh & Video: '));
              for (const s of mediaWithImg) {
                const img = document.createElement('img');
                img.className = 'srcimg';
                img.src = '/media/' + s.media_id;
                img.alt = (s.excerpt || s.kind).slice(0, 80);
                img.loading = 'lazy';
                box.appendChild(img);
                box.appendChild(srcLink(s));
              }
            }
            bot.appendChild(box);
          }
        }
      }
      bot.scrollIntoView();
    }
  } catch (e) {
    body.textContent = 'Lỗi: ' + e.message;
  } finally {
    btn.disabled = false;
    input.focus();
  }
});
