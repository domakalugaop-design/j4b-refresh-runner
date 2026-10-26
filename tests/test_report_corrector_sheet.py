from __future__ import annotations

import json
import os
import tempfile
import unittest

from src.report_corrector_pipeline import SHEET_COLUMNS
from src.report_corrector_sheet import (
    PROTECTED_TABS,
    SheetTabMissing,
    SheetAdapterError,
    SheetsGateway,
    normalize_rows,
    plan,
    publish,
    write_backup,
)

SID = "sheet-test-id"
ROWS = [
    list(SHEET_COLUMNS),
    ["671045", "671045", "763369", "7986", "2026-10-01", "56770", "Иванов И.И.", "CURRENT",
     "2026-10-22T09:00:00+00:00"],
]


class FakeSheets:
    """Minimal in-memory stand-in for the Sheets REST API."""

    def __init__(self, tabs: dict[str, list[list[str]]] | None = None) -> None:
        self.tabs = {title: [] for title in PROTECTED_TABS}
        self.tabs.update(tabs or {})
        self.mutations: list[tuple[str, dict]] = []
        self.reads: list[str] = []
        self.fail_readback = False
        self._next_id = 900

    def get(self, url: str) -> dict:
        self.reads.append(url)
        if "?fields=" in url and "/values/" not in url:
            sheets = [
                {"properties": {"title": title, "sheetId": index}}
                for index, title in enumerate(self.tabs)
            ]
            return {"spreadsheetId": SID, "sheets": sheets}
        title = _title_from(url)
        if title not in self.tabs:
            raise SheetTabMissing(title)
        if title == "report_reviewers" and self.fail_readback:
            self.fail_readback = False
            return {"values": [["broken"]]}
        return {"values": self.tabs[title]}

    def mutate(self, url: str, body: dict) -> dict:
        self.mutations.append((url, body))
        request = body["requests"][0]
        if "addSheet" in request:
            title = request["addSheet"]["properties"]["title"]
            self.tabs[title] = []
            self._next_id += 1
            return {"replies": [{"addSheet": {"properties": {"sheetId": self._next_id}}}]}
        if "deleteSheet" in request:
            target = request["deleteSheet"]["sheetId"]
            for title, sheet_id in zip(self.tabs, range(len(self.tabs))):
                if sheet_id == target:
                    del self.tabs[title]
                    break
            return {"replies": [{}]}
        raise AssertionError("unexpected mutation")

    def put(self, url: str, values: list[list[str]]) -> dict:
        self.mutations.append((url, {"values": values}))
        self.tabs[_title_from(url)] = values
        return {"updatedRows": len(values)}


def _title_from(url: str) -> str:
    import urllib.parse

    encoded = url.split("/values/")[1].split("?")[0]
    return urllib.parse.unquote(encoded).split("!")[0].strip("'")


def gateway_for(fake: FakeSheets, *, allow_mutation: bool = True) -> SheetsGateway:
    return SheetsGateway(SID, get=fake.get, mutate=fake.mutate, put_values=fake.put,
                         allow_mutation=allow_mutation)


class NormalizationTests(unittest.TestCase):
    def test_pads_and_type_coerces(self) -> None:
        self.assertEqual(normalize_rows([["1", 2, None]]), [["1", "2", "", "", "", "", "", "", ""]])

    def test_trims_extra_cells(self) -> None:
        self.assertEqual(len(normalize_rows([["x"] * 30])[0]), 9)

    def test_none_and_empty_are_equal(self) -> None:
        self.assertEqual(normalize_rows(None), [])


