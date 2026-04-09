"""
Search Keyword Performance Analyzer

Processes Adobe Analytics hit-level data to determine revenue attribution
from external search engines (Google, Yahoo, Bing/MSN) and identifies
top-performing search keywords by revenue.

Author: Sarath
"""

import csv
import sys
import os
import logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from collections import defaultdict
from typing import Optional, Tuple, Dict, List

from shared.dq_checker import DataQualityChecker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class SearchKeywordAnalyzer:
    """
    Analyzes hit-level data to attribute revenue to external search engine keywords.

    The analyzer:
    1. Identifies visitors arriving from external search engines (Google, Yahoo, Bing/MSN)
    2. Extracts the search keyword from the referrer URL
    3. Tracks visitor sessions using IP as the visitor identifier
    4. Attributes purchase revenue to the originating search engine and keyword
    5. Outputs aggregated results sorted by revenue descending
    """

    # Mapping of search engine domains to their query parameter names
    SEARCH_ENGINES = {
        "google": {"domains": ["google.com", "www.google.com"], "query_params": ["q"]},
        "yahoo": {"domains": ["search.yahoo.com"], "query_params": ["p"]},
        "bing": {"domains": ["bing.com", "www.bing.com"], "query_params": ["q"]},
    }

    # Event ID that represents a purchase
    PURCHASE_EVENT = "1"

    def __init__(self, input_file: str):
        """
        Initialize the analyzer with the path to the hit-level data file.

        Args:
            input_file: Path to the tab-separated hit-level data file.

        Raises:
            FileNotFoundError: If the input file does not exist.
        """
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Input file not found: {input_file}")
        self.input_file = input_file
        # Tracks the most recent external search attribution per visitor (IP)
        # Format: {ip: (search_engine_domain, keyword)}
        self._visitor_search_attribution: Dict[str, Tuple[str, str]] = {}
        # Aggregated revenue per (search_engine_domain, keyword)
        self._revenue_data: Dict[Tuple[str, str], float] = defaultdict(float)

    def parse_search_engine(self, referrer_url: str) -> Optional[Tuple[str, str]]:
        """
        Parse a referrer URL to extract search engine domain and keyword.

        Checks if the referrer is from a known external search engine and
        extracts the search keyword from the query string.

        Args:
            referrer_url: The full referrer URL string.

        Returns:
            A tuple of (search_engine_domain, keyword) if the referrer is from
            a known search engine with a valid keyword, otherwise None.
        """
        if not referrer_url or not referrer_url.strip():
            return None

        try:
            parsed = urlparse(referrer_url)
            referrer_domain = parsed.hostname
            if not referrer_domain:
                return None

            # Remove 'www.' prefix for matching but preserve original for output
            clean_domain = referrer_domain.lower().removeprefix("www.")

            for engine_name, config in self.SEARCH_ENGINES.items():
                # Check if referrer domain matches any known search engine domain
                matched = any(
                    clean_domain == d.lower().removeprefix("www.")
                    for d in config["domains"]
                )
                if not matched:
                    continue

                # Extract keyword from query parameters
                query_params = parse_qs(parsed.query)
                for param_name in config["query_params"]:
                    if param_name in query_params:
                        keyword = unquote(query_params[param_name][0]).strip()
                        if keyword:
                            # Use clean domain format for output (e.g., "google.com")
                            output_domain = clean_domain
                            return (output_domain, keyword)

            return None

        except Exception as e:
            logger.warning(f"Failed to parse referrer URL: {referrer_url} — {e}")
            return None

    def parse_revenue(self, product_list: str) -> float:
        """
        Extract total revenue from a product_list string.

        Product list format (per Appendix B):
            Category;Product Name;Quantity;Revenue;Custom Event;Merch eVar
        Multiple products are comma-delimited.

        Revenue is only valid when the purchase event (1) is present in event_list.
        This method only extracts the revenue value; the caller is responsible for
        checking the event_list.

        Args:
            product_list: The raw product_list string from the hit data.

        Returns:
            The total revenue across all products in the list.
        """
        if not product_list or not product_list.strip():
            return 0.0

        total_revenue = 0.0
        # Products are comma-delimited
        products = product_list.split(",")

        for product in products:
            # Each product's attributes are semicolon-delimited
            attrs = product.split(";")
            # Revenue is the 4th field (index 3)
            if len(attrs) >= 4 and attrs[3].strip():
                try:
                    total_revenue += float(attrs[3].strip())
                except ValueError:
                    logger.warning(f"Invalid revenue value in product_list: {attrs[3]}")

        return total_revenue

    def is_purchase_event(self, event_list: str) -> bool:
        """
        Check if the event_list contains a purchase event (event ID = 1).

        Args:
            event_list: Comma-separated string of event IDs.

        Returns:
            True if a purchase event is present.
        """
        if not event_list or not event_list.strip():
            return False
        events = [e.strip() for e in event_list.split(",")]
        return self.PURCHASE_EVENT in events

    def run_dq_checks(self, fail_on_error: bool = True) -> "DQReport":  # noqa: F821
        """
        Run data quality checks on the input file.

        Args:
            fail_on_error: If True, raises ValueError when ERROR-level issues are found.

        Returns:
            DQReport with full details of all issues found.

        Raises:
            ValueError: If fail_on_error=True and ERROR-level issues exist.
        """
        checker = DataQualityChecker(self.input_file)
        report = checker.run()
        report.print_summary()
        if fail_on_error and not report.passed():
            raise ValueError(
                f"DQ checks failed for {self.input_file}: "
                f"{len(report.errors)} error(s) — see log for details."
            )
        return report

    def process(self, run_dq: bool = True) -> None:
        """
        Process the hit-level data file and build the revenue attribution.

        Reads the file line by line (memory-efficient for large files),
        tracks visitor search attribution by IP, and aggregates revenue
        for purchase events.

        Args:
            run_dq: If True (default), runs DQ checks before processing and
                    raises ValueError on ERROR-level issues.
        """
        if run_dq:
            self.run_dq_checks(fail_on_error=True)

        logger.info(f"Processing file: {self.input_file}")
        row_count = 0
        purchase_count = 0

        with open(self.input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")

            for row in reader:
                row_count += 1
                ip = row.get("ip", "").strip()
                referrer = row.get("referrer", "").strip()
                event_list = row.get("event_list", "").strip()
                product_list = row.get("product_list", "").strip()

                if not ip:
                    continue

                # Step 1: Check if this hit's referrer is from an external search engine
                search_info = self.parse_search_engine(referrer)
                if search_info:
                    # Update the visitor's search attribution (last external search wins)
                    self._visitor_search_attribution[ip] = search_info

                # Step 2: If this is a purchase event, attribute revenue to the search keyword
                if self.is_purchase_event(event_list):
                    purchase_count += 1
                    attribution = self._visitor_search_attribution.get(ip)

                    if attribution:
                        revenue = self.parse_revenue(product_list)
                        if revenue > 0:
                            self._revenue_data[attribution] += revenue
                            logger.debug(
                                f"Revenue ${revenue:.2f} attributed to "
                                f"{attribution[0]} / '{attribution[1]}' (IP: {ip})"
                            )

        logger.info(
            f"Processed {row_count} rows, found {purchase_count} purchase events, "
            f"attributed revenue to {len(self._revenue_data)} keyword(s)"
        )

    def get_results(self) -> List[Dict[str, object]]:
        """
        Return the aggregated results sorted by revenue descending.

        Returns:
            A list of dicts with keys: search_engine_domain, keyword, revenue.
        """
        results = [
            {
                "Search Engine Domain": domain,
                "Search Keyword": keyword,
                "Revenue": round(revenue, 2),
            }
            for (domain, keyword), revenue in self._revenue_data.items()
        ]
        results.sort(key=lambda x: x["Revenue"], reverse=True)
        return results

    def write_output(self, output_dir: str = "output") -> str:
        """
        Write the results to a tab-delimited file.

        File naming convention: YYYY-MM-DD_SearchKeywordPerformance.tab

        Args:
            output_dir: Directory to write the output file.

        Returns:
            The full path of the generated output file.
        """
        os.makedirs(output_dir, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}_SearchKeywordPerformance.tab"
        output_path = os.path.join(output_dir, filename)

        results = self.get_results()

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["Search Engine Domain", "Search Keyword", "Revenue"],
                delimiter="\t",
            )
            writer.writeheader()
            writer.writerows(results)

        logger.info(f"Output written to: {output_path} ({len(results)} rows)")
        return output_path


def main():
    """CLI entry point. Accepts a single argument: the input file path."""
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Analyze hit-level data to attribute search engine revenue "
            "and identify top-performing keywords."
        )
    )
    parser.add_argument(
        "input_file",
        help="Path to the tab-separated hit-level data file.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",
        help="Directory for the output file (default: output/).",
    )
    args = parser.parse_args()

    try:
        analyzer = SearchKeywordAnalyzer(args.input_file)
        analyzer.process()
        output_path = analyzer.write_output(args.output_dir)

        # Print results to console as well
        results = analyzer.get_results()
        print("\n=== Search Keyword Performance Report ===\n")
        print(f"{'Search Engine Domain':<25}{'Search Keyword':<20}{'Revenue':>10}")
        print("-" * 55)
        for r in results:
            print(f"{r['Search Engine Domain']:<25}{r['Search Keyword']:<20}{r['Revenue']:>10.2f}")
        print(f"\nOutput saved to: {output_path}")

    except FileNotFoundError as e:
        logger.error(e)
        sys.exit(1)
    except Exception as e:
        logger.exception(f"An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
