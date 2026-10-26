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
| Проекты отчётов, присутствующие в `projects_current` / `Статусы проектов` | 57/57 |
| Эти проекты с mapping в `project_types` | 56/57 |

Колонки подтверждены заголовком листа: `report_id, action_id, visit_id, project_id, report_date, corrector_id, corrector_fio, source_status, last_successful_fetch`.

### Ограничение покрытия

722/722 — покрытие только строк, которые попали в опубликованный corrector-only набор. Код сборщика намеренно исключает записи без положительного corrector_id. Ограниченный portal GET по report_id из checkpoint с `NO_CORRECTOR` вернул `staff=EMPTY`, и такой report отсутствует в `report_reviewers` по конструкции. Поэтому это **не** доля назначений контролёров среди всех portal reports и не доля действительно завершённых проверок. Отчёты без контролёра нельзя посчитать по опубликованной вкладке.

## Карта JOIN и проверенная cardinality

```text
report_reviewers.report_id == report_reviewers.action_id
       |
       +-- exact action key --> portal action row (limited samples validated)
       |                            \
       |                             +-- workflow memberships (multiple per action possible)
       |
       +-- project_id + visit_id --> payment visit aggregate (720/722 match)
       |                                (no action_id; may combine multiple actions)
       +-- project_id --> project dimension / project-level status aggregates
```

- **CONFIRMED:** на текущих 722 строках `report_id==action_id`; ограниченные live samples для кодов 35, 38, 39, 40 подтвердили detail route, который связывает report/action с одним visit и project.
- **CONFIRMED:** отдельный no-corrector checkpoint sample вернул `staff=EMPTY`; отсутствие записи на corrector-only листе ожидаемо, но сам лист не измеряет их количество.
- **CONFIRMED:** JOIN по `(project_id, visit_id)` к `Выплаты по визитам` даёт 720/722 matches, 2 unmatched, 0 multiple matches на этих строках. В payment tab все 88,864 composite keys уникальны; 5 `visit_id` повторяются между разными project_id.
- **CONFIRMED:** оба unmatched report visits присутствуют в portal action search. Для одного visit обнаружены две action/report записи: старый report/action с code 15 и report_date 2026-09-24 (action_id отличается от current report_id) и отдельный current report/action с code 50 и report_date 2026-10-01 (совпадает с sheet). У второго unmatched visit найден current report/action с code 50. Оба current reports имеют corrector.
- **UNKNOWN:** точная причина отсутствия двух visit keys в `Выплаты по визитам`. По коду этот лист формируется только из payment assignments с qualified visit mapping; это вероятный класс причины, но не доказательство по каждой записи.
- **CONFIRMED:** все 57 проектов из `report_reviewers` присутствуют в `projects_current` и `Статусы проектов`; 56 имеют `project_types` mapping.
- **Важно:** у одного visit может быть несколько action/report records. JOIN по одному `visit_id` к action-level statuses может размножить строки и приписать code 15 старому report current report. Для report-level metrics нужен action/report ID; для бизнес-агрегации одной операции используйте distinct `(project_id, visit_id)` после явного выбора report/version.
- **Важно:** `Статусы проектов` — project aggregate. `Выплаты по визитам` — payment-scoped visit aggregate без action_id. Ни одну из них нельзя напрямую считать полной report-level status table.

## Статусные membership counts в текущем corrector sample

Среди 720 report-reviewer keys, совпавших с payment visit table, current `workflow_state_codes` дают: code 35 — 7; 38 — 1; 39 — 0; 37 — 0; 40 — 696; 50 — 139. Counts пересекаются и не складываются. Все 720 matched visits имеют как минимум один код из {37,40,50}; это согласуется с кодовой формулой `finished_visits`, но не доказывает completed review именно контролёром. Два unmatched reports в ограниченном portal check имеют current code 50.

Есть зафиксированный пробел покрытия: найденный live report/action с code 39, датой 2026-10-17 и выбранным corrector отсутствует в checkpoint и `report_reviewers`; date-filtered registry read тем же DMY форматом не вернул его (144 других IDs за день). Поэтому 722-строчная вкладка не полна по отношению к порталу.

Sum code 39 в nonblank project aggregate rows `projects_current` и `Статусы проектов` = 32; workflow fields заполнены только для 617/8,088 projects, поэтому это не complete portal total. `Выплаты по визитам` содержит code39 membership на 50 visit keys в payment-scoped population. Эти populations/grains не совпадают. Ни одно значение нельзя использовать как окончательный report-level numerator без reconciliation.

## Поля контролёра и временная атрибуция

- `corrector_id`/`corrector_fio` берутся из текущего выбранного поля `select[name=staff]` («Проверил») на action page. Это подтверждает текущую выбранную связь в форме, но само по себе **не доказывает**, что проверка завершена.
- `report_date` — дата filing отчёта; это не дата визита и не дата проверки.
- `last_successful_fetch` — время последнего успешного чтения сборщиком; не `review_date`.
- Нет подтверждённого `review_date`, reviewer completion event или поля «проверка завершена» в текущем Sheets contract.
- Checkpoint хранит последний успешный corrector snapshot; при смене одного назначенного контролёра на другого код заменяет `last_good`. Для снятия контролёра сохраняется limited withdrawn value/time, но полной последовательности переназначений это не создаёт.
- Текущий контролёр не может автоматически считаться контролёром, который обрабатывал историческую претензию/возврат.

## Дедупликация и безопасность JOIN

На наблюдённой вкладке `report_id` уникален. Но у одной report/action row может быть несколько workflow membership codes одновременно; live sample показал 35+40 для одной записи и 38+40 для другой. Также один visit может иметь несколько action/report IDs. При JOIN по visit ID строки могут размножиться/смешать версии; счётчики строить по точному report/action ID, а бизнес-агрегацию — через `COUNT(DISTINCT project_id, visit_id)` после определения выбранной версии. Membership codes не являются transition rows.

Canonical ID при объединении: нормализованный decimal string (`str(report_id)`), с проверкой только цифр и сохранением исходного значения для readback. Не выполнять нестрогий join по display name.

## Ссылка на первичный отчёт

Квалифицированный маршрут в существующей документации corrector collector: `https://lk.j4b.ru/action/{report_id}`. `https://lk.j4b.ru/report/{report_id}` не является подтверждённой detail-ссылкой: по прежним наблюдениям он перенаправлял на реестр. Для будущего поля `report_url` использовать только проверенный `/action/{id}` и перепроверить его перед публикацией нового dataset.
