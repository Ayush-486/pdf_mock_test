/**
 * test.js – Test page logic
 * Handles: question loading, navigation, answer storage, 30-min timer, submit.
 * Supports image_path field: renders embedded PDF diagram below question if present.
 */

/* ── State ────────────────────────────────── */
let questions = [];   // [{id, question, option_a…option_d, has_diagram, image_path}, …]
let current = 0;      // index of displayed question
let answers = {};     // { questionId: 'a'|'b'|'c'|'d' }
let timerHandle = null;
let secondsLeft = 30 * 60;  // 30 minutes

/* ── DOM refs ─────────────────────────────── */
const loadingMsg = document.getElementById('loading-msg');
const testUI = document.getElementById('test-ui');
const summarySection = document.getElementById('summary-section');
const counterEl = document.getElementById('question-counter');
const questionEl = document.getElementById('question-text');
const optionsEl = document.getElementById('options-list');
const prevBtn = document.getElementById('prev-btn');
const nextBtn = document.getElementById('next-btn');
const timerDisplay = document.getElementById('timer-display');
const imgWrap = document.getElementById('question-image-wrap');
const imgEl = document.getElementById('question-image');

/* ── Boot ─────────────────────────────────── */
window.addEventListener('DOMContentLoaded', () => {
    loadQuestions();
    startTimer();
});

/* ── Load questions from API ──────────────── */
async function loadQuestions() {
    try {
        const res = await fetch('/api/questions');
        if (!res.ok) throw new Error('Failed to fetch questions.');
        questions = await res.json();

        if (questions.length === 0) {
            loadingMsg.textContent = 'No questions found. Please upload a PDF first.';
            return;
        }

        loadingMsg.style.display = 'none';
        testUI.style.display = 'block';
        renderQuestion();
    } catch (err) {
        loadingMsg.textContent = 'Error loading questions: ' + err.message;
    }
}

/* ── Save current answer before navigating ── */
function saveCurrentAnswer() {
    const q = questions[current];
    if (!q) return;
    const checked = optionsEl.querySelector(`input[name="q_${q.id}"]:checked`);
    if (checked) {
        answers[q.id] = checked.value;
    }
}

