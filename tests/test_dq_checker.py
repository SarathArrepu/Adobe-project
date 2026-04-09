"""
Unit tests for DataQualityChecker.

Covers every check: file-level (column presence, empty file) and all
row-level checks (missing IP, bad timestamp, duplicate hit, unknown events,
purchase/product mismatches, negative revenue, malformed product list).
"""

import os
import sys
import unittest
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared.dq_checker import DataQualityChecker, REQUIRED_COLUMNS, APPENDIX_A_COLUMNS

# Minimal valid header covering all required + optional Appendix A columns
FULL_HEADER = "\t".join([
    "hit_time_gmt", "date_time", "user_agent", "ip", "event_list",
    "geo_city", "geo_region", "geo_country", "pagename", "page_url",
    "product_list", "referrer",
])

def _make_row(**kwargs) -> str:
    """Build a TSV data row from keyword args mapped to column names."""
    defaults = {
        "hit_time_gmt": "1254033280",
        "date_time": "2009-09-27 06:34:40",
        "user_agent": "Mozilla/5.0",
        "ip": "67.98.123.1",
        "event_list": "",
        "geo_city": "Salem",
        "geo_region": "OR",
        "geo_country": "US",
        "pagename": "Home",
        "page_url": "http://www.esshopzilla.com",
        "product_list": "",
        "referrer": "http://www.google.com/search?q=Ipod",
    }
    defaults.update(kwargs)
    return "\t".join(defaults[c] for c in [
        "hit_time_gmt", "date_time", "user_agent", "ip", "event_list",
        "geo_city", "geo_region", "geo_country", "pagename", "page_url",
        "product_list", "referrer",
    ])


class _BaseCheckerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _write(self, lines: list) -> str:
        path = os.path.join(self.temp_dir, "test_input.tsv")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _run(self, lines: list):
        path = self._write(lines)
        return DataQualityChecker(path).run()

    def _check_names(self, report):
        return {i.check for i in report.issues}


# ── File-level checks ─────────────────────────────────────────────────────────

class TestMissingRequiredColumns(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_all_required_columns_present_no_error(self):
        path = os.path.join(self.temp_dir, "f.tsv")
        with open(path, "w") as f:
            f.write(FULL_HEADER + "\n")
            f.write(_make_row() + "\n")
        report = DataQualityChecker(path).run()
        names = {i.check for i in report.issues}
        self.assertNotIn("MISSING_REQUIRED_COLUMNS", names)
        self.assertTrue(report.passed())

    def test_missing_ip_column_is_error(self):
        # Drop ip from header
        cols = [c for c in FULL_HEADER.split("\t") if c != "ip"]
        header = "\t".join(cols)
        path = os.path.join(self.temp_dir, "f.tsv")
        with open(path, "w") as f:
            f.write(header + "\n")
        report = DataQualityChecker(path).run()
        self.assertIn("MISSING_REQUIRED_COLUMNS", {i.check for i in report.issues})
        self.assertFalse(report.passed())

    def test_missing_optional_column_is_warn_not_error(self):
        # Keep required columns but drop geo_city (optional)
        cols = [c for c in FULL_HEADER.split("\t") if c != "geo_city"]
        header = "\t".join(cols)
        path = os.path.join(self.temp_dir, "f.tsv")
        with open(path, "w") as f:
            f.write(header + "\n")
            # Write a row without geo_city
            row_vals = {k: v for k, v in zip(FULL_HEADER.split("\t"), _make_row().split("\t")) if k != "geo_city"}
            f.write("\t".join(row_vals[c] for c in cols) + "\n")
        report = DataQualityChecker(path).run()
        names = {i.check for i in report.issues}
        self.assertNotIn("MISSING_REQUIRED_COLUMNS", names)
        self.assertIn("MISSING_APPENDIX_A_COLUMNS", names)
        self.assertTrue(report.passed())  # WARN doesn't fail


class TestEmptyFile(_BaseCheckerTest):
    def test_empty_file_is_error(self):
        report = self._run([FULL_HEADER])
        self.assertIn("EMPTY_FILE", self._check_names(report))
        self.assertFalse(report.passed())

    def test_file_with_data_rows_not_empty_error(self):
        report = self._run([FULL_HEADER, _make_row()])
        self.assertNotIn("EMPTY_FILE", self._check_names(report))


# ── Row-level checks ──────────────────────────────────────────────────────────

class TestMissingIP(_BaseCheckerTest):
    def test_empty_ip_is_warn(self):
        report = self._run([FULL_HEADER, _make_row(ip="")])
        self.assertIn("MISSING_IP", self._check_names(report))
        self.assertEqual(report.issues[0].severity, "WARN")

    def test_valid_ip_no_warn(self):
        report = self._run([FULL_HEADER, _make_row(ip="192.168.1.1")])
        self.assertNotIn("MISSING_IP", self._check_names(report))


class TestInvalidHitTime(_BaseCheckerTest):
    def test_non_integer_hit_time(self):
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="not_a_number")])
        self.assertIn("INVALID_HIT_TIME", self._check_names(report))

    def test_empty_hit_time(self):
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="")])
        self.assertIn("INVALID_HIT_TIME", self._check_names(report))

    def test_out_of_range_timestamp(self):
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="12345")])
        self.assertIn("INVALID_HIT_TIME", self._check_names(report))

    def test_valid_timestamp_no_warn(self):
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="1254033280")])
        self.assertNotIn("INVALID_HIT_TIME", self._check_names(report))


