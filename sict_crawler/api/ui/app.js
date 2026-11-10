// Khung chat SICT: gửi câu hỏi (kèm ảnh nếu có) tới /chat, đọc SSE (status → nguồn → delta/answer → citations → [DONE]).
// Giao diện 2026-11-05: cột hội thoại giữa màn hình, ô nhập cố định ở đáy, màn hình chào với câu hỏi gợi ý.
const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('q');
const btn = document.getElementById('go');
const fileInput = document.getElementById('img');
const preview = document.getElementById('preview');
const pvimg = document.getElementById('pvimg');
const empty = document.getElementById('empty');
const newchat = document.getElementById('newchat');
const topbar = document.getElementById('top');   // không đặt tên 'top': trùng window.top → SyntaxError
let pendingB64 = null;   // base64 của ảnh đính kèm, không có tiền tố data:
let pendingUrl = null;   // data: URL để hiện lại ảnh trong bong bóng câu hỏi
let busy = false;
let current = null;      // AbortController của câu hỏi đang chạy (bấm "Cuộc trò chuyện mới" thì huỷ)

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function link(cls, text, href) {
  const a = el('a', cls, text);
  a.href = href; a.target = '_blank'; a.rel = 'noopener';
  return a;
}

// ---------- ô nhập ----------
function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}
input.addEventListener('input', autosize);
input.addEventListener('keydown', (ev) => {
  // Enter gửi, Shift+Enter xuống dòng; đang gõ dấu tiếng Việt (IME, isComposing) thì Enter thuộc bộ gõ
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
    ev.preventDefault();
    form.requestSubmit ? form.requestSubmit() : btn.click();
  }
});

function attach(file) {
  if (!file || !/^image\/(jpeg|png)$/.test(file.type)) return;
  const r = new FileReader();
  r.onload = () => {
    pendingUrl = r.result;
    pendingB64 = r.result.split(',')[1];
    pvimg.src = r.result;
    preview.hidden = false;
    input.focus();
  };
  r.readAsDataURL(file);
}
function clearAttachment() {
  pendingB64 = null; pendingUrl = null; fileInput.value = ''; preview.hidden = true;
}
document.getElementById('pick').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => attach(fileInput.files[0]));
document.getElementById('pvx').addEventListener('click', clearAttachment);
input.addEventListener('paste', (ev) => {   // dán ảnh từ clipboard vào ô nhập
  const f = [...(ev.clipboardData && ev.clipboardData.files || [])].find((x) => x.type.startsWith('image/'));
  if (f) { ev.preventDefault(); attach(f); }
});

// ---------- màn hình chào / cuộc trò chuyện mới ----------
for (const chip of document.querySelectorAll('.chip')) {
  chip.addEventListener('click', () => ask(chip.dataset.q, null, null));
}
newchat.addEventListener('click', () => {
  if (current) current.abort();
  log.textContent = '';
  empty.hidden = false;
  newchat.hidden = true;
  clearAttachment();
  input.value = ''; autosize(); input.focus();
  window.scrollTo(0, 0);
});
window.addEventListener('scroll', () => topbar.classList.toggle('scrolled', window.scrollY > 4), {passive: true});

// chỉ tự cuộn theo câu trả lời khi người đọc đang ở cuối trang (đang cuộn lên đọc thì để yên)
const nearBottom = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 140;
const toBottom = () => window.scrollTo(0, document.documentElement.scrollHeight);

// ---------- xem ảnh lớn ----------
const lb = document.getElementById('lb');
const lbimg = document.getElementById('lbimg');
function openImage(src, caption, pageUrl) {
  lbimg.src = src; lbimg.alt = caption || '';
  document.getElementById('lbcap').textContent = caption || '';
  document.getElementById('lbopen').href = src;
  const pg = document.getElementById('lbpage');
  pg.hidden = !pageUrl; if (pageUrl) pg.href = pageUrl;
  lb.hidden = false;
}
const closeImage = () => { lb.hidden = true; lbimg.removeAttribute('src'); };
document.getElementById('lbx').addEventListener('click', closeImage);
lb.addEventListener('click', (ev) => { if (ev.target === lb) closeImage(); });
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && !lb.hidden) closeImage(); });

// ---------- nguồn ----------
// Thứ tự dưới một câu trả lời (2026-11-09): câu trả lời (nguồn là nút mờ superscript inline) → ghi chú → "Ảnh & Video".
const fmtDate = (d) => d ? d.slice(0, 10).split('-').reverse().join('/') : '';
const pageOf = (s) => (s.kind !== 'file' && s.parent_url) || s.url;    // ảnh/video → bài chứa nó
const VIDEO = new Set(['video', 'video_frame']);

