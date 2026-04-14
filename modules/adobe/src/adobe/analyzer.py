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
        # row 2:  referrer="http://www.google.com/search?q=Ipod"  → domain="google.com", param="q" → keyword="Ipod"
        # row 5:  referrer="http://www.google.com/search?q=ipod"  → domain="google.com", param="q" → keyword="ipod"
        "google.com":        ["q"],
        # row 3:  referrer="http://www.bing.com/search?q=Zune"    → domain="bing.com",   param="q" → keyword="Zune"
        "bing.com":          ["q"],
        # row 4:  referrer="http://search.yahoo.com/search?p=cd+player" → domain="search.yahoo.com", param="p" → keyword="cd player"
        "search.yahoo.com":  ["p"],
        "yahoo.com":         ["p"],
        "duckduckgo.com":    ["q"],
        "ask.com":           ["q"],
        "aol.com":           ["q"],
        # ── Regional / alternative engines ────────────────────────────────────
        "baidu.com":         ["wd", "kw", "word"],   # Baidu uses multiple params — tries each in order
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
    #
    # Example — unknown engine, auto-captured:
    #   referrer = "https://newengine.com/find?q=headphones"
    #   "newengine.com" not in _DOMAIN_PARAMS → Path 2
    #   "q" IS in COMMON_SEARCH_PARAMS → returns ("newengine.com", "headphones")
    #
    # Example — internal checkout page, correctly ignored:
    #   referrer = "https://www.esshopzilla.com/checkout/?a=confirm"   (rows 16, 19, 22)
    #   "esshopzilla.com" not in _DOMAIN_PARAMS → Path 2
    #   query_params = {"a": ["confirm"]}
    #   "a" is NOT in COMMON_SEARCH_PARAMS → all params tried, none match → returns None
    #
    # Example — internal site search, correctly ignored:
    #   referrer = "http://www.esshopzilla.com/search/?k=Ipod"   (rows 6, 9, 12, 15)
    #   "esshopzilla.com" not in _DOMAIN_PARAMS → Path 2
    #   query_params = {"k": ["Ipod"]}
    #   "k" is NOT in COMMON_SEARCH_PARAMS → returns None  (last-touch Google attribution preserved)
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
    # Rows with event_list="1":  row 16 (23.8.61.21, Zune $250), row 19 (44.12.96.2, iPod Nano $190), row 22 (67.98.123.1, iPod Touch $290)
    # Rows with event_list="2":  rows 3, 8, 9, 15 — Product View, not a purchase
    # Rows with event_list="12": rows 7, 11, 18  — Cart Add, not a purchase
    # Rows with event_list="11": rows 10, 14, 20 — Checkout Start, not a purchase

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
            # Example: SearchKeywordAnalyzer("/tmp/missing.sql") → FileNotFoundError immediately
            # Example: SearchKeywordAnalyzer("/tmp/data.sql")    → continues normally

        self.input_file = input_file  # "/tmp/data.sql"

        # Keyed by IP address; value is the (engine_domain, keyword) tuple from the
        # most-recent external search referral for that visitor.
        # Populated during process().  Final state after all 21 rows:
        #   {
        #     "67.98.123.1": ("google.com", "Ipod"),  # row 2 google referral; row 6 internal → no overwrite
        #     "23.8.61.21":  ("bing.com",   "Zune"),  # row 3 bing referral
        #     "44.12.96.2":  ("google.com", "ipod"),  # row 5 google referral
        #   }
        self._visitor_search_attribution: Dict[str, Tuple[str, str]] = {}

        # Keyed by (engine_domain, keyword); value is accumulated revenue float.
        # defaultdict(float) initialises missing keys to 0.0 automatically — no KeyError on first add.
        # Final state after all 21 rows:
        #   {
        #     ("bing.com",   "Zune"): 250.0,  # row 16 purchase: 23.8.61.21 bought Zune $250
        #     ("google.com", "ipod"): 190.0,  # row 19 purchase: 44.12.96.2 bought iPod Nano $190
        #     ("google.com", "Ipod"): 290.0,  # row 22 purchase: 67.98.123.1 bought iPod Touch $290
        #   }
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
            # Example: row 13 (23.8.61.21 on Order Confirmation) has empty referrer → None

        try:
            parsed = urlparse(referrer_url)
            # Example: urlparse("http://www.google.com/search?hl=en&q=Ipod&aq=f")
            #   parsed.scheme   = "http"
            #   parsed.hostname = "www.google.com"
            #   parsed.path     = "/search"
            #   parsed.query    = "hl=en&q=Ipod&aq=f"
            referrer_domain = parsed.hostname   # "www.google.com" / "search.yahoo.com" / None for relative URLs
            if not referrer_domain:
                return None

            # Strip www. so "www.google.com" and "google.com" both map to "google.com".
            # removeprefix is exact string match — safe for all domains.
            # WRONG alternative: lstrip("www.") treats arg as char set → "wordpress.com" → "rdpress.com"
            clean_domain = referrer_domain.lower().removeprefix("www.")
            # "www.google.com"      → "google.com"
            # "www.bing.com"        → "bing.com"
            # "search.yahoo.com"    → "search.yahoo.com"   (no www. prefix, unchanged)
            # "www.esshopzilla.com" → "esshopzilla.com"

            query_params = parse_qs(parsed.query)
            # "hl=en&q=Ipod&aq=f"              → {"hl": ["en"], "q": ["Ipod"], "aq": ["f"]}
            # "q=Zune&go=&form=QBLH&qs=n"      → {"q": ["Zune"], "go": [""], "form": ["QBLH"], "qs": ["n"]}
            # "p=cd+player&toggle=1&fr=yfp-t"  → {"p": ["cd player"], "toggle": ["1"], "fr": ["yfp-t"]}
            # "a=confirm"                       → {"a": ["confirm"]}
            # "k=Ipod"                          → {"k": ["Ipod"]}

            # ── Path 1: Known engine — O(1) dict lookup ───────────────────────
            known_params = self._DOMAIN_PARAMS.get(clean_domain)
            # "google.com"      → ["q"]   (known engine, check param "q")
            # "bing.com"        → ["q"]   (known engine, check param "q")
            # "search.yahoo.com"→ ["p"]   (known engine, check param "p")
            # "esshopzilla.com" → None    (unknown — falls through to Path 2)

            if known_params is not None:
                for param in known_params:
                    if param in query_params:
                        keyword = unquote(query_params[param][0]).strip()
                        # unquote handles percent-encoding: "ipod%20nano" → "ipod nano"
                        # query_params["q"][0] = "Ipod"  → unquote("Ipod") = "Ipod"
                        # query_params["p"][0] = "cd player" (parse_qs already decoded +)
                        if keyword:
                            return (clean_domain, keyword)
                            # row 2 google referral  → ("google.com", "Ipod")
                            # row 3 bing referral    → ("bing.com",   "Zune")
                            # row 4 yahoo referral   → ("search.yahoo.com", "cd player")
                            # row 5 google referral  → ("google.com", "ipod")
                return None  # recognised engine but keyword param absent or empty

            # ── Path 2: Unknown engine — try common search params ─────────────
            # Domain not in _DOMAIN_PARAMS.  Try every param in COMMON_SEARCH_PARAMS.
            #
            # rows 6, 8–9, 11–12, 15, 17, 21 — referrer is esshopzilla.com internal page:
            #   query_params has "k" or "a" or no params at all
            #   none of those are in COMMON_SEARCH_PARAMS → returns None
            #   → 67.98.123.1 keeps its google/Ipod attribution from row 2
            #   → 44.12.96.2 keeps its google/ipod attribution from row 5
            for param in self.COMMON_SEARCH_PARAMS:
                if param in query_params:
                    keyword = unquote(query_params[param][0]).strip()
                    if keyword:
                        return (clean_domain, keyword)
                        # Example: "https://newengine.com/find?q=headphones"
                        #   → ("newengine.com", "headphones")  (auto-captured, no config change needed)

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
            # Example: rows 2, 4, 5, 6, 10, 11, 13, 14, 17, 20, 21 have no product_list → 0.0

        total_revenue = 0.0
        products = product_list.split(",")
        # Single product (all rows in this dataset):
        #   "Electronics;Zune - 32GB;1;250;"      → ["Electronics;Zune - 32GB;1;250;"]
        #   "Electronics;Ipod - Nano - 8GB;1;190;" → ["Electronics;Ipod - Nano - 8GB;1;190;"]
        # Hypothetical multi-product row:
        #   "Electronics;Zune;1;250;,Electronics;Ipod;1;190;" → two entries, revenue = 440.0

        for product in products:
            attrs = product.split(";")
            # "Electronics;Zune - 32GB;1;250;"
            #   → ["Electronics", "Zune - 32GB", "1", "250", ""]
            #      index 0          index 1          2     3     4
            #
            # "Electronics;Ipod - Nano - 8GB;1;190;"
            #   → ["Electronics", "Ipod - Nano - 8GB", "1", "190", ""]
            #
            # "Electronics;Zune - 328GB;1;;" (row 3 — add-to-cart, no revenue)
            #   → ["Electronics", "Zune - 328GB", "1", "", ""]
            #      attrs[3] = "" → falsy → skipped → 0.0
            if len(attrs) >= 4 and attrs[3].strip():  # index 3 = Revenue field, must be non-empty
                try:
                    total_revenue += float(attrs[3].strip())
                    # "250" → 250.0   "190" → 190.0   "290" → 290.0
                except ValueError:
                    logger.warning(f"Invalid revenue value in product_list: {attrs[3]}")

        return total_revenue
        # row 16: "Electronics;Zune - 32GB;1;250;"       → 250.0
        # row 19: "Electronics;Ipod - Nano - 8GB;1;190;" → 190.0
        # row 22: "Electronics;Ipod - Touch - 32GB;1;290;" → 290.0
        # row 3:  "Electronics;Zune - 328GB;1;;"          → 0.0  (no revenue, add-to-cart only)

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
        if not event_list or not event_list.strip():  # blank event_list = page view, no events
            return False
            # Example: rows 2, 4, 5, 6, 13, 17, 21 have empty event_list → False

        # Split on comma, compare each token exactly to "1".
        # any() short-circuits — stops at the first match.
        #
        # "1"   → ["1"]      → "1" == "1" → True   (rows 16, 19, 22 — purchase)
        # "12"  → ["12"]     → "12" == "1" → False  (rows 7, 11, 18 — Cart Add)
        # "11"  → ["11"]     → "11" == "1" → False  (rows 10, 14, 20 — Checkout Start)
        # "2"   → ["2"]      → "2" == "1"  → False  (rows 3, 8, 9, 15 — Product View)
        # "1,2" → ["1","2"]  → "1" == "1" → True  (short-circuits, never checks "2")
        #
        # WHY NOT use "1" in event_list?
        #   "1" in "10"  → True  ← WRONG, "10" is Cart Open, not a purchase
        #   "1" in "11"  → True  ← WRONG, "11" is Checkout Start
        #   "1" in "21"  → True  ← WRONG
        #   Exact token split prevents all these false positives.
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
            reader = csv.DictReader(f, delimiter="\t")
            # DictReader reads the header row first, then each data row becomes a dict.
            # Example — row 2 of data.sql becomes:
            # {
            #   "hit_time_gmt": "1254033280",
            #   "date_time":    "2009-09-27 06:34:40",
            #   "user_agent":   "Mozilla/5.0 (Windows; U; Windows NT 5.1 ...)",
            #   "ip":           "67.98.123.1",
            #   "event_list":   "",
            #   "geo_city":     "Salem",
            #   "geo_region":   "OR",
            #   "geo_country":  "US",
            #   "pagename":     "Home",
            #   "page_url":     "http://www.esshopzilla.com",
            #   "product_list": "",
            #   "referrer":     "http://www.google.com/search?hl=en&...&q=Ipod&..."
            # }

            for row in reader:
                row_count += 1  # 1-based; after full file: row_count = 21

                ip           = row.get("ip", "").strip()
                # row 2: "67.98.123.1"   row 3: "23.8.61.21"   row 5: "44.12.96.2"
                referrer     = row.get("referrer", "").strip()
                # row 2: "http://www.google.com/search?...&q=Ipod..."  (external search)
                # row 6: "http://www.esshopzilla.com"                  (internal — will be ignored)
                # row 8: "http://www.esshopzilla.com/hotbuys/"         (internal — will be ignored)
                event_list   = row.get("event_list", "").strip()
                # row 2: ""   row 7: "12"   row 16: "1"   row 19: "1"   row 22: "1"
                product_list = row.get("product_list", "").strip()
                # row 16: "Electronics;Zune - 32GB;1;250;"
                # row 19: "Electronics;Ipod - Nano - 8GB;1;190;"
                # row 22: "Electronics;Ipod - Touch - 32GB;1;290;"

                if not ip:  # rows without IP cannot be session-stitched — skip silently
                    continue

                # ── Step 1: Search engine referral detection ──────────────────
                search_info = self.parse_search_engine(referrer)
                # row 2 (67.98.123.1): search_info = ("google.com", "Ipod")   ← external Google
                # row 3 (23.8.61.21):  search_info = ("bing.com",   "Zune")   ← external Bing
                # row 5 (44.12.96.2):  search_info = ("google.com", "ipod")   ← external Google
                # row 6 (67.98.123.1): search_info = None  ← esshopzilla.com internal, no overwrite
                # row 8 (44.12.96.2):  search_info = None  ← esshopzilla/hotbuys internal, no overwrite
                # rows 9–15: all internal esshopzilla referrers → None → no attribution changes

                if search_info:
                    # Last-touch: overwrite any earlier search referral for this IP.
                    self._visitor_search_attribution[ip] = search_info
                    # After row 2:  {"67.98.123.1": ("google.com", "Ipod")}
                    # After row 3:  {"67.98.123.1": ("google.com", "Ipod"), "23.8.61.21": ("bing.com", "Zune")}
                    # After row 5:  {"67.98.123.1": ("google.com", "Ipod"), "23.8.61.21": ("bing.com", "Zune"), "44.12.96.2": ("google.com", "ipod")}
                    # rows 6–15: all None → dict unchanged

                # ── Step 2: Revenue attribution on purchase ───────────────────
                if self.is_purchase_event(event_list):
                    purchase_count += 1
                    # row 16: purchase_count=1   row 19: purchase_count=2   row 22: purchase_count=3

                    attribution = self._visitor_search_attribution.get(ip)
                    # row 16 (23.8.61.21): attribution = ("bing.com",   "Zune")
                    # row 19 (44.12.96.2): attribution = ("google.com", "ipod")
                    # row 22 (67.98.123.1): attribution = ("google.com", "Ipod")

                    if attribution:
                        revenue = self.parse_revenue(product_list)
                        # row 16: parse_revenue("Electronics;Zune - 32GB;1;250;")       = 250.0
                        # row 19: parse_revenue("Electronics;Ipod - Nano - 8GB;1;190;") = 190.0
                        # row 22: parse_revenue("Electronics;Ipod - Touch - 32GB;1;290;") = 290.0

                        if revenue > 0:
                            self._revenue_data[attribution] += revenue
                            # row 16: _revenue_data[("bing.com",   "Zune")] = 0.0 + 250.0 = 250.0
                            # row 19: _revenue_data[("google.com", "ipod")] = 0.0 + 190.0 = 190.0
                            # row 22: _revenue_data[("google.com", "Ipod")] = 0.0 + 290.0 = 290.0
                            logger.debug(
                                f"Revenue ${revenue:.2f} attributed to "
                                f"{attribution[0]} / '{attribution[1]}' (IP: {ip})"
                            )
                            # DEBUG: "Revenue $250.00 attributed to bing.com / 'Zune' (IP: 23.8.61.21)"
                            # DEBUG: "Revenue $190.00 attributed to google.com / 'ipod' (IP: 44.12.96.2)"
                            # DEBUG: "Revenue $290.00 attributed to google.com / 'Ipod' (IP: 67.98.123.1)"

        logger.info(
            f"Processed {row_count} rows, found {purchase_count} purchase events, "
            f"attributed revenue to {len(self._revenue_data)} keyword(s)"
        )
        # CloudWatch INFO: "Processed 21 rows, found 3 purchase events, attributed revenue to 3 keyword(s)"

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
                "Search Engine Domain": domain,          # "google.com" / "bing.com"
                "Search Keyword":       keyword,         # "Ipod" / "Zune" / "ipod"
                "Revenue":              round(revenue, 2),  # 290.0 / 250.0 / 190.0
            }
            for (domain, keyword), revenue in self._revenue_data.items()
            # Iteration order from defaultdict (insertion order in Python 3.7+):
            # ("bing.com","Zune",250), ("google.com","ipod",190), ("google.com","Ipod",290)
        ]
        results.sort(key=lambda x: x["Revenue"], reverse=True)
        # After sort (highest revenue first):
        # [
        #   {"Search Engine Domain": "google.com", "Search Keyword": "Ipod",  "Revenue": 290.0},
        #   {"Search Engine Domain": "bing.com",   "Search Keyword": "Zune",  "Revenue": 250.0},
        #   {"Search Engine Domain": "google.com", "Search Keyword": "ipod",  "Revenue": 190.0},
        # ]
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
        os.makedirs(output_dir, exist_ok=True)
        # "/tmp/output" created if it doesn't exist; no-op if it already exists (exist_ok=True)

        dt_str   = datetime.utcnow().strftime("%Y-%m-%d")  # e.g. "2026-04-13"
        filename = f"{dt_str}_SearchKeywordPerformance.tab"
        # filename = "2026-04-13_SearchKeywordPerformance.tab"
        output_path = os.path.join(output_dir, filename)
        # output_path = "/tmp/output/2026-04-13_SearchKeywordPerformance.tab"

        results = self.get_results()
        # [
        #   {"Search Engine Domain": "google.com", "Search Keyword": "Ipod",  "Revenue": 290.0},
        #   {"Search Engine Domain": "bing.com",   "Search Keyword": "Zune",  "Revenue": 250.0},
        #   {"Search Engine Domain": "google.com", "Search Keyword": "ipod",  "Revenue": 190.0},
        # ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["Search Engine Domain", "Search Keyword", "Revenue"],
                delimiter="\t",
            )
            writer.writeheader()
            # Writes line: "Search Engine Domain\tSearch Keyword\tRevenue\n"
            for row in results:
                writer.writerow({**row, "Revenue": f"{row['Revenue']:.2f}"})
                # f"{290.0:.2f}" = "290.00"  (ensures "290.00" not "290.0" in the file)
                # Line 1: "google.com\tIpod\t290.00\n"
                # Line 2: "bing.com\tZune\t250.00\n"
                # Line 3: "google.com\tipod\t190.00\n"

        logger.info(f"Output written to: {output_path} ({len(results)} rows)")
        # CloudWatch: "Output written to: /tmp/output/2026-04-13_SearchKeywordPerformance.tab (3 rows)"
        return output_path  # Lambda handler uses this to upload to S3 gold layer


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



