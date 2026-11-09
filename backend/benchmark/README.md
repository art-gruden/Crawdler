# Benchmark (offline)

`python -m benchmark.run` crawls the 16 fixture websites in `sites/` through the same engine,
fetcher, extraction and SSRF-safe client the workers use. The transport and DNS resolver are
replaced by local fixtures, so **no request leaves the machine**. Ground truth:
`sites/_expected.json`. The generator is `build_fixtures.py`, and all companies and domains are
fictional.

## Measured results

Run on 9 November 2026 in the development sandbox: 2 vCPU, Python 3.13, 3 repetitions, median
wall time, `per_domain_delay_s=0`. These are real outputs of `benchmark.run`; nothing was
estimated.

| Metric | HTTP strategies only | + browser (Chrome 1 page) |
|---|---|---|
| Career-page discovery recall | 1.00 (12/12) | 1.00 |
| Vacancy precision | 1.00 | 1.00 |
| Vacancy recall | 0.92 (23/25; the 2 JS-rendered postings missing) | 1.00 (25/25) |
| Field accuracy (52 / 54 checked fields) | 1.00 | 1.00 |
| Browser fallback share | 1 of 16 sites (6 %) | 1 of 16 sites (6 %) |
| Pages per run | 61 | 64 |
| Median wall time per run | 0.20 s | 2.29 s (incl. Chromium start) |
| Pages / second | 304 | 28 |
| CPU time per run | 0.30 s | 0.42 s (Python process only) |
| Peak RSS of the Python process | 108 MB | 140 MB |

Separately measured browser footprint (Chrome, 10 renders of the JS fixture): proportional set
size of Python + Chromium processes 397 MB after launch, 475 MB peak. The browser worker
container is limited to 1100 MB. The image uses Playwright's headless shell, which is smaller
than full Chrome, but that was not measured here.

## How to read these numbers

- **Accuracy:** the fixtures were written together with the extractor, so 100 % means "the cases
  we designed for work and keep working". It is a regression check, **not** an estimate of
  accuracy on real employer websites. Real sites have layouts these fixtures don't cover. Measure
  on a sample of real companies (compare the dashboard against the career pages by hand) before
  trusting the extraction.
- **Throughput:** offline, without the 2 s per-domain politeness delay or real network latency.
  On live sites a company scan is dominated by those delays: ~5–15 pages × 2 s ≈ 10–30 s per
  company, with up to 16 companies in parallel per worker. That arithmetic gives an upper bound of
  roughly 1 900–5 700 companies per hour for HTML/ATS sites. It has **not** been measured against
  live websites. Watch *Domains / hour* and *Pages / second* on the Crawler dashboard instead.
- Peak RSS comes from `getrusage` for the Python process. Chromium runs in child processes and is
  not included; see the separate PSS measurement above.

## Commands

```bash
python -m benchmark.build_fixtures               # regenerate sites/ and _expected.json
python -m benchmark.run --repeat 3 --json out.json
python -m benchmark.run --browser --executable /usr/bin/chromium
```
