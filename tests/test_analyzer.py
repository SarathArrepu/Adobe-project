"""
Unit tests for SearchKeywordAnalyzer.

Tests cover:
- Referrer URL parsing (search engine detection + keyword extraction)
- Product list revenue extraction
- Purchase event detection
- End-to-end processing with the sample data
"""

import os
import sys
import csv
import unittest
import tempfile
import shutil

# Add src to path so shared/ and pipelines/ packages are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared.search_keyword_analyzer import SearchKeywordAnalyzer


class TestParseSearchEngine(unittest.TestCase):
    """Test referrer URL parsing for search engine detection and keyword extraction."""

    def setUp(self):
        """Create a temporary file so we can instantiate the analyzer."""
        self.temp_dir = tempfile.mkdtemp()
        self.dummy_file = os.path.join(self.temp_dir, "dummy.tsv")
        with open(self.dummy_file, "w") as f:
            f.write("hit_time_gmt\tdate_time\tip\treferrer\tevent_list\tproduct_list\n")
        self.analyzer = SearchKeywordAnalyzer(self.dummy_file)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_google_referrer(self):
        url = "http://www.google.com/search?hl=en&client=firefox-a&q=Ipod&aq=f"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "google.com")
        self.assertEqual(result[1], "Ipod")

    def test_yahoo_referrer(self):
        url = "http://search.yahoo.com/search?p=cd+player&toggle=1&cop=mss&ei=UTF-8"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "search.yahoo.com")
        self.assertEqual(result[1], "cd player")

    def test_bing_referrer(self):
        url = "http://www.bing.com/search?q=Zune&go=&form=QBLH&qs=n"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "bing.com")
        self.assertEqual(result[1], "Zune")

    def test_non_search_referrer(self):
        url = "http://www.esshopzilla.com/product/?pid=as32213"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNone(result)

    def test_empty_referrer(self):
        self.assertIsNone(self.analyzer.parse_search_engine(""))
        self.assertIsNone(self.analyzer.parse_search_engine(None))
        self.assertIsNone(self.analyzer.parse_search_engine("   "))

    def test_google_without_keyword(self):
        url = "http://www.google.com/search?hl=en"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNone(result)

    def test_encoded_keyword(self):
        url = "http://www.google.com/search?q=ipod%20nano%20case"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "ipod nano case")

    def test_yahoo_plus_encoded_spaces(self):
        url = "http://search.yahoo.com/search?p=cd+player+portable"
        result = self.analyzer.parse_search_engine(url)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "cd player portable")


