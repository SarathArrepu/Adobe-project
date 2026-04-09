"""
Unit tests for DataQualityChecker
===================================
Covers every check in the DQ suite:

File-level checks (one test class per check):
  TestMissingRequiredColumns   — MISSING_REQUIRED_COLUMNS (ERROR) and MISSING_APPENDIX_A_COLUMNS (WARN)
  TestEmptyFile                — EMPTY_FILE (ERROR)

Row-level checks (one test class per check):
  TestMissingIP                — MISSING_IP (WARN)
  TestInvalidHitTime           — INVALID_HIT_TIME (WARN): empty, non-integer, out-of-range
  TestInvalidIPFormat          — INVALID_IP_FORMAT (WARN): out-of-range octets, non-IP strings
  TestDuplicateHit             — DUPLICATE_HIT (WARN): same (hit_time, ip) pair
  TestUnknownEventID           — UNKNOWN_EVENT_ID (INFO): unrecognised event IDs
  TestPurchaseNoProduct        — PURCHASE_NO_PRODUCT (WARN): event 1 with empty product list
  TestProductRevenueNoPurchase — PRODUCT_REVENUE_NO_PURCHASE (WARN): revenue without event 1
  TestNegativeRevenue          — NEGATIVE_REVENUE (WARN): negative revenue field
  TestMalformedProductList     — MALFORMED_PRODUCT_LIST (WARN): too few fields, non-numeric revenue

Integration:
  TestWithProvidedDataFile     — data/data.sql must pass all ERROR-level checks with 21 rows
"""

import os       # path construction for the integration test
import sys      # sys.path manipulation so tests can import from src/
import unittest # standard-library test framework
import tempfile # creates isolated temporary directories per test
import shutil   # removes temp directories after each test

# Insert src/ so the shared/ package is importable without installing the project
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared.dq_checker import DataQualityChecker, REQUIRED_COLUMNS, APPENDIX_A_COLUMNS

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Minimal valid header that includes all required AND optional Appendix A columns.
# Used as the header row in all test TSV files.
FULL_HEADER = "\t".join([
    "hit_time_gmt", "date_time", "user_agent", "ip", "event_list",
    "geo_city", "geo_region", "geo_country", "pagename", "page_url",
    "product_list", "referrer",
])

# Column order used when building rows — must match FULL_HEADER exactly.
_COLUMNS = [
    "hit_time_gmt", "date_time", "user_agent", "ip", "event_list",
    "geo_city", "geo_region", "geo_country", "pagename", "page_url",
    "product_list", "referrer",
]


def _make_row(**kwargs) -> str:
    """
    Build a valid TSV data row, optionally overriding specific column values.

    Defaults provide a row that passes all DQ checks when used with
    ``FULL_HEADER``.  Pass keyword arguments to override individual columns
    and trigger specific check failures.

    Args:
        **kwargs: Column name → value overrides.

    Returns:
        A tab-separated string with values in the same order as ``FULL_HEADER``.

    Example::

        _make_row(ip="")           # triggers MISSING_IP
        _make_row(hit_time_gmt="") # triggers INVALID_HIT_TIME
    """
    defaults = {
        "hit_time_gmt": "1254033280",                    # valid Unix timestamp (2009)
        "date_time":    "2009-09-27 06:34:40",           # human-readable timestamp
        "user_agent":   "Mozilla/5.0",                   # minimal user-agent string
        "ip":           "67.98.123.1",                   # valid IPv4 address
        "event_list":   "",                              # no events — plain page view
        "geo_city":     "Salem",                         # optional geo field
        "geo_region":   "OR",                            # optional geo field
        "geo_country":  "US",                            # optional geo field
        "pagename":     "Home",                          # optional page name
        "page_url":     "http://www.esshopzilla.com",    # optional page URL
        "product_list": "",                              # no products — plain page view
        "referrer":     "http://www.google.com/search?q=Ipod",  # search referral
    }
    defaults.update(kwargs)  # apply caller-specified overrides on top of defaults
    return "\t".join(defaults[c] for c in _COLUMNS)  # build row in FULL_HEADER column order


