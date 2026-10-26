from datetime import datetime, timedelta, timezone

import pytest

from src.report_corrector_pipeline import (
    SHEET_COLUMNS,
    CorrectorSheetPublicationError,
    ReportFetchFailure,
    ReportInventoryItem,
    acquire_batch,
    due_report_ids,
    load_checkpoint,
    merge_inventory,
    new_checkpoint,
    publish_new_sheet,
    render_sheet_rows,
)


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def item(report_id="501", action_id="801", visit_id="901", project_id="1001", report_date="2026-10-03"):
    return ReportInventoryItem(report_id, action_id, visit_id, project_id, report_date)


def page(staff_id="00042", fio="Synthetic Corrector"):
    return (
        '<html><select name="staff"><option value=""></option>'
        f'<option value="{staff_id}" selected>{fio}</option></select></html>'
    )


def test_inventory_is_cutoff_deduplicated_and_requires_distinct_ids():
    checkpoint = new_checkpoint([item(), item()])
    assert list(checkpoint["reports"]) == ["501"]
    assert checkpoint["reports"]["501"]["inventory"]["action_id"] == "801"
    with pytest.raises(ValueError, match="before configured"):
        new_checkpoint([item(report_date="2026-09-30")])
    with pytest.raises(ValueError, match="conflicting"):
        new_checkpoint([item(), item(action_id="802")])


def test_inventory_merge_adds_new_report_and_rejects_identity_drift():
    checkpoint = new_checkpoint([item()])
    assert merge_inventory(checkpoint, [item(), item(report_id="502", action_id="802")]) == 1
    with pytest.raises(ValueError, match="identity mapping changed"):
        merge_inventory(checkpoint, [item(report_id="501", visit_id="999")])


def test_get_cap_limits_backfill_and_rechecks_are_due_ordered():
    checkpoint = new_checkpoint([item("503", "803"), item("501", "801"), item("502", "802")])
    assert due_report_ids(checkpoint, NOW, 2) == ["501", "502"]
    with pytest.raises(ValueError, match="positive"):
        due_report_ids(checkpoint, NOW, 0)


def test_batch_persists_success_and_resume_state_and_uses_action_id(tmp_path):
    checkpoint = new_checkpoint([item()])
    path = tmp_path / "private" / "checkpoint.json"
    requested = []

    result = acquire_batch(
        checkpoint, lambda action_id: requested.append(action_id) or page(), NOW,
        max_gets=1, recheck_after=timedelta(days=7), checkpoint_path=path,
    )
    restored = load_checkpoint(path)
    assert result == {"attempted": 1, "passed": 1, "failed": 0}
    assert requested == ["801"]
    assert restored["reports"]["501"]["last_good"]["corrector_id"] == "00042"
    assert restored["reports"]["501"]["last_good"]["corrector_fio"] == "Synthetic Corrector"
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(render_sheet_rows(restored)[0]) == len(SHEET_COLUMNS)


def test_failed_recheck_retains_last_good_and_records_only_safe_category(tmp_path):
    checkpoint = new_checkpoint([item()])
    path = tmp_path / "checkpoint.json"
    acquire_batch(checkpoint, lambda _action: page(), NOW,
                  max_gets=1, recheck_after=timedelta(seconds=1), checkpoint_path=path)
    later = NOW + timedelta(seconds=2)
    result = acquire_batch(
        checkpoint,
        lambda _action: (_ for _ in ()).throw(ReportFetchFailure("HTTP_ERROR")),
        later, max_gets=1, recheck_after=timedelta(days=7), checkpoint_path=path,
    )
    row = render_sheet_rows(checkpoint)[1]
    assert result["failed"] == 1
    assert row[5:8] == ["00042", "Synthetic Corrector", "STALE_LAST_GOOD"]
    assert "private token" not in str(checkpoint)
    assert load_checkpoint(path)["reports"]["501"]["last_good"]["corrector_id"] == "00042"


def test_failed_new_report_is_not_filled_with_fake_corrector(tmp_path):
    checkpoint = new_checkpoint([item()])
    path = tmp_path / "checkpoint.json"
    acquire_batch(checkpoint, lambda _action: (_ for _ in ()).throw(TimeoutError("secret")), NOW,
                  max_gets=1, recheck_after=timedelta(days=7), checkpoint_path=path)
    row = render_sheet_rows(checkpoint)[1]
    assert row[5:9] == ["", "", "FETCH_FAILED_NO_HISTORY", ""]
    assert "secret" not in path.read_text()


