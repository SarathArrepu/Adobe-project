"""
Unit tests for SearchKeywordAnalyzer
=====================================
Covers the three core methods independently and then exercises the full
end-to-end pipeline using both a synthetic fixture and the provided
``data/data.sql`` sample file.

Test classes
------------
TestParseSearchEngine         — referrer URL parsing (engine detection + keyword extraction)
TestParseRevenue              — product_list revenue extraction
TestIsPurchaseEvent           — purchase event detection from event_list
TestEndToEnd                  — full pipeline with synthetic fixture data
TestWithProvidedDataFile      — integration test against data/data.sql (skipped if absent)
"""

import os       # path construction for fixture files
import sys      # sys.path manipulation so tests can import from src/
import csv      # reading the written output file to verify column names and row count
import unittest # standard-library test framework
import tempfile # creates isolated temp directories so tests do not pollute each other
import shutil   # removes temp directories after each test

# Insert the src/ directory at the front of sys.path so the imports below
# resolve to the local source tree rather than any installed package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared.search_keyword_analyzer import SearchKeywordAnalyzer  # module under test


class TestParseSearchEngine(unittest.TestCase):
    """
    Tests for ``SearchKeywordAnalyzer.parse_search_engine()``.

    Verifies that:
    - Known search engine referrers return the correct (domain, keyword) tuple.
    - Non-search referrers return None.
    - Edge cases (empty, whitespace, missing keyword, URL-encoded keywords) are
      handled gracefully.
    """

    def setUp(self) -> None:
        """
        Create a minimal dummy TSV file so ``SearchKeywordAnalyzer.__init__``
        does not raise ``FileNotFoundError``.  The file content is irrelevant
        for ``parse_search_engine`` tests.
        """
        self.temp_dir    = tempfile.mkdtemp()  # isolated temp directory for this test run
        self.dummy_file  = os.path.join(self.temp_dir, "dummy.tsv")
        with open(self.dummy_file, "w") as f:
            # Minimal header with the required columns — no data rows needed
            f.write("hit_time_gmt\tdate_time\tip\treferrer\tevent_list\tproduct_list\n")
        self.analyzer = SearchKeywordAnalyzer(self.dummy_file)  # reused across all tests in this class

    def tearDown(self) -> None:
        """Remove the temp directory and all its contents after each test."""
        shutil.rmtree(self.temp_dir)

    def test_google_referrer(self) -> None:
        """Google referrer with ?q= should return ('google.com', 'Ipod')."""
        url    = "http://www.google.com/search?hl=en&client=firefox-a&q=Ipod&aq=f"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)             # should match Google
        self.assertEqual(result[0], "google.com")  # domain normalised (www. stripped)
        self.assertEqual(result[1], "Ipod")        # keyword extracted from ?q=

    def test_yahoo_referrer(self) -> None:
        """Yahoo referrer with ?p= should return ('search.yahoo.com', 'cd player')."""
        url    = "http://search.yahoo.com/search?p=cd+player&toggle=1&cop=mss&ei=UTF-8"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)                      # should match Yahoo
        self.assertEqual(result[0], "search.yahoo.com")  # Yahoo subdomain preserved
        self.assertEqual(result[1], "cd player")          # + decoded to space by parse_qs

    def test_bing_referrer(self) -> None:
        """Bing referrer with ?q= should return ('bing.com', 'Zune')."""
        url    = "http://www.bing.com/search?q=Zune&go=&form=QBLH&qs=n"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)           # should match Bing
        self.assertEqual(result[0], "bing.com")  # www. stripped
        self.assertEqual(result[1], "Zune")      # keyword extracted from ?q=

    def test_non_search_referrer(self) -> None:
        """Internal site referrers should return None."""
        url    = "http://www.esshopzilla.com/product/?pid=as32213"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNone(result)  # not a configured search engine

    def test_empty_referrer(self) -> None:
        """Empty string, None, and whitespace-only referrers should all return None."""
        self.assertIsNone(self.analyzer.parse_search_engine(""))     # empty string
        self.assertIsNone(self.analyzer.parse_search_engine(None))   # None (common in TSV data)
        self.assertIsNone(self.analyzer.parse_search_engine("   "))  # whitespace only

    def test_google_without_keyword(self) -> None:
        """Google URL with no ?q= value should return None (no keyword to extract)."""
        url    = "http://www.google.com/search?hl=en"  # ?q parameter absent
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNone(result)  # cannot attribute without a keyword

    def test_encoded_keyword(self) -> None:
        """Percent-encoded keyword in Google URL should be decoded correctly."""
        url    = "http://www.google.com/search?q=ipod%20nano%20case"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "ipod nano case")  # %20 decoded to space

    def test_yahoo_plus_encoded_spaces(self) -> None:
        """Yahoo ?p= value with + as space separator should be decoded correctly."""
        url    = "http://search.yahoo.com/search?p=cd+player+portable"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "cd player portable")  # + decoded to space by parse_qs


