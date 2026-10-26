# Методика и доступность показателей качества контролёров

**PORTAL-001.3 — discovery, 2026-10-26**

## Результат feasibility

На текущих данных можно подтвердить текущую связь report/action → выбранный corrector для 722 опубликованных строк и найти workflow memberships на уровне action/visit. Но числитель/знаменатель для полного дашборда **не готовы**: report registry пропустил как минимум один реальный October report с code 39 и выбранным контролёром; `Выплаты по визитам` не покрывает 2 из 722 current report/visit keys; status aggregation различается между вкладками; review timestamp и history transitions отсутствуют.

`source_status=CURRENT` в `report_reviewers` — статус получения/свежести corrector field, а не исходный статус отчёта. Использовать его как «отчёт проверен» нельзя.

## Показатели

| Показатель | Предлагаемое определение | Необходимые данные | Feasibility / полнота | Риск интерпретации |
|---|---|---|---|---|
| Проверено контролёром | DISTINCT business operation key с подтверждённым review completion; attribution corrector на action/report к моменту review | review event/result + report/action + corrector_id + `reviewed_at` | **PARTIAL:** existing `finished_visits` predicate = any `{37,40,50}` доступен в project aggregates; в current controller sample 720/720 payment-matched rows имеют один из этих кодов, а два unmatched live reports имеют 50. Это не подтверждает время/actor завершения. | Не подменять confirmed review event текущим selected `staff` или payment/approval state |
| Reports with current code 39 | DISTINCT operation/report key с code39 membership в текущем action snapshot | action_id/report_id, visit/project, status membership snapshot time | **PARTIAL:** точная текущая membership может считаться внутри опубликованной выборки; ранее найденный live report/action с code39 отсутствовал в source/checkpoint, поэтому total не является полным Portal числом. | Показывать только как observed snapshot; code39 не доказывает ошибку корректора |
| Reports that ever had 39 | DISTINCT report/business operation with retained code39 history | complete event history / guaranteed historical status set | **UNKNOWN / NOT COMPUTABLE:** current memberships have no transition timestamps or completeness guarantee | Не трактовать current membership как `EVER_HAD_STATUS` |
| Shopper remarks / returned to shopper | DISTINCT report/action with a validated return-to-shopper status/event | approved status ID/label, report/action key, event timestamp/reason | **BLOCKED:** code35=`Анкета отклонена` (7 matched current report rows), code38=`Вопрос координатору` (1); live samples confirm IDs and corrector link, но это не подтверждает shopper-return semantics | Нельзя переименовывать по похожему смыслу без SME/portal evidence |
| Repeat returns / rechecks | count of distinct validated return/recheck transitions | immutable event ID, previous/new status, changed_at, report_id | **NOT FEASIBLE** без transition log | Observed one visit with two distinct report/action records (old status15 / later status50) is not itself a repeated transition |
| Доля текущей status-39 membership | DISTINCT report/visit keys со status 39 / same-grain keys с известной membership | status flags `OBSERVED_PRESENT` / `OBSERVED_ABSENT` в одной генерации | **AVAILABLE_IN_OBSERVED_SNAPSHOT:** `UNKNOWN` исключён, нулевой знаменатель остаётся NULL. Не полный Portal denominator. | Не называть долей претензий среди всех завершённых проверок или Portal-wide rate |
| Report drilldown | exact report_id/action_id + relevant membership/event + source link | report/action link and membership/event source | **PARTIAL:** `/action/{id}` works for sampled links; specific report can be absent from corrector sheet/registry | Link existence does not prove event history or full coverage |

## `CURRENT_STATUS`, `EVER_HAD_STATUS`, `STATUS_TRANSITION_COUNT`

- `CURRENT_STATUS` сейчас корректнее представлять как **набор workflow membership codes на action/report row с `as_of_at`**, не single exclusive status: live samples показали одновременные code sets `{35,40}` и `{38,40}`. Report-level status field отдельно не найден.
- `EVER_HAD_STATUS` требует непрерывной истории или гарантии, что текущая запись содержит весь период. Такой контракт не подтверждён.
- `STATUS_TRANSITION_COUNT` требует одного события на переход, устойчивого event ID и временной сортировки. В текущих источниках этого нет.