# ---------------------------------------------------------------------------
# Base class with shared helpers
# ---------------------------------------------------------------------------

class _BaseCheckerTest(unittest.TestCase):
    """
    Base test class providing helpers for writing TSV files and running the
    checker.  Subclasses inherit the temp-directory lifecycle and the three
    helper methods.
    """

    def setUp(self) -> None:
        """Create a fresh temp directory for each test method."""
        self.temp_dir = tempfile.mkdtemp()  # isolated per test — avoids cross-test pollution

    def tearDown(self) -> None:
        """Delete the temp directory and all files created inside it."""
        shutil.rmtree(self.temp_dir)

    def _write(self, lines: list) -> str:
        """
        Write a list of strings (one per line) to a TSV file in the temp dir.

        Args:
            lines: List of row strings; the first element should be the header.

        Returns:
            Absolute path of the written file.
        """
        path = os.path.join(self.temp_dir, "test_input.tsv")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")  # join with newlines and add a trailing newline
        return path

    def _run(self, lines: list) -> "DQReport":  # noqa: F821
        """
        Write ``lines`` to a TSV file and run the DataQualityChecker on it.

        Args:
            lines: List of row strings (header first, then data rows).

        Returns:
            The ``DQReport`` produced by ``DataQualityChecker.run()``.
        """
        path = self._write(lines)          # write fixture to disk
        return DataQualityChecker(path).run()  # run full check suite

    def _check_names(self, report: "DQReport") -> set:  # noqa: F821
        """
        Extract the set of check names from all issues in a report.

        Convenience method for ``assertIn`` / ``assertNotIn`` assertions.

        Args:
            report: DQReport returned by ``_run()``.

        Returns:
            Set of check name strings (e.g. ``{'MISSING_IP', 'DUPLICATE_HIT'}``).
        """
        return {i.check for i in report.issues}


# ---------------------------------------------------------------------------
# File-level checks
# ---------------------------------------------------------------------------

class TestMissingRequiredColumns(unittest.TestCase):
    """
    Tests for the MISSING_REQUIRED_COLUMNS (ERROR) and MISSING_APPENDIX_A_COLUMNS
    (WARN) file-level checks.
    """

    def setUp(self) -> None:
        """Create a temp directory for this test class."""
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        """Remove temp directory after each test."""
        shutil.rmtree(self.temp_dir)

    def test_all_required_columns_present_no_error(self) -> None:
        """A file with all required columns and valid data should pass with no errors."""
        path = os.path.join(self.temp_dir, "f.tsv")
        with open(path, "w") as f:
            f.write(FULL_HEADER + "\n")  # full Appendix A header
            f.write(_make_row() + "\n")  # one valid data row
        report = DataQualityChecker(path).run()
        names  = {i.check for i in report.issues}
        self.assertNotIn("MISSING_REQUIRED_COLUMNS", names)  # no missing columns error
        self.assertTrue(report.passed())                       # overall pass

    def test_missing_ip_column_is_error(self) -> None:
        """Removing the required 'ip' column should trigger a pipeline-blocking ERROR."""
        cols   = [c for c in FULL_HEADER.split("\t") if c != "ip"]  # drop ip from header
        header = "\t".join(cols)
        path   = os.path.join(self.temp_dir, "f.tsv")
        with open(path, "w") as f:
            f.write(header + "\n")  # header without ip
        report = DataQualityChecker(path).run()
        self.assertIn("MISSING_REQUIRED_COLUMNS", {i.check for i in report.issues})
        self.assertFalse(report.passed())  # ERROR → pipeline must abort

    def test_missing_optional_column_is_warn_not_error(self) -> None:
        """
        Removing an optional Appendix A column (geo_city) should produce a WARN,
        not an ERROR — the pipeline can continue without it.
        """
        cols   = [c for c in FULL_HEADER.split("\t") if c != "geo_city"]  # drop optional column
        header = "\t".join(cols)
        path   = os.path.join(self.temp_dir, "f.tsv")
        with open(path, "w") as f:
            f.write(header + "\n")
            # Build a data row that excludes geo_city to match the trimmed header
            row_vals = {
                k: v
                for k, v in zip(FULL_HEADER.split("\t"), _make_row().split("\t"))
                if k != "geo_city"
            }
            f.write("\t".join(row_vals[c] for c in cols) + "\n")

        report = DataQualityChecker(path).run()
        names  = {i.check for i in report.issues}
        self.assertNotIn("MISSING_REQUIRED_COLUMNS", names)   # required columns all present
        self.assertIn("MISSING_APPENDIX_A_COLUMNS",  names)   # optional column absent → WARN
        self.assertTrue(report.passed())                        # WARN does not fail the pipeline