class TestParseRevenue(unittest.TestCase):
    """
    Tests for ``SearchKeywordAnalyzer.parse_revenue()``.

    Verifies that revenue is extracted and summed correctly across single and
    multiple products, and that edge cases (empty list, no revenue field,
    decimal values) are handled.
    """

    def setUp(self) -> None:
        """Create a minimal dummy TSV so the analyzer can be instantiated."""
        self.temp_dir   = tempfile.mkdtemp()
        self.dummy_file = os.path.join(self.temp_dir, "dummy.tsv")
        with open(self.dummy_file, "w") as f:
            f.write("hit_time_gmt\tdate_time\tip\treferrer\tevent_list\tproduct_list\n")
        self.analyzer = SearchKeywordAnalyzer(self.dummy_file)

    def tearDown(self) -> None:
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir)

    def test_single_product_with_revenue(self) -> None:
        """Single product with integer revenue should return that revenue."""
        product_list = "Electronics;Zune - 32GB;1;250;"
        self.assertEqual(self.analyzer.parse_revenue(product_list), 250.0)

    def test_single_product_decimal_revenue(self) -> None:
        """Single product with decimal revenue should be parsed accurately."""
        product_list = "Electronics;Ipod - Nano - 8GB;1;189.99;"
        self.assertAlmostEqual(self.analyzer.parse_revenue(product_list), 189.99)

    def test_multiple_products(self) -> None:
        """Multiple comma-separated products should have their revenues summed."""
        product_list = "Electronics;Ipod;1;200;,Accessories;Case;2;29.99;"
        self.assertAlmostEqual(self.analyzer.parse_revenue(product_list), 229.99)

    def test_empty_product_list(self) -> None:
        """Empty string and None should both return 0.0 without raising."""
        self.assertEqual(self.analyzer.parse_revenue(""),   0.0)  # empty string
        self.assertEqual(self.analyzer.parse_revenue(None), 0.0)  # None

    def test_product_list_no_revenue(self) -> None:
        """Product with an empty revenue field (;;) should return 0.0."""
        product_list = "Electronics;Zune - 32GB;1;;"  # revenue field is empty
        self.assertEqual(self.analyzer.parse_revenue(product_list), 0.0)

    def test_product_view_no_revenue_field(self) -> None:
        """Product views often omit revenue — empty revenue field returns 0.0."""
        product_list = "Electronics;Ipod - Nano - 8GB;1;;"  # product view, no sale
        self.assertEqual(self.analyzer.parse_revenue(product_list), 0.0)


class TestIsPurchaseEvent(unittest.TestCase):
    """
    Tests for ``SearchKeywordAnalyzer.is_purchase_event()``.

    Verifies exact token matching (event "10" must NOT match "1") and correct
    handling of compound event lists, empty strings, and None.
    """

    def setUp(self) -> None:
        """Create a minimal dummy TSV so the analyzer can be instantiated."""
        self.temp_dir   = tempfile.mkdtemp()
        self.dummy_file = os.path.join(self.temp_dir, "dummy.tsv")
        with open(self.dummy_file, "w") as f:
            f.write("hit_time_gmt\tdate_time\tip\treferrer\tevent_list\tproduct_list\n")
        self.analyzer = SearchKeywordAnalyzer(self.dummy_file)

    def tearDown(self) -> None:
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir)

    def test_purchase_event(self) -> None:
        """event_list = '1' alone should return True."""
        self.assertTrue(self.analyzer.is_purchase_event("1"))

    def test_purchase_with_other_events(self) -> None:
        """Purchase event mixed with other events should return True."""
        self.assertTrue(self.analyzer.is_purchase_event("1,200,201"))

    def test_product_view_only(self) -> None:
        """Event '2' (Product View) is not a purchase — should return False."""
        self.assertFalse(self.analyzer.is_purchase_event("2"))

    def test_cart_events_not_purchase(self) -> None:
        """Cart-related events (11, 12) are not purchases — should return False."""
        self.assertFalse(self.analyzer.is_purchase_event("12"))  # Cart Add
        self.assertFalse(self.analyzer.is_purchase_event("11"))  # Cart Open

    def test_empty_event_list(self) -> None:
        """Empty string and None should both return False without raising."""
        self.assertFalse(self.analyzer.is_purchase_event(""))    # empty string
        self.assertFalse(self.analyzer.is_purchase_event(None))  # None

    def test_event_10_not_confused_with_1(self) -> None:
        """
        Event 10 (Cart Open) must NOT match purchase event 1.

        This guards against a substring-match bug: checking ``"1" in "10"``
        would incorrectly return True.  The correct implementation splits on
        commas and compares tokens exactly.
        """
        self.assertFalse(self.analyzer.is_purchase_event("10"))

    def test_event_1_in_middle(self) -> None:
        """Purchase event should be detected regardless of position in the list."""
        self.assertTrue(self.analyzer.is_purchase_event("12,1,200"))


