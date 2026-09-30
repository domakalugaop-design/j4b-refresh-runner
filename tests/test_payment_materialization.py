"""Synthetic tests for payment grain, aggregates, invariants, and row payloads."""

from decimal import Decimal
import json
import unittest

from src.payment_materialization import (
    ASSIGNMENT_COLUMNS,
    PROJECT_PUBLICATION_COLUMNS,
    VISIT_PUBLICATION_COLUMNS,
    build_publication_payloads,
    materialize_payment_data,
    publication_dry_run,
    serialize_sheet_payload,
)


def payment(my_id, *, reward="500", trans="0", comp="0", bonus="0", paid="250"):
    return {
        "my_id": str(my_id) if my_id is not None else None,
        "visit_reward_raw": reward,
        "transport_expense_raw": trans,
        "expense_compensation_raw": comp,
        "bonus_penalty_raw": bonus,
        "portal_paid_amount_raw": paid,
    }


def workflow(my_id, visit_id, state, *, project_id="900"):
    return {"project_id": project_id, "action_id": str(my_id), "visit_id": str(visit_id), "workflow_state_code": state}


def materialize(payments, workflows):
    return materialize_payment_data(
        "900",
        {"project_name": "Synthetic project", "client": "Synthetic client", "primary_manager": "Synthetic manager"},
        payments,
        workflows,
    )


