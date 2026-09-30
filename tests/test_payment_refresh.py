from decimal import Decimal
from urllib.error import URLError

import pytest

from src.payment_refresh import (
    PAYMENT_WRITE_CHUNK_MAX_BYTES,
    atomic_two_tab_update_requests,
    checkpoint_counts,
    deterministic_batches,
    load_checkpoint,
    new_checkpoint,
    pending_project_ids,
    record_result,
    replace_by_project,
    require_complete,
    save_checkpoint,
    publish_payment_pair,
    normalize_payment_sheet_values,
    diagnose_payment_readback,
    _payment_readback_comparison,
)


def _apply_payment_requests(live, requests, sheet_ids, grid_counts=None):
    tab_by_id = {value: key for key, value in sheet_ids.items()}
    for request in requests:
        if "appendDimension" in request:
            dimension = request["appendDimension"]
            tab = tab_by_id[dimension["sheetId"]]
            if grid_counts is not None and dimension["dimension"] == "ROWS":
                grid_counts[tab] += dimension["length"]
            continue
        if "deleteDimension" in request:
            dimension = request["deleteDimension"]["range"]
            tab = tab_by_id[dimension["sheetId"]]
            if dimension["dimension"] == "ROWS":
                del live[tab][dimension["startIndex"]:dimension["endIndex"]]
                if grid_counts is not None:
                    grid_counts[tab] -= dimension["endIndex"] - dimension["startIndex"]
            continue
        update = request["updateCells"]
        cell_range = update["range"]
        tab = tab_by_id[cell_range["sheetId"]]
        rows = live[tab]
        width = cell_range["endColumnIndex"]
        end = cell_range["endRowIndex"]
        while len(rows) < end:
            rows.append([])
        for offset, row in enumerate(update["rows"]):
            cells = []
            for cell in row.get("values", []):
                entered = cell.get("userEnteredValue", {})
                cells.append(entered.get("stringValue", entered.get("numberValue", entered.get("boolValue"))))
            cells += [None] * max(0, width - len(cells))
            rows[cell_range["startRowIndex"] + offset] = cells[:width]


def test_zero_row_is_success_and_removes_stale_rows_from_both_layers():
    old = {"visits": [["project_id", "visit_id"], ["10", "v1"], ["11", "v2"]],
           "projects": [["project_id", "amount"], ["10", Decimal("4")], ["11", Decimal("5")]]}
    incoming = {"visits": [["project_id", "visit_id"]],
                "projects": [["project_id", "amount"], ["10", Decimal("0")]]}
    merged = replace_by_project(old, incoming, ["10"])
    assert merged["visits"] == [["project_id", "visit_id"], ["11", "v2"]]
    assert merged["projects"] == [["project_id", "amount"], ["11", Decimal("5")], ["10", Decimal("0")]]


def test_checkpoint_resume_skips_accepted_results_and_retries_failures(tmp_path):
    state = new_checkpoint([1, 2, 3], mode="initial", year=2026)
    record_result(state, 1, "SUCCESS_WITH_ROWS", [{"my_id": "a", "amount": Decimal("12.30")}])
    record_result(state, 2, "SUCCESS_ZERO_ROWS")
    record_result(state, 3, "FAILED_TRANSPORT")
    saved = save_checkpoint(tmp_path / "private" / "state.json", state)
    restored = load_checkpoint(saved["path"], saved["sha256"])
    assert pending_project_ids(restored) == ["3"]
    assert checkpoint_counts(restored)["SUCCESS_ZERO_ROWS"] == 1
    assert restored["projects"]["1"]["rows"][0]["amount"] == Decimal("12.30")
    with pytest.raises(ValueError, match="immutable"):
        record_result(restored, 1, "FAILED_PARSE")
    assert not (tmp_path / "private" / "state.json").stat().st_mode & 0o077


def test_incomplete_checkpoint_cannot_publish():
    state = new_checkpoint([1, 2], mode="initial", year=2026)
    record_result(state, 1, "SUCCESS_ZERO_ROWS")
    with pytest.raises(ValueError, match="incomplete"):
        require_complete(state)