class TestEmptyFile(_BaseCheckerTest):
    """Tests for the EMPTY_FILE (ERROR) check."""

    def test_empty_file_is_error(self) -> None:
        """A file with a header but zero data rows should fail with EMPTY_FILE."""
        report = self._run([FULL_HEADER])  # header only — no data rows
        self.assertIn("EMPTY_FILE", self._check_names(report))
        self.assertFalse(report.passed())  # ERROR → pipeline cannot continue

    def test_file_with_data_rows_not_empty_error(self) -> None:
        """A file with at least one data row should NOT trigger EMPTY_FILE."""
        report = self._run([FULL_HEADER, _make_row()])  # header + one valid row
        self.assertNotIn("EMPTY_FILE", self._check_names(report))


# ---------------------------------------------------------------------------
# Row-level checks
# ---------------------------------------------------------------------------

class TestMissingIP(_BaseCheckerTest):
    """Tests for the MISSING_IP (WARN) check."""

    def test_empty_ip_is_warn(self) -> None:
        """An empty ip field should produce a WARN (not an ERROR)."""
        report = self._run([FULL_HEADER, _make_row(ip="")])  # override ip with empty string
        self.assertIn("MISSING_IP", self._check_names(report))
        self.assertEqual(report.issues[0].severity, "WARN")  # must be WARN — row skipped, not abort

    def test_valid_ip_no_warn(self) -> None:
        """A well-formed IPv4 address should produce no MISSING_IP issue."""
        report = self._run([FULL_HEADER, _make_row(ip="192.168.1.1")])
        self.assertNotIn("MISSING_IP", self._check_names(report))


class TestInvalidHitTime(_BaseCheckerTest):
    """Tests for the INVALID_HIT_TIME (WARN) check."""

    def test_non_integer_hit_time(self) -> None:
        """A non-integer string in hit_time_gmt should be flagged as invalid."""
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="not_a_number")])
        self.assertIn("INVALID_HIT_TIME", self._check_names(report))

    def test_empty_hit_time(self) -> None:
        """An empty hit_time_gmt field should be flagged as invalid."""
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="")])
        self.assertIn("INVALID_HIT_TIME", self._check_names(report))

    def test_out_of_range_timestamp(self) -> None:
        """A Unix timestamp below the 2000-01-01 floor should be flagged as invalid."""
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="12345")])  # year ~1970
        self.assertIn("INVALID_HIT_TIME", self._check_names(report))

    def test_valid_timestamp_no_warn(self) -> None:
        """A valid 2009 Unix timestamp should produce no INVALID_HIT_TIME issue."""
        report = self._run([FULL_HEADER, _make_row(hit_time_gmt="1254033280")])
        self.assertNotIn("INVALID_HIT_TIME", self._check_names(report))


class TestInvalidIPFormat(_BaseCheckerTest):
    """Tests for the INVALID_IP_FORMAT (WARN) check."""

    def test_invalid_ip_format(self) -> None:
        """An IP with octets out of 0–255 range (e.g. 999.999.999.999) should be flagged."""
        report = self._run([FULL_HEADER, _make_row(ip="999.999.999.999")])
        self.assertIn("INVALID_IP_FORMAT", self._check_names(report))

    def test_non_ip_string(self) -> None:
        """A non-IP string in the ip field should be flagged."""
        report = self._run([FULL_HEADER, _make_row(ip="not-an-ip")])
        self.assertIn("INVALID_IP_FORMAT", self._check_names(report))

    def test_valid_ip(self) -> None:
        """A properly formatted IPv4 address should produce no INVALID_IP_FORMAT issue."""
        report = self._run([FULL_HEADER, _make_row(ip="67.98.123.1")])
        self.assertNotIn("INVALID_IP_FORMAT", self._check_names(report))


