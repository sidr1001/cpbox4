// app/static/js/main.js

/* ------------ 1. НАСТРОЙКИ ------------ */
const MAX_FILE_SIZE_MB_PHOTO = 10;
const MAX_FILE_SIZE_BYTES_PHOTO = MAX_FILE_SIZE_MB_PHOTO * 1024 * 1024;
const MAX_FILE_SIZE_MB_VIDEO = 50;
const MAX_FILE_SIZE_BYTES_VIDEO = MAX_FILE_SIZE_MB_VIDEO * 1024 * 1024;

/* ------------ 2. ИНИЦИАЛИЗАЦИЯ QUILL ------------ */
const toolbarOptions = [
  ['bold', 'italic', 'underline', 'strike'], 
  [{ 'list': 'ordered'}, { 'list': 'bullet' }],
  ['link'] 
];
let quill = null;
if (document.getElementById('editor')) {
  quill = new Quill('#editor', {
    modules: { toolbar: '#toolbar' },
    theme: 'snow'
  });
}

/* ------------ 3. ПАГИНАЦИЯ И ПОИСК (List.js) ------------ */
const historyList = new List('history-list', {
    valueNames: ['time', 'text'], 
    page: 10, 
    pagination: {
      paginationClass: 'pagination',
      innerWindow: 1,
      outerWindow: 1
    }
  });

/* ------------ 4. КНОПКИ TG ------------ */
function addBtn() {
  const c = document.getElementById('btns');
  const r = document.createElement('div');
  r.className = 'row mt-2';
  r.innerHTML = `
    <div class="col-5"><input name="button_text" class="form-control form-control-sm" placeholder="Текст"></div>
    <div class="col-7"><input name="button_url" class="form-control form-control-sm" placeholder="URL или callback_data"></div>`;
  c.appendChild(r);
}

/* ------------ 5. ТЕКСТ VK ------------ */
function toggleVkText(show) {
  document.getElementById('vk_text_block').classList.toggle('d-none', !show);
  if (show && quill) {
    document.getElementById('text_vk').value = quill.getText();
  }
}

/* ------------ 6. КЛОНИРОВАНИЕ ПОСТА ------------ */
function clonePost(tgHtml) {
    if (!quill) return; 
    let quillHtml = tgHtml
        .replace(/<b>/g, '<strong>')
        .replace(/<\/b>/g, '</strong>')
        .replace(/<i>/g, '<em>')
        .replace(/<\/i>/g, '</em>');
    quillHtml = quillHtml
        .split('\n\n') 
        .filter(line => line.trim() !== '') 
        .map(line => `<p>${line.replace(/\n/g, '<br>')}</p>`) 
        .join('');
    if (!quillHtml.startsWith('<p>')) {
         quillHtml = `<p>${quillHtml}</p>`;
    }
    quill.root.innerHTML = quillHtml;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    validateSubmit();
}

/* ------------ 7. ВАЛИДАЦИЯ КНОПКИ "ОТПРАВИТЬ" ------------ */
const submitButton = document.getElementById('submit-button');
const loadingSpinner = document.getElementById('loading-spinner');
// (УДАЛЕНО: 'formFieldset', он ломал отправку)

const validateSubmit = () => {
    if (!submitButton) return;
    
    let hasText = false;
    if (quill) {
        hasText = quill.getText().trim().length > 0;
    }
    
    const hasMedia = fileArray.length > 0;
    submitButton.disabled = !(hasText || hasMedia);
};


/* ------------ 8. ЛОГИКА ФАЙЛОВ (Drag-n-Drop, Лимиты) ------------ */
let fileArray = [];
const mediaInput = document.getElementById('media');
const dropArea   = document.getElementById('drop-area');
const fileList   = document.getElementById('file-list');
const warnBtnBox = document.getElementById('warn-buttons'); 
const btnBox     = document.getElementById('btnBox');      
const form = document.getElementById('post-form');

// (Инициализация Toasts)
const limitToastEl = document.getElementById('limitToast');
const limitToast = limitToastEl ? new bootstrap.Toast(limitToastEl) : null;
const sizeLimitToastEl = document.getElementById('sizeLimitToast');
const sizeLimitToastBody = document.getElementById('sizeLimitToastBody');
const sizeLimitToast = sizeLimitToastEl ? new bootstrap.Toast(sizeLimitToastEl) : null;
const postSuccessToastEl = document.getElementById('postSuccessToast');
const postSuccessToast = postSuccessToastEl ? new bootstrap.Toast(postSuccessToastEl) : null;
const postErrorToastEl = document.getElementById('postErrorToast');
const postErrorToastBody = document.getElementById('postErrorToastBody');
const postErrorToast = postErrorToastEl ? new bootstrap.Toast(postErrorToastEl) : null;