class TestParseRevenue(unittest.TestCase):
    """Test product_list revenue extraction."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.dummy_file = os.path.join(self.temp_dir, "dummy.tsv")
        with open(self.dummy_file, "w") as f:
            f.write("hit_time_gmt\tdate_time\tip\treferrer\tevent_list\tproduct_list\n")
        self.analyzer = SearchKeywordAnalyzer(self.dummy_file)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_single_product_with_revenue(self):
        product_list = "Electronics;Zune - 32GB;1;250;"
        self.assertEqual(self.analyzer.parse_revenue(product_list), 250.0)

    def test_single_product_decimal_revenue(self):
        product_list = "Electronics;Ipod - Nano - 8GB;1;189.99;"
        self.assertAlmostEqual(self.analyzer.parse_revenue(product_list), 189.99)

    def test_multiple_products(self):
        product_list = "Electronics;Ipod;1;200;,Accessories;Case;2;29.99;"
        self.assertAlmostEqual(self.analyzer.parse_revenue(product_list), 229.99)

    def test_empty_product_list(self):
        self.assertEqual(self.analyzer.parse_revenue(""), 0.0)
        self.assertEqual(self.analyzer.parse_revenue(None), 0.0)

    def test_product_list_no_revenue(self):
        product_list = "Electronics;Zune - 32GB;1;;"
        self.assertEqual(self.analyzer.parse_revenue(product_list), 0.0)

    def test_product_view_no_revenue_field(self):
        # Product views often have no revenue value
        product_list = "Electronics;Ipod - Nano - 8GB;1;;"
        self.assertEqual(self.analyzer.parse_revenue(product_list), 0.0)


class TestIsPurchaseEvent(unittest.TestCase):
    """Test purchase event detection in event_list."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.dummy_file = os.path.join(self.temp_dir, "dummy.tsv")
        with open(self.dummy_file, "w") as f:
            f.write("hit_time_gmt\tdate_time\tip\treferrer\tevent_list\tproduct_list\n")
        self.analyzer = SearchKeywordAnalyzer(self.dummy_file)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_purchase_event(self):
        self.assertTrue(self.analyzer.is_purchase_event("1"))

    def test_purchase_with_other_events(self):
        self.assertTrue(self.analyzer.is_purchase_event("1,200,201"))

    def test_product_view_only(self):
        self.assertFalse(self.analyzer.is_purchase_event("2"))

    def test_cart_events_not_purchase(self):
        self.assertFalse(self.analyzer.is_purchase_event("12"))
        self.assertFalse(self.analyzer.is_purchase_event("11"))

    def test_empty_event_list(self):
        self.assertFalse(self.analyzer.is_purchase_event(""))
        self.assertFalse(self.analyzer.is_purchase_event(None))

    def test_event_10_not_confused_with_1(self):
        """Event 10 (Cart Open) should NOT match purchase event 1."""
        self.assertFalse(self.analyzer.is_purchase_event("10"))

    def test_event_1_in_middle(self):
        self.assertTrue(self.analyzer.is_purchase_event("12,1,200"))


