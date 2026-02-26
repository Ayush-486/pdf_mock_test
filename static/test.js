/**
 * test.js – Test page logic
 * Handles: question loading, navigation, answer storage, 30-min timer, submit.
 */

/* ── State ────────────────────────────────── */
let questions = [];   // [{id, question, option_a, option_b, option_c, option_d, has_diagram}, …]
let current = 0;    // index of displayed question
let answers = {};   // { questionId: 'a'|'b'|'c'|'d' }
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
    // Read whichever radio is currently checked for the displayed question
    const q = questions[current];
    if (!q) return;
    const checked = optionsEl.querySelector(`input[name="q_${q.id}"]:checked`);
    if (checked) {
        answers[q.id] = checked.value;
    }
    // Note: if nothing is checked and user had previously answered, we keep
    // their earlier answer (don't wipe it). Only update if something is checked.
}

/* ── Render current question ──────────────── */
function renderQuestion() {
    const q = questions[current];
    const total = questions.length;
    const num = current + 1;

    counterEl.textContent = `Question ${num} of ${total}`;
    questionEl.textContent = `${num}. ${q.question}`;

    // Diagram notice
    let diagramEl = document.getElementById('diagram-notice');
    if (!diagramEl) {
        diagramEl = document.createElement('p');
        diagramEl.id = 'diagram-notice';
        diagramEl.className = 'diagram-notice';
        questionEl.insertAdjacentElement('afterend', diagramEl);
    }
    if (q.has_diagram) {
        diagramEl.textContent = '📷 This question may reference a diagram or figure in the original PDF.';
        diagramEl.style.display = 'block';
    } else {
        diagramEl.style.display = 'none';
    }

    // Build options list
    optionsEl.innerHTML = '';
    const opts = [
        { key: 'a', label: 'A', text: q.option_a },
        { key: 'b', label: 'B', text: q.option_b },
        { key: 'c', label: 'C', text: q.option_c },
        { key: 'd', label: 'D', text: q.option_d },
    ];

    opts.forEach(opt => {
        if (!opt.text) return;   // skip missing options

        const li = document.createElement('li');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = `q_${q.id}`;
        radio.value = opt.key;
        radio.id = `q${q.id}_${opt.key}`;

        // Restore previously selected answer
        if (answers[q.id] === opt.key) {
            radio.checked = true;
        }

        radio.addEventListener('change', () => {
            answers[q.id] = opt.key;
        });

        const lbl = document.createElement('label');
        lbl.htmlFor = radio.id;
        lbl.textContent = `${opt.label}) ${opt.text}`;

        li.appendChild(radio);
        li.appendChild(lbl);
        optionsEl.appendChild(li);
    });

    // Update nav button states
    prevBtn.disabled = (current === 0);
    nextBtn.disabled = (current === questions.length - 1);

    // Update submit button visibility: show only on last question
    // (keep always visible so user can submit any time)
}

/* ── Navigation ───────────────────────────── */
function navigate(direction) {
    // Always save before moving so we never lose a click
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

    // Turn timer red when ≤ 5 minutes
    if (secondsLeft <= 300) {
        timerDisplay.style.color = '#ff4444';
    }
}

/* ── Submit ───────────────────────────────── */
function submitTest() {
    // Save the currently displayed question's answer before computing stats
    saveCurrentAnswer();

    clearInterval(timerHandle);

    const total = questions.length;
    const attempted = Object.keys(answers).length;
    const unanswered = total - attempted;

    // Hide test UI and timer, show summary
    testUI.style.display = 'none';
    document.getElementById('timer-bar').style.display = 'none';
    summarySection.style.display = 'block';

    document.getElementById('s-total').textContent = total;
    document.getElementById('s-attempted').textContent = attempted;
    document.getElementById('s-unanswered').textContent = unanswered;

    // Build per-question summary list
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