def test_complete_checkpoint_allows_zero_row_terminal_success():
    state = new_checkpoint([1], mode="initial", year=2026)
    record_result(state, 1, "SUCCESS_ZERO_ROWS")
    require_complete(state)


def test_atomic_pair_is_one_batch_with_stale_tail_clear_and_exact_decimal():
    previous = {"visits": [["project_id", "amount"], ["1", Decimal("3.10")], ["1", Decimal("2")]],
                "projects": [["project_id", "amount"], ["1", Decimal("5.10")]]}
    candidate = {"visits": [["project_id", "amount"], ["1", Decimal("5.10")]],
                 "projects": [["project_id", "amount"], ["1", Decimal("5.10")]]}
    requests = atomic_two_tab_update_requests({"visits": 8, "projects": 9}, previous, candidate)
    assert len(requests) == 2  # Caller sends these together in one API batchUpdate.
    assert requests[0]["updateCells"]["range"]["endRowIndex"] == 3
    assert requests[0]["updateCells"]["rows"][2]["values"][1] == {}
    assert requests[0]["updateCells"]["rows"][1]["values"][1]["userEnteredValue"]["numberValue"] == Decimal("5.10")


def test_replace_rejects_unselected_incoming_project():
    old = {"visits": [["project_id"], ["1"]], "projects": [["project_id"], ["1"]]}
    incoming = {"visits": [["project_id"], ["2"]], "projects": [["project_id"], ["2"]]}
    with pytest.raises(ValueError, match="outside selected"):
        replace_by_project(old, incoming, ["1"])


def test_exact_2026_scope_batch_boundaries_are_deterministic():
    ids = [str(i) for i in range(1, 1781)]
    batches = deterministic_batches(ids, 25)
    assert len(batches) == 72
    assert [len(batch) for batch in batches[:2]] == [25, 25]
    assert len(batches[-1]) == 5
    assert [item for batch in batches for item in batch] == ids


def test_pair_publication_chunks_each_payment_tab_and_only_targets_those_tabs():
    previous = {"visits": [["project_id", "visit_id"], ["1", "old"]],
                "projects": [["project_id", "amount"], ["1", Decimal("2")]]}
    candidate = {"visits": [["project_id", "visit_id"], ["1", "new"]],
                 "projects": [["project_id", "amount"], ["1", Decimal("3")]]}
    live = {key: [row[:] for row in value] for key, value in previous.items()}
    writes = []
    sheet_ids = {"visits": 10, "projects": 20}

    def write_batch(requests):
        writes.append(requests)
        assert len(requests) == 1
        assert {r["updateCells"]["range"]["sheetId"] for r in requests}.issubset({10, 20})
        _apply_payment_requests(live, requests, sheet_ids)

    result = publish_payment_pair(sheet_ids=sheet_ids, previous=previous,
                                  candidate=candidate, write_batch=write_batch,
                                  read_tab=lambda tab: live[tab])
    assert result["status"] == "PASS"
    assert len(writes) == 2
    assert result["chunks_written"] == 2
    assert live == candidate


@pytest.mark.parametrize("fail_at", ["first_tab_write", "second_tab_write"])
def test_atomic_pair_write_failure_leaves_both_previous_tabs(fail_at):
    previous = {"visits": [["project_id"], ["1"]], "projects": [["project_id"], ["1"]]}
    candidate = {"visits": [["project_id"], ["2"]], "projects": [["project_id"], ["2"]]}
    live = {key: [row[:] for row in value] for key, value in previous.items()}

    def write_batch(requests):
        # A Google spreadsheets.batchUpdate is one transaction; failure at
        # either request is injected before any tab is changed.
        raise RuntimeError(fail_at)

    with pytest.raises(RuntimeError, match=fail_at):
        publish_payment_pair(sheet_ids={"visits": 1, "projects": 2}, previous=previous,
                             candidate=candidate, write_batch=write_batch,
                             read_tab=lambda tab: live[tab])
    assert live == previous


