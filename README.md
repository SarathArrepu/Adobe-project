# Search Keyword Performance Analyzer

## Business Problem

The client wants to understand **how much revenue is driven by external search engines** (Google, Yahoo, Bing/MSN) and **which search keywords perform best** based on revenue.

This Python application processes Adobe Analytics hit-level data, attributes purchase revenue to the originating search engine and keyword, and outputs a ranked report.

## How It Works

### Attribution Logic

1. **Visitor Identification** — Each unique IP address is treated as a distinct visitor (the dataset does not include a cookie/visitor ID).
2. **Search Engine Detection** — When a visitor arrives from an external search engine (Google, Yahoo, Bing), the referrer URL is parsed to extract the search engine domain and the keyword.
3. **Revenue Attribution** — When a purchase event (`event_list` contains `1`) occurs, the revenue from `product_list` is attributed to the search engine and keyword that originally brought the visitor. Revenue is only counted on purchase events, as specified in Appendix B of the requirements.
4. **Aggregation** — Revenue is summed per (search engine domain, keyword) pair and sorted descending.

### Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Input: TSV File                    │
│            (hit-level data, streamed row by row)     │
└──────────────┬───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│           SearchKeywordAnalyzer.process()            │
│                                                      │
│  For each row:                                       │
│  1. Parse referrer → detect search engine + keyword  │
│  2. Track attribution per visitor IP                 │
│  3. On purchase event → extract revenue              │
│  4. Aggregate revenue by (engine, keyword)           │
└──────────────┬───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│          Output: YYYY-MM-DD_SearchKeyword            │
│                  Performance.tab                     │
│  (tab-delimited, sorted by revenue desc)             │
└──────────────────────────────────────────────────────┘
```

## Project Structure

```
adobe-assessment/
├── src/
│   └── search_keyword_analyzer.py    # Main application with SearchKeywordAnalyzer class
├── tests/
│   └── test_analyzer.py              # 26 unit tests
├── data/
│   └── data.sql                      # Provided hit-level data file
├── output/                           # Generated output files
├── README.md
└── requirements.txt                  # No external dependencies
```

## Setup & Execution

### Prerequisites
- Python 3.8+
- No external dependencies (uses only the Python standard library)

### Running the Application

```bash
python src/search_keyword_analyzer.py data/data.sql
```

With a custom output directory:

```bash
python src/search_keyword_analyzer.py data/data.sql -o /path/to/output/
```

### Running Tests

```bash
python -m unittest tests.test_analyzer -v
```

### Expected Output (from provided sample data)

```
Search Engine Domain     Search Keyword       Revenue
-------------------------------------------------------
google.com               Ipod                  290.00
bing.com                 Zune                  250.00
google.com               ipod                  190.00
```

Three visitors arrived from search engines. Two came from Google (different keyword casing), one from Bing. One Yahoo visitor ("cd player") browsed but did not purchase, so they do not appear in the revenue report.

## AWS Deployment

For AWS deployment, this application can run as:

- **AWS Lambda + S3**: Triggered when a data file is uploaded to an S3 bucket. The Lambda reads the file from S3, processes it, and writes the output back to S3.
- **AWS Glue / EMR**: For production-scale processing (see Scalability below).

A basic Lambda handler would wrap the existing class:

```python
import boto3
from search_keyword_analyzer import SearchKeywordAnalyzer

def lambda_handler(event, context):
    s3 = boto3.client('s3')
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = event['Records'][0]['s3']['object']['key']

    local_input = '/tmp/input.tsv'
    s3.download_file(bucket, key, local_input)

    analyzer = SearchKeywordAnalyzer(local_input)
    analyzer.process()
    output_path = analyzer.write_output('/tmp/output')

    s3.upload_file(output_path, bucket, f"output/{os.path.basename(output_path)}")
```

## Scalability Considerations

The current application reads the file **line by line** using `csv.DictReader`, which means memory usage is O(unique visitors) rather than O(file size). This is already efficient for moderate file sizes.

However, for **10 GB+ files**, several improvements would be needed:

### Current Bottlenecks
1. **Single-threaded processing** — one core processes the entire file sequentially.
2. **In-memory visitor map** — the `_visitor_search_attribution` dict grows with unique IPs. At scale (millions of IPs), this could become large.
3. **Lambda limitations** — AWS Lambda has a 15-minute timeout and 10 GB ephemeral storage, which may not be sufficient.

### Recommended Improvements for Scale

| Approach | When to Use | How |
|----------|-------------|-----|
| **Chunked reading with multiprocessing** | 10-50 GB files, single machine | Split file into chunks, process in parallel, merge results |
| **Apache Spark on EMR** | 50+ GB files, recurring jobs | Distribute across cluster, native DataFrame operations |
| **AWS Glue (PySpark)** | Serverless, AWS-native | Managed Spark, auto-scaling, integrates with S3/Glue Catalog |
| **Streaming with Kinesis** | Real-time processing | Process hits as they arrive instead of batch |

### Spark Implementation Sketch

```python
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, sum as spark_sum, udf
from pyspark.sql.types import StringType, StructType, StructField

spark = SparkSession.builder.appName("SearchKeywordAnalyzer").getOrCreate()

# Read TSV — Spark handles partitioning automatically
df = spark.read.csv("s3://bucket/data.sql", sep="\t", header=True)

# Parse referrer UDF, filter purchases, extract revenue, group by keyword
# ... (same logic, distributed across cluster)
```

### Additional Production Hardening
- **Logging & monitoring**: CloudWatch metrics for processing time, row counts, error rates.
- **Data validation**: Schema checks on input file before processing.
- **Idempotency**: Check if output already exists for a given date before reprocessing.
- **Configuration**: Externalize search engine definitions (domains, query params) to a config file so new engines can be added without code changes.
- **Session handling**: For production, use a proper visitor ID (cookie-based) rather than IP, and implement session windowing to handle returning visitors.

## Design Decisions

1. **IP as visitor ID**: The data doesn't include a visitor/cookie ID, so IP is the best available proxy. In production Adobe Analytics data, `visid_high + visid_low` would be used instead.

2. **Keyword case sensitivity**: "Ipod" and "ipod" are treated as separate keywords because the requirement doesn't specify normalization. This preserves the raw search data. In a production setting, I'd discuss with the client whether to normalize.

3. **Standard library only**: No external dependencies (pandas, etc.) — the application uses only `csv`, `urllib.parse`, `argparse`, and `collections`. This simplifies deployment and reduces Lambda package size.

4. **Line-by-line processing**: The file is streamed row by row, never loaded entirely into memory. This is the foundation for scaling to larger files.
