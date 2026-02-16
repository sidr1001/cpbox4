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

/* ------------ 3. ПАГИНАЦИЯ И ПОИСК (List.js) - ИСПРАВЛЕНО ------------ */
// Инициализируем переменную, чтобы она была доступна везде
let historyList = null;

// Проверяем наличие элемента перед инициализацией
const historyListEl = document.getElementById('history-list');
if (historyListEl) {
    try {
        historyList = new List('history-list', {
            valueNames: ['time', 'text'], 
            page: 10, 
            pagination: {
              paginationClass: 'pagination',
              innerWindow: 1,
              outerWindow: 1
            }
        });
    } catch (e) {
        console.warn("List.js init error:", e);
    }
}

/* ------------ 4. КНОПКИ TG ------------ */
function addBtn() {
  const c = document.getElementById('btns');
  const r = document.createElement('div');
  
  r.className = 'row mt-2 align-items-start'; 
  r.innerHTML = `
    <div class="col-5 p-0">
        <input name="button_text" 
               class="form-control form-control-sm" 
               placeholder="Текст кнопки">
    </div>

    <div class="col-6 p-0">
        <input name="button_url" 
               class="form-control form-control-sm" 
               placeholder="URL или callback_data"
			   maxlength="64"
			   oninput="updateCharCounter(this)">
		<div class="text-end lh-1 mt-1">
            <small class="text-muted char-counter" style="font-size: 10px;">0/64</small>
        </div>
    </div>

    <div class="col-1 text-end p-0">
        <button type="button" 
                class="btn btn-outline-danger btn-sm px-2 py-1" 
                onclick="this.closest('.row').remove()"
                title="Удалить кнопку">
            &times;
        </button>
    </div>`;
    
  c.appendChild(r);
}

// Функция обновления счетчика (вызывается при вводе)
function updateCharCounter(input) {
    const max = input.getAttribute('maxlength');
    const len = input.value.length;
    // Ищем элемент small внутри родительского div'а
    const counter = input.parentNode.querySelector('.char-counter');
    
    if (counter) {
        counter.innerText = len + '/' + max;
        // Если осталось меньше 5 символов, красим в красный
        if (max - len < 5) {
            counter.classList.add('text-danger');
        } else {
            counter.classList.remove('text-danger');
        }
    }
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

// --- (Инициализация Sortable) --- 
if (fileList) {
  new Sortable(fileList, {
    animation: 150, 
    ghostClass: 'sortable-ghost', 
    onEnd: function (evt) {
      const oldIndex = evt.oldIndex;
      const newIndex = evt.newIndex;
      if (oldIndex === newIndex) return; 

      const movedItem = fileArray.splice(oldIndex, 1)[0];
      fileArray.splice(newIndex, 0, movedItem);
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

  if (fileArray.length > 10) {
      if (typeof showToast === 'function') showToast('warning', 'В Telegram нельзя отправить более 10 файлов.');
  }
}

function refreshAndRender() {
  const dt = new DataTransfer();
  fileArray.forEach(f => dt.items.add(f));
  if (mediaInput) mediaInput.files = dt.files;
  renderFiles();
  validateSubmit(); 
  updateCharTxtCounter();
}

/* ------------ ВСТАВКА ПОДПИСИ ------------ */
function insertSignature(text) {
    if (!quill) return;
    const length = quill.getLength();
    quill.insertText(length, `\n${text}`, 'user');
    quill.setSelection(length + text.length + 1);
    validateSubmit();
}

/**
 * Функция добавления файлов с проверкой размера
 */
function addFiles(newFiles) {
    let validFiles = [];
    
    for (const file of newFiles) {
        let limit_bytes = MAX_FILE_SIZE_BYTES_VIDEO;
        let limit_mb = MAX_FILE_SIZE_MB_VIDEO;

        if (file.type.startsWith('image/')) {
            limit_bytes = MAX_FILE_SIZE_BYTES_PHOTO;
            limit_mb = MAX_FILE_SIZE_MB_PHOTO;
        }

        if (file.size > limit_bytes) {
            if (typeof showToast === 'function') {
                showToast('danger', `Файл "${file.name}" слишком большой! Лимит: ${limit_mb} МБ.`);
            }
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
          refreshAndRender(); 
        }
      })
    );
}

/* ------------ СЧЕТЧИК СИМВОЛОВ (TELEGRAM) ------------ */
function updateCharTxtCounter() {
    const counterEl = document.getElementById('char_counter');
    // Если элемента нет или редактор не инициализирован — выходим
    if (!counterEl || !quill) return;

    // 1. Получаем длину чистого текста (без HTML тегов)
    // trim() убирает лишние пробелы в начале/конце, которые Quill иногда добавляет
    const textLength = quill.getText().trim().length;
    
    // 2. Проверяем наличие файлов (fileArray определен в вашем коде выше)
    const hasMedia = fileArray && fileArray.length > 0;
    
    // 3. Определяем лимит: 1024 с картинкой, 4096 без
    const limit = hasMedia ? 1024 : 4096;

    // 4. Обновляем текст
    counterEl.textContent = `${textLength} / ${limit}`;
    // 5. Красим в красный, если превышен лимит
    if (textLength > limit) {
        counterEl.className = 'text-danger fw-bold small mt-2 text-end me-2';
        counterEl.textContent += " (Лимит Telegram превышен!)";
    } else {
        counterEl.className = 'text-muted small mt-2 text-end me-2';
    }
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
            updateCharTxtCounter();
        });
    }
    validateSubmit();
	updateCharTxtCounter();
});


