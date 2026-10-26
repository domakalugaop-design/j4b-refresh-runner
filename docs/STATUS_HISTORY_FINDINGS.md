# История изменения статусов отчётов — результаты поиска

**PORTAL-001.3 — discovery, 2026-10-26**

## Краткий результат

**STATUS_HISTORY_AVAILABLE=NOT IN GOOGLE SHEETS / NOT CONFIRMED IN PORTAL.** В Google Sheets и refresh-коде нет event rows с previous/new status, timestamp перехода и actor. Ограниченные HTML/action reads показали multi-code membership на одной строке, но не журнал переходов. На одном status39 detail sample структурной таблицы с историей, датой изменения и actor не обнаружено. Полное отсутствие истории в других portal screens/endpoints не доказано.

Это не доказывает, что audit log нигде не существует. Это означает, что доступные и проверенные источники не подтверждают его наличие.

## Что было проверено

- `src/workflow_analytics.py`, `src/parsers.py`, `src/acquisition.py`: workflow memberships строятся из текущего action response. Ключ — `(project_id, visit_id, workflow_state_code)`; дата перехода, старое состояние и actor в этой структуре отсутствуют.
- `Статусы проектов`: текущий материализованный project-level лист хранит агрегаты числа визитов по workflow-кодам, а не последовательность событий.
- `report_reviewers`: содержит текущие поля отчёта/корректора и `last_successful_fetch`. Последнее — дата/время успешного чтения страницы сборщиком, не дата проверки и не дата смены статуса.
- `Выплаты по визитам`: `workflow_state_codes` — JSON-подобный набор текущих кодов на payment-mapped `(project_id, visit_id)`; это не события и не полный реестр визитов/отчётов.
- `JustForBusy/portal/data/DATA_MODEL.md` и `portal/api/API_INVENTORY.md`: история изменений и timestamps не подтверждены; `GET /api/report/{id}` ранее давал пустое тело в 8 проверках.
- PostgreSQL не запрашивался: единая Google-таблица и refresh-код были обязательным первым источником, и на этом этапе этого достаточно, чтобы установить отсутствие event history в материализованной модели. Схема БД не нужна для текущего вывода.
- Ограниченные authenticated read-only `POST /action` фильтры подтвердили, что текущая action row может содержать одновременно коды 35+40 или 38+40. Это подтверждает множество workflow memberships, но не устанавливает, являются ли они одновременно действующими predicates или накопленной последовательностью. В одном visit были найдены две action/report записи: старая с code 15 и датой 2026-09-24 и отдельная с code 50 и датой 2026-10-01. Это отдельные report/action records, не доказательство перехода одного report между статусами.
- Ограниченный status39 sample: report date 2026-10-17, detail route связал action/report/visit/project и показывал выбранного контролёра; sample отсутствовал в checkpoint и `report_reviewers`. Однодневный registry read с документированным DMY форматом (`17.10.2026`) вернул 144 other report IDs, но не этот report. Это подтверждённая неполнота registry/collector path для исследованного report, а не доказательство отсутствия других пропусков.

## Поля истории

| Требование | Доступность | Основание |
|---|---|---|
| PREVIOUS_STATUS | **UNKNOWN** | Ни одна проверенная таблица/структура не хранит прежний report status |
| NEW_STATUS | **PARTIAL** | Текущие workflow membership codes читаются как snapshot; одно action row может нести несколько кодов, но нет доказанного порядка или статуса именно перехода |
| changed_at | **UNKNOWN** | `last_successful_fetch` — время чтения, не перехода |
| changed_by | **UNKNOWN** | В проверенной модели membership actor отсутствует |
| несколько одинаковых переходов | **UNKNOWN** | Нет event IDs/sequence или журнала, позволяющего различить повторения |
| повторные возвраты | **UNKNOWN** | В текущем snapshot повторные переходы не различимы |
| история за прошлые периоды | **UNKNOWN** | Ни API, ни DB history не подтверждены |

## Разделение временных понятий

- **CURRENT_STATUS:** потенциально может быть восстановлен из актуального report-level поля, если оно будет найдено и проверено. Сейчас поле в доступном наборе отсутствует.
- **EVER_HAD_STATUS:** не выводится из текущего snapshot; для этого нужна полная история или гарантированно append-only событие.
- **STATUS_TRANSITION_COUNT:** не рассчитывается без записей событий и устойчивого event key.

Нельзя заменить `EVER_HAD_STATUS` текущим состоянием или количеством строк в агрегате. Несколько кодов в одном action response означают несколько memberships; они не доказывают `EVER_HAD_STATUS`, последовательность, количество возвратов или время перехода.

## Ограничения доступа в этом аудите

- Live portal reads выполнялись в ограниченном scope через штатный auth и project/date/status-filter POST `/action` (read/search), GET `/action/{id}` и один date-filter POST `/report/`. Бизнес-данные не менялись; массового backfill не было.
- PostgreSQL query не выполнялся, поскольку он не требовался по требованиям этой фазы. Не использовались неизвестные credentials/hostnames.
- Исторический результат из документов не выдавался за текущую live-проверку.

## Что подтвердит/опровергнет наличие истории

Для полного закрытия вопроса нужен разрешённый read-only source, который хранит transition event (Portal audit route или PostgreSQL analytics, если такая view существует). Нужны event key, report/action/visit key, old/new status ID, event timestamp и actor. Пока такого источника нет, `EVER_HAD_STATUS` и `STATUS_TRANSITION_COUNT` не считать; current workflow membership публиковать только с `as_of_at` и с явной оговоркой о неполноте.