class TestEndToEnd(unittest.TestCase):
    """
    End-to-end tests using synthetic fixture data.

    Exercises the full ``process() → get_results() → write_output()`` chain
    without needing the real data.sql file.
    """

    def setUp(self) -> None:
        """Create temp directories for input fixture and output file."""
        self.temp_dir  = tempfile.mkdtemp()  # root temp directory
        self.output_dir = os.path.join(self.temp_dir, "output")  # output sub-directory

    def tearDown(self) -> None:
        """Remove all temp files and directories created during the test."""
        shutil.rmtree(self.temp_dir)

    def _create_sample_data(self) -> str:
        """
        Build a minimal TSV fixture that mimics the provided sample data.

        Visitors:
        - IP 67.98.123.1  — arrives via Google "Ipod",   purchases for $290
        - IP 23.8.61.21   — arrives via Bing "Zune",     purchases for $250
        - IP 112.33.98.231— arrives via Yahoo "cd player", does NOT purchase

        Returns:
            Absolute path to the written fixture file.
        """
        filepath = os.path.join(self.temp_dir, "test_data.tsv")
        rows = [
            # Header row — all required + optional Appendix A columns
            "hit_time_gmt\tdate_time\tuser_agent\tip\tevent_list\tgeo_city\tgeo_region\tgeo_country\tpagename\tpage_url\tproduct_list\treferrer",
            # Visitor A: Google referral → no event → purchase
            "1254033280\t2009-09-27 06:34:40\tMozilla\t67.98.123.1\t\tSalem\tOR\tUS\tHome\thttp://www.esshopzilla.com\t\thttp://www.google.com/search?q=Ipod",
            # Visitor A: product view (event 2, no purchase)
            "1254034567\t2009-09-27 06:56:07\tMozilla\t67.98.123.1\t2\tSalem\tOR\tUS\tIpod - Touch\thttp://www.esshopzilla.com/product/\tElectronics;Ipod - Touch - 32GB;1;;\thttp://www.esshopzilla.com/search/",
            # Visitor A: purchase (event 1, $290 revenue)
            "1254035260\t2009-09-27 07:07:40\tMozilla\t67.98.123.1\t1\tSalem\tOR\tUS\tOrder Complete\thttps://www.esshopzilla.com/checkout/\tElectronics;Ipod - Touch - 32GB;1;290;\thttps://www.esshopzilla.com/checkout/?a=confirm",
            # Visitor B: Bing referral + product view
            "1254033379\t2009-09-27 06:36:19\tSafari\t23.8.61.21\t2\tRochester\tNY\tUS\tZune\thttp://www.esshopzilla.com/product/\tElectronics;Zune - 32GB;1;;\thttp://www.bing.com/search?q=Zune",
            # Visitor B: purchase (event 1, $250 revenue)
            "1254034666\t2009-09-27 06:57:46\tSafari\t23.8.61.21\t1\tRochester\tNY\tUS\tOrder Complete\thttps://www.esshopzilla.com/checkout/\tElectronics;Zune - 32GB;1;250;\thttps://www.esshopzilla.com/checkout/?a=confirm",
            # Visitor C: Yahoo referral, no purchase
            "1254033478\t2009-09-27 06:37:58\tSafari\t112.33.98.231\t\tSLC\tUT\tUS\tHome\thttp://www.esshopzilla.com\t\thttp://search.yahoo.com/search?p=cd+player",
        ]
        with open(filepath, "w") as f:
            f.write("\n".join(rows))  # write all rows as a single string joined by newlines
        return filepath

    def test_full_pipeline(self) -> None:
        """
        Verify that the pipeline produces the correct two keyword entries
        sorted by revenue descending.
        """
        filepath = self._create_sample_data()
        analyzer = SearchKeywordAnalyzer(filepath)
        analyzer.process()           # run DQ + attribution
        results = analyzer.get_results()  # sorted revenue list

        self.assertEqual(len(results), 2)  # two keyword entries: Google/Ipod and Bing/Zune

        # Highest revenue entry should be Google/Ipod at $290
        self.assertEqual(results[0]["Search Engine Domain"], "google.com")
        self.assertEqual(results[0]["Search Keyword"],       "Ipod")
        self.assertEqual(results[0]["Revenue"],              290.0)

        # Second entry should be Bing/Zune at $250
        self.assertEqual(results[1]["Search Engine Domain"], "bing.com")
        self.assertEqual(results[1]["Search Keyword"],       "Zune")
        self.assertEqual(results[1]["Revenue"],              250.0)

    def test_output_file_created(self) -> None:
        """
        Verify that write_output() creates a tab-delimited file with the
        correct filename suffix and three required columns.
        """
        filepath = self._create_sample_data()
        analyzer = SearchKeywordAnalyzer(filepath)
        analyzer.process()
        output_path = analyzer.write_output(self.output_dir)  # write to temp output dir

        self.assertTrue(os.path.exists(output_path))                    # file was created
        self.assertTrue(output_path.endswith("_SearchKeywordPerformance.tab"))  # naming convention

        # Open the output and verify structure
        with open(output_path, "r") as f:
            reader = csv.DictReader(f, delimiter="\t")  # tab-delimited
            rows   = list(reader)                       # read all data rows
            self.assertEqual(len(rows), 2)              # two result rows (Google + Bing)
            self.assertIn("Search Engine Domain", reader.fieldnames)  # required column present
            self.assertIn("Search Keyword",       reader.fieldnames)  # required column present
            self.assertIn("Revenue",              reader.fieldnames)  # required column present

    def test_no_purchases_produces_empty_output(self) -> None:
        """
        A file with no purchase events (event_list never contains '1') should
        produce an empty results list, not an error.
        """
        filepath = os.path.join(self.temp_dir, "no_purchase.tsv")
        with open(filepath, "w") as f:
            # Minimal required header
            f.write("hit_time_gmt\tdate_time\tip\tevent_list\tproduct_list\treferrer\n")
            # Product view only (event 2) — not a purchase, revenue should not be recorded
            f.write("123\t2009-09-27\t1.2.3.4\t2\tElectronics;Ipod;1;;\thttp://www.google.com/search?q=test\n")
        analyzer = SearchKeywordAnalyzer(filepath)
        analyzer.process()           # DQ will warn (invalid timestamp "123") but not fail
        results = analyzer.get_results()
        self.assertEqual(len(results), 0)  # no purchase events → no revenue to attribute

    def test_file_not_found(self) -> None:
        """Passing a non-existent path to the constructor should raise FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            SearchKeywordAnalyzer("/nonexistent/path/file.tsv")  # should raise immediately


class TestWithProvidedDataFile(unittest.TestCase):
    """
    Integration tests that run against the actual ``data/data.sql`` sample.

    Skipped automatically when the file is not found so the test suite still
    passes in environments that only have the source code (e.g. fresh clones).

    Expected results (from the provided data.sql):
    - IP 67.98.123.1  → Google "Ipod"       → $290
    - IP 23.8.61.21   → Bing "Zune"         → $250
    - IP 44.12.96.2   → Google "ipod"       → $190
    - IP 112.33.98.231→ Yahoo "cd player"   → no purchase → $0
    """

    # Resolve path relative to this test file so it works from any working directory
    DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")

    def setUp(self) -> None:
        """Create a temp directory to hold the output file written during tests."""
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        """Remove temp directory after each test."""
        shutil.rmtree(self.temp_dir)

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")),
        "Provided data file not found in data/ directory"  # skip message shown in test output
    )
    def test_provided_data(self) -> None:
        """
        Verify the three expected revenue entries and total revenue against
        the provided sample file.
        """
        analyzer = SearchKeywordAnalyzer(self.DATA_FILE)
        analyzer.process()          # DQ + attribution against the real data
        results = analyzer.get_results()

        self.assertEqual(len(results), 3)  # three distinct (engine, keyword) pairs with revenue

        # Total revenue across all keywords should equal 290 + 250 + 190 = 730
        total_revenue = sum(r["Revenue"] for r in results)
        self.assertAlmostEqual(total_revenue, 730.0)

        # Highest-revenue entry should be $290 (Google/Ipod)
        self.assertEqual(results[0]["Revenue"], 290.0)

        # All three engines that generated revenue should be present
        engines = {r["Search Engine Domain"] for r in results}
        self.assertTrue(engines.issubset({"google.com", "bing.com", "search.yahoo.com"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)  # run with verbose output when executed directly