// --- (Инициализация Sortable) --- 
if (fileList) {
  new Sortable(fileList, {
    animation: 150, // Плавная анимация (мс)
    ghostClass: 'sortable-ghost', // Класс для "призрака" при перетаскивании
    
    // Функция вызывается, когда перетаскивание завершено
    onEnd: function (evt) {
      // 1. Получаем старый и новый индекс
      const oldIndex = evt.oldIndex;
      const newIndex = evt.newIndex;

      if (oldIndex === newIndex) return; // Ничего не изменилось

      // 2. Перемещаем файл внутри массива fileArray
      // (Вырезаем элемент со старого места и вставляем на новое)
      const movedItem = fileArray.splice(oldIndex, 1)[0];
      fileArray.splice(newIndex, 0, movedItem);

      // 3. ВАЖНО: Обновляем скрытый <input> и ПЕРЕРИСОВЫВАЕМ список
      // Нам нужно перерисовать (renderFiles), чтобы обновить индексы 
      // в кнопках "Удалить" (иначе они будут удалять не те файлы).
      refreshAndRender();
    }
  });
}

function renderFiles() {
  if (!fileList) return; 
  fileList.innerHTML = '';
  fileArray.forEach((file, idx) => {
    const item = document.createElement('div');
    item.className = 'file-item col-12 col-md-4'; 

    const media = document.createElement(file.type.startsWith('video') ? 'video' : 'img');
    media.src = URL.createObjectURL(file);
    media.className = file.type.startsWith('video') ? 'video-thumb' : '';
    if (file.type.startsWith('video')) media.muted = true;

    const info = document.createElement('div');
    info.innerHTML = `<strong>${file.name}</strong><br>
                      <span class="file-info">${(file.size/1024/1024).toFixed(2)} МБ</span>`;

    const remove = document.createElement('span');
    remove.innerHTML = '✕';
    remove.className = 'remove-btn';
    remove.onclick = (e) => { 
        e.stopPropagation(); 
        fileArray.splice(idx, 1); 
        refreshAndRender(); 
    };

    item.append(media, info, remove);
    fileList.appendChild(item);
  });

  const manyMedia = fileArray.length > 1;
  if(warnBtnBox) warnBtnBox.style.display = manyMedia ? 'block' : 'none';
  if(btnBox) btnBox.style.display     = manyMedia ? 'none'  : 'block';

  if (fileArray.length > 10 && limitToast) {
      limitToast.show();
  }
}

function refreshAndRender() {
  const dt = new DataTransfer();
  fileArray.forEach(f => dt.items.add(f));
  if (mediaInput) mediaInput.files = dt.files;
  renderFiles();
  validateSubmit(); 
}

/* ------------ ВСТАВКА ПОДПИСИ (Новая функция) ------------ */
function insertSignature(text) {
    if (!quill) return;

    // 1. Получаем текущую длину текста
    const length = quill.getLength();
    
    // 2. Вставляем новую строку (если текст не пустой) + саму подпись в конец
    // 'user' означает, что изменение сделано пользователем (сохраняет историю undo/redo)
    quill.insertText(length, `\n${text}`, 'user');
    
    // 3. Прокручиваем вниз
    quill.setSelection(length + text.length + 1);
    
    // 4. Обновляем валидацию кнопки
    validateSubmit();
}

/**
 * Функция добавления файлов с проверкой размера
 */
function addFiles(newFiles) {
    if (!sizeLimitToast || !sizeLimitToastBody) {
        console.error("Toast для размера не найден");
        return;
    }
    
    let validFiles = [];
    
    for (const file of newFiles) {
        let limit_bytes = MAX_FILE_SIZE_BYTES_VIDEO;
        let limit_mb = MAX_FILE_SIZE_MB_VIDEO;

        if (file.type.startsWith('image/')) {
            limit_bytes = MAX_FILE_SIZE_BYTES_PHOTO;
            limit_mb = MAX_FILE_SIZE_MB_PHOTO;
        }

        if (file.size > limit_bytes) {
            sizeLimitToastBody.textContent = `Файл "${file.name}" (${(file.size/1024/1024).toFixed(1)} МБ) слишком большой! Лимит: ${limit_mb} МБ.`;
            sizeLimitToast.show();
        } else {
            validFiles.push(file);
        }
    }
    
    fileArray.push(...validFiles);
}

if (mediaInput) {
    mediaInput.addEventListener('change', () => {
      const newFiles = Array.from(mediaInput.files);
      mediaInput.value = ''; 
      addFiles(newFiles);
      refreshAndRender(); 
    });
}

if (dropArea) {
    ['dragover','dragleave','drop'].forEach(evt =>
      dropArea.addEventListener(evt, e => {
        e.preventDefault();
        e.stopPropagation();
        if (evt === 'dragover') dropArea.classList.add('drag-over');
        if (evt === 'dragleave') dropArea.classList.remove('drag-over');
        if (evt === 'drop') {
          dropArea.classList.remove('drag-over');
          const newFiles = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/') || f.type.startsWith('video/'));
          
          addFiles(newFiles);
          refreshAndRender(); // (Исправление Drag-n-Drop)
        }
      })
    );
}