Три понятия не взаимозаменяемы и не должны подменяться текущим status code.

## Знаменатель и когортирование

Базовый знаменатель должен быть **completed reviews/business operations**, а не число строк с corrector_id. Текущее поле `staff` («Проверил») само по себе может быть assignment. Кодовая формула существующей project metric `finished_visits` — DISTINCT visits с любым из {37,40,50}; её можно показать как `workflow-finished`, но attribution завершения контролёру и дата review отдельно не зафиксированы.

Нельзя смешивать периоды:

- `visit_date` — когда состоялся визит;
- `report_date` — дата filing отчёта (`input[name=dt]` по существующей документации);
- `review_date` — дата завершения проверки, пока не доступна;
- `claim_date` — дата возникновения/регистрации претензии, пока не доступна.

Для будущего показателя нужно явно выбрать когорту (например, report filing month или review completion month) и сопоставить числитель/знаменатель по одной и той же когорте, если источники это позволяют.

## Атрибуция и интерпретация

Статус «Есть претензия» является сигналом/исходом процесса, не причинным доказательством ошибки контролёра. Перед индивидуальным сравнением требуется:

1. подтверждённый corrector-at-review event, а не текущий assignment;
2. статус и время, привязанные к тому же report_id;
3. разбивка claim outcome (обоснована/не обоснована/в ожидании) либо явно более слабая метрика `claim-associated reports`;
4. альтернативные факторы: ТП, сложность проекта, неясная инструкция, клиентское правило, координатор;
5. знаменатель завершённых проверок и сопоставимый период.

До этого допустимо говорить только о корреляции «отчёт был связан с претензией» после проверки семантики. Нельзя называть это частотой ошибок контролёра.

## Выбор источника по колонкам

| Поле/метрика | Приоритетный источник | Почему/ограничение |
|---|---|---|
| report_id, action_id, visit_id, project_id, report_date, текущий corrector_id/FIO | `report_reviewers` как материализованный snapshot | Текущая readback-проверка: 722 уникальных ID, 0 дублей, report_id=action_id, schema содержит `last_successful_fetch`; это не completion history |
| актуальная workflow membership | существующий filter POST `/action` (read-only search), только project-scoped; output должен сохранять action_id | Ограниченные live samples подтвердили membership и `action_id==report_id` для выбранных current reports. Одно visit может иметь разные action IDs; join по одному visit может размножить/перепутать статусы |
| project-level workflow totals | Google Sheets `Статусы проектов` | Быстрый контроль агрегатов по визитам; непригоден для report-level drilldown |
| report status history / actor / timestamps | В шести вкладках и refresh outputs отсутствуют; sampled action detail не показал transition table | PostgreSQL на этом этапе не нужен и не использовался; если history станет обязательной, сначала квалифицировать отдельный approved read-only source |
| report URL | портал `https://lk.j4b.ru/action/{report_id}` | Так указано в существующей ограниченной квалификации; перепроверять при переходе к dataset |

## Статус валидации примеров

Ограниченная валидация проведена без вывода ID/FIO: code39 report/action с корректно выбранным контролёром найден напрямую, но отсутствовал в registry/checkpoint/sheet; code35, 38, 40 verified на report-linked action rows; no-corrector checkpoint sample дал portal `staff=EMPTY`; status40/50 reports имеют report links. Это не массовая completeness validation. В опубликованной вкладке нет строк без corrector; отсутствие контролёра в общем portal population по ней не измеряется.

## Текущая read-only проверка источника, 2026-10-26

Выполнена ограниченная проверка metadata и `report_reviewers!A1:I1000` в существующей книге `J4B Portal — DataLens Materialized Layer`. Чтение было read-only; строки и имена не сохранялись в репозиторий.