class TestEndToEnd(unittest.TestCase):
    """End-to-end test using the provided sample data."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.output_dir = os.path.join(self.temp_dir, "output")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _create_sample_data(self) -> str:
        """Create a minimal test data file mimicking the provided sample."""
        filepath = os.path.join(self.temp_dir, "test_data.tsv")
        rows = [
            # Header
            "hit_time_gmt\tdate_time\tuser_agent\tip\tevent_list\tgeo_city\tgeo_region\tgeo_country\tpagename\tpage_url\tproduct_list\treferrer",
            # Visitor A arrives from Google searching "Ipod"
            "1254033280\t2009-09-27 06:34:40\tMozilla\t67.98.123.1\t\tSalem\tOR\tUS\tHome\thttp://www.esshopzilla.com\t\thttp://www.google.com/search?q=Ipod",
            # Visitor A browses internally, then purchases Ipod Touch 32GB for $290
            "1254034567\t2009-09-27 06:56:07\tMozilla\t67.98.123.1\t2\tSalem\tOR\tUS\tIpod - Touch\thttp://www.esshopzilla.com/product/\tElectronics;Ipod - Touch - 32GB;1;;\thttp://www.esshopzilla.com/search/",
            "1254035260\t2009-09-27 07:07:40\tMozilla\t67.98.123.1\t1\tSalem\tOR\tUS\tOrder Complete\thttps://www.esshopzilla.com/checkout/\tElectronics;Ipod - Touch - 32GB;1;290;\thttps://www.esshopzilla.com/checkout/?a=confirm",
            # Visitor B arrives from Bing searching "Zune", purchases for $250
            "1254033379\t2009-09-27 06:36:19\tSafari\t23.8.61.21\t2\tRochester\tNY\tUS\tZune\thttp://www.esshopzilla.com/product/\tElectronics;Zune - 32GB;1;;\thttp://www.bing.com/search?q=Zune",
            "1254034666\t2009-09-27 06:57:46\tSafari\t23.8.61.21\t1\tRochester\tNY\tUS\tOrder Complete\thttps://www.esshopzilla.com/checkout/\tElectronics;Zune - 32GB;1;250;\thttps://www.esshopzilla.com/checkout/?a=confirm",
            # Visitor C arrives from Yahoo searching "cd player" — no purchase
            "1254033478\t2009-09-27 06:37:58\tSafari\t112.33.98.231\t\tSLC\tUT\tUS\tHome\thttp://www.esshopzilla.com\t\thttp://search.yahoo.com/search?p=cd+player",
        ]
        with open(filepath, "w") as f:
            f.write("\n".join(rows))
        return filepath

    def test_full_pipeline(self):
        filepath = self._create_sample_data()
        analyzer = SearchKeywordAnalyzer(filepath)
        analyzer.process()
        results = analyzer.get_results()

        # Should have 2 keyword entries (Google/Ipod and Bing/Zune)
        self.assertEqual(len(results), 2)

        # First result should be Google/Ipod with $290 (highest revenue)
        self.assertEqual(results[0]["Search Engine Domain"], "google.com")
        self.assertEqual(results[0]["Search Keyword"], "Ipod")
        self.assertEqual(results[0]["Revenue"], 290.0)

        # Second result should be Bing/Zune with $250
        self.assertEqual(results[1]["Search Engine Domain"], "bing.com")
        self.assertEqual(results[1]["Search Keyword"], "Zune")
        self.assertEqual(results[1]["Revenue"], 250.0)

    def test_output_file_created(self):
        filepath = self._create_sample_data()
        analyzer = SearchKeywordAnalyzer(filepath)
        analyzer.process()
        output_path = analyzer.write_output(self.output_dir)

        self.assertTrue(os.path.exists(output_path))
        self.assertTrue(output_path.endswith("_SearchKeywordPerformance.tab"))

        # Verify the output is tab-delimited with correct headers
        with open(output_path, "r") as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertIn("Search Engine Domain", reader.fieldnames)
            self.assertIn("Search Keyword", reader.fieldnames)
            self.assertIn("Revenue", reader.fieldnames)

    def test_no_purchases_produces_empty_output(self):
        filepath = os.path.join(self.temp_dir, "no_purchase.tsv")
        with open(filepath, "w") as f:
            f.write("hit_time_gmt\tdate_time\tip\tevent_list\tproduct_list\treferrer\n")
            f.write("123\t2009-09-27\t1.2.3.4\t2\tElectronics;Ipod;1;;\thttp://www.google.com/search?q=test\n")
        analyzer = SearchKeywordAnalyzer(filepath)
        analyzer.process()
        results = analyzer.get_results()
        self.assertEqual(len(results), 0)

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            SearchKeywordAnalyzer("/nonexistent/path/file.tsv")


class TestWithProvidedDataFile(unittest.TestCase):
    """Test with the actual provided data.sql file to verify expected output."""

    DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "data.sql")),
        "Provided data file not found in data/ directory"
    )
    def test_provided_data(self):
        """
        Expected results from the provided data:

        Visitors and their journeys:
        - IP 67.98.123.1  → Google, keyword "Ipod"  → Purchased Ipod Touch 32GB → $290
        - IP 23.8.61.21   → Bing, keyword "Zune"    → Purchased Zune 32GB      → $250
        - IP 44.12.96.2   → Google, keyword "ipod"   → Purchased Ipod Nano 8GB  → $190
        - IP 112.33.98.231→ Yahoo, keyword "cd player"→ No purchase              → $0
        """
        analyzer = SearchKeywordAnalyzer(self.DATA_FILE)
        analyzer.process()
        results = analyzer.get_results()

        self.assertEqual(len(results), 3)

        # Verify total revenue = 290 + 250 + 190 = 730
        total_revenue = sum(r["Revenue"] for r in results)
        self.assertAlmostEqual(total_revenue, 730.0)

        # First result (highest revenue): Google / Ipod = $290
        self.assertEqual(results[0]["Revenue"], 290.0)

        # Verify all three search engines are represented
        engines = {r["Search Engine Domain"] for r in results}
        self.assertTrue(engines.issubset({"google.com", "bing.com", "search.yahoo.com"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
