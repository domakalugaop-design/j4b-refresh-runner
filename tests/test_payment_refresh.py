from decimal import Decimal

import pytest

from src.payment_refresh import (
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


def test_pair_publication_is_one_batch_and_only_targets_payment_tabs():
    previous = {"visits": [["project_id", "visit_id"], ["1", "old"]],
                "projects": [["project_id", "amount"], ["1", Decimal("2")]]}
    candidate = {"visits": [["project_id", "visit_id"], ["1", "new"]],
                 "projects": [["project_id", "amount"], ["1", Decimal("3")]]}
    live = {key: [row[:] for row in value] for key, value in previous.items()}
    writes = []

    def write_batch(requests):
        writes.append(requests)
        assert {r["updateCells"]["range"]["sheetId"] for r in requests} == {10, 20}
        live.update({key: [row[:] for row in value] for key, value in candidate.items()})

    result = publish_payment_pair(sheet_ids={"visits": 10, "projects": 20}, previous=previous,
                                  candidate=candidate, write_batch=write_batch,
                                  read_tab=lambda tab: live[tab])
    assert result["status"] == "PASS"
    assert len(writes) == 1 and len(writes[0]) == 2


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

    def write_batch(_requests):
        nonlocal writes
        writes += 1
        replacement = candidate if writes == 1 else previous
        live.update({key: [row[:] for row in value] for key, value in replacement.items()})

    def read_tab(tab):
        readbacks[tab] += 1
        if writes == 1 and readbacks[tab] == 1 and tab == mismatch_tab:
            return [["wrong schema"]]
        return live[tab]

    with pytest.raises(RuntimeError, match="previous publication restored"):
        publish_payment_pair(sheet_ids={"visits": 1, "projects": 2}, previous=previous,
                             candidate=candidate, write_batch=write_batch, read_tab=read_tab)
    assert live == previous
    assert writes == 2


def test_rollback_failure_is_visible():
    previous = {"visits": [["project_id"], ["1"]], "projects": [["project_id"], ["1"]]}
    candidate = {"visits": [["project_id"], ["2"]], "projects": [["project_id"], ["2"]]}
    writes = 0
    live = {key: [row[:] for row in value] for key, value in previous.items()}

    def write_batch(_requests):
        nonlocal writes
        writes += 1
        if writes == 1:
            live.update({key: [row[:] for row in value] for key, value in candidate.items()})
        else:
            raise RuntimeError("injected rollback failure")

    def read_tab(tab):
        return [["bad"]] if writes == 1 and tab == "visits" else live[tab]

    with pytest.raises(RuntimeError, match="rollback failed"):
        publish_payment_pair(sheet_ids={"visits": 1, "projects": 2}, previous=previous,
                             candidate=candidate, write_batch=write_batch, read_tab=read_tab)


def test_failed_readback_recovers_from_prewrite_backup():
    previous = {"visits": [["project_id"], ["1"]], "projects": [["project_id"], ["1"]]}
    candidate = {"visits": [["project_id"], ["2"]], "projects": [["project_id"], ["2"]]}
    live = {key: [row[:] for row in value] for key, value in previous.items()}
    writes = 0
    failed_read = False

    def write_batch(_requests):
        nonlocal writes
        writes += 1
        replacement = candidate if writes == 1 else previous
        live.update({key: [row[:] for row in value] for key, value in replacement.items()})

    def read_tab(tab):
        nonlocal failed_read
        if writes == 1 and not failed_read:
            failed_read = True
            raise RuntimeError("readback transport failure")
        return live[tab]

    with pytest.raises(RuntimeError, match="readback transport failure"):
        publish_payment_pair(sheet_ids={"visits": 1, "projects": 2}, previous=previous,
                             candidate=candidate, write_batch=write_batch, read_tab=read_tab)
    assert live == previous


def test_sheet_readback_money_is_normalized_to_decimal_and_ids_to_string():
    rows = normalize_payment_sheet_values("Выплаты по визитам", [
        ["project_id", "project_name", "client", "manager", "visit_id", "payment_assignment_count",
         "reward", "paid", "positive", "zero", "states", "status"],
        [123, "p", None, None, 456, 1.0, 500.25, 0.0, 0.0, 1.0, "[]", "COMPLETE"],
    ])
    assert rows[1][0] == "123" and rows[1][4] == "456"
    assert rows[1][6] == Decimal("500.25") and rows[1][7] == Decimal("0.0")
    assert rows[1][5] == 1 and rows[1][9] == 1
