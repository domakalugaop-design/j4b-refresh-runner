# Методика и доступность показателей качества контролёров

**PORTAL-001.3 — discovery, 2026-10-26**

## Результат feasibility

На текущих данных можно подтвердить текущую связь report/action → выбранный corrector для 722 опубликованных строк и найти workflow memberships на уровне action/visit. Но числитель/знаменатель для полного дашборда **не готовы**: report registry пропустил как минимум один реальный October report с code 39 и выбранным контролёром; `Выплаты по визитам` не покрывает 2 из 722 current report/visit keys; status aggregation различается между вкладками; review timestamp и history transitions отсутствуют.

`source_status=CURRENT` в `report_reviewers` — статус получения/свежести corrector field, а не исходный статус отчёта. Использовать его как «отчёт проверен» нельзя.

## Показатели

| Показатель | Предлагаемое определение | Необходимые данные | Feasibility / полнота | Риск интерпретации |
|---|---|---|---|---|
| Проверено контролёром | DISTINCT business operation key с подтверждённым review completion; attribution corrector на action/report к моменту review | review event/result + report/action + corrector_id + `reviewed_at` | **PARTIAL:** existing `finished_visits` predicate = any `{37,40,50}` доступен в project aggregates; в current controller sample 720/720 payment-matched rows имеют один из этих кодов, а два unmatched live reports имеют 50. Это не подтверждает время/actor завершения. | Не подменять confirmed review event текущим selected `staff` или payment/approval state |
| Reports with current code 39 | DISTINCT operation/report key с code39 membership в текущем action snapshot | action_id/report_id, visit/project, status membership snapshot time | **PARTIAL:** один live report/action sample с `Есть претензия`, выбранным corrector и report date 2026-10-17 подтверждён; он отсутствует в checkpoint, sheet и date-filtered report registry. В 720 matched current rows code39=0; в 2 unmatched report rows live status50. | Coverage gap делает sheet numerator неполным; code39 не доказывает ошибку корректора |
| Reports that ever had 39 | DISTINCT report/business operation with retained code39 history | complete event history / guaranteed historical status set | **UNKNOWN / NOT COMPUTABLE:** current memberships have no transition timestamps or completeness guarantee | Не трактовать current membership как `EVER_HAD_STATUS` |
| Shopper remarks / returned to shopper | DISTINCT report/action with a validated return-to-shopper status/event | approved status ID/label, report/action key, event timestamp/reason | **BLOCKED:** code35=`Анкета отклонена` (7 matched current report rows), code38=`Вопрос координатору` (1); live samples confirm IDs and corrector link, но это не подтверждает shopper-return semantics | Нельзя переименовывать по похожему смыслу без SME/portal evidence |
| Repeat returns / rechecks | count of distinct validated return/recheck transitions | immutable event ID, previous/new status, changed_at, report_id | **NOT FEASIBLE** без transition log | Observed one visit with two distinct report/action records (old status15 / later status50) is not itself a repeated transition |
| Share of claims/returns | distinct operations with validated numerator / completed reviews in the same cohort | accepted status semantics + complete report coverage + completion event/date | **BLOCKED** | source-date mismatch and registry miss make a polished percentage false precision |
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
