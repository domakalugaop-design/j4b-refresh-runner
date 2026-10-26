# История изменения статусов отчётов — результаты поиска

**PORTAL-001.3 — discovery, 2026-10-26**

## Краткий результат

**STATUS_HISTORY_AVAILABLE=UNKNOWN (BLOCKED for verification).** В доступном коде, текущих Google Sheets и прочитанных документах не найдено подтверждённого журнала переходов report statuses. Документация портала прямо перечисляет timestamps и историю переходов как неизвестные области. PostgreSQL-схема и read-only query connection недоступны в текущей сессии.

Это не доказывает, что audit log нигде не существует. Это означает, что доступные и проверенные источники не подтверждают его наличие.

## Что было проверено

- `src/workflow_analytics.py`, `src/parsers.py`, `src/acquisition.py`: workflow memberships строятся из текущего action response. Ключ — `(project_id, visit_id, workflow_state_code)`; дата перехода, старое состояние и actor в этой структуре отсутствуют.
- `Статусы проектов`: текущий материализованный project-level лист хранит агрегаты числа визитов по workflow-кодам, а не последовательность событий.
- `report_reviewers`: содержит текущие поля отчёта/корректора и `last_successful_fetch`. Последнее — дата/время успешного чтения страницы сборщиком, не дата проверки и не дата смены статуса.
- `JustForBusy/portal/data/DATA_MODEL.md` и `portal/api/API_INVENTORY.md`: история изменений и timestamps не подтверждены; `GET /api/report/{id}` ранее давал пустое тело в 8 проверках.
- OpenWork capability discovery не показал PostgreSQL/SQL read-only capability. В репозитории runner нет схемы PostgreSQL или query path. Известные материалы портала называют DB schema/history неизвестными; утверждение о существующем read-only доступе в рамках этой сессии проверить не удалось.

## Поля истории

| Требование | Доступность | Основание |
|---|---|---|
| PREVIOUS_STATUS | **UNKNOWN** | Ни одна проверенная таблица/структура не хранит прежний report status |
| NEW_STATUS | **PARTIAL** | Текущие workflow membership codes читаются как snapshot; это не доказанное событие перехода и не report-level status |
| changed_at | **UNKNOWN** | `last_successful_fetch` — время чтения, не перехода |
| changed_by | **UNKNOWN** | В проверенной модели membership actor отсутствует |
| несколько одинаковых переходов | **UNKNOWN** | Нет event IDs/sequence или журнала, позволяющего различить повторения |
| повторные возвраты | **UNKNOWN** | В текущем snapshot повторные переходы не различимы |
| история за прошлые периоды | **UNKNOWN** | Ни API, ни DB history не подтверждены |

## Разделение временных понятий

- **CURRENT_STATUS:** потенциально может быть восстановлен из актуального report-level поля, если оно будет найдено и проверено. Сейчас поле в доступном наборе отсутствует.
- **EVER_HAD_STATUS:** не выводится из текущего snapshot; для этого нужна полная история или гарантированно append-only событие.
- **STATUS_TRANSITION_COUNT:** не рассчитывается без записей событий и устойчивого event key.

Нельзя заменить `EVER_HAD_STATUS` текущим состоянием или количеством строк в агрегате. Несколько состояний одного визита в workflow-таблице означают membership нескольких predicates, а не несколько переходов во времени.

## Ограничения доступа в этом аудите

- Live portal reads не выполнялись: известные API/web auth bootstrap использует POST, а инструкция запрещала все POST к порталу. В этой сессии не обнаружен уже аутентифицированный GET-only канал.
- PostgreSQL query не выполнялся: отсутствует обнаруженный read-only capability/connection и в доступном repo нет подтверждённого PG endpoint/schema. Не использовались неизвестные credentials/hostnames.
- Исторический результат из документов не выдавался за текущую live-проверку.

## Что подтвердит/опровергнет наличие истории

Только после предоставления/обнаружения разрешённого read-only PG-коннектора: schema-only запрос к каталогу таблиц и колонок по report/status/event/audit и затем ограниченная выборка по нескольким ID. Для каждого события нужны event key, report/action/visit foreign key, old/new status ID, event timestamp и actor. Если такого лога нет, история остаётся **UNKNOWN/NOT STORED** до подтверждения владельцем системы; текущий статус можно публиковать отдельно с `as_of_at`.
