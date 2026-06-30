const dropzone  = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const loading   = document.getElementById('loading');
const errorEl   = document.getElementById('error');
const results   = document.getElementById('results');
const bookTitle = document.getElementById('bookTitle');
const pageCount = document.getElementById('pageCount');
const booksBody = document.getElementById('booksBody');
const exportBtn = document.getElementById('exportBtn');
const tocToggle = document.getElementById('tocToggle');
const tocPanel  = document.getElementById('tocPanel');
const tocList   = document.getElementById('tocList');

let lastData = null;

// ── Drag & drop ──────────────────────────────────────────────────────────────

dropzone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropzone.classList.add('over');
});

dropzone.addEventListener('dragleave', () => {
  dropzone.classList.remove('over');
});

dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('over');
  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
});

dropzone.addEventListener('click', (e) => {
  if (e.target.tagName !== 'LABEL' && e.target.tagName !== 'INPUT') {
    fileInput.click();
  }
});

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) handleFile(fileInput.files[0]);
});

// ── Upload & parse ───────────────────────────────────────────────────────────

async function handleFile(file) {
  if (!file.name.toLowerCase().endsWith('.epub')) {
    showError('Please select an .epub file.');
    return;
  }

  setState('loading');

  const form = new FormData();
  form.append('file', file);

  try {
    const resp = await fetch('/api/extract', { method: 'POST', body: form });
    const data = await resp.json();

    if (!resp.ok || data.error) {
      showError(data.error || `Server error (${resp.status})`);
      return;
    }

    lastData = data;
    renderResults(data);
    setState('results');
  } catch (err) {
    showError('Network error — is the server running?');
  }
}

// ── Render ───────────────────────────────────────────────────────────────────

function renderResults(data) {
  bookTitle.textContent = data.omnibus_title;
  pageCount.textContent = `${data.total_pages.toLocaleString()} pages total`;

  renderBooks(data.child_books);
  renderTocEditor(data.toc_entries);
}

function renderBooks(books) {
  booksBody.innerHTML = '';

  if (!books || books.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="3" style="color:var(--text-muted);text-align:center;padding:1.5rem">
      No child books selected — use “Edit book starts” below to mark them.
    </td>`;
    booksBody.appendChild(tr);
    return;
  }

  books.forEach((book, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>${escHtml(book.title)}</td>
      <td>${book.start_page.toLocaleString()}</td>
    `;
    booksBody.appendChild(tr);
  });
}

// ── Book-start editor ──────────────────────────────────────────────────────────

function renderTocEditor(entries) {
  tocList.innerHTML = '';

  if (!entries || entries.length === 0) {
    tocToggle.classList.add('hidden');
    return;
  }
  tocToggle.classList.remove('hidden');

  entries.forEach((entry, i) => {
    const li = document.createElement('li');
    const id = `toc-${i}`;
    li.innerHTML = `
      <label for="${id}">
        <input type="checkbox" id="${id}" data-idx="${i}" ${entry.is_book_start ? 'checked' : ''}>
        <span class="toc-title">${escHtml(entry.title) || '<em>(untitled)</em>'}</span>
        <span class="toc-page">p.${entry.start_page.toLocaleString()}</span>
      </label>
    `;
    li.querySelector('input').addEventListener('change', onTocChange);
    tocList.appendChild(li);
  });
}

function onTocChange(e) {
  const idx = Number(e.target.dataset.idx);
  lastData.toc_entries[idx].is_book_start = e.target.checked;

  // Rebuild child_books from every entry currently marked as a book start.
  const marked = lastData.toc_entries.filter((entry) => entry.is_book_start);
  lastData.child_books = numberDuplicateTitles(
    marked.map((entry) => ({ title: entry.title, start_page: entry.start_page }))
  );

  renderBooks(lastData.child_books);
}

// Mirror of the server's _number_duplicate_titles: when several books share a
// title, append "1", "2", … so each child book is distinguishable.
function numberDuplicateTitles(books) {
  const counts = {};
  books.forEach((b) => { counts[b.title] = (counts[b.title] || 0) + 1; });
  const seen = {};
  return books.map((b) => {
    if (counts[b.title] > 1) {
      seen[b.title] = (seen[b.title] || 0) + 1;
      return { title: `${b.title} ${seen[b.title]}`, start_page: b.start_page };
    }
    return b;
  });
}

tocToggle.addEventListener('click', () => {
  const open = tocPanel.classList.toggle('hidden') === false;
  tocToggle.setAttribute('aria-expanded', String(open));
  tocToggle.querySelector('.chevron').textContent = open ? '▾' : '▸';
});

// ── Export ───────────────────────────────────────────────────────────────────

exportBtn.addEventListener('click', () => {
  if (!lastData) return;
  // Export the clean public structure, not the internal toc_entries list.
  const payload = {
    omnibus_title: lastData.omnibus_title,
    total_pages: lastData.total_pages,
    page_count_method: lastData.page_count_method,
    child_books: lastData.child_books,
  };
  const json = JSON.stringify(payload, null, 2);
  const blob = new Blob([json], { type: 'application/json' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = sanitizeFilename(lastData.omnibus_title) + '.json';
  a.click();
  URL.revokeObjectURL(url);
});

// ── Helpers ──────────────────────────────────────────────────────────────────

function setState(state) {
  loading.classList.toggle('hidden', state !== 'loading');
  errorEl.classList.add('hidden');
  results.classList.toggle('hidden', state !== 'results');
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.classList.remove('hidden');
  loading.classList.add('hidden');
  results.classList.add('hidden');
}

function escHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function sanitizeFilename(name) {
  return name.replace(/[^a-z0-9\-_ ]/gi, '_').trim() || 'export';
}
