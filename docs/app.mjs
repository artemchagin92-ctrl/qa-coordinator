import { scenarios, runScenario } from './engine.mjs';
import { documentScenarios, checkDocument } from './document.mjs';

const get = (id) => document.getElementById(id);
const make = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

function choices(root, options, select) {
  const buttons = options.map(({ id, label }) => {
    const button = make('button', label, 'choice');
    button.type = 'button';
    button.addEventListener('click', () => activate(id));
    root.append(button);
    return { button, id };
  });
  function activate(id) {
    for (const item of buttons) item.button.setAttribute('aria-pressed', String(item.id === id));
    select(id);
  }
  activate(options[0].id);
}

const qaSteps = get('qa-steps');
const qaStatus = get('qa-status');
const qaRun = get('qa-run');
let selectedQa, generation = 0;
const qaPhases = ['Прочитать задачу', 'Сверить изменения и тесты', 'Проверить релиз', 'Повторно прочитать очередь'];

function qaRow(index, title, kind, detail, lines = []) {
  const row = make('li', undefined, 'step');
  row.dataset.kind = kind;
  const marker = make('span', String(index + 1).padStart(2, '0'), 'step-marker');
  marker.setAttribute('aria-hidden', 'true');
  const body = make('div');
  body.append(make('h3', title));
  if (detail) body.append(make('p', detail));
  for (const line of lines) body.append(make('code', line));
  row.append(marker, body);
  return row;
}

function queuePlaceholder() {
  qaSteps.replaceChildren(...qaPhases.map((title, index) => qaRow(index, title, 'pending')));
}

choices(get('qa-choices'), scenarios, (id) => {
  generation += 1;
  selectedQa = id;
  qaRun.disabled = false;
  qaRun.setAttribute('aria-busy', 'false');
  queuePlaceholder();
  delete qaStatus.dataset.kind;
  qaStatus.textContent = 'Сценарий выбран. Запустите проверки.';
});

qaRun.addEventListener('click', async () => {
  const currentRun = ++generation;
  const result = runScenario(selectedQa);
  qaRun.disabled = true;
  qaRun.setAttribute('aria-busy', 'true');
  delete qaStatus.dataset.kind;
  qaStatus.textContent = 'Идёт проверка…';
  queuePlaceholder();
  for (const [index, step] of result.entries()) {
    if (!reduceMotion.matches) await new Promise((resolve) => setTimeout(resolve, 160));
    if (currentRun !== generation) return;
    qaSteps.children[index].replaceWith(qaRow(index, step.title, step.kind, step.detail, step.lines));
  }
  while (qaSteps.children.length > result.length) qaSteps.lastElementChild.remove();
  const last = result.at(-1);
  qaStatus.dataset.kind = last.kind;
  qaStatus.textContent = last.kind === 'ok' ? 'Статус демо-задачи: «Ждёт ретеста».' : 'Проверка остановлена. Статус задачи не изменён.';
  qaRun.disabled = false;
  qaRun.setAttribute('aria-busy', 'false');
});

function documentFields(root, fields, source) {
  root.replaceChildren(...fields.map(([label, value], index) => {
    const group = make('div');
    const field = make('dd', value);
    if (source && source[index]?.[1] !== value) field.dataset.diff = 'true';
    group.append(make('dt', label), field);
    return group;
  }));
}

choices(get('doc-choices'), documentScenarios, (id) => {
  const result = checkDocument(id);
  documentFields(get('doc-original'), result.original);
  documentFields(get('doc-extracted'), result.extracted, result.original);
  get('doc-checks').replaceChildren(...result.checks.map(({ label, passed, detail }) => {
    const item = make('li');
    item.dataset.pass = String(passed);
    const icon = make('span', passed ? '✓' : '×', 'check-icon');
    icon.setAttribute('aria-hidden', 'true');
    const copy = make('span');
    copy.append(make('strong', label));
    if (detail) copy.append(make('small', detail));
    item.append(icon, copy);
    return item;
  }));
  const status = get('doc-status');
  status.dataset.kind = result.kind;
  status.textContent = result.outcome;
});