- Фактический заголовок совпал с девятью полями: `report_id`, `action_id`, `visit_id`, `project_id`, `report_date`, `corrector_id`, `corrector_fio`, `source_status`, `last_successful_fetch`.
- Найдено 722 непустые строки; 722 уникальных `report_id`, 722 уникальных `action_id`, 722/722 строк имеют `report_id = action_id`.
- 722 уникальных ключа `(project_id, visit_id)`, без повторов в этой выгрузке; 57 проектов и 22 corrector ID представлены.
- Во всех 722 строках `source_status=CURRENT`; обязательные ID/date/corrector-поля заполнены.
- Диапазон `report_date`: 2026-10-01—2026-10-25.
- Это подтверждает гранулярность «одна наблюдаемая report/action-запись на строку» только внутри текущей выборки. Это не доказывает полноту Portal population, завершение проверки, временную атрибуцию корректора или отсутствие нескольких версий в Portal.
- Источник не содержит полей претензий, отклонений, результата проверки, review timestamp или истории переходов. `source_status` — статус коллектора, не статус отчёта.

Эти числа являются снимком на момент чтения, а не ожидаемыми контрольными totals. Ранее подтверждённый пропуск одного Portal report в реестре остаётся ограничением полноты.

## Формулы и разрешённая подпись показателей

| Запрошенная метрика | Формула / ключ | Решение для текущего источника |
|---|---|---|
| Наблюдаемые report/action-записи | `COUNTD(report_id)` | Доступно только с подписью «наблюдаемые записи с выбранным corrector»; не называть проверенными отчётами. |
| Наблюдаемые записи по corrector | `COUNTD(report_id)` по `corrector_id` в текущем snapshot | Доступно как snapshot распределение записей; не называть workload или производительностью проверок. |
| Проекты в наблюдаемом snapshot по corrector | `COUNTD(project_id)` по текущему corrector snapshot | Доступно как число представленных проектов; не доказывает активную работу или закрытый review. |
| Current status-39 associations | `COUNTD(report_id)` / `COUNTD(project_id,visit_id)` with exact current membership 39 | Available within this generation as **observed snapshot** counts; not an individual claim count and not a complete Portal total. |
| Observed status-39 report/visit share | same-grain current status-39 distinct keys / keys whose membership is known in the same snapshot | Available as observed-snapshot share only; `UNKNOWN` excluded, NULL denominator remains NULL. Not a completed-review or Portal-wide rate. |
| Project status-39 distribution | unique report/visit keys with status39, grouped by project | Available as observed project associations, not a full claims register or ranking. |
| Отклонения на проект | подтверждённые deviation outcomes / проекты с завершённой проверкой | `NOT_AVAILABLE`: ни поле/событие отклонения, ни семантика outcome не подтверждены. Не заменять status 35/38/39. |
| Возвраты / повторные проверки | уникальные подтверждённые transition/event IDs | `NOT_AVAILABLE`: нет transition history; несколько action/report records на visit не доказывают повторный переход. |

Полные `claims_per_report`, `claims_per_project`, `completed_review_rate` и причинные рейтинги остаются `NOT_AVAILABLE`. Точечные observed status-39 rates имеют отдельные same-generation знаменатели, исключают `UNKNOWN`, сохраняют NULL при нулевом denominator и всегда показывают snapshot coverage.

## DataLens first-version scope

Утверждённая для подготовки структура не должна показывать неподтверждённые KPI как числа:

1. **Coverage/freshness:** active generation, source snapshot, successful collector time, observed row counts, unknown/unmatched statuses и `OBSERVED_CORRECTOR_SNAPSHOT_ONLY`.
2. **Наблюдаемый source snapshot:** отчётные записи и представленные проекты по выбранному corrector; подписи не должны называть это закрытыми проверками или workload.
3. **Текущие workflow memberships:** observed present/unknown по кодам; код 39 — сигнал «Есть претензия», не оценка качества корректора.
4. **Проектная и report/action детализация:** точные ID и ссылка на `/action/{action_id}`; несколько версий visit сохраняются, current version показывается как unresolved.

Фильтры для набора срезов: `report_date`, corrector ID, project ID и coverage/status. `report_date` — filing date, не review date. Все fact datasets обязаны быть ограничены одной активной генерацией через manifest. Существующий production workbook и действующие DataLens dashboard/dataset не изменялись.

