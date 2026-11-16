export const documentScenarios = [
  { id: 'matched', label: 'Поля совпали' },
  { id: 'wrong-total', label: 'Сумма не сходится' },
  { id: 'duplicate', label: 'Акт уже в таблице' },
];

const source = {
  number: 'А-017',
  date: '12.11.2026',
  service: 'Организация трансфера участников мероприятия',
  total: 48_500,
};

const sampleResponse = {
  number: 'А-017',
  date: '12.11.2026',
  service: 'Организация трансфера участников мероприятия',
  total: 48_500,
};

const existingRow = { number: 'А-017', date: '19.10.2026' };
const fixtures = {
  matched: { response: sampleResponse, rows: [existingRow] },
  'wrong-total': {
    response: { ...sampleResponse, total: 45_800 },
    rows: [existingRow],
  },
  duplicate: { response: sampleResponse, rows: [existingRow, source] },
};

const fields = [
  ['number', 'Номер акта'],
  ['date', 'Дата'],
  ['service', 'Услуга'],
  ['total', 'Сумма'],
];

const rubles = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  maximumFractionDigits: 0,
});

function displayValue(field, value) {
  return field === 'total' ? rubles.format(value) : value;
}

function actKey({ number, date }) {
  return `${number} / ${date}`;
}

export function checkDocument(id) {
  if (!Object.hasOwn(fixtures, id)) {
    throw new RangeError('Неизвестный сценарий');
  }

  const { response, rows } = fixtures[id];
  const original = fields.map(([field, label]) => [label, displayValue(field, source[field])]);
  const extracted = fields.map(([field, label]) => [label, displayValue(field, response[field])]);
  const checks = fields.map(([field, label]) => {
    const passed = source[field] === response[field];
    return {
      label,
      passed,
      detail: passed
        ? 'Совпадает с актом.'
        : `В акте ${displayValue(field, source[field])}, в ответе ${displayValue(field, response[field])}.`,
    };
  });

  const fieldsMatch = checks.every(({ passed }) => passed);
  const key = actKey(response);
  const isDuplicate = rows.some((row) => actKey(row) === key);
  checks.push({
    label: 'Дубликат по номеру и дате',
    passed: !isDuplicate,
    detail: isDuplicate
      ? `В демонстрационной таблице уже есть акт ${key}.`
      : `В демонстрационной таблице нет акта ${key}.`,
  });

  let outcome = 'Готово к ручной проверке';
  if (!fieldsMatch) outcome = 'Нужна сверка полей';
  else if (isDuplicate) outcome = 'Акт уже в таблице';

  return {
    original,
    extracted,
    checks,
    outcome,
    kind: fieldsMatch && !isDuplicate ? 'ok' : 'blocked',
  };
}
