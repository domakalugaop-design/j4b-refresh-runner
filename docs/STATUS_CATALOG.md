# Каталог статусов и состояний портала

**PORTAL-001.3 — discovery, 2026-10-26**

**Статус документа:** подтверждённый каталог workflow-кодов, но не полный словарь report transitions.

## Вывод

- **CONFIRMED:** код `39` — workflow membership с названием **«Есть претензия»** в `projects_current.workflow_state_39_visits` и заголовке `Статусы проектов!P1` (`Есть претензия [39]`). Ограниченная HTTPS-проверка нашла action/report detail с membership 39, `report_id == action_id`, одним visit/project link и выбранным контролёром. Это подтверждает связь статуса с реальным report/action в исследованном случае, но не подтверждает, что label точно означает «Претензия клиента» или что претензия доказывает ошибку контролёра.
- **CONFIRMED:** код `35` имеет label «Анкета отклонена», код `38` — «Вопрос координатору». Для одного report/action каждого кода ограниченный HTTPS read-only search подтвердил membership и совпадение `action_id` со строкой `report_reviewers`. Это не доказывает возврат на замечания ТП.
- **PARTIAL:** текущий словарь workflow кодов подтверждён, но значения в разных вкладках относятся к различным population/grain. Сумма `workflow_state_39_visits` по nonblank rows двух project-level вкладок — 32; в `Выплаты по визитам.workflow_state_codes` код 39 встретился в 50 composite visit keys. Workflow поля заполнены только для 617/8,088 projects; остальные blanks не означают zero. Эти итоги не совпадают и не должны взаимозаменяться.
- **UNKNOWN:** отдельный report-level `status_id`/current field, точная семантика возврата тайному покупателю и complete report-transition history.
- **CONFIRMED limitation:** в ограниченной выборке обнаружен report с датой 2026-10-17, выбранным контролёром и workflow 39, присутствующий в `/proj/{project_id}/report`, но отсутствующий в `report_reviewers`, checkpoint и дневном global registry read (`POST /report/`, dt1=dt2=`17.10.2026`; 144 других unique report IDs returned). Значит 722-строчный corrector sheet не полон по отношению к порталу.

## Подтверждённые workflow-коды

Источники: `src/workflow_analytics.py::WORKFLOW_STATES`; `src/parsers.py::parse_action_table`; `src/acquisition.py::action_memberships`; текущие `projects_current` и `Статусы проектов` (readback 2026-10-26). `projects_current` хранит project-level counters `workflow_state_{code}_visits`; `Статусы проектов` хранит те же counters под заголовками с именами и ID. `Выплаты по визитам` хранит JSON-подобный `workflow_state_codes` на payment-mapped visit key, не на report/action key.

| ID | Точное имя | Текущий Google Sheets источник | Назначение, подтверждённое только названием | HTTPS/API | PostgreSQL (not queried; not required) | Current/history |
|---:|---|---|---|---|---|---|
| 0 | Отправлено приглашение | `projects_current.workflow_state_0_visits`; `Статусы проектов!F` | приглашение отправлено | **PARTIAL:** общий action-search endpoint используется; code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership, не история |
| 5 | Приглашение отклонено | `projects_current.workflow_state_5_visits`; `Статусы проектов!G` | приглашение отклонено | code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership |
| 10 | Приглашение принято | `projects_current.workflow_state_10_visits`; `Статусы проектов!H` | приглашение принято | code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership |
| 15 | Отклонено менеджером | `projects_current.workflow_state_15_visits`; `Статусы проектов!I` | отклонено менеджером | **CONFIRMED sample:** отдельный старый action/report на visit | NOT QUERIED (not required) | snapshot membership |
| 20 | Подтверждено менеджером | `projects_current.workflow_state_20_visits`; `Статусы проектов!J` | подтверждено менеджером | code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership |
| 25 | Провалено пользователем | `projects_current.workflow_state_25_visits`; `Статусы проектов!K` | визит/действие провалено пользователем | code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership |
| 30 | Выполнено пользователем | `projects_current.workflow_state_30_visits`; `Статусы проектов!L` | выполнено пользователем | code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership |
| 35 | Анкета отклонена | `projects_current.workflow_state_35_visits`; `Статусы проектов!M` | анкета отклонена | **CONFIRMED sample:** report/action membership; `action_id == report_id` | NOT QUERIED (not required) | snapshot membership |
| 37 | Анкета проверена | `projects_current.workflow_state_37_visits`; `Статусы проектов!N` | анкета проверена | code-specific live sample не делался | NOT QUERIED (not required) | snapshot membership |
| 38 | Вопрос координатору | `projects_current.workflow_state_38_visits`; `Статусы проектов!O` | вопрос координатору | **CONFIRMED sample:** report/action membership; `action_id == report_id` | NOT QUERIED (not required) | snapshot membership |
| 39 | Есть претензия | `projects_current.workflow_state_39_visits`; `Статусы проектов!P` | наличие претензии (точная бизнес-семантика требует уточнения) | то же; report/action sample code 39 подтверждён | **UNKNOWN/BLOCKED** | snapshot membership |
| 40 | Анкета утверждена | `projects_current.workflow_state_40_visits`; `Статусы проектов!Q` | анкета утверждена | то же; report/action sample code 40 подтверждён | **UNKNOWN/BLOCKED** | snapshot membership |
| 45 | В оплате отказано | `projects_current.workflow_state_45_visits`; `Статусы проектов!R` | отказ в оплате | то же | **UNKNOWN/BLOCKED** | snapshot |
| 50 | Оплачено | `projects_current.workflow_state_50_visits`; `Статусы проектов!S` | оплата отмечена | то же; code 50 подтверждён в report/action samples | **UNKNOWN/BLOCKED** | snapshot |