/* ------------ 9. ЗАГРУЗКА СТРАНИЦЫ (DOMContentLoaded) ------------ */
document.addEventListener('DOMContentLoaded', () => {
    
    // --- 1. Отправка часового пояса ---
    const offsetInput = document.getElementById('tz_offset_minutes');
    const scheduleInput = document.querySelector('input[type="datetime-local"]');
    const setTimezoneOffset = () => {
        if (offsetInput) offsetInput.value = -(new Date().getTimezoneOffset()); 
    };
    setTimezoneOffset();
    if (scheduleInput) scheduleInput.addEventListener('change', setTimezoneOffset);

    // --- 2. Форматирование Дат ---
    document.querySelectorAll('.utc-timestamp').forEach(el => {
        formatTimestamp(el); 
    });
    
    // --- 3. Настройка Quill и Валидации ---
    const hiddenInput = document.getElementById('text_html');
    if (quill && form && hiddenInput) {
        quill.on('text-change', () => {
            validateSubmit();
        });
    }
    validateSubmit();
});


/* ------------ 10. AJAX-ОТПРАВКА И "ОПРОС" (Polling) ------------ */

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function pollPostStatus(postId) {
    let retries = 20; 
    let postHtml = null;

    while (retries > 0) {
        try {
            const response = await fetch(`/post-status/${postId}`);
            if (!response.ok) throw new Error('Ошибка сети при опросе статуса');
            
            const data = await response.json();

            if (data.status === 'published' || data.status === 'failed') {
                const historyUl = document.querySelector('ul.history');
                if (historyUl) {
                    historyUl.insertAdjacentHTML('afterbegin', data.html);
                    const newEl = historyUl.firstChild.querySelector('.utc-timestamp');
                    if(newEl) formatTimestamp(newEl);
                }
                historyList.reIndex();
                
                if (data.status === 'published' && postSuccessToast) {
                    postSuccessToast.show();
                } else if (data.status === 'failed' && postErrorToast && postErrorToastBody) {
                    postErrorToastBody.textContent = data.error_message || "Пост не опубликован (неизвестная ошибка).";
                    postErrorToast.show();
                }
                return; 
            }
            retries--;
            await sleep(3000); 
        } catch (error) {
            console.error(error);
            if(postErrorToastBody) postErrorToastBody.textContent = "Ошибка опроса статуса.";
            if(postErrorToast) postErrorToast.show();
            retries = 0; 
        }
    }
}

function formatTimestamp(el) {
    function pad(n) { return n < 10 ? '0' + n : n; }
    const utcIsoString = el.textContent.trim(); 
    const utcDateStr = utcIsoString.endsWith('Z') ? utcIsoString : utcIsoString + 'Z';
    if (!utcIsoString || utcIsoString === 'N/A' || !utcIsoString.includes('T')) return; 
    try {
        const date = new Date(utcDateStr);
        const localFormatted = 
            date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + 
            pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' + 
            pad(date.getMinutes());
        el.textContent = localFormatted;
    } catch (e) { console.error("Ошибка форматирования даты:", utcIsoString, e); }
}

if (form) {
    form.addEventListener('submit', async function(e) {
        e.preventDefault(); 
        
        // --- V --- ИСПРАВЛЕНИЕ ЗДЕСЬ --- V ---
        // 1. Показываем спиннер, БЛОКИРУЕМ КНОПКУ
        if (submitButton) submitButton.disabled = true;
        if (loadingSpinner) loadingSpinner.style.display = 'inline-block';
        // (if (formFieldset) formFieldset.disabled = true;) // <-- УДАЛЕНО (ЭТО БЫЛ БАГ)
        
        // 2. Обновляем скрытые поля
        if (quill) {
            document.getElementById('text_html').value = quill.root.innerHTML;
        }
        document.getElementById('tz_offset_minutes').value = -(new Date().getTimezoneOffset());
        
        const formData = new FormData(form);

        try {
            const response = await fetch(form.action, {
                method: 'POST',
                body: formData
            });
            
            const data = await response.json(); 

            if (data.status === 'ok') {
                pollPostStatus(data.post_id);
                if (quill) quill.root.innerHTML = '';
                fileArray = [];
                refreshAndRender(); 
                validateSubmit();
            } else {
                if(postErrorToastBody) postErrorToastBody.textContent = data.message || 'Ошибка валидации.';
                if(postErrorToast) postErrorToast.show();
            }

        } catch (error) {
            console.error('Ошибка отправки формы:', error);
            if(postErrorToastBody) postErrorToastBody.textContent = 'Не удалось отправить форму. Проверьте консоль.';
            if(postErrorToast) postErrorToast.show();
        } finally {
            // 7. РАЗБЛОКИРУЕМ КНОПКУ (форма уже не заблокирована)
            if (loadingSpinner) loadingSpinner.style.display = 'none';
            // (if (formFieldset) formFieldset.disabled = false;) // <-- УДАЛЕНО
            validateSubmit(); 
        }
    });
}