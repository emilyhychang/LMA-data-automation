import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.pipeline import (
    analysis_csv_bytes,
    category_period_comparisons,
    csv_bytes,
    first_seen_customer_rows,
    job_status_analysis,
    load_settings,
    run_import,
    run_uploaded_import,
    weekly_opportunity_ytd,
)


class PipelineTest(unittest.TestCase):

    def test_local_and_uploaded_imports_share_the_same_results(self):
        settings = load_settings(PROJECT / "config/settings.json")
        settings["required_columns"] = ["Customer ID", "Created Date"]
        master_rows = [{
            "Customer ID": "existing",
            "Created Date": "09/01/2026",
            "Invoice Business Unit": "HVAC Service",
            "Job Type": "Repair",
            "Job Status": "Scheduled",
            "Total": "100",
        }]
        source_rows = [
            {
                **master_rows[0],
                "Job Status": "Completed",
                "Total": "250",
            },
            {
                "Customer ID": "new",
                "Created Date": "09/02/2026",
                "Invoice Business Unit": "Plumbing",
                "Job Type": "Drain",
                "Job Status": "Scheduled",
                "Total": "300",
            },
        ]
        master_contents = csv_bytes(master_rows, settings["master_columns"])
        source_contents = csv_bytes(source_rows, settings["master_columns"])

        uploaded = run_uploaded_import(source_contents, master_contents, settings)
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            master_path = temp_path / "master.csv"
            source_path = temp_path / "source.csv"
            master_path.write_bytes(master_contents)
            source_path.write_bytes(source_contents)
            local = run_import(
                source_path, master_path, settings, temp_path / "output"
            )

            self.assertEqual(
                (
                    local.cleaned_rows,
                    local.appended_rows,
                    local.updated_rows,
                    local.skipped_duplicate_rows,
                    local.new_customers,
                    local.period_start,
                    local.period_end,
                ),
                (
                    uploaded.cleaned_rows,
                    uploaded.appended_rows,
                    uploaded.updated_rows,
                    uploaded.skipped_duplicate_rows,
                    uploaded.new_customers,
                    uploaded.period_start,
                    uploaded.period_end,
                ),
            )
            self.assertEqual(master_path.read_bytes(), uploaded.master_csv)
            self.assertEqual(
                (temp_path / "output/analysis.csv").read_bytes(),
                uploaded.analysis_csv,
            )

    def test_first_seen_and_reimport_are_idempotent(self):
        settings = load_settings(PROJECT / "config/settings.json")
        # Legacy fixtures intentionally contain only the minimum lead fields.
        settings["required_columns"] = ["Customer ID", "Created Date"]
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            period_1 = temp_path / "period_1.csv"
            period_2 = temp_path / "period_2.csv"
            period_1.write_bytes(csv_bytes([
                {"Customer ID": "1001", "Created Date": "09/01/2026"},
                {"Customer ID": "1001", "Created Date": "09/02/2026"},
                {"Customer ID": "1002", "Created Date": "09/03/2026"},
            ], settings["master_columns"]))
            period_2.write_bytes(csv_bytes([
                {"Customer ID": "1002", "Created Date": "09/04/2026"},
                {"Customer ID": "1003", "Created Date": "09/05/2026"},
                {"Customer ID": "1003", "Created Date": "09/06/2026"},
            ], settings["master_columns"]))

            first = run_import(period_1, temp_path / "master.csv", settings, temp_path / "out1")
            self.assertEqual((first.appended_rows, first.new_customers), (3, 2))
            self.assertTrue((temp_path / "out1/analysis.csv").is_file())
            self.assertFalse((temp_path / "out1/period_comparison.csv").exists())
            with (temp_path / "out1/period_1_cleaned.csv").open() as handle:
                cleaned = list(csv.DictReader(handle))

            self.assertEqual(
                list(cleaned[0]),
                settings["master_columns"],
            )
            second = run_import(period_2, temp_path / "master.csv", settings, temp_path / "out2")
            self.assertEqual((second.appended_rows, second.new_customers), (3, 1))
            repeat = run_import(period_2, temp_path / "master.csv", settings, temp_path / "out3")
            self.assertEqual((repeat.appended_rows, repeat.new_customers), (0, 0))
            repeat_first = run_import(period_1, temp_path / "master.csv", settings, temp_path / "out4")
            self.assertEqual((repeat_first.appended_rows, repeat_first.new_customers), (0, 0))
            with (temp_path / "out2/new_customers.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Customer ID"] for row in rows], ["1003"])

    def test_period_comparisons_are_split_into_hvac_and_plumbing(self):
        settings = load_settings(PROJECT / "config/settings.json")
        master_rows = [
            {
                "Customer ID": "existing-across-categories",
                "Created Date": "09/08/2025",
                "Invoice Business Unit": "Plumbing",
                "Total": "50",
            },
            {
                "Customer ID": "existing-across-categories",
                "Created Date": "09/08/2026",
                "Invoice Business Unit": "HVAC Service",
                "Total": "50",
            },
            {
                "Customer ID": "hvac-1",
                "Created Date": "09/08/2026",
                "Invoice Business Unit": "HVAC Service",
                "Total": "100",
            },
            {
                "Customer ID": "plumbing-1",
                "Created Date": "09/09/2026",
                "Invoice Business Unit": "Plumbing",
                "Total": "200",
            },
            {
                "Customer ID": "other-1",
                "Created Date": "09/10/2026",
                "Invoice Business Unit": "Electrical",
                "Total": "300",
            },
            {
                "Customer ID": "dual-category",
                "Created Date": "09/09/2026",
                "Invoice Business Unit": "Plumbing Service",
                "Total": "100",
            },
            {
                "Customer ID": "dual-category",
                "Created Date": "09/10/2026",
                "Invoice Business Unit": "HVAC Service",
                "Total": "100",
            },
        ]

        comparisons = category_period_comparisons(
            master_rows, date(2026, 9, 7), date(2026, 9, 20), settings
        )

        self.assertEqual(set(comparisons), {"HVAC", "Plumbing"})
        self.assertEqual(comparisons["HVAC"][0]["Records"], 3)
        self.assertEqual(comparisons["Plumbing"][0]["Records"], 2)
        self.assertEqual(comparisons["HVAC"][0]["New Customers"], 1)
        self.assertEqual(comparisons["HVAC"][0]["Unique Customers"], 3)
        self.assertEqual(comparisons["Plumbing"][0]["Unique Customers"], 2)
        self.assertEqual(comparisons["HVAC"][0]["Revenue"], "250")
        self.assertEqual(comparisons["Plumbing"][0]["Revenue"], "300")

    def test_overlapping_comparison_windows_include_all_matching_jobs(self):
        settings = load_settings(PROJECT / "config/settings.json")
        rows = [
            {"Customer ID": str(i), "Created Date": day,
             "Invoice Business Unit": "HVAC Service", "Total": "100"}
            for i, day in enumerate([
                "2022-06-01", "2023-06-01", "2024-06-01", "2026-06-01"
            ])
        ]
        comparisons = category_period_comparisons(
            rows, date(2023, 1, 1), date(2026, 10, 4), settings
        )["HVAC"]
        self.assertEqual([row["Records"] for row in comparisons], [3, 1, 3])
        self.assertEqual([row["Revenue"] for row in comparisons], ["300", "100", "300"])

    def test_existing_job_group_is_replaced_by_latest_snapshot(self):
        settings = load_settings(PROJECT / "config/settings.json")
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            master_path = temp_path / "master.csv"
            original = {
                "Customer ID": "customer-1",
                "Created Date": "2026-09-01",
                "Invoice Business Unit": "HVAC Service",
                "Job Type": "Repair",
                "Job Status": "Scheduled",
                "Total": "100",
            }
            changed = {
                **original,
                "Completion Date": "09/02/2026",
                "Job Status": "Completed",
                "Total": "250",
            }
            master_path.write_bytes(csv_bytes([original], settings["master_columns"]))
            upload = temp_path / "changed.csv"
            upload.write_bytes(csv_bytes([changed], settings["master_columns"]))

            result = run_import(upload, master_path, settings, temp_path / "out")
            with master_path.open() as handle:
                master_rows = list(csv.DictReader(handle))

            self.assertEqual(result.appended_rows, 0)
            self.assertEqual(result.updated_rows, 1)
            self.assertEqual(result.skipped_duplicate_rows, 0)
            self.assertEqual(len(master_rows), 1)
            self.assertEqual(master_rows[0]["Job Status"], "Completed")
            self.assertEqual(master_rows[0]["Total"], "250")

            repeat = run_import(upload, master_path, settings, temp_path / "repeat")
            self.assertEqual(repeat.updated_rows, 0)
            self.assertEqual(repeat.skipped_duplicate_rows, 1)

    def test_category_period_comparisons_show_current_new_customer_gain_loss(self):
        settings = load_settings(PROJECT / "config/settings.json")
        master_rows = []
        for category, current, previous, last_year in [
            ("HVAC Service", 78, 126, 34),
            ("Plumbing", 333, 312, 358),
        ]:
            for period_date, count in [
                ("09/21/2026", current),
                ("09/07/2026", previous),
                ("09/21/2025", last_year),
            ]:
                master_rows.extend({
                    "Customer ID": f"{category}-{period_date}-{index}",
                    "Created Date": period_date,
                    "Invoice Business Unit": category,
                    "Total": "0",
                } for index in range(count))
        master_rows.append({
            "Customer ID": "history",
            "Created Date": "01/01/2025",
            "Invoice Business Unit": "Plumbing",
            "Total": "0",
        })

        comparisons = category_period_comparisons(
            master_rows, date(2026, 9, 21), date(2026, 10, 4), settings
        )

        self.assertEqual(
            [row["New Customer Gain/Loss"] for row in comparisons["HVAC"]],
            ["—", "-38.1%", "+129.4%"],
        )
        self.assertEqual(
            [row["New Customer Gain/Loss"] for row in comparisons["Plumbing"]],
            ["—", "+6.7%", "-7.0%"],
        )

    def test_new_customer_gain_loss_is_unavailable_for_zero_comparison(self):
        settings = load_settings(PROJECT / "config/settings.json")
        master_rows = [{
            "Customer ID": "history",
            "Created Date": "01/01/2025",
            "Invoice Business Unit": "HVAC Service",
            "Total": "0",
        }, {
            "Customer ID": "current",
            "Created Date": "09/21/2026",
            "Invoice Business Unit": "HVAC Service",
            "Total": "0",
        }]

        comparisons = category_period_comparisons(
            master_rows, date(2026, 9, 21), date(2026, 10, 4), settings
        )

        self.assertEqual(comparisons["HVAC"][1]["New Customer Gain/Loss"], "N/A")

    def test_job_status_analysis_has_one_row_per_job_type(self):
        analysis, columns = job_status_analysis([
            {"Job Type": "Repair", "Job Status": "Completed"},
            {"Job Type": "Repair", "Job Status": "Completed"},
            {"Job Type": "Repair", "Job Status": "Scheduled"},
            {"Job Type": "Install", "Job Status": "Scheduled"},
        ])

        self.assertEqual(columns, ["Job Type", "Completed", "Scheduled"])
        self.assertEqual(analysis, [
            {"Job Type": "Install", "Completed": 0, "Scheduled": 1},
            {"Job Type": "Repair", "Completed": 2, "Scheduled": 1},
        ])

    def test_first_seen_rows_rebuild_missing_new_lead_weeks_from_master(self):
        settings = load_settings(PROJECT / "config/settings.json")
        rows = [
            {"Customer ID": "week-35", "Created Date": "08/25/2026"},
            {"Customer ID": "week-35", "Created Date": "09/10/2026"},
            {"Customer ID": "week-36", "Created Date": "09/01/2026"},
            {"Customer ID": "older", "Created Date": "01/01/2026"},
        ]

        rebuilt = first_seen_customer_rows(rows, settings)

        self.assertEqual(
            [(row["Customer ID"], row["Created Date"]) for row in rebuilt],
            [
                ("older", "01/01/2026"),
                ("week-35", "08/25/2026"),
                ("week-36", "09/01/2026"),
            ],
        )

    def test_first_seen_same_day_choice_is_stable_and_prefers_opportunity(self):
        settings = load_settings(PROJECT / "config/settings.json")
        blank = {
            "Customer ID": "same-day",
            "Created Date": "09/01/2026",
            "Invoice Business Unit": "Plumbing",
            "Job Type": "Repair",
            "Opportunity": "",
        }
        opportunity = {
            **blank,
            "Invoice Business Unit": "HVAC Service",
            "Opportunity": "False",
        }

        forward = first_seen_customer_rows([blank, opportunity], settings)
        reverse = first_seen_customer_rows([opportunity, blank], settings)

        self.assertEqual(forward, [opportunity])
        self.assertEqual(reverse, [opportunity])

    def test_analysis_csv_starts_with_category_period_sections(self):
        comparisons = {
            category: [{
                "Period": "Current Period",
                "Start": "2026-09-07",
                "End": "2026-09-20",
                "Records": records,
                "Unique Customers": customers,
                "New Customers": new_customers,
                "Revenue": revenue,
            }]
            for category, records, customers, new_customers, revenue in [
                ("HVAC", 200, 126, 75, "25000"),
                ("Plumbing", 500, 304, 190, "80000"),
            ]
        }

        contents = analysis_csv_bytes(
            comparisons,
            [{"Job Type": "Repair", "Completed": 2, "Scheduled": 1}],
            ["Job Type", "Completed", "Scheduled"],
        )
        rows = list(csv.reader(contents.decode().splitlines()))

        self.assertEqual(rows[0], ["HVAC Period Comparison"])
        self.assertEqual(rows[1], [
            "Period", "Start", "End", "Records", "Unique Customers",
            "New Customers", "New Customer Gain/Loss", "Revenue",
        ])
        self.assertEqual(rows[2][0:6], [
            "Current Period", "2026-09-07", "2026-09-20", "200", "126", "75",
        ])
        self.assertEqual(rows[4], ["Plumbing Period Comparison"])
        self.assertEqual(rows[8], ["Job Type Status Analysis"])
        self.assertEqual(rows[9], ["Job Type", "Completed", "Scheduled"])
        self.assertEqual(rows[10], ["Repair", "2", "1"])

    def test_weekly_opportunity_ytd_compares_same_weeks(self):
        settings = load_settings(PROJECT / "config/settings.json")
        rows = [
            {
                "Created Date": "01/06/2026",
                "Invoice Business Unit": "Plumbing Service",
                "Opportunity": "True",
            },
            {
                "Created Date": "01/07/2026",
                "Invoice Business Unit": "Plumbing Service",
                "Opportunity": "False",
            },
            {
                "Created Date": "01/07/2025",
                "Invoice Business Unit": "Plumbing Sales",
                "Opportunity": "TRUE",
            },
            {
                "Created Date": "12/01/2025",
                "Invoice Business Unit": "Plumbing Service",
                "Opportunity": "True",
            },
            {
                "Created Date": "03/10/2026",
                "Invoice Business Unit": "HVAC Service",
                "Opportunity": "True",
            },
        ]

        chart, years = weekly_opportunity_ytd(rows, "Plumbing", settings)

        self.assertEqual(years, (2025, 2026))
        self.assertEqual(len(chart), 53)
        self.assertEqual(chart[1], {"Week": 2, "2025": 1, "2026": 2})
        self.assertEqual(sum(row["2025"] for row in chart), 2)
        self.assertIsNone(chart[-1]["2026"])


if __name__ == "__main__":
    unittest.main()
