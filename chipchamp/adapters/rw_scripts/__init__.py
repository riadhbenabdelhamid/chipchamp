"""RapidWright worker scripts — one JVM per job, never in the agent process.

Each module here is run as ``python -m chipchamp.adapters.rw_scripts.<name>
<post_route.dcp> <report.json> [...]`` by the rapidwright adapter. It starts
RapidWright through the `rapidwright` pip package (JPype), does one job, and
writes a JSON report the adapter parses. A failure of any kind is still a
JSON report with an "error" field, so the job record carries the reason.
"""