@pytest.mark.parametrize("mismatch_tab", ["visits", "projects"])
def test_readback_mismatch_rolls_back_both_payment_tabs(mismatch_tab):
    previous = {"visits": [["project_id", "visit_id"], ["1", "old"]],
                "projects": [["project_id", "amount"], ["1", 2]]}
    candidate = {"visits": [["project_id", "visit_id"], ["1", "new"]],
                 "projects": [["project_id", "amount"], ["1", 3]]}
    live = {key: [row[:] for row in value] for key, value in previous.items()}
    writes = 0
    readbacks = {"visits": 0, "projects": 0}
    sheet_ids = {"visits": 1, "projects": 2}

    def write_batch(requests):
        nonlocal writes
        writes += 1
        _apply_payment_requests(live, requests, sheet_ids)

    corrupt_once = True
    def read_tab(tab):
        nonlocal corrupt_once
        readbacks[tab] += 1
        if corrupt_once and writes == 2 and tab == mismatch_tab:
            corrupt_once = False
            return [["wrong schema"]]
        return live[tab]

    with pytest.raises(RuntimeError, match="previous publication restored"):
        publish_payment_pair(sheet_ids=sheet_ids, previous=previous,
                             candidate=candidate, write_batch=write_batch, read_tab=read_tab)
    assert live == previous
    assert writes == 4


def test_rollback_failure_is_visible():
    previous = {"visits": [["project_id"], ["1"]], "projects": [["project_id"], ["1"]]}
    candidate = {"visits": [["project_id"], ["2"]], "projects": [["project_id"], ["2"]]}
    writes = 0
    live = {key: [row[:] for row in value] for key, value in previous.items()}
    sheet_ids = {"visits": 1, "projects": 2}

    def write_batch(requests):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise RuntimeError("injected rollback failure")
        _apply_payment_requests(live, requests, sheet_ids)

    corrupt_once = True
    def read_tab(tab):
        nonlocal corrupt_once
        if corrupt_once and writes == 2 and tab == "visits":
            corrupt_once = False
            return [["bad"]]
        return live[tab]

    with pytest.raises(RuntimeError, match="payment readback mismatch and rollback failed"):
        publish_payment_pair(sheet_ids=sheet_ids, previous=previous,
                             candidate=candidate, write_batch=write_batch, read_tab=read_tab)


def test_failed_readback_recovers_from_prewrite_backup():
    previous = {"visits": [["project_id"], ["1"]], "projects": [["project_id"], ["1"]]}
    candidate = {"visits": [["project_id"], ["2"]], "projects": [["project_id"], ["2"]]}
    live = {key: [row[:] for row in value] for key, value in previous.items()}
    writes = 0
    failed_read = False
    sheet_ids = {"visits": 1, "projects": 2}

    def write_batch(requests):
        nonlocal writes
        writes += 1
        _apply_payment_requests(live, requests, sheet_ids)

    def read_tab(tab):
        nonlocal failed_read
        if writes == 2 and not failed_read:
            failed_read = True
            raise RuntimeError("readback transport failure")
        return live[tab]

    with pytest.raises(RuntimeError, match="readback transport failure"):
        publish_payment_pair(sheet_ids=sheet_ids, previous=previous,
                             candidate=candidate, write_batch=write_batch, read_tab=read_tab)
    assert live == previous


def test_82530_row_visit_payload_is_chunked_under_serialized_size_limit():
    from src.payment_refresh import _payment_write_chunks
    from src.payment_materialization import serialize_sheet_payload

    cell = {"userEnteredValue": {"stringValue": "x"}}
    rows = [{"values": [cell] * 12}] * 82_530
    request = {"updateCells": {
        "range": {"sheetId": 10, "startRowIndex": 0, "endRowIndex": len(rows),
                  "startColumnIndex": 0, "endColumnIndex": 12},
        "rows": rows, "fields": "userEnteredValue",
    }}
    chunks = _payment_write_chunks([request], PAYMENT_WRITE_CHUNK_MAX_BYTES)
    assert len(chunks) > 25
    assert all(len(serialize_sheet_payload({"requests": [chunk]}).encode()) <= PAYMENT_WRITE_CHUNK_MAX_BYTES
               for chunk in chunks)
    assert sum(len(chunk["updateCells"]["rows"]) for chunk in chunks) == 82_530
    assert chunks[0]["updateCells"]["range"]["startRowIndex"] == 0
    assert chunks[-1]["updateCells"]["range"]["endRowIndex"] == 82_530
    assert all(a["updateCells"]["range"]["endRowIndex"] == b["updateCells"]["range"]["startRowIndex"]
               for a, b in zip(chunks, chunks[1:]))


