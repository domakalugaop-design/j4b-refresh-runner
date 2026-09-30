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
)


def _apply_payment_requests(live, requests, sheet_ids):
    tab_by_id = {value: key for key, value in sheet_ids.items()}
    for request in requests:
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