## Реализация и публикация: фактический статус

- Реализованные builder, validation, generation-safe publisher, safe aggregate CLI, tests и DataLens runbook находятся в отдельной ветке разработки; полный suite проверен локально.
- В production workbook существуют только шесть текущих вкладок, включая source `report_reviewers`; отдельного owner-provisioned controller-quality workbook с четырьмя целевыми схемами не найдено.
- В доступных подключённых инструментах DataLens read/write integration отсутствует. Поэтому фактическую конфигурацию/наличие действующего controller-quality dashboard, его filters и drilldown подтвердить или изменить из этой среды нельзя.
- Для UI-приёмки и тестовой публикации требуются отдельный непубличный owner-provisioned workbook с Editor-доступом сервисному аккаунту и DataLens read/write access в отдельном тестовом пространстве. До этого `--plan`/`--publish` не запускать; production workbook не использовать как target.

## Уточнённый scope: ассоциации контролёров и статус 39

Уточнение владельца разделяет замечания контролёров покупателям и претензии клиентов к результатам компании. Замечания контролёров сейчас не измеряются, не выводятся из workflow-кодов и остаются вне scope. Workflow code 39 (`Есть претензия`) — индикатор клиентской претензии по связанной workflow-сущности; он не доказывает ошибку контролёра/покупателя, обоснованность претензии или признание вины. Текущий `/action` snapshot не содержит истории переходов и отдельных claim IDs, поэтому показываем текущую наблюдаемую membership, не число индивидуальных претензий и не историческое «проходил статус 39».

Для `controller_report_snapshot` доступны описательные ассоциации внутри опубликованной выборки: distinct `report_id`, composite visit `(project_id, visit_id)`, `project_id`, а также те же counts по связанному `corrector_id` и проекту. Они не равны полному Portal workload и не доказывают завершённую проверку. Визит/проект может быть связан с несколькими контролёрами, поэтому сгруппированные distinct visit/project totals перекрываются и неаддитивны.

Доли статуса 39 допустимы только как наблюдаемые same-grain shares: distinct status-39 reports / distinct reports со статусом `OBSERVED_PRESENT` или `OBSERVED_ABSENT`; для visits — distinct composite visits с хотя бы одной status-39 report/action / distinct composite visits хотя бы с одной известной membership. Строки `UNKNOWN` не входят в знаменатель и никогда не считаются нулём. Нулевой знаменатель даёт `NULL`/`NOT_AVAILABLE`. Эти доли не являются Portal-wide claim rates, так как source coverage неполон. Порог «необычно высокая доля» не квалифицирован; такие флаги и рейтинги остаются `NOT_AVAILABLE`.

`report_id=action_id` подтверждено для ограниченного snapshot 722 строк, но не является общим Portal contract. Дата — `report_date`/дата подачи отчёта, не дата проверки или претензии. Project name отсутствует в текущем source contract; не подставлять название из не связанного источника.

### Fixture/регрессионная проверка метрик

`src.controller_quality_mart.summarize_observed_metrics` проверяет агрегаты на одном candidate generation и не делает сетевых запросов. Тестовый fixture включает status 39, known absence, полностью unknown status, повторные report rows одного composite visit и одного визита, связанного с разными контролёрами. Проверяются distinct grains, same-grain denominators, неаддитивность grouped visits и сохранение `NULL` при нулевом знаменателе. Эти synthetic fixtures являются только unit tests; они не являются опубликованными наблюдениями.

## Тестовая публикация и dashboard — уточнение 2026-10-26

Ранее в документе описан feasibility review до создания тестовой витрины. После owner-approved historical backfill TEST workbook содержит реальные accumulated records; private DataLens connection, два датасета и отдельный TEST dashboard созданы и проверены. Это не изменяет ограничение полноты source: 722 уникальных отчёта/визита за `report_date` 2026-10-01—2026-10-25, 22 контролёра и 57 проектов — наблюдаемая выборка, а не полная совокупность Portal.