function srcLabel(s) {
  // không còn tiền tố kỹ thuật "text ·" / "image ·"; file giữ "tải file", video có dấu ▶ (kèm giây nếu có)
  const pre = s.kind === 'file' ? 'tải file · '
    : VIDEO.has(s.kind) ? '▶ ' + (s.timestamp != null ? `${s.timestamp.toFixed(0)}s · ` : '') : '';
  // D23: mục chứa đoạn trích trong bài (heading_path) giúp biết đoạn nằm ở đâu
  const where = s.heading_path && s.heading_path.length ? ' › ' + s.heading_path.join(' › ') : '';
  const date = s.published_at ? ` (${fmtDate(s.published_at)})` : '';
  return pre + (s.title || s.excerpt || s.url).slice(0, 70) + where.slice(0, 80) + date;
}

function srcLink(s, href) {
  const label = srcLabel(s);
  const a = link('src ' + s.kind, label, href || s.url);
  a.title = label;
  return a;
}

// lưới ảnh/video ngay dưới câu trả lời: câu hỏi xin ảnh thấy ảnh ngay, không phải cuộn qua các chip nguồn
function renderMedia(box, media) {
  const withImg = (media || []).filter((s) => s.media_key || s.media_id);
  if (!withImg.length) return;
  box.hidden = false;
  // D21: ảnh giống ảnh người gửi (via=i2i) tách khỏi ảnh liên quan tới câu hỏi
  for (const [title, pick] of [['Ảnh tương tự', (s) => s.via === 'i2i'],
                               ['Ảnh & Video', (s) => s.via !== 'i2i']]) {
    const group = withImg.filter(pick);
    if (!group.length) continue;
    box.appendChild(el('span', 'mhead', title));
    // bấm ảnh mở khung xem lớn (Ctrl/⌘ + bấm mở ảnh trong tab mới); chú thích là link tới bài chứa ảnh
    const grid = el('div', 'mgrid');
    for (const s of group) {
      const card = el('figure', 'mcard');
      const src = s.media_key ? '/media/s/' + s.media_key : '/media/' + s.media_id;
      const a = link(null, null, src);
      const img = document.createElement('img');
      img.src = src;
      img.alt = (s.excerpt || s.title || s.kind).slice(0, 80);
      img.loading = 'lazy';
      a.appendChild(img);
      a.addEventListener('click', (ev) => {
        if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button !== 0) return;
        ev.preventDefault();
        openImage(src, s.title || s.excerpt || '', pageOf(s));
      });
      card.appendChild(a);
      const cap = el('figcaption');
      cap.appendChild(srcLink(s, pageOf(s)));
      card.appendChild(cap);
      grid.appendChild(card);
    }
    box.appendChild(grid);
  }
}

// 2026-11-09 (chủ duyệt): không còn hàng chip "Trích dẫn" / "tải file" cuối câu trả lời — nguồn hiện thành nút mờ
// superscript ngay sau đoạn dùng nó (md.js). Hàm này chỉ chọn link cho [n]: file → mở thẳng file (PDF/PPTX),
// ảnh/video → bài chứa nó, đoạn văn → bài. all = nguồn chữ + media theo đúng thứ tự đánh số [1]…[n] của server.
function citeTarget(c, all) {
  const s = all[c.n - 1];
  const href = (s && s.url === c.url ? pageOf(s) : null) || c.url;
  return {...c, url: href, cite: c.cite || c.title || href};
}

function httpError(status) {
  if (status === 429) return 'Hệ thống đang nhận quá nhiều câu hỏi. Vui lòng thử lại sau ít phút.';
  if (status >= 500) return `Máy chủ đang gặp sự cố (HTTP ${status}). Vui lòng thử lại.`;
  return `Không gửi được câu hỏi (HTTP ${status}).`;
}

