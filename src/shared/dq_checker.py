"""
Data Quality Checker for Adobe Analytics hit-level TSV files.

Validates input data before the SearchKeywordAnalyzer processes it,
surfacing issues that would silently corrupt or drop revenue attribution.

Severity levels:
  ERROR  — file cannot be reliably processed (missing columns, empty file)
  WARN   — row-level issues that cause silent data loss or misattribution
  INFO   — noteworthy patterns that don't affect correctness

Usage:
    checker = DataQualityChecker(input_file)
    report = checker.run()
    report.print_summary()
    if not report.passed():
        raise ValueError("DQ checks failed — aborting pipeline")
"""

import csv
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Columns required by the Appendix A schema that the analyzer depends on
REQUIRED_COLUMNS = {
    "hit_time_gmt",
    "ip",
    "event_list",
    "product_list",
    "referrer",
}

# All columns defined in Appendix A (full schema)
APPENDIX_A_COLUMNS = {
    "hit_time_gmt", "date_time", "user_agent", "ip",
    "geo_city", "geo_country", "geo_region", "pagename",
    "page_url", "product_list", "referrer", "event_list",
}

# Valid event IDs per Appendix A
VALID_EVENT_IDS = {"1", "2", "10", "11", "12", "13", "14"}

# Rough valid Unix timestamp range: 2000-01-01 to 2100-01-01
_TS_MIN = 946_684_800
_TS_MAX = 4_102_444_800

_IPV4_RE = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$"
)


@dataclass
class DQIssue:
    severity: str       # "ERROR", "WARN", "INFO"
    check: str          # short check name, e.g. "MISSING_IP"
    row: Optional[int]  # 1-based data row number; None for file-level issues
    detail: str         # human-readable description

    def __str__(self) -> str:
        location = f"row {self.row}" if self.row is not None else "file"
        return f"[{self.severity}] {self.check} ({location}): {self.detail}"


