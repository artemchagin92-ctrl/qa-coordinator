export const scenarios = [
  { id: 'ready', label: 'Проверки прошли' },
  { id: 'test-failed', label: 'Тест упал' },
  { id: 'row-changed', label: 'Задача изменилась' },
];

const fixtures = {
  ready: { testExit: 0, currentHash: '92f1c7' },
  'test-failed': { testExit: 1, currentHash: '92f1c7' },
  'row-changed': { testExit: 0, currentHash: 'b30a16' },
};

const snapshot = {
  id: 'QA-017',
  hash: '92f1c7',
  status: 'В работе',
  title: 'Кнопка «Сохранить» перекрывает подпись на узком экране',
};

const patch = {
  allowedPaths: ['src/profile.css', 'tests/profile.spec.js'],
  changedPaths: ['src/profile.css', 'tests/profile.spec.js'],
  reportedExit: 0,
  testCommand: 'profile.spec.js',
};

const release = {
  expectedRevision: '8c21da4',
  servedRevision: '8c21da4',
  healthHttp: 200,
  expectedAssetHash: 'c04a32',
  servedAssetHash: 'c04a32',
};

export function runScenario(id) {
  if (!Object.hasOwn(fixtures, id)) {
    throw new RangeError('Неизвестный сценарий');
  }

  const fixture = fixtures[id];
  const steps = [
    {
      id: 'snapshot',
      kind: 'ok',
      title: 'Снимок очереди',
      detail: snapshot.title,
      lines: [`Google Sheets → ${snapshot.id}`, `Хеш строки: ${snapshot.hash}`, `Статус: ${snapshot.status}`],
    },
  ];

  const pathsAllowed = patch.changedPaths.length > 0
    && patch.changedPaths.every((path) => patch.allowedPaths.includes(path));
  const testPassed = fixture.testExit === 0 && fixture.testExit === patch.reportedExit;
  const patchAccepted = pathsAllowed && testPassed;
  steps.push({
    id: 'checks',
    kind: patchAccepted ? 'ok' : 'blocked',
    title: patchAccepted ? 'Патч проверен' : 'Патч не принят',
    detail: patchAccepted
      ? 'Изменения в разрешённых файлах. Успешный код выхода подтверждён журналом.'
      : 'Журнал не подтверждает успешную проверку. Релиз и обновление статуса остановлены.',
    lines: [
      `Файлы: ${patch.changedPaths.join(', ')}`,
      `Playwright · журнал ${patch.testCommand}: exit ${fixture.testExit}`,
    ],
  });
  if (!patchAccepted) return steps;

  const releaseConfirmed = release.servedRevision === release.expectedRevision
    && release.healthHttp === 200
    && release.servedAssetHash === release.expectedAssetHash;
  steps.push({
    id: 'release',
    kind: releaseConfirmed ? 'ok' : 'blocked',
    title: releaseConfirmed ? 'Релиз подтверждён' : 'Релиз не подтверждён',
    detail: releaseConfirmed
      ? 'Код выпуска, ответ сервиса и хеш веб-ресурса совпали с ожидаемыми.'
      : 'Проверка релиза не прошла. Статус задачи сохранён.',
    lines: [
      `Ревизия: ${release.servedRevision}`,
      `/health: HTTP ${release.healthHttp}`,
      `Хеш ресурса: ${release.servedAssetHash}`,
    ],
  });
  if (!releaseConfirmed) return steps;

  const currentRow = { ...snapshot, hash: fixture.currentHash };
  const rowUnchanged = currentRow.id === snapshot.id && currentRow.hash === snapshot.hash;
  if (rowUnchanged) currentRow.status = 'Ждёт ретеста';
  steps.push({
    id: 'status',
    kind: rowUnchanged ? 'ok' : 'blocked',
    title: rowUnchanged ? currentRow.status : 'Строка изменилась',
    detail: rowUnchanged
      ? 'Хеш строки совпал. Задача передана тестировщику на повторную проверку.'
      : 'Хеш строки поменялся после исходного чтения. Обновление статуса пропущено.',
    lines: [
      `Хеш при повторном чтении: ${currentRow.hash}`,
      rowUnchanged ? `Записан статус: ${currentRow.status}` : `Сохранён статус: ${currentRow.status}`,
    ],
  });
  return steps;
}
