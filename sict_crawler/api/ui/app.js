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
// Thứ tự dưới một câu trả lời (2026-11-05): câu trả lời → ghi chú → lưới "Ảnh & Video" → "Trích dẫn" (gộp theo bài)
// → tệp đính kèm (hành động, luôn hiện). Không còn danh sách mọi đoạn tìm được: chỉ một danh sách nguồn là "Trích dẫn".
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

// tệp đính kèm là hành động (tải về / mở trang chứa file) → luôn hiện; mọi đoạn văn bản tìm được → mục thu gọn
function renderText(box, text) {
  if (!(text || []).length) return;
  box.hidden = false;
  const files = [], seenFile = new Set();
  for (const s of text) if (s.kind === 'file' && !seenFile.has(s.url)) { seenFile.add(s.url); files.push(s); }
  if (files.length) {
    const row = el('div', 'files');
    for (const s of files) {
      row.appendChild(srcLink(s));
      if (s.parent_url) row.appendChild(link('src text', 'trang chứa file', s.parent_url));
    }
    box.appendChild(row);
  }
  // Không hiện danh sách "Nguồn tham khảo" (mọi đoạn tìm được): chủ dự án 2026-11-05 chỉ giữ một danh sách nguồn
  // dưới câu trả lời là "Trích dẫn" (nguồn câu trả lời thật sự dùng; không trích gì thì server thêm "Nguồn:").
  if (!files.length) box.hidden = true;
}

// "[3][7]", "[11]–[16]": số liền nhau ≥ 3 gộp thành khoảng
function fmtNums(ns) {
  const out = [];
  for (let i = 0; i < ns.length;) {
    let j = i;
    while (j + 1 < ns.length && ns[j + 1] === ns[j] + 1) j++;
    if (j - i >= 2) out.push(`[${ns[i]}]–[${ns[j]}]`);
    else for (let k = i; k <= j; k++) out.push(`[${ns[k]}]`);
    i = j + 1;
  }
  return out.join('');
}

// D24: mỗi chip một bài/tài liệu: các [n] trỏ cùng bài (đoạn văn của bài + ảnh của bài) gộp lại, link tới bài đó.
// all = nguồn chữ + nguồn media theo đúng thứ tự đánh số [1]…[n] của server (citations.index / enforce).
function renderCitations(box, list, all) {
  box.textContent = '';
  box.hidden = false;
  box.appendChild(el('span', 'lbl', 'Trích dẫn'));
  const groups = new Map();
  for (const c of list) {
    const s = all[c.n - 1];
    const href = (s && s.url === c.url ? pageOf(s) : null) || c.url;
    const key = (href || c.title || String(c.n)).replace(/^https?:\/\//, '').replace(/\/+$/, '').toLowerCase();
    if (!groups.has(key)) groups.set(key, {href, items: []});
    groups.get(key).items.push(c);
  }
  const ordered = [...groups.values()].sort((x, y) => Math.min(...x.items.map((c) => c.n)) - Math.min(...y.items.map((c) => c.n)));
  for (const g of ordered) {
    const ns = g.items.map((c) => c.n).sort((a, b) => a - b);
    const first = g.items[0];
    // một [n]: nhãn cite đầy đủ (file kèm trang, bài kèm mục); nhiều [n] cùng bài: tên bài
    const cites = new Set(g.items.map((c) => c.cite || c.title));
    const name = cites.size === 1 ? (first.cite || first.title || g.href) : (first.title || first.cite || g.href);
    const kind = g.items.some((c) => c.kind === 'file') ? 'file' : 'text';
    const a = link('src cite-' + kind, `${fmtNums(ns)} ${name}`.slice(0, 140), g.href);
    a.title = [...cites].join('\n');
    box.appendChild(a);
  }
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
  const cbox = el('div', 'srcs'); cbox.hidden = true;     // "Trích dẫn" — [n] ↔ nguồn (D24), gộp theo bài
  const sbox = el('div', 'srcs'); sbox.hidden = true;     // tệp đính kèm (tải file / trang chứa file)
  bot.append(phase, body, notes, mbox, cbox, sbox);
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
        // D24: trích dẫn bắt buộc — [n] ↔ nguồn (file PDF/PPTX kèm số trang trong nhãn cite)
        if (obj.citations && obj.citations.length) {
          for (const c of obj.citations) cites[c.n] = c;      // [n] trong câu trả lời → link tới nguồn n
          paint();
          renderCitations(cbox, obj.citations, all);
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
          renderText(sbox, obj.text);
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