// ---------- một lượt hỏi ----------
async function ask(q, b64, imgUrl) {
  if (busy || (!q && !b64)) return;
  busy = true; btn.disabled = true;
  empty.hidden = true; newchat.hidden = false;
  const ctrl = new AbortController();
  current = ctrl;

  const ut = el('div', 'turn user');
  const um = el('div', 'msg user');
  if (b64) {
    const uimg = document.createElement('img');
    uimg.src = imgUrl || 'data:image/jpeg;base64,' + b64;
    uimg.alt = 'Ảnh đã gửi';
    um.appendChild(uimg);
  }
  if (q) um.appendChild(document.createTextNode(q));
  ut.appendChild(um);

  const bt = el('div', 'turn bot');
  const av = document.createElement('img');
  av.className = 'avatar'; av.src = '/ui/haui-emblem.svg?v=1'; av.alt = '';
  const bot = el('div', 'msg bot');
  const phase = el('div', 'phase');
  const phaseText = el('span', null, b64 ? 'Đang tìm ảnh và bài viết liên quan' : 'Đang tìm trong dữ liệu của Trường');
  const dots = el('span', 'dots');
  dots.append(el('i'), el('i'), el('i'));
  phase.append(dots, phaseText);
  const body = el('div', 'md');
  const notes = el('div');
  const mbox = el('div', 'media'); mbox.hidden = true;   // lưới "Ảnh & Video" ngay dưới câu trả lời
  bot.append(phase, body, notes, mbox);
  bt.append(av, bot);
  log.append(ut, bt);
  toBottom();

  let raw = '', cites = {}, done = false, all = [];
  // câu trả lời là markdown: dựng lại toàn bộ sau mỗi delta (md.js escape HTML trước khi dựng thẻ)
  const paint = () => {
    const stick = nearBottom();
    if (phase.isConnected && raw) { phase.remove(); body.classList.add('streaming'); }
    body.innerHTML = renderMarkdown(raw, cites);
    if (stick) toBottom();
  };
  const fail = (msg) => {
    phase.remove();
    body.classList.remove('streaming');
    const box = el('div', 'err', msg);
    const again = el('button', null, 'Thử lại');
    again.type = 'button';
    again.addEventListener('click', () => { ut.remove(); bt.remove(); ask(q, b64, imgUrl); });
    box.appendChild(again);
    (raw ? notes : body).appendChild(box);
  };

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q, top_k: 8, image: b64}),
      signal: ctrl.signal,
    });
    if (!res.ok || !res.body) throw new Error(httpError(res.status));
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done: end, value} = await reader.read();
      if (end) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') { done = true; continue; }
        let obj;
        try { obj = JSON.parse(data); } catch { continue; }
        if (obj.delta) { raw += obj.delta; paint(); }
        if (obj.answer) { raw = obj.answer; paint(); }          // server chuẩn hoá lại câu trả lời (bỏ [n] ảo)
        // D24: trích dẫn bắt buộc — [n] ↔ nguồn; md.js vẽ thành nút mờ superscript, bấm mở nguồn
        if (obj.citations && obj.citations.length) {
          for (const c of obj.citations) cites[c.n] = citeTarget(c, all);
          paint();
        }
        if (obj.status) {
          if (phase.isConnected) phaseText.textContent = 'Đang soạn câu trả lời';
          // router D17: hỏi ảnh mà không có ảnh đạt bằng chứng → ghi chú, không hiện ảnh sai
          if (obj.status.media_note) notes.appendChild(el('div', 'note', obj.status.media_note));
          // D27: bằng chứng chưa đủ (agent.evidence) → báo người đọc câu trả lời có thể thiếu
          if (obj.status.sufficient === false) {
            notes.appendChild(el('div', 'note', 'Tài liệu tìm được chỉ liên quan một phần, câu trả lời có thể chưa đầy đủ.'));
          }
        }
        if (obj.text || obj.media) {
          all = [...(obj.text || []), ...(obj.media || [])];   // đánh số [n] như server: chữ trước, media sau
          const stick = nearBottom();
          renderMedia(mbox, obj.media);
          if (stick) toBottom();
        }
      }
    }
    body.classList.remove('streaming');
    if (!raw) fail('Không nhận được câu trả lời từ máy chủ.');
    else if (!done) fail('Kết nối bị ngắt giữa chừng, câu trả lời có thể chưa trọn.');
  } catch (e) {
    if (e.name === 'AbortError') return;
    fail(e instanceof TypeError ? 'Không kết nối được máy chủ. Kiểm tra mạng rồi thử lại.' : e.message);
  } finally {
    if (current === ctrl) {
      current = null; busy = false; btn.disabled = false;
      input.focus({preventScroll: true});
    }
  }
}

form.addEventListener('submit', (ev) => {
  ev.preventDefault();
  const q = input.value.trim();
  if (busy || (!q && !pendingB64)) return;
  const b64 = pendingB64, url = pendingUrl;
  input.value = ''; autosize();
  clearAttachment();
  ask(q, b64, url);
});