class TestInvalidIPFormat(_BaseCheckerTest):
    def test_invalid_ip_format(self):
        report = self._run([FULL_HEADER, _make_row(ip="999.999.999.999")])
        self.assertIn("INVALID_IP_FORMAT", self._check_names(report))

    def test_non_ip_string(self):
        report = self._run([FULL_HEADER, _make_row(ip="not-an-ip")])
        self.assertIn("INVALID_IP_FORMAT", self._check_names(report))

    def test_valid_ip(self):
        report = self._run([FULL_HEADER, _make_row(ip="67.98.123.1")])
        self.assertNotIn("INVALID_IP_FORMAT", self._check_names(report))


class TestDuplicateHit(_BaseCheckerTest):
    def test_duplicate_hit_time_ip(self):
        row = _make_row(hit_time_gmt="1254033280", ip="67.98.123.1")
        report = self._run([FULL_HEADER, row, row])
        self.assertIn("DUPLICATE_HIT", self._check_names(report))
        dupes = [i for i in report.issues if i.check == "DUPLICATE_HIT"]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].row, 2)  # second occurrence flagged

    def test_different_ip_not_duplicate(self):
        row1 = _make_row(hit_time_gmt="1254033280", ip="67.98.123.1")
        row2 = _make_row(hit_time_gmt="1254033280", ip="23.8.61.21")
        report = self._run([FULL_HEADER, row1, row2])
        self.assertNotIn("DUPLICATE_HIT", self._check_names(report))


class TestUnknownEventID(_BaseCheckerTest):
    def test_unknown_event_id_is_info(self):
        report = self._run([FULL_HEADER, _make_row(event_list="999")])
        self.assertIn("UNKNOWN_EVENT_ID", self._check_names(report))
        issue = next(i for i in report.issues if i.check == "UNKNOWN_EVENT_ID")
        self.assertEqual(issue.severity, "INFO")

    def test_known_event_ids_no_issue(self):
        report = self._run([FULL_HEADER, _make_row(event_list="1,2,12")])
        self.assertNotIn("UNKNOWN_EVENT_ID", self._check_names(report))

    def test_empty_event_list_no_issue(self):
        report = self._run([FULL_HEADER, _make_row(event_list="")])
        self.assertNotIn("UNKNOWN_EVENT_ID", self._check_names(report))


class TestPurchaseNoProduct(_BaseCheckerTest):
    def test_purchase_event_with_no_product_list(self):
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="")])
        self.assertIn("PURCHASE_NO_PRODUCT", self._check_names(report))

    def test_purchase_event_with_product_list_ok(self):
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;290;")])
        self.assertNotIn("PURCHASE_NO_PRODUCT", self._check_names(report))

    def test_non_purchase_event_no_issue(self):
        report = self._run([FULL_HEADER, _make_row(event_list="2", product_list="")])
        self.assertNotIn("PURCHASE_NO_PRODUCT", self._check_names(report))


class TestProductRevenueNoPurchase(_BaseCheckerTest):
    def test_revenue_in_product_but_no_purchase_event(self):
        # Revenue > 0 in product_list but event 1 not in event_list
        report = self._run([FULL_HEADER, _make_row(event_list="2", product_list="Electronics;Ipod;1;290;")])
        self.assertIn("PRODUCT_REVENUE_NO_PURCHASE", self._check_names(report))

    def test_revenue_with_purchase_event_no_issue(self):
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;290;")])
        self.assertNotIn("PRODUCT_REVENUE_NO_PURCHASE", self._check_names(report))

    def test_zero_revenue_no_purchase_no_issue(self):
        # Revenue field is empty (product view) — no issue
        report = self._run([FULL_HEADER, _make_row(event_list="2", product_list="Electronics;Ipod;1;;")])
        self.assertNotIn("PRODUCT_REVENUE_NO_PURCHASE", self._check_names(report))


class TestNegativeRevenue(_BaseCheckerTest):
    def test_negative_revenue_flagged(self):
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;-50;")])
        self.assertIn("NEGATIVE_REVENUE", self._check_names(report))

    def test_positive_revenue_ok(self):
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;290;")])
        self.assertNotIn("NEGATIVE_REVENUE", self._check_names(report))


class TestMalformedProductList(_BaseCheckerTest):
    def test_too_few_fields(self):
        report = self._run([FULL_HEADER, _make_row(product_list="Electronics;Ipod")])
        self.assertIn("MALFORMED_PRODUCT_LIST", self._check_names(report))

    def test_non_numeric_revenue(self):
        report = self._run([FULL_HEADER, _make_row(product_list="Electronics;Ipod;1;abc;")])
        self.assertIn("MALFORMED_PRODUCT_LIST", self._check_names(report))

    def test_valid_product_list_no_issue(self):
        report = self._run([FULL_HEADER, _make_row(product_list="Electronics;Ipod;1;290;")])
        names = self._check_names(report)
        self.assertNotIn("MALFORMED_PRODUCT_LIST", names)
        self.assertNotIn("NEGATIVE_REVENUE", names)


# ── Integration: provided data file ──────────────────────────────────────────

class TestWithProvidedDataFile(unittest.TestCase):
    DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")),
        "Provided data file not found"
    )
    def test_provided_data_passes_dq(self):
        """The sample data.sql should pass all ERROR-level checks."""
        report = DataQualityChecker(self.DATA_FILE).run()
        report.print_summary()
        self.assertTrue(report.passed(), f"DQ errors: {[str(i) for i in report.errors]}")
        self.assertEqual(report.total_rows, 21)

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")),
        "Provided data file not found"
    )
    def test_provided_data_row_count(self):
        report = DataQualityChecker(self.DATA_FILE).run()
        self.assertEqual(report.total_rows, 21)


if __name__ == "__main__":
    unittest.main(verbosity=2)