/* ------------ 10. AJAX-ОТПРАВКА И "ОПРОС" (Polling) - ИСПРАВЛЕНО ------------ */

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
                
                // Если блок истории есть - добавляем элемент
                if (historyUl) {
                    if (data.html) {
                        historyUl.insertAdjacentHTML('afterbegin', data.html);
                        const newEl = historyUl.firstChild.querySelector('.utc-timestamp');
                        if(newEl) formatTimestamp(newEl);
                    }
                    
                    // Безопасно обновляем List.js
                    if (historyList) {
                        historyList.reIndex();
                    }
                } else {
                    // Если это был первый пост (истории не было) - перезагружаем страницу
                    // чтобы отрисовался блок истории
                    setTimeout(() => window.location.reload(), 2000);
                }
                
                // ПОКАЗ УВЕДОМЛЕНИЙ (Используем глобальный showToast)
                if (typeof showToast === 'function') {
                    if (data.status === 'published') {
                        showToast('success', 'Пост успешно опубликован!');
                    } else if (data.status === 'failed') {
                        showToast('error', data.error_message || "Ошибка публикации.");
                    }
                }
                return; 
            }
            retries--;
            await sleep(3000); 
        } catch (error) {
            console.error(error);
            retries = 0; 
        }
    }
}

/* ------------ 11. ПЕРЕКЛЮЧЕНИЕ ТЕМЫ (DARK MODE) ------------ */
document.addEventListener('DOMContentLoaded', () => {
    const themeToggleBtn = document.getElementById('theme-toggle');
    const icon = themeToggleBtn ? themeToggleBtn.querySelector('i') : null;
    const html = document.documentElement;

    // 1. Получаем текущую тему (из localStorage или системы)
    const getPreferredTheme = () => {
        const storedTheme = localStorage.getItem('theme');
        if (storedTheme) {
            return storedTheme;
        }
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    };

    // 2. Функция применения темы
    const setTheme = (theme) => {
        html.setAttribute('data-bs-theme', theme);
        localStorage.setItem('theme', theme);
        
        // Меняем иконку
        if (icon) {
            if (theme === 'dark') {
                icon.className = 'bi bi-moon-stars-fill fs-5 text-warning'; // Луна
                themeToggleBtn.classList.add('bg-dark', 'text-white');
                themeToggleBtn.classList.remove('btn-light');
            } else {
                icon.className = 'bi bi-sun-fill fs-5 text-warning'; // Солнце
                themeToggleBtn.classList.add('btn-light');
                themeToggleBtn.classList.remove('bg-dark', 'text-white');
            }
        }
    };

    // 3. Инициализация при загрузке
    setTheme(getPreferredTheme());

    // 4. Обработчик клика
    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const currentTheme = html.getAttribute('data-bs-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            setTheme(newTheme);
        });
    }
});

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
        
        // --- 1. БЛОКИРОВКА И ИНТЕРФЕЙС ---
        if (submitButton) submitButton.disabled = true;
        
        // Проверяем, есть ли видео и включена ли оптимизация
        const optimizeChk = document.getElementById('optimize_video');
        // fileArray - это наша глобальная переменная с файлами
        const hasVideo = fileArray.some(f => f.type.startsWith('video/'));
        
        const alertBox = document.getElementById('video-processing-alert');
        const spinner = document.getElementById('loading-spinner');

        // Если включена оптимизация И есть видео -> Показываем специальное уведомление
        if (optimizeChk && optimizeChk.checked && hasVideo) {
            if (alertBox) alertBox.style.display = 'block';
            // Меняем текст кнопки, чтобы было понятно
            submitButton.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span> Сжимаю видео...';
        } else {
            // Обычная загрузка
            if (spinner) spinner.style.display = 'inline-block';
        }

        // 2. Обновляем скрытые поля
        if (quill) {
            document.getElementById('text_html').value = quill.root.innerHTML;
        }
		
        const offsetInput = document.getElementById('tz_offset_minutes');
        if (offsetInput) offsetInput.value = -(new Date().getTimezoneOffset());
        
        const formData = new FormData(form);

        try {
            const response = await fetch(form.action, {
                method: 'POST',
                body: formData
            });
            
            const data = await response.json(); 

            if (data.status === 'ok') {
                // Сбрасываем форму
                if (quill) quill.root.innerHTML = '';
                fileArray = [];
                refreshAndRender(); 
                validateSubmit();
                
                // Показываем уведомление сразу, что задача принята
                if (typeof showToast === 'function') {
                    showToast('info', data.message || 'Пост отправлен в очередь...');
                }
                
                // Запускаем опрос
                pollPostStatus(data.post_id);
                
            } else {
                if (typeof showToast === 'function') {
                    showToast('danger', data.message || 'Ошибка валидации.');
                }
            }

        } catch (error) {
            console.error('Ошибка отправки формы:', error);
            if (typeof showToast === 'function') {
                showToast('danger', 'Не удалось отправить форму. Проверьте консоль.');
            }
		} finally {
            // 7. РАЗБЛОКИРУЕМ КНОПКУ
            if (loadingSpinner) loadingSpinner.style.display = 'none';
            
            // --- ДОБАВИТЬ ЭТО: Сброс интерфейса ---
            const alertBox = document.getElementById('video-processing-alert');
            if (alertBox) alertBox.style.display = 'none';
            
            if (submitButton) {
                // Возвращаем исходный текст кнопки
                submitButton.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true" id="loading-spinner" style="display: none;"></span><i class="bi bi-send me-2"></i> Опубликовать';
                submitButton.disabled = false; // Разблокируем только в finally, если была ошибка
            }
            
            validateSubmit(); 
        }
    });
}