@pytest.mark.parametrize("target_count", [5, 1])
def test_chunked_publication_larger_and_smaller_than_old_sheet_clears_stale_tail(target_count):
    previous = {"visits": [["id", "amount"], ["old1", 1], ["old2", 2], ["old3", 3]],
                "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id", "amount"], *[[str(i), i] for i in range(target_count)]],
                 "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 10, "projects": 20}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}

    def writer(requests):
        _apply_payment_requests(live, requests, sheet_ids)

    result = publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                                  write_batch=writer, read_tab=lambda tab: live[tab], max_request_bytes=400)
    assert result["status"] == "PASS"
    from src.payment_refresh import _normalized_readback
    assert all(_normalized_readback(live[tab]) == _normalized_readback(candidate[tab]) for tab in sheet_ids)


def test_first_chunk_atomically_expands_only_target_payment_tab_grid():
    previous = {"visits": [["id"], ["old"]], "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id"], ["new1"], ["new2"], ["new3"]],
                 "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 10, "projects": 20}
    grid_counts = {"visits": 2, "projects": 2}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}

    def writer(requests):
        _apply_payment_requests(live, requests, sheet_ids, grid_counts)

    result = publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                                  write_batch=writer, read_tab=lambda tab: live[tab],
                                  grid_row_counts=grid_counts)
    assert result["status"] == "PASS"
    assert grid_counts == {"visits": 4, "projects": 2}
    assert live == candidate


def test_readback_mismatch_rolls_back_grid_rows_added_for_publication():
    previous = {"visits": [["id"], ["old"]], "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id"], ["new1"], ["new2"], ["new3"]],
                 "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 10, "projects": 20}
    grid_counts = {"visits": 2, "projects": 2}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}

    def writer(requests):
        _apply_payment_requests(live, requests, sheet_ids, grid_counts)

    corrupted = False
    def read_tab(tab):
        nonlocal corrupted
        if not corrupted and live["visits"] == candidate["visits"] and tab == "visits":
            corrupted = True
            return [["unexpected"]]
        return live[tab]

    with pytest.raises(RuntimeError, match="previous publication restored"):
        publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                              write_batch=writer, read_tab=read_tab,
                              grid_row_counts={"visits": 2, "projects": 2})
    assert live == previous
    assert grid_counts == {"visits": 2, "projects": 2}


def test_timeout_before_apply_verifies_range_then_retries():
    previous = {"visits": [["id"], ["old"]], "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id"], ["new"]], "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 1, "projects": 2}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}
    calls = 0

    def writer(requests):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise URLError("timeout before apply")
        _apply_payment_requests(live, requests, sheet_ids)

    result = publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                                  write_batch=writer, read_tab=lambda tab: live[tab])
    assert result["status"] == "PASS"
    assert calls == 3
    assert live == candidate


def test_timeout_after_apply_is_confirmed_without_replaying_ambiguous_chunk():
    previous = {"visits": [["id"], ["old"]], "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id"], ["new"]], "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 1, "projects": 2}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}
    calls = 0

    def writer(requests):
        nonlocal calls
        calls += 1
        _apply_payment_requests(live, requests, sheet_ids)
        if calls == 1:
            raise URLError("timeout after apply")

    result = publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                                  write_batch=writer, read_tab=lambda tab: live[tab])
    assert result["status"] == "PASS"
    assert result["ambiguous_write_response"] is True
    assert calls == 2
    assert live == candidate


