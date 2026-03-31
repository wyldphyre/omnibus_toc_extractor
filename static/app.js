const dropzone  = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const loading   = document.getElementById('loading');
const errorEl   = document.getElementById('error');
const results   = document.getElementById('results');
const bookTitle = document.getElementById('bookTitle');
const pageCount = document.getElementById('pageCount');
const booksBody = document.getElementById('booksBody');
const exportBtn = document.getElementById('exportBtn');

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

  booksBody.innerHTML = '';

  if (!data.child_books || data.child_books.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="3" style="color:var(--text-muted);text-align:center;padding:1.5rem">
      No child books detected — the TOC may not have a nested structure.
    </td>`;
    booksBody.appendChild(tr);
    return;
  }

  data.child_books.forEach((book, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>${escHtml(book.title)}</td>
      <td>${book.start_page.toLocaleString()}</td>
    `;
    booksBody.appendChild(tr);
  });
}

// ── Export ───────────────────────────────────────────────────────────────────

exportBtn.addEventListener('click', () => {
  if (!lastData) return;
  const json = JSON.stringify(lastData, null, 2);
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
