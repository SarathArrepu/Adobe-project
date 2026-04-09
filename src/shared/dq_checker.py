"""
Data Quality Checker for Adobe Analytics hit-level TSV files
=============================================================
Validates input data **before** the SearchKeywordAnalyzer processes it,
surfacing issues that would otherwise silently corrupt or drop revenue
attribution results.

Severity levels
---------------
ERROR  — the file cannot be reliably processed (missing required columns,
         empty file).  Pipeline must abort.
WARN   — row-level issues that cause silent data loss or revenue
         misattribution.  Pipeline may continue but the affected rows will
         be skipped or produce incorrect results.
INFO   — noteworthy patterns that do not affect pipeline correctness but
         may indicate upstream data issues worth investigating.

Usage
-----
::

    checker = DataQualityChecker("data/data.sql")
    report  = checker.run()
    report.print_summary()
    if not report.passed():
        raise ValueError("DQ checks failed — aborting pipeline")
"""

import csv                        # standard-library TSV reader
import logging                    # structured log output
import re                         # regular expression for IPv4 validation
from dataclasses import dataclass, field  # lightweight immutable-ish data containers
from typing import List, Optional, Set, Tuple  # type hints for IDE support

logger = logging.getLogger(__name__)  # name = 'shared.dq_checker'

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

# Columns the analyzer reads directly — absent → pipeline cannot run (ERROR).
REQUIRED_COLUMNS = {
    "hit_time_gmt",   # Unix timestamp; used for duplicate-hit detection
    "ip",             # visitor identifier; required for session stitching
    "event_list",     # comma-separated event IDs; "1" signals a purchase
    "product_list",   # semicolon-delimited product attributes including revenue
    "referrer",       # previous page URL; parsed to detect search engine source
}

# Full schema from Appendix A — includes optional enrichment columns.
# Missing optional columns produce a WARN (not ERROR) so the pipeline continues.
APPENDIX_A_COLUMNS = {
    "hit_time_gmt", "date_time", "user_agent", "ip",
    "geo_city", "geo_country", "geo_region", "pagename",
    "page_url", "product_list", "referrer", "event_list",
}

# Event IDs documented in Appendix A as valid for this dataset.
# Any other ID is flagged at INFO level (not an error — may be valid custom events).
VALID_EVENT_IDS = {"1", "2", "10", "11", "12", "13", "14"}

# Rough valid Unix timestamp range covering years 2000–2100.
# Timestamps outside this window are almost certainly corrupt or test data.
_TS_MIN = 946_684_800   # 2000-01-01 00:00:00 UTC
_TS_MAX = 4_102_444_800  # 2100-01-01 00:00:00 UTC

# Compiled regex for IPv4 address validation — matches exactly four octets.
# Groups capture each octet so we can verify the 0–255 range separately.
_IPV4_RE = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$"
)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class DQIssue:
    """
    A single data-quality finding from one check.

    Attributes:
        severity: One of ``"ERROR"``, ``"WARN"``, or ``"INFO"``.
        check:    Short constant name identifying the check, e.g.
                  ``"MISSING_IP"``.  Use this for programmatic filtering.
        row:      1-based data row number where the issue was found.
                  ``None`` for file-level issues (column checks, empty file).
        detail:   Human-readable description of the specific issue found.
    """
    severity: str        # "ERROR", "WARN", or "INFO"
    check:    str        # short check name used for programmatic filtering
    row:      Optional[int]  # 1-based row number; None for file-level issues
    detail:   str        # human-readable description

    def __str__(self) -> str:
        """Return a compact one-line string representation for log output."""
        location = f"row {self.row}" if self.row is not None else "file"  # context label
        return f"[{self.severity}] {self.check} ({location}): {self.detail}"


