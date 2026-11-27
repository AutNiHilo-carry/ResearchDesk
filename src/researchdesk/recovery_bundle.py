"""Verify or compare offline recovery bundles without running a research job."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from .citations import CITATION

RECOVERY_CASES = (("report", 3), ("receipt", 3), ("evidence", 3), ("model_response", 3), ("model_response", 1))
COMPARISON_METRICS = ("model_calls", "tool_calls", "retry_count", "recovery_replays", "evidence_count")
EVIDENCE_APPENDIX = "\n\n## 引用证据\n\n"
MANIFEST = "checksums.json"
HEX_ID = re.compile(r"[0-9a-f]{32}")


class BundleError(ValueError):
    pass


def write_checksums(archive):
    hashes = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
    archive.writestr(MANIFEST, json.dumps({"schema_version": 1, "sha256": hashes}, indent=2) + "\n")


def json_object(files, name):
    value = json.loads(files[name])
    if not isinstance(value, dict):
        raise BundleError(f"Expected a JSON object in {name}.")
    return value


def read_verified_members(archive):
    members = archive.infolist()
    names = [info.filename for info in members]
    # A fixed five-case export is small. Bound reads before decompressing.
    if len(names) > 16 or len(names) != len(set(names)) or sum(info.file_size for info in members) > 4 * 1024**2:
        raise BundleError("Unexpected ZIP size, member count, or duplicate file names.")
    if MANIFEST not in names:
        raise BundleError("This ZIP has no checksum manifest. Download a fresh batch export.")
    files = {name: archive.read(name) for name in names}
    manifest = json_object(files, MANIFEST)
    expected = manifest.get("sha256")
    if manifest.get("schema_version") != 1 or not isinstance(expected, dict):
        raise BundleError("Unsupported checksum manifest.")
    del files[MANIFEST]
    if files.keys() != expected.keys():
        raise BundleError("ZIP members differ from the checksum manifest.")
    for name, content in files.items():
        if hashlib.sha256(content).hexdigest() != expected[name]:
            raise BundleError(f"Checksum mismatch: {name!r}.")
    return files


def count(value):
    return type(value) is int and value >= 0


def read_verified_batch(path):
    with ZipFile(path) as archive:
        files = read_verified_members(archive)
    batch = json_object(files, "batch.json")
    suite_id = batch["id"]
    cases = batch["cases"]
    if batch["schema_version"] != 1 or batch["status"] != "completed" or not HEX_ID.fullmatch(suite_id):
        raise BundleError("Invalid completed batch record.")
    if tuple((row["boundary"], row["model_limit"]) for row in cases) != RECOVERY_CASES:
        raise BundleError("The batch does not contain the five recovery scenarios in order.")

    comparison = files["comparison.md"].decode("utf-8")
    expected_files = {"batch.json", "comparison.md"}
    run_ids = set()
    reports = 0
    missing_snapshots = 0
    run_summaries = []
    for case in cases:
        run_id = case["run_id"]
        point, budget = case["boundary"], case["model_limit"]
        expected_status = "budget_exhausted" if budget == 1 else "completed"
        if not HEX_ID.fullmatch(run_id) or run_id in run_ids:
            raise BundleError("Invalid or duplicate run ID.")
        run_ids.add(run_id)
        prefix = f"runs/{run_id}/"
        summary_name = prefix + "run-summary.json"
        summary = json_object(files, summary_name)
        expected_summary = dict(schema_version=1, id=run_id, recovery_suite_id=suite_id, mode="recovery_demo",
                                status=expected_status, model_limit=budget, recovery_demo_injected=True,
                                recovery_demo_boundary=point)
        if (case["phase"] != "finished" or case["outcome"] != expected_status
                or any(summary.get(key) != value for key, value in expected_summary.items())
                or not count(summary.get("resume_count")) or summary["resume_count"] == 0
                or f"]({summary_name})" not in comparison):
            raise BundleError(f"Run summary does not match the batch: {run_id}.")
        expected_files.add(summary_name)

        metrics = summary["metrics"]
        if (any(not count(metrics.get(key)) for key in COMPARISON_METRICS)
                or metrics["model_calls"] > budget or metrics.get("provider_requests") != 0):
            raise BundleError(f"Invalid run counters: {run_id}.")
        run_summaries.append(summary)
        before = summary["recovery_demo_before"]
        if before is None:
            missing_snapshots += 1
        elif (not isinstance(before, dict) or not {"model_calls", "tool_calls", "recovery_replays"} <= before.keys()
              or any(key not in COMPARISON_METRICS or not count(value) or value > metrics[key]
                     for key, value in before.items())):
            raise BundleError(f"Inconsistent recovery counters: {run_id}.")

        if expected_status == "completed":
            report_name = prefix + "report.md"
            evidence_name = prefix + "evidence.json"
            report = files[report_name].decode("utf-8").split(EVIDENCE_APPENDIX, 1)[0]
            evidence = json_object(files, evidence_name)
            citations = set(CITATION.findall(report))
            if (not citations or not citations <= evidence.keys() or f"]({report_name})" not in comparison
                    or any(not evidence[eid].get("snippet") or evidence[eid].get("integrity") != "valid"
                           for eid in citations)
                    or summary["citation_check"] != dict(total=len(citations), matched=len(citations),
                                                         missing=0, mismatched=0)):
                raise BundleError(f"Report citations do not match the evidence ledger: {run_id}.")
            expected_files.update((report_name, evidence_name))
            reports += 1
        elif summary["citation_check"] is not None or metrics["model_calls"] != budget:
            raise BundleError(f"Invalid budget-stop outcome: {run_id}.")

    if files.keys() != expected_files or suite_id not in comparison:
        raise BundleError("Unexpected files or batch reference.")
    return dict(batch_id=suite_id, cases=len(cases), reports=reports, budget_stops=len(cases) - reports,
                missing_before_snapshots=missing_snapshots), run_summaries


def verify_batch(path):
    summary, _ = read_verified_batch(path)
    return summary


def compare_batches(baseline, candidate):
    base, base_runs = read_verified_batch(baseline)
    new, new_runs = read_verified_batch(candidate)
    cases = []
    for (boundary, budget), left, right in zip(RECOVERY_CASES, base_runs, new_runs, strict=True):
        changes, unavailable = [], []
        for phase, field in (("before", "recovery_demo_before"), ("after", "metrics")):
            for metric in COMPARISON_METRICS:
                previous = (left[field] or {}).get(metric)
                current = (right[field] or {}).get(metric)
                if previous is None or current is None:
                    unavailable.append(dict(phase=phase, metric=metric, baseline=previous, candidate=current))
                elif previous != current:
                    changes.append(dict(phase=phase, metric=metric, baseline=previous, candidate=current))
        if left["resume_count"] != right["resume_count"]:
            changes.append(dict(phase="after", metric="resume_count",
                                baseline=left["resume_count"], candidate=right["resume_count"]))
        cases.append(dict(boundary=boundary, model_limit=budget, outcome=right["status"],
                          baseline_run_id=left["id"], candidate_run_id=right["id"],
                          changes=changes, unavailable=unavailable))
    changed_cases = sum(bool(case["changes"]) for case in cases)
    incomplete_cases = sum(bool(case["unavailable"]) for case in cases)
    status = "different" if changed_cases else "incomplete" if incomplete_cases else "matching"
    return dict(status=status, baseline_batch_id=base["batch_id"], candidate_batch_id=new["batch_id"],
                changed_cases=changed_cases, incomplete_cases=incomplete_cases, cases=cases)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="A downloaded ResearchDesk recovery review ZIP")
    parser.add_argument("--baseline", type=Path, help="Compare the verified bundle with this baseline ZIP")
    args = parser.parse_args(argv)
    try:
        result = compare_batches(args.baseline, args.bundle) if args.baseline else verify_batch(args.bundle)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, BadZipFile, CompressionError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if args.baseline:
        return {"matching": 0, "different": 1, "incomplete": 2}[result["status"]]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