def test_scheduler_respects_retry_backoff_and_periodic_recheck(tmp_path):
    checkpoint = new_checkpoint([item()])
    path = tmp_path / "checkpoint.json"
    acquire_batch(checkpoint, lambda _action: (_ for _ in ()).throw(ReportFetchFailure("TRANSPORT_ERROR")), NOW,
                  max_gets=1, recheck_after=timedelta(days=1), checkpoint_path=path)
    assert due_report_ids(checkpoint, NOW + timedelta(minutes=4), 1) == []
    assert due_report_ids(checkpoint, NOW + timedelta(minutes=5), 1) == ["501"]


def _publisher_state(initial=None, fail_once=False):
    state = {"rows": initial, "writes": 0, "non_target": {"five_existing_tabs": "unchanged"}, "backups": []}

    def write(rows):
        state["writes"] += 1
        state["rows"] = rows
        if fail_once and state["writes"] == 1:
            raise RuntimeError("secret write detail")

    return state, write


def test_new_sheet_publication_requires_verified_backup_and_readback():
    rows = [list(SHEET_COLUMNS), ["501", "801", "901", "1001", "2026-10-03", "", "", "PENDING", ""]]
    state, write = _publisher_state()
    publish_new_sheet(
        rows, existing_titles=set(), read_sheet=lambda: None,
        create_sheet=lambda: None, write_sheet=write, delete_sheet=lambda: state.update(rows=None),
        save_backup=lambda backup: state["backups"].append(backup) or True,
        readback=lambda: state["rows"], non_target_fingerprints=lambda: state["non_target"],
    )
    assert state["rows"] == rows
    assert state["backups"][0] == {"tab": "report_reviewers", "existed": False, "rows": None}


def test_publication_rejects_duplicate_report_rows_before_mutation():
    row = ["501", "801", "901", "1001", "2026-10-03", "", "", "PENDING", ""]
    with pytest.raises(ValueError, match="duplicated"):
        publish_new_sheet(
            [list(SHEET_COLUMNS), row, row], existing_titles=set(), read_sheet=lambda: None,
            create_sheet=lambda: pytest.fail("must validate before create"), write_sheet=lambda _rows: None,
            delete_sheet=lambda: None, save_backup=lambda _backup: True,
            readback=lambda: None, non_target_fingerprints=lambda: {},
        )


def test_publication_failure_rolls_back_only_new_sheet_and_sanitizes_error():
    rows = [list(SHEET_COLUMNS)]
    state, write = _publisher_state(fail_once=True)
    created = {"yes": False}

    def create():
        created["yes"] = True

    def delete():
        created["yes"] = False
        state["rows"] = None

    with pytest.raises(CorrectorSheetPublicationError) as error:
        publish_new_sheet(
            rows, existing_titles=set(), read_sheet=lambda: None,
            create_sheet=create, write_sheet=write, delete_sheet=delete,
            save_backup=lambda _backup: True,
            readback=lambda: state["rows"] if created["yes"] else None,
            non_target_fingerprints=lambda: state["non_target"],
        )
    assert error.value.rollback_status == "PASS"
    assert "secret write detail" not in str(error.value)
    assert state["rows"] is None


def test_existing_sheet_restore_and_unverified_backup_fail_closed():
    old = [list(SHEET_COLUMNS), ["501", "801", "901", "1001", "2026-10-03", "", "", "PENDING", ""]]
    rows = [list(SHEET_COLUMNS)]
    state, write = _publisher_state(initial=old, fail_once=True)
    with pytest.raises(CorrectorSheetPublicationError) as error:
        publish_new_sheet(
            rows, existing_titles={"report_reviewers"}, read_sheet=lambda: old,
            create_sheet=lambda: pytest.fail("must not create existing tab"), write_sheet=write,
            delete_sheet=lambda: pytest.fail("must not delete existing tab"),
            save_backup=lambda _backup: True, readback=lambda: state["rows"],
            non_target_fingerprints=lambda: state["non_target"],
        )
    assert error.value.rollback_status == "PASS"
    assert state["rows"] == old

    with pytest.raises(RuntimeError, match="backup was not verified"):
        publish_new_sheet(
            rows, existing_titles=set(), read_sheet=lambda: None,
            create_sheet=lambda: pytest.fail("write blocked before creation"), write_sheet=lambda _rows: None,
            delete_sheet=lambda: None, save_backup=lambda _backup: False,
            readback=lambda: None, non_target_fingerprints=lambda: {},
        )


def test_failed_rollback_is_reported_as_failure():
    rows = [list(SHEET_COLUMNS)]
    state, write = _publisher_state(fail_once=True)
    with pytest.raises(CorrectorSheetPublicationError) as error:
        publish_new_sheet(
            rows, existing_titles=set(), read_sheet=lambda: None,
            create_sheet=lambda: None, write_sheet=write, delete_sheet=lambda: (_ for _ in ()).throw(RuntimeError()),
            save_backup=lambda _backup: True,
            readback=lambda: state["rows"], non_target_fingerprints=lambda: state["non_target"],
        )
    assert error.value.rollback_status == "FAIL"