`POST /action` — существующий поиск/фильтр по проекту и workflow-кодам; код refresh применяет тот же endpoint только как read/search, не как бизнес-запись. Code-specific live samples были для кодов 15, 35, 38, 39, 40 и 50; для остальных кодов подтверждены mapping в коде/Sheets, но не отдельная live-строка. `GET /api/report/{id}` в прежней qualification возвращал пустое тело в 8 проверках; это не заменяет HTML/action detail.

PostgreSQL в этой фазе **не использовался и не требовался**: основной доказательный путь — Google Sheets + существующий refresh-код + ограниченный portal read-only search. Значения `UNKNOWN/BLOCKED` в столбце PostgreSQL означают, что этот источник отдельно не квалифицировался, а не что задача заблокирована.

## Обязательные разграничения

1. **Статус проекта ≠ состояние визита/action ≠ report event.** `Статусы проектов` хранит project-level sums of visit predicates, не report rows.
2. На одном live action row статусы 35 и 40 присутствовали одновременно; на другом — 38 и 40. Значит эти значения не образуют взаимоисключающий single `current_status_id`. Список кодов может быть membership set; порядок/время переходов не доказаны.
3. В ограниченной выборке один visit имел два разных action/report ID: старый report/action с кодом 15 (дата 2026-09-24) и отдельный report/action с кодом 50 (дата 2026-10-01). Это отдельные связанные с визитом records; нельзя считать их одним status transition без дополнительной модели версий.
4. Код `39` подтверждён как **«Есть претензия»**, не как персональная ошибка корректора и не как доказательство причинности. В ограниченном sample у report был выбран контролёр, но этот report отсутствовал в report registry/checkpoint/sheet; точный общий claim-rate по `report_reviewers` поэтому неполон.
5. Код `38` не равен замечанию ТП по подтверждённой семантике; код `35` не равен возврату на замечания без бизнес-определения.
6. В `report_reviewers.source_status` значение `CURRENT` означает результат/свежесть чтения поля корректора сборщиком, а не завершение проверки и не workflow status.

## Источники и открытые вопросы

- `src/workflow_analytics.py` — набор кодов и названий.
- `src/parsers.py` — извлечение цифровых кодов состояния из action-link rows; произвольные числа не считаются статусами.
- `src/acquisition.py` — membership содержит project_id, visit_id, workflow_state_code и action_id; дата перехода/actor отсутствуют.
- `docs/REPORT_CORRECTORS.md` — рабочая модель `/action/{id}` для поля корректора и файловой даты; это не справочник report statuses.
- В `JustForBusy` `portal/data/DATA_MODEL.md` (main, прочитан 2026-10-26) полный каталог visit/report statuses и история названы критическими неизвестными.

**Нужно подтвердить отдельно:** report-level `status_id` и label; код/событие замечаний ТП; точный смысл «Есть претензия»; состояние завершённой проверки; источник и временную семантику каждого статуса.
