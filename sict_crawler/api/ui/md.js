// Dựng markdown của câu trả lời LLM thành HTML cho khung chat (không thư viện ngoài).
// An toàn: mọi chữ được escape HTML TRƯỚC khi dựng thẻ; link chỉ nhận http(s). Hỗ trợ đúng những gì model
// hay viết: tiêu đề #, danh sách (-, *, +, 1.) lồng nhau theo thụt lề, trích dẫn >, bảng |, khối ```, ---,
// **đậm**, *nghiêng*, `mã`, [chữ](url), URL trần và [n] → link tới nguồn n (cites: {n: {url, cite}}).
(function (root) {
  'use strict';

  function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function inline(text, cites) {
    const keep = [];
    const hold = (html) => '\u0000' + (keep.push(html) - 1) + '\u0000';
    let s = esc(text);
    s = s.replace(/`([^`]+)`/g, (_, c) => hold('<code>' + c + '</code>'));
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, t, u) => hold('<a href="' + u + '" target="_blank" rel="noopener">' + t + '</a>'));
    s = s.replace(/https?:\/\/[^\s<]*[^\s<.,;:!?)\]'"]/g,
      (u) => hold('<a href="' + u + '" target="_blank" rel="noopener">' + u + '</a>'));
    s = s.replace(/\[(\d{1,3})\]/g, (m, n) => {
      const c = cites && cites[n];
      return hold(c && c.url
        ? '<a class="cite" href="' + esc(c.url) + '" target="_blank" rel="noopener" title="' + esc(c.cite || c.url) + '">[' + n + ']</a>'
        : '<span class="cite">[' + n + ']</span>');
    });
    s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^\p{L}\p{N}_])__(?=\S)([\s\S]*?\S)__(?![\p{L}\p{N}_])/gu, '$1<strong>$2</strong>');
    s = s.replace(/(^|[^*\p{L}\p{N}])\*(?=[^\s*])([^*]*?[^\s*])\*(?!\*)/gu, '$1<em>$2</em>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => keep[+i]);
  }

  const LIST = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  const SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function cells(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
  }

  function render(md, cites) {
    const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];
    const lists = [];   // ngăn xếp {ind, tag}

    const flushPara = () => {
      if (para.length) out.push('<p>' + para.map((l) => inline(l, cites)).join('<br>') + '</p>');
      para = [];
    };
    const closeLists = (toInd) => {
      while (lists.length && (toInd == null || lists[lists.length - 1].ind > toInd)) {
        out.push('</li></' + lists.pop().tag + '>');
      }
    };
    const closeAll = () => { flushPara(); closeLists(null); };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const t = line.trim();

      if (/^```/.test(t)) {                       // khối mã
        closeAll();
        const code = [];
        while (++i < lines.length && !/^```/.test(lines[i].trim())) code.push(lines[i]);
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      if (!t) { flushPara(); continue; }           // dòng trống: danh sách vẫn mở (danh sách thưa)

      const li = LIST.exec(line);
      if (li && !/^(-{3,}|\*{3,})$/.test(t)) {
        flushPara();
        const ind = li[1].replace(/\t/g, '    ').length;
        const tag = /\d/.test(li[2]) ? 'ol' : 'ul';
        closeLists(ind);
        const top = lists[lists.length - 1];
        if (top && top.ind === ind && top.tag !== tag) { out.push('</li></' + lists.pop().tag + '>'); }
        const cur = lists[lists.length - 1];
        if (!cur || ind > cur.ind) {
          const start = tag === 'ol' && parseInt(li[2], 10) !== 1 ? ' start="' + parseInt(li[2], 10) + '"' : '';
          out.push('<' + tag + start + '>');
          lists.push({ ind, tag });
        } else {
          out.push('</li>');
        }
        out.push('<li>' + inline(li[3], cites));
        continue;
      }
      if (lists.length && /^\s{2,}\S/.test(line)) {  // dòng nối tiếp của mục danh sách
        out.push('<br>' + inline(t, cites));
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { closeAll(); out.push('<hr>'); continue; }

      const h = /^(#{1,6})\s+(.*)$/.exec(t);
      if (h) {
        closeAll();
        const lv = Math.min(6, h[1].length + 2);   // # trong khung chat → h3, không to hơn
        out.push('<h' + lv + '>' + inline(h[2].replace(/\s#+$/, ''), cites) + '</h' + lv + '>');
        continue;
      }

      if (t.startsWith('>')) {                     // trích dẫn: gom các dòng > liền nhau, dựng đệ quy
        closeAll();
        const q = [];
        while (i < lines.length && lines[i].trim().startsWith('>')) q.push(lines[i++].trim().replace(/^>\s?/, ''));
        i--;
        out.push('<blockquote>' + render(q.join('\n'), cites) + '</blockquote>');
        continue;
      }

      if (t.startsWith('|') && i + 1 < lines.length && SEP.test(lines[i + 1])) {   // bảng
        closeAll();
        const head = cells(t);
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(cells(lines[i++]));
        i--;
        out.push('<table><thead><tr>' + head.map((c) => '<th>' + inline(c, cites) + '</th>').join('') +
          '</tr></thead><tbody>' + rows.map((r) => '<tr>' + r.map((c) => '<td>' + inline(c, cites) + '</td>').join('') +
          '</tr>').join('') + '</tbody></table>');
        continue;
      }

      if (lists.length) closeLists(null);
      para.push(t);
    }
    closeAll();
    return out.join('');
  }

  root.renderMarkdown = render;
  if (typeof module !== 'undefined' && module.exports) module.exports = { renderMarkdown: render };
})(typeof window !== 'undefined' ? window : globalThis);