class TestDuplicateHit(_BaseCheckerTest):
    """Tests for the DUPLICATE_HIT (WARN) check."""

    def test_duplicate_hit_time_ip(self) -> None:
        """
        Two rows with identical (hit_time_gmt, ip) should flag the second row
        as a duplicate.  Only one DUPLICATE_HIT issue should be emitted.
        """
        row    = _make_row(hit_time_gmt="1254033280", ip="67.98.123.1")
        report = self._run([FULL_HEADER, row, row])  # same row written twice
        self.assertIn("DUPLICATE_HIT", self._check_names(report))

        dupes  = [i for i in report.issues if i.check == "DUPLICATE_HIT"]
        self.assertEqual(len(dupes), 1)     # only one DUPLICATE_HIT issue
        self.assertEqual(dupes[0].row, 2)   # flagged on the second occurrence (data row 2)

    def test_different_ip_not_duplicate(self) -> None:
        """Same hit_time_gmt but different IPs should NOT be treated as a duplicate."""
        row1   = _make_row(hit_time_gmt="1254033280", ip="67.98.123.1")
        row2   = _make_row(hit_time_gmt="1254033280", ip="23.8.61.21")  # different IP
        report = self._run([FULL_HEADER, row1, row2])
        self.assertNotIn("DUPLICATE_HIT", self._check_names(report))


class TestUnknownEventID(_BaseCheckerTest):
    """Tests for the UNKNOWN_EVENT_ID (INFO) check."""

    def test_unknown_event_id_is_info(self) -> None:
        """An event ID not in the Appendix A list should be flagged at INFO severity."""
        report = self._run([FULL_HEADER, _make_row(event_list="999")])  # 999 is not in VALID_EVENT_IDS
        self.assertIn("UNKNOWN_EVENT_ID", self._check_names(report))
        issue  = next(i for i in report.issues if i.check == "UNKNOWN_EVENT_ID")
        self.assertEqual(issue.severity, "INFO")  # INFO — does not affect pipeline correctness

    def test_known_event_ids_no_issue(self) -> None:
        """All-known event IDs should produce no UNKNOWN_EVENT_ID issue."""
        report = self._run([FULL_HEADER, _make_row(event_list="1,2,12")])  # all in VALID_EVENT_IDS
        self.assertNotIn("UNKNOWN_EVENT_ID", self._check_names(report))

    def test_empty_event_list_no_issue(self) -> None:
        """An empty event_list (plain page view) should produce no UNKNOWN_EVENT_ID issue."""
        report = self._run([FULL_HEADER, _make_row(event_list="")])
        self.assertNotIn("UNKNOWN_EVENT_ID", self._check_names(report))


class TestPurchaseNoProduct(_BaseCheckerTest):
    """Tests for the PURCHASE_NO_PRODUCT (WARN) check."""

    def test_purchase_event_with_no_product_list(self) -> None:
        """Purchase event (1) with an empty product_list should be flagged."""
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="")])
        self.assertIn("PURCHASE_NO_PRODUCT", self._check_names(report))

    def test_purchase_event_with_product_list_ok(self) -> None:
        """Purchase event with a non-empty product_list should NOT be flagged."""
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;290;")])
        self.assertNotIn("PURCHASE_NO_PRODUCT", self._check_names(report))

    def test_non_purchase_event_no_issue(self) -> None:
        """A non-purchase event (e.g. product view = '2') with no product_list is fine."""
        report = self._run([FULL_HEADER, _make_row(event_list="2", product_list="")])
        self.assertNotIn("PURCHASE_NO_PRODUCT", self._check_names(report))