@dataclass
class DQReport:
    input_file: str
    total_rows: int = 0
    issues: List[DQIssue] = field(default_factory=list)

    # ── Convenience filters ───────────────────────────────────────────────

    @property
    def errors(self) -> List[DQIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> List[DQIssue]:
        return [i for i in self.issues if i.severity == "WARN"]

    @property
    def infos(self) -> List[DQIssue]:
        return [i for i in self.issues if i.severity == "INFO"]

    def passed(self, fail_on_error: bool = True) -> bool:
        """Return True if the file is safe to process."""
        return len(self.errors) == 0 if fail_on_error else True

    # ── Reporting ─────────────────────────────────────────────────────────

    def print_summary(self) -> None:
        """Print a formatted DQ summary to the logger."""
        status = "PASSED" if self.passed() else "FAILED"
        logger.info(
            f"DQ Report [{status}] — {self.input_file} | "
            f"{self.total_rows} rows | "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings, {len(self.infos)} info"
        )
        for issue in self.issues:
            level = logging.ERROR if issue.severity == "ERROR" else (
                logging.WARNING if issue.severity == "WARN" else logging.INFO
            )
            logger.log(level, str(issue))

    def as_dict(self) -> dict:
        return {
            "input_file": self.input_file,
            "total_rows": self.total_rows,
            "passed": self.passed(),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "issues": [
                {"severity": i.severity, "check": i.check, "row": i.row, "detail": i.detail}
                for i in self.issues
            ],
        }


class DataQualityChecker:
    """
    Runs a suite of data quality checks on a hit-level TSV file.

    Checks performed
    ────────────────
    File-level (ERROR — abort if triggered):
      MISSING_REQUIRED_COLUMNS   Required columns absent from header
      MISSING_APPENDIX_A_COLUMNS Optional columns absent (WARN only)
      EMPTY_FILE                 No data rows

    Row-level (WARN — silent data loss risk):
      MISSING_IP                 IP empty — row cannot be session-stitched
      INVALID_HIT_TIME           hit_time_gmt not a valid Unix timestamp
      INVALID_IP_FORMAT          IP not a valid IPv4 address
      DUPLICATE_HIT              Same (hit_time_gmt, ip) seen twice
      UNKNOWN_EVENT_ID           event_list contains an unrecognised event
      PURCHASE_NO_PRODUCT        Event 1 present but product_list empty
      PRODUCT_REVENUE_NO_PURCHASE Revenue > 0 in product_list but no event 1 (revenue silently dropped)
      NEGATIVE_REVENUE           Revenue field < 0
      MALFORMED_PRODUCT_LIST     product_list present but cannot be parsed
    """

    def __init__(self, input_file: str):
        self.input_file = input_file

    def run(self) -> DQReport:
        report = DQReport(input_file=self.input_file)
        seen_hits: Set[Tuple[str, str]] = set()

        with open(self.input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            columns = set(reader.fieldnames or [])

            # ── File-level checks ─────────────────────────────────────────
            self._check_columns(columns, report)
            if report.errors:
                # Cannot proceed — required columns missing
                return report

            row_num = 0
            for row in reader:
                row_num += 1
                ip          = (row.get("ip") or "").strip()
                hit_time    = (row.get("hit_time_gmt") or "").strip()
                event_list  = (row.get("event_list") or "").strip()
                product_list = (row.get("product_list") or "").strip()

                self._check_missing_ip(ip, row_num, report)
                self._check_hit_time(hit_time, row_num, report)
                self._check_ip_format(ip, row_num, report)
                self._check_duplicate_hit(hit_time, ip, seen_hits, row_num, report)
                self._check_event_ids(event_list, row_num, report)
                self._check_purchase_no_product(event_list, product_list, row_num, report)
                self._check_product_revenue_no_purchase(event_list, product_list, row_num, report)
                self._check_product_list(product_list, row_num, report)

            report.total_rows = row_num

        if row_num == 0:
            report.issues.append(DQIssue(
                severity="ERROR", check="EMPTY_FILE", row=None,
                detail="File contains a header but no data rows."
            ))

        return report

    # ── File-level ────────────────────────────────────────────────────────

    def _check_columns(self, columns: set, report: DQReport) -> None:
        missing_required = REQUIRED_COLUMNS - columns
        if missing_required:
            report.issues.append(DQIssue(
                severity="ERROR", check="MISSING_REQUIRED_COLUMNS", row=None,
                detail=f"Required columns absent: {sorted(missing_required)}. "
                       f"Pipeline cannot run without them."
            ))

        missing_optional = APPENDIX_A_COLUMNS - REQUIRED_COLUMNS - columns
        if missing_optional:
            report.issues.append(DQIssue(
                severity="WARN", check="MISSING_APPENDIX_A_COLUMNS", row=None,
                detail=f"Optional Appendix A columns absent: {sorted(missing_optional)}. "
                       f"Pipeline will proceed but enrichment fields will be missing."
            ))

    # ── Row-level ─────────────────────────────────────────────────────────

    def _check_missing_ip(self, ip: str, row: int, report: DQReport) -> None:
        if not ip:
            report.issues.append(DQIssue(
                severity="WARN", check="MISSING_IP", row=row,
                detail="ip is empty — this row cannot be session-stitched and will be skipped."
            ))

    def _check_hit_time(self, hit_time: str, row: int, report: DQReport) -> None:
        if not hit_time:
            report.issues.append(DQIssue(
                severity="WARN", check="INVALID_HIT_TIME", row=row,
                detail="hit_time_gmt is empty."
            ))
            return
        try:
            ts = int(hit_time)
            if not (_TS_MIN <= ts <= _TS_MAX):
                report.issues.append(DQIssue(
                    severity="WARN", check="INVALID_HIT_TIME", row=row,
                    detail=f"hit_time_gmt={ts} is outside the expected range "
                           f"[{_TS_MIN}, {_TS_MAX}] — possible corrupt timestamp."
                ))
        except ValueError:
            report.issues.append(DQIssue(
                severity="WARN", check="INVALID_HIT_TIME", row=row,
                detail=f"hit_time_gmt='{hit_time}' is not an integer."
            ))

    def _check_ip_format(self, ip: str, row: int, report: DQReport) -> None:
        if not ip:
            return  # already caught by MISSING_IP
        m = _IPV4_RE.match(ip)
        if not m or not all(0 <= int(o) <= 255 for o in m.groups()):
            report.issues.append(DQIssue(
                severity="WARN", check="INVALID_IP_FORMAT", row=row,
                detail=f"ip='{ip}' is not a valid IPv4 address."
            ))

    def _check_duplicate_hit(
        self, hit_time: str, ip: str, seen: Set[Tuple[str, str]], row: int, report: DQReport
    ) -> None:
        if not hit_time or not ip:
            return
        key = (hit_time, ip)
        if key in seen:
            report.issues.append(DQIssue(
                severity="WARN", check="DUPLICATE_HIT", row=row,
                detail=f"Duplicate (hit_time_gmt={hit_time}, ip={ip}) — "
                       f"this hit may be replayed or double-counted."
            ))
        seen.add(key)

    def _check_event_ids(self, event_list: str, row: int, report: DQReport) -> None:
        if not event_list:
            return
        unknown = {e.strip() for e in event_list.split(",") if e.strip()} - VALID_EVENT_IDS
        if unknown:
            report.issues.append(DQIssue(
                severity="INFO", check="UNKNOWN_EVENT_ID", row=row,
                detail=f"event_list contains unrecognised event IDs: {sorted(unknown)}. "
                       f"Known IDs per Appendix A: {sorted(VALID_EVENT_IDS)}."
            ))

    def _check_purchase_no_product(
        self, event_list: str, product_list: str, row: int, report: DQReport
    ) -> None:
        is_purchase = "1" in {e.strip() for e in event_list.split(",")} if event_list else False
        if is_purchase and not product_list:
            report.issues.append(DQIssue(
                severity="WARN", check="PURCHASE_NO_PRODUCT", row=row,
                detail="Purchase event (1) present but product_list is empty — "
                       "revenue cannot be attributed for this hit."
            ))

    def _check_product_revenue_no_purchase(
        self, event_list: str, product_list: str, row: int, report: DQReport
    ) -> None:
        if not product_list:
            return
        is_purchase = "1" in {e.strip() for e in event_list.split(",")} if event_list else False
        if is_purchase:
            return
        # Check if any product has a non-zero revenue field (index 3)
        for product in product_list.split(","):
            attrs = product.split(";")
            if len(attrs) >= 4 and attrs[3].strip():
                try:
                    if float(attrs[3].strip()) > 0:
                        report.issues.append(DQIssue(
                            severity="WARN", check="PRODUCT_REVENUE_NO_PURCHASE", row=row,
                            detail=f"product_list has revenue > 0 but event_list='{event_list}' "
                                   f"has no purchase event (1) — revenue will be silently dropped "
                                   f"by the analyzer (per Appendix B spec)."
                        ))
                        return
                except ValueError:
                    pass

    def _check_product_list(self, product_list: str, row: int, report: DQReport) -> None:
        if not product_list:
            return
        for i, product in enumerate(product_list.split(","), start=1):
            attrs = product.split(";")
            if len(attrs) < 4:
                report.issues.append(DQIssue(
                    severity="WARN", check="MALFORMED_PRODUCT_LIST", row=row,
                    detail=f"Product {i} in product_list has {len(attrs)} field(s); "
                           f"expected at least 4 (Category;Name;Quantity;Revenue). "
                           f"Raw: '{product.strip()}'"
                ))
                continue
            # Revenue field (index 3) must be numeric if present
            rev = attrs[3].strip()
            if rev:
                try:
                    val = float(rev)
                    if val < 0:
                        report.issues.append(DQIssue(
                            severity="WARN", check="NEGATIVE_REVENUE", row=row,
                            detail=f"Product {i} has negative revenue={val}."
                        ))
                except ValueError:
                    report.issues.append(DQIssue(
                        severity="WARN", check="MALFORMED_PRODUCT_LIST", row=row,
                        detail=f"Product {i} revenue field='{rev}' is not numeric."
                    ))
