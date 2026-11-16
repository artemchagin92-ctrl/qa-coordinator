import assert from 'node:assert/strict';
import test from 'node:test';
import { runScenario, scenarios } from '../docs/engine.mjs';

test('успешный сценарий передаёт задачу на ретест после проверки релиза', () => {
  assert.deepEqual(scenarios.map(({ id }) => id), ['ready', 'test-failed', 'row-changed']);

  const steps = runScenario('ready');
  assert.deepEqual(steps.map(({ id, kind }) => [id, kind]), [
    ['snapshot', 'ok'],
    ['checks', 'ok'],
    ['release', 'ok'],
    ['status', 'ok'],
  ]);
  assert.equal(steps.at(-1).title, 'Ждёт ретеста');
  assert.ok(steps.at(-1).lines.includes('Записан статус: Ждёт ретеста'));
});

test('падение теста останавливает сценарий до релиза и записи статуса', () => {
  const steps = runScenario('test-failed');
  assert.deepEqual(steps.map(({ id }) => id), ['snapshot', 'checks']);
  assert.equal(steps.at(-1).kind, 'blocked');
  assert.ok(steps.at(-1).lines.some((line) => line.endsWith('exit 1')));
});

test('изменение хеша оставляет статус строки прежним даже после подтверждения релиза', () => {
  runScenario('ready');
  const steps = runScenario('row-changed');
  assert.deepEqual(steps.slice(-2).map(({ id, kind }) => [id, kind]), [
    ['release', 'ok'],
    ['status', 'blocked'],
  ]);
  assert.ok(steps.at(-1).lines.includes('Сохранён статус: В работе'));
  assert.ok(steps.every(({ lines }) => lines.every((line) => !line.startsWith('Записан статус'))));
});