class TestProductRevenueNoPurchase(_BaseCheckerTest):
    """Tests for the PRODUCT_REVENUE_NO_PURCHASE (WARN) check."""

    def test_revenue_in_product_but_no_purchase_event(self) -> None:
        """
        Revenue > 0 in product_list without purchase event "1" should be flagged —
        the analyzer will silently drop this revenue per the Appendix B spec.
        """
        report = self._run([FULL_HEADER, _make_row(event_list="2", product_list="Electronics;Ipod;1;290;")])
        self.assertIn("PRODUCT_REVENUE_NO_PURCHASE", self._check_names(report))

    def test_revenue_with_purchase_event_no_issue(self) -> None:
        """Revenue with a matching purchase event should NOT be flagged."""
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;290;")])
        self.assertNotIn("PRODUCT_REVENUE_NO_PURCHASE", self._check_names(report))

    def test_zero_revenue_no_purchase_no_issue(self) -> None:
        """Product view with empty revenue field (;;) and no purchase event is fine."""
        report = self._run([FULL_HEADER, _make_row(event_list="2", product_list="Electronics;Ipod;1;;")])
        self.assertNotIn("PRODUCT_REVENUE_NO_PURCHASE", self._check_names(report))


class TestNegativeRevenue(_BaseCheckerTest):
    """Tests for the NEGATIVE_REVENUE (WARN) check."""

    def test_negative_revenue_flagged(self) -> None:
        """A negative revenue value in product_list should be flagged."""
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;-50;")])
        self.assertIn("NEGATIVE_REVENUE", self._check_names(report))

    def test_positive_revenue_ok(self) -> None:
        """A positive revenue value should produce no NEGATIVE_REVENUE issue."""
        report = self._run([FULL_HEADER, _make_row(event_list="1", product_list="Electronics;Ipod;1;290;")])
        self.assertNotIn("NEGATIVE_REVENUE", self._check_names(report))


class TestMalformedProductList(_BaseCheckerTest):
    """Tests for the MALFORMED_PRODUCT_LIST (WARN) check."""

    def test_too_few_fields(self) -> None:
        """A product entry with fewer than 4 semicolon-delimited fields should be flagged."""
        report = self._run([FULL_HEADER, _make_row(product_list="Electronics;Ipod")])  # only 2 fields
        self.assertIn("MALFORMED_PRODUCT_LIST", self._check_names(report))

    def test_non_numeric_revenue(self) -> None:
        """A non-numeric revenue field (e.g. 'abc') should be flagged as malformed."""
        report = self._run([FULL_HEADER, _make_row(product_list="Electronics;Ipod;1;abc;")])
        self.assertIn("MALFORMED_PRODUCT_LIST", self._check_names(report))

    def test_valid_product_list_no_issue(self) -> None:
        """A properly formatted product list with positive revenue should pass both checks."""
        report = self._run([FULL_HEADER, _make_row(product_list="Electronics;Ipod;1;290;")])
        names  = self._check_names(report)
        self.assertNotIn("MALFORMED_PRODUCT_LIST", names)  # structure is valid
        self.assertNotIn("NEGATIVE_REVENUE",       names)  # revenue is positive


# ---------------------------------------------------------------------------
# Integration test against the provided data file
# ---------------------------------------------------------------------------

class TestWithProvidedDataFile(unittest.TestCase):
    """
    Integration tests that run the full DQ suite against the actual
    ``data/data.sql`` sample file.

    Skipped automatically when the file is absent so CI still passes on
    environments without the data file.
    """

    # Resolve path relative to this test file so it works from any CWD
    DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")),
        "Provided data file not found"  # message shown when the test is skipped
    )
    def test_provided_data_passes_dq(self) -> None:
        """
        The sample data.sql file should pass all ERROR-level DQ checks.
        The pipeline should be able to process it without aborting.
        """
        report = DataQualityChecker(self.DATA_FILE).run()
        report.print_summary()  # log full summary so CI output shows the results
        self.assertTrue(
            report.passed(),
            f"DQ errors: {[str(i) for i in report.errors]}"  # helpful failure message
        )
        self.assertEqual(report.total_rows, 21)  # data.sql has exactly 21 data rows

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")),
        "Provided data file not found"
    )
    def test_provided_data_row_count(self) -> None:
        """Verify that the checker reads exactly 21 data rows from data.sql."""
        report = DataQualityChecker(self.DATA_FILE).run()
        self.assertEqual(report.total_rows, 21)  # 21 rows documented in assessment


if __name__ == "__main__":
    unittest.main(verbosity=2)  # run with verbose output when executed directly
