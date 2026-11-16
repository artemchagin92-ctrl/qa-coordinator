import assert from 'node:assert/strict';
import test from 'node:test';
import { checkDocument, documentScenarios } from '../docs/document.mjs';

test('совпадающие поля готовы к ручной проверке', () => {
  assert.deepEqual(documentScenarios.map(({ id }) => id), ['matched', 'wrong-total', 'duplicate']);

  const result = checkDocument('matched');
  assert.deepEqual(result.original.map(([label]) => label), ['Номер акта', 'Дата', 'Услуга', 'Сумма']);
  assert.deepEqual(result.extracted, result.original);
  assert.ok(result.checks.every(({ passed }) => passed));
  assert.equal(result.kind, 'ok');
  assert.equal(result.outcome, 'Готово к ручной проверке');
});

test('расхождение суммы приостанавливает проверку', () => {
  const result = checkDocument('wrong-total');
  assert.equal(result.kind, 'blocked');
  assert.equal(result.outcome, 'Нужна сверка полей');
  assert.deepEqual(result.extracted.slice(0, 3), result.original.slice(0, 3));
  assert.notEqual(result.extracted.at(-1)[1], result.original.at(-1)[1]);
  assert.deepEqual(result.checks.filter(({ passed }) => !passed).map(({ label }) => label), ['Сумма']);
});

test('дубликат блокируется даже при совпадении всех полей', () => {
  const result = checkDocument('duplicate');
  assert.deepEqual(result.extracted, result.original);
  assert.equal(result.kind, 'blocked');
  assert.equal(result.outcome, 'Акт уже в таблице');
  assert.deepEqual(result.checks.filter(({ passed }) => !passed).map(({ label }) => label), [
    'Дубликат по номеру и дате',
  ]);

  assert.equal(checkDocument('matched').kind, 'ok');
});

test('неизвестный идентификатор сценария отклоняется', () => {
  assert.throws(() => checkDocument('unknown'), RangeError);
});