@dataclass
class DQReport:
    """
    Aggregated results from a full DataQualityChecker run.

    Attributes:
        input_file:  Path to the file that was checked.
        total_rows:  Number of data rows read (excludes the header row).
        issues:      All issues found, in order of discovery.
    """
    input_file: str                          # path to the checked file
    total_rows: int = 0                      # populated after full file scan
    issues: List[DQIssue] = field(default_factory=list)  # accumulates all findings

    # ------------------------------------------------------------------
    # Convenience filters
    # ------------------------------------------------------------------

    @property
    def errors(self) -> List[DQIssue]:
        """Return only ERROR-severity issues (pipeline-blocking)."""
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> List[DQIssue]:
        """Return only WARN-severity issues (data-loss risk)."""
        return [i for i in self.issues if i.severity == "WARN"]

    @property
    def infos(self) -> List[DQIssue]:
        """Return only INFO-severity issues (noteworthy patterns)."""
        return [i for i in self.issues if i.severity == "INFO"]

    def passed(self, fail_on_error: bool = True) -> bool:
        """
        Return ``True`` when the file is safe to process.

        Args:
            fail_on_error: When ``True`` (default) the file only passes if
                           there are zero ERROR-level issues.  WARNs and INFOs
                           do not cause failure.

        Returns:
            ``True`` if the file can be processed; ``False`` otherwise.
        """
        return len(self.errors) == 0 if fail_on_error else True  # WARNs never block

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        """
        Emit a one-line DQ summary followed by each issue to the logger.

        Uses ERROR log level for ERROR issues, WARNING for WARN issues, and
        INFO for INFO issues so CloudWatch / log aggregators can filter by
        severity.
        """
        status = "PASSED" if self.passed() else "FAILED"  # top-level pass/fail label
        logger.info(
            f"DQ Report [{status}] — {self.input_file} | "
            f"{self.total_rows} rows | "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings, {len(self.infos)} info"
        )
        for issue in self.issues:  # emit each finding at its appropriate log level
            level = logging.ERROR if issue.severity == "ERROR" else (
                logging.WARNING if issue.severity == "WARN" else logging.INFO
            )
            logger.log(level, str(issue))  # format via DQIssue.__str__

    def as_dict(self) -> dict:
        """
        Serialise the report to a plain dict suitable for JSON responses.

        Returns:
            Dict with summary counts and a full ``"issues"`` list.
        """
        return {
            "input_file": self.input_file,      # path for traceability
            "total_rows": self.total_rows,       # rows scanned
            "passed":     self.passed(),         # boolean pass/fail
            "errors":     len(self.errors),      # count of ERROR issues
            "warnings":   len(self.warnings),    # count of WARN issues
            "infos":      len(self.infos),       # count of INFO issues
            "issues": [
                {
                    "severity": i.severity,  # "ERROR" / "WARN" / "INFO"
                    "check":    i.check,     # short check name
                    "row":      i.row,       # None for file-level issues
                    "detail":   i.detail,    # human-readable description
                }
                for i in self.issues
            ],
        }


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class DataQualityChecker:
    """
    Runs a suite of data-quality checks on a hit-level TSV file and returns
    a ``DQReport`` with all findings categorised by severity.

    Checks performed
    ----------------
    File-level (ERROR — abort if triggered):
      MISSING_REQUIRED_COLUMNS    Required columns absent from the header row
      EMPTY_FILE                  Header present but no data rows found

    File-level (WARN — optional columns only):
      MISSING_APPENDIX_A_COLUMNS  Optional Appendix A columns absent; enrichment
                                  fields will be missing but pipeline can continue

    Row-level (WARN — silent data-loss risk):
      MISSING_IP                  ip column is empty — row cannot be session-stitched
      INVALID_HIT_TIME            hit_time_gmt is not a valid Unix timestamp
      INVALID_IP_FORMAT           ip is not a valid IPv4 address
      DUPLICATE_HIT               Same (hit_time_gmt, ip) pair seen more than once
      PURCHASE_NO_PRODUCT         event_list has event "1" but product_list is empty
      PRODUCT_REVENUE_NO_PURCHASE product_list has revenue > 0 but no purchase event
      NEGATIVE_REVENUE            Revenue field is negative
      MALFORMED_PRODUCT_LIST      product_list present but cannot be parsed

    Row-level (INFO — noteworthy, no correctness impact):
      UNKNOWN_EVENT_ID            event_list contains an unrecognised event ID
    """

    def __init__(self, input_file: str) -> None:
        """
        Initialise the checker with the path to the file to validate.

        Args:
            input_file: Path to the tab-separated hit-level data file.
        """
        self.input_file = input_file  # store for use in run()

    def run(self) -> DQReport:
        """
        Execute all checks and return the populated ``DQReport``.

        The method streams the file once — both file-level and row-level checks
        are performed in a single pass to keep I/O minimal.

        Returns:
            ``DQReport`` with ``total_rows`` set and all ``issues`` populated.
        """
        report = DQReport(input_file=self.input_file)  # accumulates all findings
        seen_hits: Set[Tuple[str, str]] = set()         # tracks (hit_time, ip) for duplicate detection

        with open(self.input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")      # TSV — tab-delimited
            columns = set(reader.fieldnames or [])           # extract header column names as a set

            # ── File-level checks ─────────────────────────────────────────
            self._check_columns(columns, report)  # verify required + optional columns
            if report.errors:
                # Required columns are missing — row-level checks cannot run meaningfully
                # because DictReader would return None for every expected field.
                return report  # abort early; caller will see MISSING_REQUIRED_COLUMNS error

            row_num = 0  # 1-based counter; used as the `row` field in DQIssue
            for row in reader:
                row_num += 1  # increment before checks so issue.row matches the actual data line

                # Extract the four columns used by row-level checks.
                ip           = (row.get("ip") or "").strip()            # visitor IP address
                hit_time     = (row.get("hit_time_gmt") or "").strip()  # Unix epoch string
                event_list   = (row.get("event_list") or "").strip()    # comma-separated event IDs
                product_list = (row.get("product_list") or "").strip()  # semicolon-delimited products

                # Run every row-level check in sequence.
                self._check_missing_ip(ip, row_num, report)
                self._check_hit_time(hit_time, row_num, report)
                self._check_ip_format(ip, row_num, report)
                self._check_duplicate_hit(hit_time, ip, seen_hits, row_num, report)
                self._check_event_ids(event_list, row_num, report)
                self._check_purchase_no_product(event_list, product_list, row_num, report)
                self._check_product_revenue_no_purchase(event_list, product_list, row_num, report)
                self._check_product_list(product_list, row_num, report)  # malformed + negative revenue

            report.total_rows = row_num  # set after loop so value is correct even on empty files

        if row_num == 0:  # header was present but no data rows followed
            report.issues.append(DQIssue(
                severity="ERROR",
                check="EMPTY_FILE",
                row=None,  # file-level issue — no row number
                detail="File contains a header but no data rows."
            ))

        return report  # caller uses report.passed() to decide whether to proceed

    # ------------------------------------------------------------------
    # File-level checks
    # ------------------------------------------------------------------

    def _check_columns(self, columns: set, report: DQReport) -> None:
        """
        Verify that all required columns are present in the file header.

        Also warns when optional Appendix A columns are absent — the pipeline
        can continue without them but enrichment fields will be missing.

        Args:
            columns: Set of column names from the file's header row.
            report:  DQReport to append issues to.
        """
        missing_required = REQUIRED_COLUMNS - columns  # set difference → absent required columns
        if missing_required:  # any absent required column is a pipeline-blocking ERROR
            report.issues.append(DQIssue(
                severity="ERROR",
                check="MISSING_REQUIRED_COLUMNS",
                row=None,  # file-level issue
                detail=f"Required columns absent: {sorted(missing_required)}. "
                       f"Pipeline cannot run without them."
            ))

        # Check optional columns separately — absence is a WARN, not an ERROR.
        missing_optional = APPENDIX_A_COLUMNS - REQUIRED_COLUMNS - columns  # optional only
        if missing_optional:
            report.issues.append(DQIssue(
                severity="WARN",
                check="MISSING_APPENDIX_A_COLUMNS",
                row=None,  # file-level issue
                detail=f"Optional Appendix A columns absent: {sorted(missing_optional)}. "
                       f"Pipeline will proceed but enrichment fields will be missing."
            ))

    # ------------------------------------------------------------------
    # Row-level checks
    # ------------------------------------------------------------------

    def _check_missing_ip(self, ip: str, row: int, report: DQReport) -> None:
        """
        Flag rows where the IP address is empty.

        Without an IP the row cannot be linked to a visitor session, so any
        referral attribution from an earlier row will not carry forward and
        the row will be silently skipped by the analyzer.

        Args:
            ip:     Stripped IP value from the current row.
            row:    1-based row number for the DQIssue.
            report: DQReport to append the issue to.
        """
        if not ip:  # empty string after stripping whitespace
            report.issues.append(DQIssue(
                severity="WARN",
                check="MISSING_IP",
                row=row,
                detail="ip is empty — this row cannot be session-stitched and will be skipped."
            ))

    def _check_hit_time(self, hit_time: str, row: int, report: DQReport) -> None:
        """
        Validate that ``hit_time_gmt`` is a plausible Unix timestamp integer.

        Checks:
        1. Non-empty.
        2. Parseable as an integer.
        3. Within the expected range 2000–2100 (``_TS_MIN`` to ``_TS_MAX``).

        Args:
            hit_time: Stripped ``hit_time_gmt`` value from the current row.
            row:      1-based row number.
            report:   DQReport to append the issue to.
        """
        if not hit_time:  # empty timestamp — cannot validate or use for duplicate detection
            report.issues.append(DQIssue(
                severity="WARN",
                check="INVALID_HIT_TIME",
                row=row,
                detail="hit_time_gmt is empty."
            ))
            return  # no further checks make sense for an empty value

        try:
            ts = int(hit_time)  # Unix timestamps must be whole integers
            if not (_TS_MIN <= ts <= _TS_MAX):  # outside the 2000–2100 window
                report.issues.append(DQIssue(
                    severity="WARN",
                    check="INVALID_HIT_TIME",
                    row=row,
                    detail=f"hit_time_gmt={ts} is outside the expected range "
                           f"[{_TS_MIN}, {_TS_MAX}] — possible corrupt timestamp."
                ))
        except ValueError:  # non-integer string (e.g. "not_a_number" or a date string)
            report.issues.append(DQIssue(
                severity="WARN",
                check="INVALID_HIT_TIME",
                row=row,
                detail=f"hit_time_gmt='{hit_time}' is not an integer."
            ))

    def _check_ip_format(self, ip: str, row: int, report: DQReport) -> None:
        """
        Validate that the IP address is a properly formatted IPv4 address.

        Accepts dotted-quad notation only (e.g. ``"192.168.1.1"``).
        Each octet must be in the range 0–255.

        Args:
            ip:     Stripped IP value.  Empty strings are skipped (already
                    caught by ``_check_missing_ip``).
            row:    1-based row number.
            report: DQReport to append the issue to.
        """
        if not ip:  # already flagged by MISSING_IP — avoid duplicate issues
            return

        m = _IPV4_RE.match(ip)  # test dotted-quad pattern
        # Both the regex match AND the octet range must be satisfied.
        if not m or not all(0 <= int(o) <= 255 for o in m.groups()):
            report.issues.append(DQIssue(
                severity="WARN",
                check="INVALID_IP_FORMAT",
                row=row,
                detail=f"ip='{ip}' is not a valid IPv4 address."
            ))

    def _check_duplicate_hit(
        self,
        hit_time: str,
        ip: str,
        seen: Set[Tuple[str, str]],
        row: int,
        report: DQReport,
    ) -> None:
        """
        Detect rows that share the same ``(hit_time_gmt, ip)`` pair.

        Duplicate hits typically indicate replayed events from an upstream
        queue or an ETL re-processing bug.  Only the second (and later)
        occurrences are flagged; the first is treated as the canonical hit.

        Args:
            hit_time: Stripped ``hit_time_gmt`` value.
            ip:       Stripped IP value.
            seen:     Mutable set of ``(hit_time, ip)`` tuples seen so far;
                      updated in-place.
            row:      1-based row number.
            report:   DQReport to append the issue to.
        """
        if not hit_time or not ip:  # cannot form a meaningful key without both fields
            return

        key = (hit_time, ip)  # composite key uniquely identifies a single hit
        if key in seen:        # this exact (time, visitor) pair was already processed
            report.issues.append(DQIssue(
                severity="WARN",
                check="DUPLICATE_HIT",
                row=row,
                detail=f"Duplicate (hit_time_gmt={hit_time}, ip={ip}) — "
                       f"this hit may be replayed or double-counted."
            ))
        seen.add(key)  # always record the key, whether first or duplicate occurrence

    def _check_event_ids(self, event_list: str, row: int, report: DQReport) -> None:
        """
        Flag event IDs in ``event_list`` that are not in the Appendix A schema.

        Unknown IDs are flagged at INFO level — they may be valid custom events
        not documented in Appendix A.  They do not affect pipeline correctness
        because the analyzer only acts on event ``"1"`` (purchase).

        Args:
            event_list: Stripped ``event_list`` column value.
            row:        1-based row number.
            report:     DQReport to append the issue to.
        """
        if not event_list:  # empty event list is valid — many hits are page views
            return

        # Build the set of IDs in this row and subtract the known-valid set.
        unknown = {e.strip() for e in event_list.split(",") if e.strip()} - VALID_EVENT_IDS
        if unknown:  # at least one unrecognised ID present
            report.issues.append(DQIssue(
                severity="INFO",
                check="UNKNOWN_EVENT_ID",
                row=row,
                detail=f"event_list contains unrecognised event IDs: {sorted(unknown)}. "
                       f"Known IDs per Appendix A: {sorted(VALID_EVENT_IDS)}."
            ))

    def _check_purchase_no_product(
        self, event_list: str, product_list: str, row: int, report: DQReport
    ) -> None:
        """
        Flag purchase events where ``product_list`` is empty.

        If event "1" fires but there are no products listed, the analyzer will
        record a purchase with $0 revenue — silently losing the transaction.

        Args:
            event_list:   Stripped ``event_list`` value.
            product_list: Stripped ``product_list`` value.
            row:          1-based row number.
            report:       DQReport to append the issue to.
        """
        # Build a set of event IDs from the comma-delimited string.
        is_purchase = "1" in {e.strip() for e in event_list.split(",")} if event_list else False
        if is_purchase and not product_list:  # purchase fired but no products defined
            report.issues.append(DQIssue(
                severity="WARN",
                check="PURCHASE_NO_PRODUCT",
                row=row,
                detail="Purchase event (1) present but product_list is empty — "
                       "revenue cannot be attributed for this hit."
            ))

    def _check_product_revenue_no_purchase(
        self, event_list: str, product_list: str, row: int, report: DQReport
    ) -> None:
        """
        Flag rows where ``product_list`` has revenue but no purchase event fired.

        Per the Appendix B spec, the analyzer only reads revenue when event "1"
        is present.  Revenue in ``product_list`` without a matching purchase
        event will be silently dropped.

        Args:
            event_list:   Stripped ``event_list`` value.
            product_list: Stripped ``product_list`` value.
            row:          1-based row number.
            report:       DQReport to append the issue to.
        """
        if not product_list:  # nothing to check without a product list
            return

        is_purchase = "1" in {e.strip() for e in event_list.split(",")} if event_list else False
        if is_purchase:  # purchase present — revenue will be processed correctly
            return

        # No purchase event: check if any product has a non-zero revenue field.
        for product in product_list.split(","):  # iterate each comma-delimited product
            attrs = product.split(";")           # split into Category;Name;Qty;Revenue;...
            if len(attrs) >= 4 and attrs[3].strip():  # revenue field (index 3) is present
                try:
                    if float(attrs[3].strip()) > 0:  # revenue is positive — will be silently dropped
                        report.issues.append(DQIssue(
                            severity="WARN",
                            check="PRODUCT_REVENUE_NO_PURCHASE",
                            row=row,
                            detail=f"product_list has revenue > 0 but event_list='{event_list}' "
                                   f"has no purchase event (1) — revenue will be silently dropped "
                                   f"by the analyzer (per Appendix B spec)."
                        ))
                        return  # one warning per row is sufficient
                except ValueError:  # non-numeric revenue — caught separately by _check_product_list
                    pass

    def _check_product_list(self, product_list: str, row: int, report: DQReport) -> None:
        """
        Validate the structural integrity of each product entry in ``product_list``.

        Checks per product entry:
        1. At least 4 semicolon-delimited fields (Category;Name;Qty;Revenue).
        2. The revenue field (index 3) is numeric if present.
        3. The revenue field is not negative (also raises ``NEGATIVE_REVENUE``).

        Args:
            product_list: Stripped ``product_list`` value.
            row:          1-based row number.
            report:       DQReport to append issues to.
        """
        if not product_list:  # empty product list is valid for non-product hits
            return

        for i, product in enumerate(product_list.split(","), start=1):  # i = 1-based product index
            attrs = product.split(";")  # each product has ≥4 semicolon-delimited fields
            if len(attrs) < 4:          # malformed — missing required fields
                report.issues.append(DQIssue(
                    severity="WARN",
                    check="MALFORMED_PRODUCT_LIST",
                    row=row,
                    detail=f"Product {i} in product_list has {len(attrs)} field(s); "
                           f"expected at least 4 (Category;Name;Quantity;Revenue). "
                           f"Raw: '{product.strip()}'"
                ))
                continue  # cannot validate revenue if fields are missing

            rev = attrs[3].strip()  # revenue is the fourth field (index 3)
            if rev:  # only validate if the revenue field is non-empty
                try:
                    val = float(rev)  # must be parseable as a float
                    if val < 0:       # negative revenue is almost certainly a data error
                        report.issues.append(DQIssue(
                            severity="WARN",
                            check="NEGATIVE_REVENUE",
                            row=row,
                            detail=f"Product {i} has negative revenue={val}."
                        ))
                except ValueError:  # revenue field is present but not a valid number
                    report.issues.append(DQIssue(
                        severity="WARN",
                        check="MALFORMED_PRODUCT_LIST",
                        row=row,
                        detail=f"Product {i} revenue field='{rev}' is not numeric."
                    ))
