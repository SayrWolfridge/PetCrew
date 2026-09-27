# Penpot по требованию

updated_at: 2026-09-14T15:25:00+03:00
status: installed manual-only; live design acceptance deferred after Codex App Server stall

## Purpose contract

- `job_to_be_done`: дать Codex полноценную работу с живым Penpot-файлом только тогда, когда Лиса
  явно просит Penpot, не заставляя Codex App Server постоянно подключаться к выключенному порту.
- Боль: глобально включённый HTTP MCP создаёт фоновые connection failures, а прежняя Windows-задача
  запускала watchdog при каждом входе в систему даже без дизайнерской работы.
- Источник истины: текущий Penpot-файл и его plugin UI для дизайна; существующая scheduled task
  `Tribunska Penpot MCP` для локального сервера; отсутствие `mcp_servers.penpot` в Codex config;
  этот документ и skill `use-penpot-on-demand` для жизненного цикла.

## Allowed state

- Глобальная запись Penpot MCP отсутствует в Codex; skill использует прямой локальный клиент.
- Scheduled task существует в одном экземпляре, допускает ручной запуск и не имеет активного
  logon-trigger.
- Навык запускается только явно, поднимает существующий pinned `@penpot/mcp@2.15.4`, проверяет порт
  4401 и отдельно требует соединения Penpot plugin UI.
- Клиент обращается только к `localhost:4401/mcp`, поддерживая IPv4 и IPv6 loopback; мутации
  ограничены выбранным Penpot-файлом.
- Сервер останавливается после работы только если его запустил текущий вызов навыка.

## Forbidden state

- Любая глобальная запись Penpot MCP, heartbeat, polling, новый watchdog или второй сервер.
- Автоматическая загрузка/обновление npm-пакета; `latest` вместо зафиксированной версии.
- Открытие или изменение Penpot без явного запроса; убийство процессов по имени, порту или маске.
- Повтор мутирующего вызова после timeout без проверки фактического Penpot-файла.
- Признание порта 4401 достаточным доказательством: plugin UI должен быть подключён к выбранному
  файлу.

## Fitness acceptance

1. После входа в Windows scheduled task остаётся `Ready`, а порты 4400-4402 закрыты.
2. Без вызова навыка Codex не получает Penpot connection failures.
3. Явный вызов запускает ровно существующую задачу и даёт `tools/list` через локальный клиент.
4. В открытом Penpot-файле выполняется одна read-only проверка через MCP.
5. Завершение навыка останавливает созданный им сервер; повторный status показывает task `Ready` и
   закрытые порты.
6. Существующий сервер, запущенный не этим навыком, не останавливается.

## Rollback and current gate

Prechange scheduled-task XML and launcher copy:
`C:/Work/PetCrew/artifacts/backups/penpot-on-demand-prechange-20260914-1310`.

The exact npm package `@penpot/mcp@2.15.4`, manual-only scheduled task, and skill-only plugin are
installed. The first live start proved that Penpot can build and expose its local services, but also
coincided with a Codex App Server six-slot queue stall. The global Penpot config entry was therefore
removed, and the controller was corrected to detect both IPv4 and IPv6 loopback. Do not repeat the
live design smoke during the current session. A later acceptance run is limited to start -> list ->
one read-only Penpot call -> stop, after a fresh Codex start confirms no background Penpot catalog
entry. Rollback re-registers the saved task XML, restores the protected Codex config backup, removes
the plugin, and stops only the exact task started by the smoke.