class GatewayTests(unittest.TestCase):
    def test_metadata_lists_titles_and_ids(self) -> None:
        meta = gateway_for(FakeSheets()).metadata()
        self.assertEqual(set(meta.titles), set(PROTECTED_TABS))
        self.assertEqual(len(meta.sheet_ids), len(PROTECTED_TABS))

    def test_read_returns_none_for_absent_tab(self) -> None:
        self.assertIsNone(gateway_for(FakeSheets()).read("report_reviewers"))

    def test_read_returns_rows_for_absent_cells(self) -> None:
        self.assertEqual(gateway_for(FakeSheets({"report_reviewers": []})).read("report_reviewers"), [])

    def test_fingerprints_cover_all_protected_tabs(self) -> None:
        prints = gateway_for(FakeSheets()).fingerprints()
        self.assertEqual(set(prints), set(PROTECTED_TABS))

    def test_fingerprint_changes_when_protected_tab_changes(self) -> None:
        fake = FakeSheets()
        gateway = gateway_for(fake)
        before = gateway.fingerprints()
        fake.tabs["projects_current"] = [["changed"]]
        self.assertNotEqual(gateway.fingerprints()["projects_current"], before["projects_current"])

    def test_mutation_guard_blocks_writes(self) -> None:
        gateway = gateway_for(FakeSheets(), allow_mutation=False)
        with self.assertRaises(SheetAdapterError):
            gateway.create("report_reviewers")

    def test_delete_requires_known_tab(self) -> None:
        with self.assertRaises(SheetAdapterError):
            gateway_for(FakeSheets()).delete("absent")


class BackupTests(unittest.TestCase):
    def test_backup_is_private_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "nested")
            self.assertTrue(write_backup(target, {"tab": "report_reviewers", "existed": False}))
            path = os.path.join(target, "report_reviewers_backup.json")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["tab"], "report_reviewers")


class PlanTests(unittest.TestCase):
    def test_plan_reports_create_without_mutation(self) -> None:
        fake = FakeSheets()
        outcome = plan(ROWS, gateway_for(fake))
        self.assertEqual(outcome["action"], "CREATE")
        self.assertEqual(outcome["rows"], 1)
        self.assertEqual(fake.mutations, [])

    def test_plan_detects_existing_target(self) -> None:
        fake = FakeSheets({"report_reviewers": [list(SHEET_COLUMNS)]})
        self.assertEqual(plan(ROWS, gateway_for(fake))["action"], "REPLACE")

    def test_plan_flags_missing_protected_tab(self) -> None:
        fake = FakeSheets()
        del fake.tabs["project_types"]
        self.assertEqual(plan(ROWS, gateway_for(fake))["failure"], "MISSING_PROTECTED_TABS")


class PublishTests(unittest.TestCase):
    def test_creates_writes_and_verifies(self) -> None:
        fake = FakeSheets()
        with tempfile.TemporaryDirectory() as directory:
            outcome = publish(ROWS, gateway_for(fake), backup_dir=directory)
            self.assertEqual(outcome, "PUBLISHED")
            self.assertEqual(fake.tabs["report_reviewers"], ROWS)
            self.assertTrue(os.path.exists(os.path.join(directory, "report_reviewers_backup.json")))
            self.assertTrue(all(not values for title, values in fake.tabs.items()
                                if title != "report_reviewers"))

    def test_readback_mismatch_deletes_new_tab(self) -> None:
        fake = FakeSheets()
        fake.fail_readback = True
        with tempfile.TemporaryDirectory() as directory:
            from src.report_corrector_pipeline import CorrectorSheetPublicationError

            with self.assertRaises(CorrectorSheetPublicationError) as caught:
                publish(ROWS, gateway_for(fake), backup_dir=directory)
            self.assertEqual(caught.exception.rollback_status, "PASS")
            self.assertNotIn("report_reviewers", fake.tabs)

    def test_rejects_invalid_rows_before_touching_sheets(self) -> None:
        fake = FakeSheets()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                publish([list(SHEET_COLUMNS), ["x"]], gateway_for(fake), backup_dir=directory)
        self.assertEqual(fake.mutations, [])

    def test_protected_tab_change_aborts(self) -> None:
        fake = FakeSheets()

        original_put = fake.put

        def put_then_tamper(url: str, values: list[list[str]]) -> dict:
            result = original_put(url, values)
            fake.tabs["project_types"] = [["tampered"]]
            return result

        gateway = SheetsGateway(SID, get=fake.get, mutate=fake.mutate, put_values=put_then_tamper)
        with tempfile.TemporaryDirectory() as directory:
            from src.report_corrector_pipeline import CorrectorSheetPublicationError

            with self.assertRaises(CorrectorSheetPublicationError):
                publish(ROWS, gateway, backup_dir=directory)


if __name__ == "__main__":
    unittest.main()