def test_retryable_chunk_failure_exhaustion_rolls_back_and_fails_closed():
    previous = {"visits": [["id"], ["old"]], "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id"], ["new"]], "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 1, "projects": 2}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}
    calls = 0

    def writer(_requests):
        nonlocal calls
        calls += 1
        raise URLError("persistent timeout before apply")

    with pytest.raises(URLError, match="persistent timeout"):
        publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                             write_batch=writer, read_tab=lambda tab: live[tab], max_attempts=2)
    assert calls == 2
    assert live == previous


@pytest.mark.parametrize("fail_call", [2, 4, 6])
def test_middle_final_or_project_tab_chunk_failure_restores_both_tabs(fail_call):
    previous = {"visits": [["id"], ["old1"], ["old2"]], "projects": [["id"], ["old"]]}
    candidate = {"visits": [["id"], ["new1"], ["new2"], ["new3"]], "projects": [["id"], ["new"]]}
    sheet_ids = {"visits": 1, "projects": 2}
    live = {key: [row[:] for row in rows] for key, rows in previous.items()}
    calls = 0

    def writer(requests):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise RuntimeError("injected chunk failure")
        _apply_payment_requests(live, requests, sheet_ids)

    with pytest.raises(RuntimeError, match="injected chunk failure"):
        publish_payment_pair(sheet_ids=sheet_ids, previous=previous, candidate=candidate,
                             write_batch=writer, read_tab=lambda tab: live[tab], max_request_bytes=250)
    from src.payment_refresh import _normalized_readback
    assert all(_normalized_readback(live[tab]) == _normalized_readback(previous[tab]) for tab in sheet_ids)


def test_sheet_readback_money_is_normalized_to_decimal_and_ids_to_string():
    rows = normalize_payment_sheet_values("Выплаты по визитам", [
        ["project_id", "project_name", "client", "manager", "visit_id", "payment_assignment_count",
         "reward", "paid", "positive", "zero", "states", "status"],
        [123, "p", None, None, 456, 1.0, 500.25, 0.0, 0.0, 1.0, "[]", "COMPLETE"],
    ])
    assert rows[1][0] == "123" and rows[1][4] == "456"
    assert rows[1][6] == Decimal("500.25") and rows[1][7] == Decimal("0.0")
    assert rows[1][5] == 1 and rows[1][9] == 1


def test_readback_diagnostics_preserve_existing_equality_for_numeric_types():
    assert diagnose_payment_readback("t", [["metric"], [3]], [["metric"], [3]])["matches_existing_contract"]
    assert diagnose_payment_readback("t", [["metric"], [3]], [["metric"], [3.0]])["matches_existing_contract"]
    mismatch = diagnose_payment_readback("t", [["metric"], [3]], [["metric"], ["3"]])
    assert not mismatch["matches_existing_contract"]
    assert mismatch["first_mismatch"]["class"] == "STRING_VS_NUMBER"


def test_readback_diagnostics_report_trailing_blank_cells_without_waiving_other_differences():
    result = diagnose_payment_readback("t", [["id", "note"], ["x", ""]], [["id", "note"], ["x"]])
    assert result["matches_existing_contract"]
    assert result["mismatch_counts_by_class"] == {"MISSING_TRAILING_EMPTY": 1}
    assert result["total_mismatched_cells"] == 0


def test_readback_diagnostics_keep_null_and_empty_distinct_but_ignore_trailing_empty_columns():
    mismatch = diagnose_payment_readback("t", [["id", "note", "tail"], ["x", None, "kept"]], [["id", "note", "tail"], ["x", "", "kept"]])
    assert not mismatch["matches_existing_contract"]
    assert mismatch["first_mismatch"]["class"] == "NULL_VS_EMPTY"
    padded = diagnose_payment_readback("t", [["id", "note", ""], ["x", "y", ""]], [["id", "note"], ["x", "y"]])
    assert padded["matches_existing_contract"]
    assert "MISSING_TRAILING_EMPTY" in padded["mismatch_counts_by_class"]