class PaymentMaterializationTests(unittest.TestCase):
    def test_assignment_materialization_preserves_raw_decimal_and_context_without_pii(self):
        result = materialize([payment(1)], [workflow(1, 101, 50)])
        row = result["assignment_rows"][0]
        self.assertEqual(row["project_id"], "900")
        self.assertEqual(row["manager"], "Synthetic manager")
        self.assertEqual(row["portal_paid_amount_raw"], "250")
        self.assertEqual(row["portal_paid_amount"], Decimal("250"))
        self.assertEqual(row["payment_join_status"], "MATCHED")
        self.assertEqual(set(row).intersection({"ФИО ТП", "Логин", "Адрес", "phone", "email"}), set())
        self.assertTrue(set(ASSIGNMENT_COLUMNS).issubset(row))

    def test_saved_workflow_metadata_envelopes_are_unwrapped(self):
        result = materialize_payment_data(
            "900",
            {
                "project_name": {"state": "VALUE_PRESENT", "value": "Synthetic project"},
                "client": {"state": "VALUE_PRESENT", "selected_count": 1, "selected_values": ["Synthetic client"], "value": "Synthetic client"},
                "primary_manager": {"state": "VALUE_PRESENT", "selected_count": 1, "selected_values": ["Synthetic manager"], "value": "Synthetic manager"},
            },
            [payment(1)], [workflow(1, 101, 50)],
        )
        row = result["assignment_rows"][0]
        self.assertEqual((row["project_name"], row["client"], row["manager"]),
                         ("Synthetic project", "Synthetic client", "Synthetic manager"))

    def test_multiple_assignments_visit_reward_dedup_and_workflow_overlap(self):
        result = materialize(
            [payment(1, paid="0"), payment(2, paid="-10"), payment(3, paid="40")],
            [workflow(1, 101, 50), workflow(1, 101, 15), workflow(2, 101, 40), workflow(3, 101, 50)],
        )
        visit = result["visit_payment_aggregate"][0]
        project = result["project_payment_aggregate"][0]
        self.assertEqual(len(result["assignment_rows"]), 3)
        self.assertEqual(visit["payment_assignment_count"], 3)
        self.assertEqual(visit["visit_reward"], Decimal("500"))
        self.assertEqual(visit["portal_paid_total"], Decimal("30"))
        self.assertEqual(visit["positive_paid_assignment_count"], 1)
        self.assertEqual(visit["zero_paid_assignment_count"], 1)
        self.assertEqual(json.loads(visit["workflow_state_codes"]), [15, 40, 50])
        self.assertEqual(project["total_visit_reward"], Decimal("500"))
        self.assertEqual(project["total_portal_paid"], Decimal("30"))
        self.assertEqual(result["diagnostics"]["multi_assignment_visits"], 1)
        self.assertEqual(result["diagnostics"]["max_assignments_per_visit"], 3)
        self.assertEqual(result["diagnostics"]["negative_payment_rows"], 1)
        self.assertEqual(result["diagnostics"]["zero_payment_rows"], 1)
        self.assertEqual(
            result["invariants"],
            {key: True for key in (
                "A", "B", "C", "D1_PROJECT_COMPLETENESS", "D2_VISIT_PROVENANCE",
                "D2_RECONCILIATION", "E", "F", "G", "H", "I",
            )},
        )

    def test_conflicting_visit_rewards_are_flagged_not_arbitrarily_selected(self):
        result = materialize(
            [payment(1, reward="500"), payment(2, reward="600")],
            [workflow(1, 101, 50), workflow(2, 101, 50)],
        )
        visit = result["visit_payment_aggregate"][0]
        project = result["project_payment_aggregate"][0]
        self.assertIsNone(visit["visit_reward"])
        self.assertTrue(visit["visit_reward_conflict"])
        self.assertEqual(visit["visit_reward_conflict_code"], "VISIT_REWARD_CONFLICT")
        self.assertEqual(result["diagnostics"]["visit_reward_conflicts"], 1)
        self.assertEqual(project["payment_data_status"], "REVIEW")

    def test_zero_is_kept_and_negative_is_not_clamped(self):
        result = materialize(
            [payment(1, paid="0"), payment(2, paid="-5")],
            [workflow(1, 101, 50), workflow(2, 102, 50)],
        )
        self.assertEqual(len(result["assignment_rows"]), 2)
        self.assertEqual(result["diagnostics"]["zero_payment_rows"], 1)
        self.assertEqual(result["diagnostics"]["negative_payment_rows"], 1)
        self.assertEqual(result["project_payment_aggregate"][0]["total_portal_paid"], Decimal("-5"))
        self.assertTrue(result["invariants"]["H"])
        self.assertTrue(result["invariants"]["I"])

    def test_blank_and_malformed_amounts_remain_distinct(self):
        result = materialize(
            [payment(1, paid=""), payment(2, paid="bad")],
            [workflow(1, 101, 50), workflow(2, 102, 50)],
        )
        self.assertEqual(result["diagnostics"]["numeric_rows_incomplete"], 1)
        self.assertEqual(result["diagnostics"]["numeric_rows_review"], 1)
        self.assertEqual(result["assignment_rows"][0]["payment_numeric_status"], "INCOMPLETE")
        self.assertEqual(result["assignment_rows"][1]["payment_numeric_status"], "REVIEW")
        self.assertIsNone(result["project_payment_aggregate"][0]["total_portal_paid"])

    def test_unmapped_assignment_stays_in_project_aggregate_not_visit_aggregate(self):
        result = materialize(
            [payment(1, paid="250"), payment(2, paid="125")],
            [workflow(1, 101, 50)],
        )
        project = result["project_payment_aggregate"][0]
        self.assertEqual(len(result["assignment_rows"]), 2)
        self.assertEqual(project["payment_assignment_count"], 2)
        self.assertEqual(project["total_portal_paid"], Decimal("375"))
        self.assertEqual(project["payment_rows_unmatched"], 1)
        self.assertEqual(project["payment_data_status"], "INCOMPLETE")
        self.assertEqual(len(result["visit_payment_aggregate"]), 1)
        self.assertEqual(result["visit_payment_aggregate"][0]["payment_assignment_count"], 1)
        self.assertEqual(result["diagnostics"]["visit_mapped_assignments"], 1)
        self.assertEqual(result["diagnostics"]["unmatched_visit_mapping_assignments"], 1)
        self.assertTrue(result["invariants"]["D1_PROJECT_COMPLETENESS"])
        self.assertTrue(result["invariants"]["D2_VISIT_PROVENANCE"])
        self.assertTrue(result["invariants"]["D2_RECONCILIATION"])

    def test_zero_mapped_project_remains_a_project_row_without_synthetic_visit(self):
        result = materialize([payment(1), payment(2)], [])
        self.assertEqual(len(result["assignment_rows"]), 2)
        self.assertEqual(result["visit_payment_aggregate"], [])
        self.assertEqual(len(result["project_payment_aggregate"]), 1)
        project = result["project_payment_aggregate"][0]
        self.assertEqual(project["payment_assignment_count"], 2)
        self.assertEqual(project["payment_rows_unmatched"], 2)
        self.assertEqual(project["payment_data_status"], "INCOMPLETE")
        self.assertEqual(result["diagnostics"]["visit_mapped_assignments"], 0)
        self.assertEqual(result["diagnostics"]["unmatched_visit_mapping_assignments"], 2)
        self.assertTrue(result["invariants"]["D1_PROJECT_COMPLETENESS"])
        self.assertTrue(result["invariants"]["D2_VISIT_PROVENANCE"])
        self.assertTrue(result["invariants"]["D2_RECONCILIATION"])

    def test_matched_action_without_visit_id_is_unmapped_for_visit_provenance(self):
        workflow_without_visit = {"project_id": "900", "action_id": "1", "visit_id": None,
                                  "workflow_state_code": 50}
        result = materialize([payment(1)], [workflow_without_visit])
        self.assertEqual(result["assignment_rows"][0]["payment_join_status"], "MATCHED")
        self.assertEqual(result["visit_payment_aggregate"], [])
        project = result["project_payment_aggregate"][0]
        self.assertEqual(project["payment_rows_unmatched"], 1)
        self.assertEqual(project["payment_data_status"], "INCOMPLETE")
        self.assertEqual(result["diagnostics"]["unmatched_visit_mapping_assignments"], 1)
        self.assertTrue(result["invariants"]["D2_RECONCILIATION"])

    def test_duplicate_key_and_my_id_multi_visit_conflicts_fail_acceptance(self):
        duplicate = materialize([payment(1), payment(1)], [workflow(1, 101, 50)])
        self.assertEqual(duplicate["diagnostics"]["assignment_duplicate_primary_keys"], 1)
        self.assertFalse(duplicate["invariants"]["B"])
        conflict = materialize(
            [payment(1)], [workflow(1, 101, 50), workflow(1, 102, 15)]
        )
        self.assertEqual(conflict["diagnostics"]["my_id_multi_visit_conflicts"], 1)
        self.assertEqual(conflict["assignment_rows"][0]["payment_join_status"], "PAYMENT_ONLY")

    def test_publication_contract_types_serialization_and_pii_exclusion(self):
        result = materialize(
            [payment(1, paid="25.10"), payment(2, paid="0")],
            [workflow(1, 101, 50), workflow(1, 101, 15), workflow(2, 101, 50)],
        )
        payloads = build_publication_payloads(result)
        self.assertEqual(payloads["Выплаты по визитам"][0], list(VISIT_PUBLICATION_COLUMNS))
        self.assertEqual(payloads["Выплаты по проектам"][0], list(PROJECT_PUBLICATION_COLUMNS))
        self.assertEqual(len(payloads["Выплаты по визитам"][0]), 12)
        self.assertEqual(len(payloads["Выплаты по проектам"][0]), 15)
        self.assertEqual(len(payloads["Выплаты по визитам"]) - 1, 1)
        self.assertEqual(len(payloads["Выплаты по проектам"]) - 1, 1)
        self.assertNotIn("my_id", payloads["Выплаты по визитам"][0])
        self.assertNotIn("ФИО ТП", payloads["Выплаты по визитам"][0])
        visit_info = publication_dry_run(result)["Выплаты по визитам"]
        self.assertTrue(visit_info["pass"])
        serialized = serialize_sheet_payload([[Decimal("12.340"), None, "Портал"]])
        self.assertEqual(serialized, '[[12.340,null,"Портал"]]')

    def test_visit_publication_key_is_project_and_visit(self):
        left = materialize_payment_data("900", {"project_name": "A"}, [payment(1)], [workflow(1, 101, 50, project_id="900")])
        right = materialize_payment_data("901", {"project_name": "B"}, [payment(2)], [workflow(2, 101, 50, project_id="901")])
        combined = {
            "assignment_rows": left["assignment_rows"] + right["assignment_rows"],
            "visit_payment_aggregate": left["visit_payment_aggregate"] + right["visit_payment_aggregate"],
            "project_payment_aggregate": left["project_payment_aggregate"] + right["project_payment_aggregate"],
        }
        payloads = build_publication_payloads(combined)
        self.assertEqual(len(payloads["Выплаты по визитам"]), 3)


if __name__ == "__main__":
    unittest.main()
