"""Collect UCI 498 and derive prefix-only, provenance-preserving workflow records."""
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from urllib.request import Request, urlopen
import zipfile


class PublicIncidentCollector:
    URL = "https://archive.ics.uci.edu/static/public/498/incident+management+process+enriched+event+log.zip"
    LANDING = "https://archive.ics.uci.edu/dataset/498/incident+management+process+enriched+event+log"
    CITATION = "Amaral, C., Fantinato, M., & Peres, S. (2018). Incident management process enriched event log [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C57S4H"
    # Whitelist fields available at the recorded event. Final resolved_at,
    # closed_at, close_code, resolved_by and retrospective made_sla stay out.
    EVENT_FIELDS = ("incident_state", "active", "reassignment_count", "reopen_count", "sys_mod_count",
                    "contact_type", "category", "subcategory", "priority", "impact", "urgency",
                    "assignment_group", "assigned_to")

    @staticmethod
    def value(raw):
        value = (raw or "").strip()
        return None if value in {"", "?"} else value

    @staticmethod
    def time(raw):
        raw = PublicIncidentCollector.value(raw)
        if raw is None:
            return None
        for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, pattern)
            except ValueError:
                pass
        return None

    @staticmethod
    def integer(raw):
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def pseudonym(value, prefix):
        return prefix + "-" + hashlib.sha256(value.encode()).hexdigest()[:20]

    def derive(self, stream, directory, *, limit=400):
        """Keep source rows, null unknowns, and export genuine observed prefixes.

        No diagnosis labels, fabricated ticket text, fake time zones or inferred
        user verification. Equal projected rows are retained as source events.
        """
        directory = Path(directory)
        groups = defaultdict(list)
        missing, states, parse_failures = Counter(), Counter(), Counter()
        reader = csv.DictReader(stream)
        required = {"number", "incident_state", "sys_updated_at", "reopen_count", "reassignment_count"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError("unexpected UCI event schema")
        count = 0
        for index, raw in enumerate(reader):
            if index >= 200000:
                raise ValueError("unexpected public event count")
            row = {key: self.value(value) for key, value in raw.items()}
            if not row.get("number"):
                raise ValueError("public event without incident identifier")
            for key, value in row.items():
                if value is None:
                    missing[key] += 1
            stamp = self.time(row.get("sys_updated_at"))
            if stamp is None:
                parse_failures["sys_updated_at"] += 1
            event = {key: row.get(key) for key in self.EVENT_FIELDS}
            for field in ("reassignment_count", "reopen_count", "sys_mod_count"):
                event[field] = self.integer(event[field])
            event["active"] = {"true": True, "false": False}.get((event["active"] or "").lower())
            event.update(event_at=stamp.isoformat() if stamp else None, source_row=index + 2)
            states[str(row["incident_state"])] += 1
            groups[row["number"]].append((stamp, event, row))
            count += 1
        transitions, strata, candidates = Counter(), defaultdict(list), {}
        timestamp_ties = invalid_time_cases = duplicate_projections = reopened_cases = reassigned_cases = 0
        event_lengths = Counter()
        for number, records in groups.items():
            records.sort(key=lambda item: (item[0] or datetime.max, item[1]["sys_mod_count"] if item[1]["sys_mod_count"] is not None else -1,
                                            item[1]["source_row"]))
            events = [e for _, e, _ in records]
            duplicate_projections += len(events) - len({json.dumps({k: v for k, v in e.items() if k != "source_row"}, sort_keys=True) for e in events})
            times = [stamp for stamp, _, _ in records]
            invalid = any(t is None for t in times)
            tied = len(times) != len(set(times))
            timestamp_ties += int(tied)
            invalid_time_cases += int(invalid)
            event_lengths[str(len(events))] += 1
            for earlier, later in zip(events, events[1:]):
                transitions[f"{earlier['incident_state']} -> {later['incident_state']}"] += 1
            reopens = max((e["reopen_count"] or 0 for e in events), default=0)
            assignments = max((e["reassignment_count"] or 0 for e in events), default=0)
            reopened_cases += int(reopens > 0)
            reassigned_cases += int(assignments > 0)
            made_sla = next((row["made_sla"] for _, _, row in reversed(records) if row.get("made_sla") is not None), None)
            prefix_len = min(4, max(1, len(events) // 2))
            # Prefix/next labels cannot establish order if timestamps are absent or
            # equal at the cut. Keep such cases in profile, exclude this export.
            if len(events) < 2 or invalid or times[prefix_len-1] == times[prefix_len]:
                continue
            tier = "reopened" if reopens > 0 else "many_reassignments" if assignments >= 3 else "sla_not_met" if (made_sla or "").lower() == "false" else "pending_history" if any("await" in str(e["incident_state"]).lower() or "pending" in str(e["incident_state"]).lower() for e in events) else "other"
            ident = self.pseudonym(number, "uci498")
            strata[tier].append(ident)
            candidates[ident] = (events, prefix_len, reopens, assignments, made_sla)
        for members in strata.values():
            members.sort()
        selected = [members[i] for i in range(max(map(len, strata.values()), default=0)) for _, members in sorted(strata.items()) if i < len(members)][:limit]
        inputs, labels = [], []
        for ident in selected:
            events, cut, reopens, assignments, made_sla = candidates[ident]
            bucket = int(hashlib.sha256(ident.encode()).hexdigest()[:8], 16) % 10
            split = "development" if bucket < 6 else "regression" if bucket < 8 else "challenge"
            inputs.append({"id": ident, "suite": "public_workflow_prefix", "source": "uci498", "synthetic": False,
                           "source_timezone": None, "events": events[:cut], "resolution_verified": None})
            labels.append({"id": ident, "split": split, "synthetic": False, "annotation": "observed_public_outcome_not_prescriptive_policy",
                           "observed_next_state": events[cut]["incident_state"], "observed_remaining_events": len(events)-cut,
                           "observed_max_reopen_count": reopens, "observed_max_reassignment_count": assignments,
                           "source_made_sla": made_sla, "root_cause": None, "human_review": None,
                           "note": "Historical source state is not a required action or verified resolution in this project"})
        for name, rows in (("workflow-inputs.jsonl", inputs), ("workflow-labels.jsonl", labels)):
            with (directory / name).open("x", encoding="utf-8") as output:
                output.writelines(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        profile = {"events": count, "incidents": len(groups), "states": dict(states), "transitions": dict(transitions),
                   "missing_values": dict(missing), "time_parse_failures": dict(parse_failures), "incident_event_counts": dict(event_lengths),
                   "incidents_with_timestamp_ties": timestamp_ties, "incidents_with_invalid_time": invalid_time_cases,
                   "equal_projected_events_retained": duplicate_projections, "incidents_with_reopen_counter": reopened_cases,
                   "incidents_with_reassignment_counter": reassigned_cases, "prefix_cases": len(selected),
                   "candidate_strata": {key: len(value) for key, value in strata.items()},
                   "cleaning": ["trim fields; ?/empty become null, never negative/default success",
                                "parse day-first event time; source timezone remains unknown",
                                "preserve repeated projected events, source row and anonymized categories",
                                "order by timestamp, update count, source row; flag ties rather than claim strict causal order",
                                "exclude missing/tied-cut timestamps from next-event prefix export",
                                "prefix whitelist excludes future closure/resolution fields and retrospective SLA target",
                                "sample whole incident IDs; no incident crosses splits; strata intentionally oversample edge cases"],
                   "limitations": ["No original ticket free text, VPN/ERP telemetry or root-cause labels",
                                   "Source is enriched data; whitelist removes explicit future outcomes but other retrospective fields still require review before strict temporal prediction",
                                   "Observed status transitions are not local authorization policy",
                                   "No evidence of this project's user-verification step",
                                   "One historical organization; exported strata do not represent its incident frequency"]}
        with (directory / "profile.json").open("x", encoding="utf-8") as output:
            json.dump(profile, output, ensure_ascii=False, indent=2)
        return profile

    def collect(self, directory, *, public_http=False):
        directory = Path(directory)
        if directory.exists():
            raise FileExistsError("public incident snapshot already exists")
        # Explicit transport choice for this public, non-executable data only;
        # never disable certificate verification or silently downgrade HTTPS.
        download_url = self.URL.replace("https://", "http://", 1) if public_http else self.URL
        request = Request(download_url, headers={"User-Agent": "FinalPro-public-dataset-research"})
        with urlopen(request, timeout=45) as response:
            archive = response.read(10 * 1024 * 1024 + 1)
        if not archive or len(archive) > 10 * 1024 * 1024:
            raise ValueError("public archive size invalid")
        directory.mkdir(parents=True)
        (directory / "source.zip").write_bytes(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            members = [info for info in zf.infolist() if Path(info.filename).name == "incident_event_log.csv"]
            if len(members) != 1 or members[0].file_size > 128 * 1024 * 1024:
                raise ValueError("unexpected public archive members")
            # No extractall: archive paths are never used as filesystem targets.
            with zf.open(members[0]) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                profile = self.derive(stream, directory)
        attribution = f"# UCI 498 attribution\n\n{self.CITATION}\n\nSource: {self.LANDING}\n\nLicense: Creative Commons Attribution 4.0 International (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/ .\n\nThe downloaded archive is unmodified. Derived files normalize missing values/timestamps, pseudonymize incident IDs, and export sampled event prefixes; transformation details are in profile.json. No endorsement by the authors is implied. Dataset files retain their source license and are not relicensed under the project's MIT.\n"
        (directory / "ATTRIBUTION.md").write_text(attribution, encoding="utf-8")
        manifest = {"source": "UCI 498", "url": download_url, "landing": self.LANDING, "citation": self.CITATION,
                    "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "transport": "http" if public_http else "https",
                    "integrity": "SHA-256 of retrieved content; not a publisher signature",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(), "events": profile["events"], "incidents": profile["incidents"],
                    "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir()) if p.is_file()}}
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