def test_readback_diagnostics_distinguish_leading_and_interior_empty_cells():
    same = diagnose_payment_readback("t", [["a", "b", "c"], ["", "", "z"]], [["a", "b", "c"], ["", "", "z"]])
    assert same["matches_existing_contract"]
    changed = diagnose_payment_readback("t", [["a", "b", "c"], ["", "x", "z"]], [["a", "b", "c"], ["", "", "z"]])
    assert changed["first_mismatch"]["a1"] == "B2"
    assert changed["first_mismatch"]["class"] == "TEXT_VALUE"


def test_readback_diagnostics_numeric_precision_unicode_and_a1_coordinates():
    decimal = diagnose_payment_readback("t", [["amount"], [Decimal("10.50")]], [["amount"], [10.5]])
    assert decimal["matches_existing_contract"]
    large = diagnose_payment_readback("t", [["count"], [9007199254740993]], [["count"], [float(9007199254740993)]])
    assert not large["matches_existing_contract"]
    assert large["first_mismatch"]["class"] == "NUMERIC_VALUE"
    unicode_same = diagnose_payment_readback("t", [["name"], ["Клиент 東京"]], [["name"], ["Клиент 東京"]])
    assert unicode_same["matches_existing_contract"]
    unicode_diff = diagnose_payment_readback("t", [["name"], ["Клиент 東京"]], [["name"], ["Клиент 京都"]])
    assert unicode_diff["first_mismatch"]["a1"] == "A2"
    assert unicode_diff["first_mismatch"]["class"] == "TEXT_VALUE"


def test_readback_diagnostics_summarize_row_column_and_systematic_mismatches():
    row_count = diagnose_payment_readback("t", [["a", "b"], [1, 2], [3, 4]], [["a", "b"], [1, 2]])
    assert row_count["mismatch_counts_by_class"]["ROW_COUNT"] == 1
    assert row_count["mismatched_rows"] == 1
    short_row = diagnose_payment_readback("t", [["a", "b"], [1, 2]], [["a", "b"], [1]])
    assert short_row["first_mismatch"]["a1"] == "B2"
    assert short_row["first_mismatch"]["class"] == "OTHER"
    column_count = diagnose_payment_readback("t", [["a", "b"], [1, 2]], [["a"], [1]])
    assert column_count["mismatch_counts_by_class"]["COLUMN_COUNT"] == 1
    assert column_count["mismatch_counts_by_class"]["OTHER"] == 1
    systematic = diagnose_payment_readback("t", [["code"], [100], [200], [300]], [["code"], ["100"], ["200"], ["300"]])
    assert systematic["total_mismatched_cells"] == 3
    assert systematic["mismatched_rows"] == 3
    assert systematic["mismatch_counts_by_column"] == {"A: code": 3}
    assert systematic["mismatch_counts_by_class"] == {"STRING_VS_NUMBER": 3}
    assert systematic["first_mismatch"]["row"] == 2


def test_readback_diagnostics_single_cell_and_row_shift():
    single = diagnose_payment_readback("t", [["id", "label"], ["1", "ok"]], [["id", "label"], ["1", "bad"]])
    assert single["total_mismatched_cells"] == 1
    assert single["mismatch_counts_by_class"] == {"TEXT_VALUE": 1}
    shifted = diagnose_payment_readback("t", [["id"], ["a"], ["b"]], [["id"], ["b"], ["a"]])
    assert shifted["mismatch_counts_by_class"]["ROW_SHIFT"] == 2


def test_readback_diagnostics_never_expose_cell_pii():
    import json

    pii = "person@example.test / +7-999-111-22-33"
    result = diagnose_payment_readback("t", [["client"], [pii]], [["client"], ["different private value"]])
    serialized = json.dumps(result, ensure_ascii=False)
    assert pii not in serialized
    assert "person@example" not in serialized
    assert result["first_mismatch"]["expected"]["type"] == "str"
    assert result["first_mismatch"]["expected"]["length"] == len(pii)
    assert len(result["first_mismatch"]["expected"]["sha256"]) == 64


