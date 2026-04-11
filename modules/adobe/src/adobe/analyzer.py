"""
Adobe Analytics — Search Keyword Performance Analyzer
======================================================
Pipeline-specific transformation module for the Adobe Analytics hit-level pipeline.
Processes hit-level TSV data to attribute e-commerce purchase revenue to the
external search engine and keyword that originally referred each visitor.

Search engine detection is **dynamic**: known engines (Google, Yahoo, Bing, etc.)
are resolved via an O(1) flat lookup table (``_DOMAIN_PARAMS``).  Referrers from
engines *not* in the lookup are detected automatically via ``COMMON_SEARCH_PARAMS``
— any referrer that carries a recognised keyword query parameter is captured and
its bare domain is used as the engine name.  This means new search engines are
picked up without any code changes.

Visitor identity is determined by IP address (the dataset contains no
cookie/visitor-ID column).  The last external search referral for an IP is
used when a purchase event is later recorded for that same IP.

Output: tab-delimited report sorted by revenue descending with columns:
    Search Engine Domain | Search Keyword | Revenue

Author: Sarath
"""

import csv                          # standard-library TSV/CSV reader and writer
import sys                          # used for sys.exit in the CLI entry point
import os                           # file-system operations (path checks, makedirs)
import logging                      # structured log output (INFO/WARN/DEBUG)
from datetime import datetime       # timestamp for output filename generation
from urllib.parse import urlparse, parse_qs, unquote  # URL decomposition utilities
from collections import defaultdict # auto-initialises missing dict keys to 0.0
from typing import Optional, Tuple, Dict, List  # type hints for IDE and readability

from shared.dq_checker import DataQualityChecker  # DQ gate runs before any processing

# ---------------------------------------------------------------------------
# Module-level logger — inherits root level set in basicConfig below
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,                                          # minimum log level to emit
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"  # include timestamp + module
)
logger = logging.getLogger(__name__)  # name = 'shared.search_keyword_analyzer'