/* ── Render current question ──────────────── */
function renderQuestion() {
    const q = questions[current];
    const total = questions.length;
    const num = current + 1;

    counterEl.textContent = `Question ${num} of ${total}`;

    if (q.question_image) {
        // ── Screenshot mode: full question as a cropped PDF image ─────────

        // Show question image instead of text
        questionEl.innerHTML = '';
        const qImg = document.createElement('img');
        qImg.src = q.question_image;
        qImg.alt = `Question ${num}`;
        qImg.style.cssText = 'max-width:100%;border:1px solid #d0d0d0;border-radius:6px;' +
                             'box-shadow:0 2px 8px rgba(0,0,0,.12);display:block;margin:0.5rem 0;';
        questionEl.appendChild(qImg);

        // Hide embedded-diagram strip and notice (already visible in screenshot)
        imgWrap.innerHTML = '';
        imgWrap.style.display = 'none';
        const dn = document.getElementById('diagram-notice');
        if (dn) dn.style.display = 'none';

        // Simple A / B / C / D answer buttons (labels from the screenshot)
        optionsEl.innerHTML = '';
        ['a', 'b', 'c', 'd'].forEach(key => {
            const li    = document.createElement('li');
            const radio = document.createElement('input');
            radio.type  = 'radio';
            radio.name  = `q_${q.id}`;
            radio.value = key;
            radio.id    = `q${q.id}_${key}`;
            if (answers[q.id] === key) radio.checked = true;
            radio.addEventListener('change', () => { answers[q.id] = key; });

            const lbl      = document.createElement('label');
            lbl.htmlFor    = radio.id;
            lbl.textContent = key.toUpperCase();
            lbl.style.cssText = 'font-weight:bold;font-size:1.05rem;padding-left:4px;';

            li.appendChild(radio);
            li.appendChild(lbl);
            optionsEl.appendChild(li);
        });

    } else {
        // ── Text-fallback mode: render parsed question + options as text ───

        questionEl.innerHTML = `${num}. ${q.question}`;

        // Embedded diagram image(s) at question level
        if (q.image_path) {
            const paths = q.image_path.split(',').map(p => p.trim()).filter(Boolean);
            imgWrap.innerHTML = '';
            paths.forEach(p => {
                const img = document.createElement('img');
                img.className = 'question-diagram';
                img.src = p;
                img.alt = 'Question diagram';
                img.style.cssText = 'max-width:100%;max-height:320px;border:1px solid #d0d0d0;' +
                    'border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.12);' +
                    'object-fit:contain;display:block;margin:0 auto 0.5rem;';
                imgWrap.appendChild(img);
            });
            const cap = document.createElement('p');
            cap.className = 'question-img-caption';
            cap.textContent = '📷 Diagram extracted from PDF';
            imgWrap.appendChild(cap);
            imgWrap.style.display = 'block';
        } else {
            imgWrap.innerHTML = '';
            imgWrap.style.display = 'none';
        }

        // Diagram notice (shown only when has_diagram but no image extracted)
        let diagramEl = document.getElementById('diagram-notice');
        if (!diagramEl) {
            diagramEl = document.createElement('p');
            diagramEl.id = 'diagram-notice';
            diagramEl.className = 'diagram-notice';
            imgWrap.insertAdjacentElement('afterend', diagramEl);
        }
        const hasAnyOptionImg = ['a','b','c','d'].some(l => q[`option_${l}_image`]);
        if (q.has_diagram && !q.image_path && !hasAnyOptionImg) {
            diagramEl.textContent = '📷 This question may reference a diagram or figure in the original PDF.';
            diagramEl.style.display = 'block';
        } else {
            diagramEl.style.display = 'none';
        }

        // Options list with text (and optional option-level images)
        optionsEl.innerHTML = '';
        const opts = [
            { key: 'a', label: 'A', text: q.option_a, image: q.option_a_image },
            { key: 'b', label: 'B', text: q.option_b, image: q.option_b_image },
            { key: 'c', label: 'C', text: q.option_c, image: q.option_c_image },
            { key: 'd', label: 'D', text: q.option_d, image: q.option_d_image },
        ];
        opts.forEach(opt => {
            if (!opt.text && !opt.image) return;
            const li    = document.createElement('li');
            const radio = document.createElement('input');
            radio.type  = 'radio';
            radio.name  = `q_${q.id}`;
            radio.value = opt.key;
            radio.id    = `q${q.id}_${opt.key}`;
            if (answers[q.id] === opt.key) radio.checked = true;
            radio.addEventListener('change', () => { answers[q.id] = opt.key; });

            const lbl   = document.createElement('label');
            lbl.htmlFor = radio.id;
            lbl.innerHTML = `${opt.label}) ${opt.text || ''}`;
            if (opt.image) {
                const img = document.createElement('img');
                img.src = opt.image;
                img.className = 'option-diagram';
                img.alt = `Option ${opt.label} diagram`;
                lbl.appendChild(img);
            }
            li.appendChild(radio);
            li.appendChild(lbl);
            optionsEl.appendChild(li);
        });
    }

    // ── Nav button states ──────────────────────────────────────────────────
    prevBtn.disabled = (current === 0);
    nextBtn.disabled = (current === questions.length - 1);

    // ── Re-render math after DOM update ───────────────────────────────────
    if (window.MathJax) {
        MathJax.typeset();
    }
}

/* ── Navigation ───────────────────────────── */
function navigate(direction) {
    saveCurrentAnswer();
    const next = current + direction;
    if (next >= 0 && next < questions.length) {
        current = next;
        renderQuestion();
    }
}

/* ── Timer ────────────────────────────────── */
function startTimer() {
    updateTimerDisplay();
    timerHandle = setInterval(() => {
        secondsLeft--;
        if (secondsLeft <= 0) {
            secondsLeft = 0;
            updateTimerDisplay();
            clearInterval(timerHandle);
            alert('Time is up! The test will be submitted automatically.');
            submitTest();
        } else {
            updateTimerDisplay();
        }
    }, 1000);
}

function updateTimerDisplay() {
    const m = Math.floor(secondsLeft / 60).toString().padStart(2, '0');
    const s = (secondsLeft % 60).toString().padStart(2, '0');
    timerDisplay.textContent = `${m}:${s}`;

    if (secondsLeft <= 300) {
        timerDisplay.style.color = '#ff4444';
    }
}

/* ── Submit ───────────────────────────────── */
function submitTest() {
    saveCurrentAnswer();
    clearInterval(timerHandle);

    const total = questions.length;
    const attempted = Object.keys(answers).length;
    const unanswered = total - attempted;

    testUI.style.display = 'none';
    document.getElementById('timer-bar').style.display = 'none';
    summarySection.style.display = 'block';

    document.getElementById('s-total').textContent = total;
    document.getElementById('s-attempted').textContent = attempted;
    document.getElementById('s-unanswered').textContent = unanswered;

    const listEl = document.getElementById('s-question-list');
    if (listEl) {
        listEl.innerHTML = '';
        questions.forEach((q, idx) => {
            const li = document.createElement('li');
            const answered = answers[q.id];
            const label = answered ? `Answered: ${answered.toUpperCase()}` : 'Unanswered';
            const badge = answered ? '✅' : '⬜';
            li.textContent = `${badge} Q${idx + 1}: ${label}`;
            li.style.color = answered ? '#2ecc71' : '#e74c3c';
            listEl.appendChild(li);
        });
    }
}