@pytest.mark.parametrize(("tab", "header", "other_header"), [
    ("Выплаты по визитам", "Вознаграждение за визит", "Оплачено по данным портала"),
    ("Выплаты по визитам", "Оплачено по данным портала", "Вознаграждение за визит"),
    ("Выплаты по проектам", "Вознаграждение за визиты", "Оплачено по данным портала"),
    ("Выплаты по проектам", "Оплачено по данным портала", "Вознаграждение за визиты"),
])
def test_qualified_nullable_money_none_to_blank_is_equal_by_schema_field_name(tab, header, other_header):
    headers = ["identifier", header, "middle", other_header, "status"]
    expected = [headers, ["x", None, "present", None, "OK"]]
    actual = [headers, ["x", "", "present", "", "OK"]]
    matches, equivalences = _payment_readback_comparison(tab, expected, actual)
    assert matches and equivalences == 2
    diagnostic = diagnose_payment_readback(tab, expected, actual)
    assert diagnostic["matches_existing_contract"]
    assert diagnostic["raw_null_vs_empty_equivalences"] == 2


@pytest.mark.parametrize("actual_value", [0, "0"])
def test_qualified_nullable_money_none_does_not_equal_zero_or_numeric_text(actual_value):
    headers = ["project_id", "Оплачено по данным портала", "note"]
    matches, equivalences = _payment_readback_comparison(
        "Выплаты по визитам", [headers, ["x", None, "keep"]], [headers, ["x", actual_value, "keep"]]
    )
    assert not matches and equivalences == 0


def test_nullable_money_rule_is_symmetric_only_for_qualified_fields():
    headers = ["client", "Оплачено по данным портала", "note"]
    expected_empty_text = [headers, [None, Decimal("4"), "keep"]]
    actual_empty_text = [headers, ["", Decimal("4"), "keep"]]
    assert not _payment_readback_comparison("Выплаты по визитам", expected_empty_text, actual_empty_text)[0]

    expected_empty_string = [headers, ["x", "", "keep"]]
    actual_null = [headers, ["x", None, "keep"]]
    matches, equivalences = _payment_readback_comparison("Выплаты по визитам", expected_empty_string, actual_null)
    assert matches and equivalences == 1

    # The same empty/null pair is not interchangeable in an unqualified field.
    text_headers = ["client", "note"]
    assert not _payment_readback_comparison(
        "Выплаты по визитам", [text_headers, [None, "keep"]], [text_headers, ["", "keep"]]
    )[0]


def test_nullable_money_blank_and_null_are_equivalent_in_chunk_rows():
    headers = ["project_id", "Оплачено по данным портала", "note"]
    matches, equivalences = _payment_readback_comparison(
        "Выплаты по визитам", [["x", "", "keep"]], [["x", None, "keep"]], headers=headers
    )
    assert matches and equivalences == 1


def test_payment_readback_preserves_numeric_and_real_amount_mismatch_semantics():
    headers = ["project_id", "Вознаграждение за визиты", "note"]
    assert _payment_readback_comparison("Выплаты по проектам", [headers, ["x", 1, "keep"]], [headers, ["x", 1, "keep"]])[0]
    assert _payment_readback_comparison("Выплаты по проектам", [headers, ["x", 1, "keep"]], [headers, ["x", 1.0, "keep"]])[0]
    assert not _payment_readback_comparison("Выплаты по проектам", [headers, ["x", 1, "keep"]], [headers, ["x", "1", "keep"]])[0]
    amount_change = diagnose_payment_readback("Выплаты по проектам", [headers, ["x", Decimal("100"), "keep"]], [headers, ["x", Decimal("101"), "keep"]])
    assert not amount_change["matches_existing_contract"]
    assert amount_change["first_mismatch"]["class"] == "NUMERIC_VALUE"


@pytest.mark.parametrize("expected,actual", [
    ([["project_id", "amount"], ["x", 1]], [["project_id", "amount"]]),
    ([["project_id", "amount"]], [["project_id", "amount"], ["x", 1]]),
    ([["project_id", "amount"], ["x", 1], ["y", 2]], [["project_id", "amount"], ["y", 2], ["x", 1]]),
])
def test_qualified_payment_comparison_keeps_missing_extra_and_shifted_rows_as_mismatches(expected, actual):
    assert not _payment_readback_comparison("Выплаты по проектам", expected, actual)[0]