class SearchKeywordAnalyzer:
    """
    Attributes purchase revenue to the external search keyword that referred
    each visitor.

    Processing pipeline
    -------------------
    1. (Optional) Run DataQualityChecker — abort on ERROR-level issues.
    2. Stream the input file row-by-row (O(1) memory relative to file size).
    3. For every row: if the referrer is a known search engine, record
       ``{ip: (engine_domain, keyword)}`` — last-touch attribution model.
    4. For every purchase event (event_list contains "1"): look up the stored
       attribution for that IP and add the row's revenue to the running total.
    5. Expose results sorted by revenue descending via ``get_results()``.
    6. Write a tab-delimited output file via ``write_output()``.

    Thread safety
    -------------
    Not thread-safe — ``_visitor_search_attribution`` and ``_revenue_data``
    are instance-level mutable state modified during ``process()``.
    """

    # Flat domain → keyword-param lookup.  All keys are clean (www. already stripped).
    # O(1) dict.get() replaces the former O(engines × domains) nested loop.
    #
    # To add a new engine: insert one line here — no other code change needed.
    # To override or extend at runtime: subclass and shadow this attribute, or
    # update it before calling process() via MyAnalyzer._DOMAIN_PARAMS["new.com"] = ["q"].
    _DOMAIN_PARAMS: Dict[str, List[str]] = {
        # ── Major western engines ──────────────────────────────────────────────
        "google.com":        ["q"],
        "bing.com":          ["q"],
        "search.yahoo.com":  ["p"],
        "yahoo.com":         ["p"],
        "duckduckgo.com":    ["q"],
        "ask.com":           ["q"],
        "aol.com":           ["q"],
        # ── Regional / alternative engines ────────────────────────────────────
        "baidu.com":         ["wd", "kw", "word"],   # Baidu uses multiple params
        "yandex.com":        ["text"],
        "yandex.ru":         ["text"],
        "ecosia.org":        ["q"],
        "startpage.com":     ["q"],
        "search.brave.com":  ["q"],
    }

    # Params commonly used by search engines as the keyword carrier.
    # Stored as a frozenset for O(1) membership tests.
    #
    # Used as a fallback: if the referrer domain is NOT in _DOMAIN_PARAMS, the
    # analyzer tries each of these params.  If a value is found, the referrer's
    # own bare domain is used as the engine name — so a brand-new engine is
    # captured automatically without updating _DOMAIN_PARAMS.
    COMMON_SEARCH_PARAMS: frozenset = frozenset([
        "q",       # Google, Bing, DuckDuckGo, Ecosia, Ask, ...
        "p",       # Yahoo
        "query",   # many generic search implementations
        "search",  # generic
        "wd",      # Baidu primary
        "text",    # Yandex
        "kw",      # Baidu alternate, various
        "keyword", # various
    ])

    PURCHASE_EVENT = "1"  # Adobe Analytics event ID that signals a completed purchase

    def __init__(self, input_file: str) -> None:
        """
        Initialise the analyzer with a path to the hit-level TSV data file.

        Args:
            input_file: Absolute or relative path to the tab-separated data file.

        Raises:
            FileNotFoundError: If ``input_file`` does not exist on disk.
        """
        if not os.path.exists(input_file):  # validate early so errors surface at construction time
            raise FileNotFoundError(f"Input file not found: {input_file}")

        self.input_file = input_file  # store path for later use in process() and run_dq_checks()

        # Keyed by IP address; value is the (engine_domain, keyword) tuple from the
        # most-recent external search referral for that visitor.
        self._visitor_search_attribution: Dict[str, Tuple[str, str]] = {}

        # Keyed by (engine_domain, keyword); value is accumulated revenue float.
        # defaultdict(float) initialises missing keys to 0.0 automatically.
        self._revenue_data: Dict[Tuple[str, str], float] = defaultdict(float)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_search_engine(self, referrer_url: str) -> Optional[Tuple[str, str]]:
        """
        Parse a referrer URL and return the search engine domain and keyword.

        Two-path matching
        -----------------
        **Path 1 — Known engine (O(1) lookup):**
        The cleaned referrer domain is looked up directly in ``_DOMAIN_PARAMS``.
        If found, only the engine-specific keyword params are tried.

        **Path 2 — Dynamic fallback:**
        If the domain is NOT in ``_DOMAIN_PARAMS``, any param in
        ``COMMON_SEARCH_PARAMS`` is tried.  This automatically captures new or
        niche search engines without requiring a config update — the referrer's
        own bare domain is used as the engine name in the output.

        In both paths ``www.`` is stripped with ``removeprefix`` (NOT ``lstrip``,
        which treats its argument as a character *set* and would corrupt domains
        like ``wordpress.com`` whose host starts with ``w``).

        Args:
            referrer_url: Raw referrer string from the hit data.

        Returns:
            ``(engine_domain, keyword)`` tuple when the referrer is a known or
            dynamically detected search engine with a non-empty keyword; ``None``
            otherwise.
        """
        if not referrer_url or not referrer_url.strip():  # skip blank/whitespace-only referrers
            return None

        try:
            parsed = urlparse(referrer_url)     # decompose URL into scheme/host/path/query
            referrer_domain = parsed.hostname   # hostname strips port; None for relative URLs
            if not referrer_domain:
                return None

            # Strip www. so "www.google.com" and "google.com" resolve to the same key.
            clean_domain = referrer_domain.lower().removeprefix("www.")

            # Parse query string once — reused by both paths below.
            query_params = parse_qs(parsed.query)  # {param: [values, ...]}

            # ── Path 1: Known engine — O(1) dict lookup ───────────────────────
            known_params = self._DOMAIN_PARAMS.get(clean_domain)
            if known_params is not None:
                for param in known_params:
                    if param in query_params:
                        keyword = unquote(query_params[param][0]).strip()
                        if keyword:
                            return (clean_domain, keyword)
                return None  # recognised engine but no usable keyword param

            # ── Path 2: Unknown engine — try common search params ─────────────
            # Any referrer that carries a standard keyword param is treated as a
            # search engine.  Using the referrer's own domain as the output key
            # means new engines are captured automatically.
            for param in self.COMMON_SEARCH_PARAMS:
                if param in query_params:
                    keyword = unquote(query_params[param][0]).strip()
                    if keyword:
                        return (clean_domain, keyword)

            return None  # no search keyword found in this referrer

        except Exception as e:  # catch malformed URLs that urlparse cannot handle
            logger.warning(f"Failed to parse referrer URL: {referrer_url} — {e}")
            return None

    def parse_revenue(self, product_list: str) -> float:
        """
        Sum the revenue field across all products in a ``product_list`` string.

        Product list format (Appendix B)
        ---------------------------------
        Each product is semicolon-delimited with fields:
            ``Category;Product Name;Quantity;Revenue;Custom Event;Merch eVar``
        Multiple products are comma-delimited.

        Revenue is only monetised when event ID 1 is present in ``event_list``.
        This method only extracts the numeric value; the caller must check the
        event list separately.

        Args:
            product_list: Raw ``product_list`` column value from the hit data.

        Returns:
            Sum of all revenue fields as a float; 0.0 if the list is empty or
            no revenue fields are populated.
        """
        if not product_list or not product_list.strip():  # empty / whitespace-only → no revenue
            return 0.0

        total_revenue = 0.0              # running sum across all products in the row
        products = product_list.split(",")  # comma separates individual product entries

        for product in products:              # iterate each product entry
            attrs = product.split(";")        # semicolon separates the six attribute fields
            # Revenue is at index 3; guard against malformed entries with fewer fields.
            if len(attrs) >= 4 and attrs[3].strip():  # field exists and is non-empty
                try:
                    total_revenue += float(attrs[3].strip())  # accumulate numeric revenue
                except ValueError:  # non-numeric revenue field — log and skip
                    logger.warning(f"Invalid revenue value in product_list: {attrs[3]}")

        return total_revenue  # caller decides whether to apply this based on event_list

    def is_purchase_event(self, event_list: str) -> bool:
        """
        Return ``True`` if ``event_list`` contains the purchase event ID (``"1"``).

        The check splits on commas and compares each token exactly, so event
        ``"10"`` (Cart Open) does NOT trigger a false positive.

        Args:
            event_list: Comma-separated string of Adobe Analytics event IDs.

        Returns:
            ``True`` when event ID ``"1"`` is present; ``False`` otherwise.
        """
        if not event_list or not event_list.strip():  # blank event_list means no events fired
            return False
        # any() short-circuits on the first match — avoids building a full list in memory.
        # Exact token comparison ensures "10" (Cart Open) does NOT match "1" (Purchase).
        return any(e.strip() == self.PURCHASE_EVENT for e in event_list.split(","))

    def run_dq_checks(self, fail_on_error: bool = True) -> "DQReport":  # noqa: F821
        """
        Run the full DataQualityChecker suite against the input file.

        This is called automatically by ``process()`` unless ``run_dq=False`` is
        passed.  Call it directly when you need the report object without
        triggering the full processing pipeline.

        Args:
            fail_on_error: When ``True`` (default), raise ``ValueError`` if any
                ERROR-level DQ issue is found.  Set to ``False`` to surface the
                report without aborting.

        Returns:
            A ``DQReport`` instance with all issues categorised by severity.

        Raises:
            ValueError: If ``fail_on_error=True`` and ERROR-level issues exist.
        """
        checker = DataQualityChecker(self.input_file)  # create checker for this file
        report = checker.run()                          # execute all 10+ checks
        report.print_summary()                          # log summary line + all issues

        if fail_on_error and not report.passed():  # ERROR-level issues mean file cannot be trusted
            raise ValueError(
                f"DQ checks failed for {self.input_file}: "
                f"{len(report.errors)} error(s) — see log for details."
            )
        return report  # caller can inspect warnings/infos even when passed=True

    def process(self, run_dq: bool = True) -> None:
        """
        Stream the input file and build the revenue attribution table.

        Memory usage is O(unique IPs) because only the per-IP attribution dict
        and the per-(engine, keyword) revenue dict are held in memory — the file
        itself is never loaded in full.

        Args:
            run_dq: Run DQ checks before processing (default ``True``).
                    Set to ``False`` when the caller has already validated the
                    file (e.g. the Lambda handler runs DQ explicitly before
                    calling ``process``).

        Raises:
            ValueError: If ``run_dq=True`` and the file has ERROR-level DQ issues.
        """
        if run_dq:  # skip DQ if the caller already validated (avoids double-pass over the file)
            self.run_dq_checks(fail_on_error=True)

        logger.info(f"Processing file: {self.input_file}")
        row_count = 0       # total rows read (for progress logging)
        purchase_count = 0  # rows where event_list contains event "1"

        with open(self.input_file, "r", encoding="utf-8") as f:
            # DictReader maps each row to {column_name: value} using the header row.
            reader = csv.DictReader(f, delimiter="\t")  # TSV format — tab delimiter

            for row in reader:
                row_count += 1  # count every data row (excluding header)

                # Extract and normalise key fields; default to empty string if column absent.
                ip           = row.get("ip", "").strip()           # visitor identifier
                referrer     = row.get("referrer", "").strip()     # previous page URL
                event_list   = row.get("event_list", "").strip()   # fired event IDs
                product_list = row.get("product_list", "").strip() # products interacted with

                if not ip:  # rows without an IP cannot be linked to a session — skip silently
                    continue

                # ── Step 1: Search engine referral detection ──────────────────
                # If this hit's referrer is a recognised search engine, update
                # the attribution for this visitor (last-touch model).
                search_info = self.parse_search_engine(referrer)
                if search_info:  # referrer resolved to (engine_domain, keyword)
                    # Overwrite any earlier search attribution for this IP.
                    self._visitor_search_attribution[ip] = search_info

                # ── Step 2: Revenue attribution on purchase ───────────────────
                # Purchase events trigger revenue attribution only if the visitor
                # previously arrived via a tracked search engine.
                if self.is_purchase_event(event_list):
                    purchase_count += 1  # track how many purchase rows we saw
                    attribution = self._visitor_search_attribution.get(ip)  # may be None

                    if attribution:  # visitor had a prior search referral we can attribute to
                        revenue = self.parse_revenue(product_list)  # extract revenue from product list
                        if revenue > 0:  # only record rows with actual revenue (not product views)
                            self._revenue_data[attribution] += revenue  # accumulate per (engine, kw)
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
        Return the aggregated revenue results sorted by revenue descending.

        Each entry represents one unique (search engine, keyword) pair.

        Returns:
            List of dicts with keys:
                - ``"Search Engine Domain"`` — normalised engine hostname (e.g. ``"google.com"``)
                - ``"Search Keyword"``        — raw keyword string (case-sensitive)
                - ``"Revenue"``               — total attributed revenue, rounded to 2 dp
        """
        results = [
            {
                "Search Engine Domain": domain,   # e.g. "google.com"
                "Search Keyword":       keyword,  # e.g. "Ipod"
                "Revenue":              round(revenue, 2),  # round to cents
            }
            for (domain, keyword), revenue in self._revenue_data.items()  # unpack composite key
        ]
        results.sort(key=lambda x: x["Revenue"], reverse=True)  # highest revenue first
        return results

    def write_output(self, output_dir: str = "output") -> str:
        """
        Write the aggregated results to a tab-delimited ``.tab`` file.

        File naming convention: ``YYYY-mm-dd_SearchKeywordPerformance.tab``

        Date format matches the assessment spec (e.g. ``2009-10-08``).
        The Lambda insert-overwrite strategy deletes the existing partition
        objects before writing, so same-day reruns replace rather than append.

        Args:
            output_dir: Directory to create the output file in.
                        Created automatically if it does not exist.

        Returns:
            Absolute path of the written output file.
        """
        os.makedirs(output_dir, exist_ok=True)  # create output directory if missing

        # Spec requirement: [Date]_SearchKeywordPerformance.tab, format YYYY-mm-dd.
        dt_str   = datetime.utcnow().strftime("%Y-%m-%d")  # UTC date of execution
        filename = f"{dt_str}_SearchKeywordPerformance.tab"
        output_path = os.path.join(output_dir, filename)       # full path including directory

        results = self.get_results()  # fetch sorted results before opening the file

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["Search Engine Domain", "Search Keyword", "Revenue"],  # fixed column order
                delimiter="\t",  # tab-delimited to match the input format convention
            )
            writer.writeheader()  # write column names as first line
            # Format Revenue as a fixed 2-decimal string so the file is consistent
            # (float repr "290.0" vs "290.00") and Athena DOUBLE parsing is reliable.
            for row in results:
                writer.writerow({**row, "Revenue": f"{row['Revenue']:.2f}"})

        logger.info(f"Output written to: {output_path} ({len(results)} rows)")
        return output_path  # return path so callers (Lambda, CLI) can reference or upload it


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Command-line interface for the Search Keyword Analyzer.

    Usage::

        PYTHONPATH=src python src/shared/search_keyword_analyzer.py <input_file> [-o <output_dir>]

    Exits with status 1 on any error so the caller (CI step, shell script)
    can detect failure.
    """
    import argparse  # imported here so the module can be imported without argparse overhead

    parser = argparse.ArgumentParser(
        description=(
            "Analyze hit-level data to attribute search engine revenue "
            "and identify top-performing keywords."
        )
    )
    parser.add_argument(
        "input_file",
        help="Path to the tab-separated hit-level data file.",  # positional — required
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",  # default matches the gitignored output/ directory
        help="Directory for the output file (default: output/).",
    )
    args = parser.parse_args()  # parse sys.argv

    try:
        analyzer = SearchKeywordAnalyzer(args.input_file)  # raises FileNotFoundError if missing
        analyzer.process()                                  # DQ check + attribution
        output_path = analyzer.write_output(args.output_dir)  # write tab file

        # Print a human-readable table to stdout for quick visual inspection.
        results = analyzer.get_results()
        print("\n=== Search Keyword Performance Report ===\n")
        print(f"{'Search Engine Domain':<25}{'Search Keyword':<20}{'Revenue':>10}")
        print("-" * 55)  # visual separator line
        for r in results:
            print(f"{r['Search Engine Domain']:<25}{r['Search Keyword']:<20}{r['Revenue']:>10.2f}")
        print(f"\nOutput saved to: {output_path}")

    except FileNotFoundError as e:   # input file does not exist
        logger.error(e)
        sys.exit(1)
    except Exception as e:           # unexpected error — log full traceback
        logger.exception(f"An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()  # allow running the module directly: python search_keyword_analyzer.py data/data.sql