В этой генерации текущая status-39 membership = 0 из 719 composite visits с известной membership; 3 визита имеют `UNKNOWN` и не считаются отсутствием статуса. Наблюдаемая доля = 0/719. Значение не является Portal-wide долей претензий: ранее обнаруженный реальный report с code 39 отсутствовал в source/checkpoint. Dashboard использует только report filing date как дату фильтра; это не дата визита, review или претензии. Четыре `last_successful_fetch` значения — provenance строк, а не четыре независимых дневных снимка. Status trend, baseline/moving average и среднее по интервалу остаются `NOT_AVAILABLE`.

Regression fixtures теперь также фиксируют однозначные примеры для одной и той же формулы: 5 status-39 visits / 500 eligible visits = 1%, и 5 / 50 = 10%. Это искусственные unit-test inputs и не входят в опубликованный workbook.

## Требования владельца к KPI и временной аналитике — 2026-10-26

Главная доступная доля для этой TEST-выборки:

```text
CLAIM_RATE_OBSERVED = distinct composite visits with current status_39=OBSERVED_PRESENT
                      / distinct composite visits with status_39 known
                      * 100
```

Обе части вычисляются на одних и тех же фильтрах и в одной генерации. Сначала дедуплицируется `(project_id, visit_id)`, поэтому повторная report/action-запись не увеличивает ни числитель, ни знаменатель. `UNKNOWN` остаётся вне знаменателя; пустой denominator должен давать `NULL/N/A`. Это доля только наблюдаемой выборки, а не Portal-wide claim rate. Нельзя усреднять проценты отдельных проектов для получения общей доли.

Для средних по проектам допустим только явный observed denominator: уникальные представленные project IDs, имеющие хотя бы один визит с известной status-39 membership. Среднее status-39 visit/project использует distinct composite visit keys. Для контролёров допустимо показать `status-39 visit associations / represented controllers with known status`; один визит, ассоциированный с двумя контролёрами, входит в обе группы. Это описательная ассоциация, не число уникальных претензий компании и не оценка ответственности контролёра. `NULL` обязателен при нулевом знаменателе.

Среднее за день/месяц, средняя доля по периодам, изменение к предыдущему сопоставимому периоду и 7-дневная/3-месячная скользящая средняя остаются `NOT_AVAILABLE`: собранный реестр содержит одну текущую membership status-39, а четыре даты успешного получения — provenance строк, не четыре независимых снимка статуса. `report_date` подтверждён как дата подачи отчёта; это не дата визита, события или претензии. Не использовать её как временную ось возникновения претензий. Без доказанной полноты календаря нельзя считать ненаблюдавшиеся интервалы нулевыми.

Для TEST dashboard подтверждены реальные данные накопленного реестра: 722 уникальные записи отчётов/визитов, 57 проектов, 22 контролёра; известная status-39 membership у 719 визитов, `UNKNOWN` у 3; 0 известных визитов со статусом 39 в этой generation. Это partial corrector registry, и один ранее подтверждённый status-39 report отсутствовал в source/checkpoint. Следовательно, dashboard может честно показывать наблюдаемые totals/rate с coverage warning; он не может утверждать отсутствие претензий во всём Portal.

Текущий TEST dashboard содержит фильтры «Контролёр», «Проект» и «Дата отчёта», KPI наблюдаемой status-39 доли/числа, project visit chart и report detail с реальными `/action/{report_id}` ссылками. Фильтр даты ограничивает только дату подачи отчёта. Источник не предоставляет project name или business visit date, поэтому детализация использует project ID и `report_date`. Отдельные год/месяц-селекторы, проверка совместного cross-filter всех KPI, агрегатные таблицы претензий по проектам/контролёрам и фильтр пересечения project × controller должны считаться неподтверждёнными, пока не пройдёт интерактивная проверка их связей.

Для приемки дашборда синтетические проверки 5/500 = 1% и 5/50 = 10% остаются только в unit tests. Реальная выборка не дополняется искусственными строками. Абсолютный count и процент показываются раздельно, с размером знаменателя и подписью наблюдаемого покрытия.
