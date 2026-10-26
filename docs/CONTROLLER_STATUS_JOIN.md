# Связь контролёра с отчётом и состояниями

**PORTAL-001.3 — discovery, 2026-10-26**

## Подтверждённый текущий Google Sheets readback

Spreadsheet `14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8`, лист `report_reviewers`, sheetId `146075996`. Readback выполнен через уже настроенный Google Workspace/production read-only gateway; на выходе использовались только агрегаты без ФИО и ID конкретных людей.

| Проверка | Результат на момент readback |
|---|---:|
| Строк данных | 722 |
| Уникальные `report_id` | 722 |
| Повторы `report_id` | 0 |
| Незаполненные `action_id`; `action_id == report_id` | 0; 722/722 |
| Незаполненные `visit_id`; уникальные visit_id | 0; 722 |
| Незаполненные `project_id` / `report_date` | 0 / 0 |
| Незаполненные `corrector_id` среди опубликованных строк | 0 |
| Уникальные `corrector_id` | 22 |
| `source_status` | 722 `CURRENT` |
| Checkpoint schema / published IDs | schema 2 / 722 |
| Совпадение множества report_id sheet ↔ checkpoint | полное; sheet-only=0, checkpoint-only=0 |
| Максимальный `last_successful_fetch` | 2026-10-25 15:15:11.582 UTC (18:15:11 MSK) |

Колонки подтверждены заголовком листа: `report_id, action_id, visit_id, project_id, report_date, corrector_id, corrector_fio, source_status, last_successful_fetch`.

### Ограничение покрытия

722/722 — покрытие только строк, которые попали в опубликованный corrector-only набор. Код сборщика намеренно исключает записи без положительного corrector_id. Поэтому это **не** доля назначений контролёров среди всех portal reports и не доля действительно завершённых проверок. Отчёты без контролёра нельзя посчитать по опубликованной вкладке.

## Карта JOIN

```text
report_reviewers.report_id  -- verified 722/722 equal --> report_reviewers.action_id
       |                                                       |
       | report_id/action_id                                   | action action row
       |                                                       v
       +----------------------------------------------> portal /action/{id}
       |
       +--> visit_id --> portal visit/workflow membership (1:1 only in observed sheet)
       +--> project_id --> project dimension
```

- **CONFIRMED:** на текущих 722 опубликованных строках report_id/action_id совпадают, каждый visit_id уникален в этой конкретной выборке.
- **PARTIAL:** `docs/REPORT_CORRECTORS.md` описывает `/action/{id}` как detail route, который связывает тот же report ID, project и visit; `/report/{id}` направляет обратно в listing. Это основано на прежних ограниченных live checks, а не на новых portal requests этого исследования.
- **UNKNOWN:** на уровне всего портала связь 1:1 report↔visit/action, включая записи без текущего контролёра, не доказана.
- **Важно:** tab `Статусы проектов` содержит агрегаты по project_id; его нельзя напрямую JOIN-ить к 722 строкам report_reviewers и трактовать как report status. Переход к visit-level membership требует отдельного набора и проверки grain.

## Поля контролёра и временная атрибуция

- `corrector_id`/`corrector_fio` берутся из текущего выбранного поля `select[name=staff]` («Проверил») на action page. Это подтверждает текущую выбранную связь в форме, но само по себе **не доказывает**, что проверка завершена.
- `report_date` — дата filing отчёта; это не дата визита и не дата проверки.
- `last_successful_fetch` — время последнего успешного чтения сборщиком; не `review_date`.
- Нет подтверждённого `review_date`, reviewer completion event или поля «проверка завершена» в текущем Sheets contract.
- Checkpoint хранит последний успешный corrector snapshot; при смене одного назначенного контролёра на другого код заменяет `last_good`. Для снятия контролёра сохраняется limited withdrawn value/time, но полной последовательности переназначений это не создаёт.
- Текущий контролёр не может автоматически считаться контролёром, который обрабатывал историческую претензию/возврат.

## Дедупликация и безопасность JOIN

На наблюдённой вкладке ключ `report_id` уникален. Но workflow membership допускает несколько разных `workflow_state_code` на один visit (ключ членства включает код). Поэтому при JOIN событий к отчётам одна строка отчёта может размножиться на несколько состояний. Для count metrics потребуется `COUNT(DISTINCT report_id)` на конкретном статусе либо отдельная event fact; суммы обычных строк без дедупликации запрещены.

Canonical ID при объединении: нормализованный decimal string (`str(report_id)`), с проверкой только цифр и сохранением исходного значения для readback. Не выполнять нестрогий join по display name.

## Ссылка на первичный отчёт

Квалифицированный маршрут в существующей документации corrector collector: `https://lk.j4b.ru/action/{report_id}`. `https://lk.j4b.ru/report/{report_id}` не является подтверждённой detail-ссылкой: по прежним наблюдениям он перенаправлял на реестр. Для будущего поля `report_url` использовать только проверенный `/action/{id}` и перепроверить его перед публикацией нового dataset.
