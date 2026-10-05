# Portal project edit and currency contract

Status: locally implemented, not live-production acceptance. The one
post-recovery control read returned an authenticated project edit response,
HTTP 200, selected currency ID `1`, label `рубль`, dictionary match `RUB` / `₽`.
No additional currency request is needed: project edit is already one of the
normal three project acquisition requests.

## Resource validation

HTTP status alone is insufficient. For `/proj/{id}/edit`, acquisition requires
an HTML document with one form containing the expected project controls
(`name`, `dt1`, `dt2`, `visits`, `client`, and `user`), canonical project name
agreement, and the requested project identity in the effective URL. Login
form, PHP error, access-denied markers, missing project form, missing required
fields, or identity mismatch are semantic failures. Such responses are never
converted into empty business fields.

The login POST itself does not establish an authenticated session. The first
normal project and edit GETs must pass their existing identity and structural
checks. No extra authentication probe is introduced.

## Currency fields

`select[name="currency"]` is parsed from the already acquired edit response.
The selected option value is retained as the raw nullable ID. `currency_id`,
`currency_code`, `currency_name`, and `currency_symbol` are project-level
columns appended after existing project data. No payment-tab schema is changed.

An authenticated project form with no currency control, no explicit selected
option, or an explicitly empty selection produces null currency values. The
parser does not infer the browser's first-option fallback and does not default
to RUB. Unknown IDs and mismatched option labels fail closed so dictionary
drift cannot silently corrupt the dimension.

The Portal-side model contract is `proj.currency` (nullable integer) pointing
to `currency.i`; the dictionary supplies `code`, `name`, and `sym`. The parser's
dictionary snapshot is the read-only server-side Portal dictionary observed
during qualification; HTML entities in `sym` are decoded for display:

| ID | Code | Name | Symbol |
| --- | --- | --- | --- |
| 1 | RUB | рубль | ₽ |
| 2 | ARM | Драм - армянский | ֏ |
| 3 | AZN | Манат - азейбарджаский | ₼ |
| 4 | GEL | Лари - грузинский | ₾ |
| 5 | BYN | Белорусский рубль | Б |
| 6 | KZT | Казахский тенге | ₸ |
| 7 | KGS | Киргизский сом | с |
| 8 | USDT | долар - крипта | $ |
| 9 | UZS | Узбекский сум | UZS |
| 10 | XOF | Западноафриканский франк | ₣ |

This is not a claim that the dictionary is immutable. If Portal adds or edits
an entry, the parser intentionally stops on an unqualified ID/label until the
mapping is verified and updated.

## Schema compatibility

The non-workflow `projects_current` candidate is 37 columns: the original 31
operational columns, the two Project Type columns, then the four currency
columns. Workflow-enabled production keeps its 54 existing columns in place
and appends currency, for 58 columns. Existing payment tabs are untouched.

Migration/readback code accepts the exact legacy 31- and 33-column layouts,
the current 37-column layout, and the exact 54-/58-column workflow layouts.
Other layouts fail closed. Rollback uses the original grid capacity, not just
the current populated header width.
