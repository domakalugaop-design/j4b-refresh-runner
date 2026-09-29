from decimal import Decimal

import pytest

from src.payment_refresh import (
    atomic_two_tab_update_requests,
    checkpoint_counts,
    load_checkpoint,
    new_checkpoint,
    pending_project_ids,
    record_result,
    replace_by_project,
    require_complete,
    save_checkpoint,
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
