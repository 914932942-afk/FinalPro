"""Server-owned incident time resolution and bounded, account-local evidence views."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from .business_accounts import DiagnosticDataGap


FULL_TIME = re.compile(r"(?<!\d)(\d{4}-\d{1,2}-\d{1,2})[T\s]+(\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)(Z|[+-]\d{2}:\d{2})?(?!\d)")
RELATIVE_TIME = re.compile(r"(今天|昨天|前天|明天|后天)\s*(上午|下午|晚上|中午|凌晨)?\s*(\d{1,2})(?:\s*(?::(\d{2})|点(?:(\d{1,2})分?)?))")
PLAIN_TIME = re.compile(r"(?<![\d:-])(\d{1,2}):(\d{2})(?!\d)")


def fixed_timezone(value):
    if value in {"UTC", "Z", "+00:00"}:
        return timezone.utc
    if not isinstance(value, str) or not re.fullmatch(r"[+-](?:0\d|1[0-4]):[0-5]\d", value):
        raise ValueError("time zone must be an explicit UTC offset")
    sign = -1 if value[0] == "-" else 1
    minutes = int(value[1:3])*60 + int(value[4:])
    if minutes > 14*60:
        raise ValueError("time zone offset out of range")
    return timezone(sign*timedelta(minutes=minutes))


def parse_instant(value, default_timezone=None):
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        if default_timezone is None:
            raise ValueError("timestamp requires an offset")
        result = result.replace(tzinfo=fixed_timezone(default_timezone))
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class IncidentWindow:
    start_at: str
    end_at: str
    basis: str

    def __post_init__(self):
        start, end = parse_instant(self.start_at), parse_instant(self.end_at)
        if start > end or end-start > timedelta(hours=6):
            raise ValueError("invalid or oversized time window")

    def to_dict(self):
        return {"start_at": self.start_at, "end_at": self.end_at, "basis": self.basis}

    def contains(self, value):
        return parse_instant(self.start_at) <= value <= parse_instant(self.end_at)


class IncidentTimeResolver:
    def __init__(self, *, timezone_offset="+08:00", before_minutes=15, after_minutes=15):
        self.timezone_offset = timezone_offset
        self.zone = fixed_timezone(timezone_offset)
        self.before, self.after = before_minutes, after_minutes
        if min(before_minutes, after_minutes) < 0 or before_minutes + after_minutes > 360:
            raise ValueError("invalid incident context window")

    def resolve(self, text, *, reference_at, time_answer=None, time_answer_at=None):
        # A correction to a controlled time question takes precedence. Relative
        # dates use that answer's timestamp, not a later retry's wall clock.
        reference = parse_instant(time_answer_at if time_answer and time_answer_at else reference_at).astimezone(self.zone)
        selected = time_answer if time_answer else text
        selected = str(selected)[:20000]
        selected = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3 ", selected)
        status = {"status": "unspecified", "window": None, "timezone": self.timezone_offset,
                  "reference_at": reference.isoformat(), "reason": "date_and_time_required"}
        matches = []
        try:
            for match in FULL_TIME.finditer(selected):
                day = datetime.strptime(match[1], "%Y-%m-%d").strftime("%Y-%m-%d")
                point = parse_instant(day + "T" + match[2].zfill(5) + (match[3] or ""), self.timezone_offset)
                matches.append((point, match.start(), match.end()))
            for match in RELATIVE_TIME.finditer(selected):
                day = reference.date() + timedelta(days={"今天": 0, "昨天": -1, "前天": -2, "明天": 1, "后天": 2}[match[1]])
                hour, minute = int(match[3]), int(match[4] or match[5] or 0)
                if match[2] in {"下午", "晚上"} and hour < 12:
                    hour += 12
                if match[2] == "凌晨" and hour == 12:
                    hour = 0
                point = datetime.combine(day, datetime.min.time(), self.zone).replace(hour=hour, minute=minute).astimezone(timezone.utc)
                matches.append((point, match.start(), match.end()))
            # Users commonly state the date once: “今天 9:00 到 10:00”.
            # Add unqualified clock times on the same reference day while
            # avoiding the time component already consumed by a full match.
            occupied = [(m[1], m[2]) for m in matches]
            occupied.extend((m.start(), m.end()) for m in FULL_TIME.finditer(selected))
            for match in PLAIN_TIME.finditer(selected):
                if any(left <= match.start() < right for left, right in occupied):
                    continue
                point = datetime.combine(reference.date(), datetime.min.time(), self.zone).replace(
                    hour=int(match[1]), minute=int(match[2])).astimezone(timezone.utc)
                matches.append((point, match.start(), match.end()))
            recent = re.search(r"最近\s*(\d{1,3})\s*(分钟|小时)", selected)
            if recent and not matches:
                duration = timedelta(minutes=int(recent[1]) * (60 if recent[2] == "小时" else 1))
                if not timedelta(0) < duration <= timedelta(hours=6):
                    return {**status, "status": "ambiguous", "reason": "narrower_time_range_required"}
                window = IncidentWindow((reference-duration).astimezone(timezone.utc).isoformat(), reference.astimezone(timezone.utc).isoformat(), "user_relative_range")
                return {**status, "status": "bounded", "reason": None, "window": window.to_dict()}
            if not matches and re.search(r"刚刚|刚才|just now", selected, re.I):
                window = IncidentWindow((reference-timedelta(minutes=15)).astimezone(timezone.utc).isoformat(),
                                        (reference+timedelta(minutes=1)).astimezone(timezone.utc).isoformat(), "user_recent_approximation")
                return {**status, "status": "bounded", "reason": None, "window": window.to_dict()}
            if not matches:
                return status
            matches.sort(key=lambda item: item[1])
            points = sorted({m[0] for m in matches})
            if len(points) > 1:
                connector = selected[matches[0][2]:matches[1][1]]
                if len(points) != 2 or not re.fullmatch(r"\s*(?:至|到|~|—|until|to)\s*", connector, re.I):
                    return {**status, "status": "ambiguous", "reason": "multiple_incident_times"}
                start, end = points
                basis = "user_explicit_range"
            else:
                start, end = points[0]-timedelta(minutes=self.before), points[0]+timedelta(minutes=self.after)
                basis = "user_explicit_time"
            if points[-1] > reference.astimezone(timezone.utc)+timedelta(minutes=10):
                return {**status, "status": "ambiguous", "reason": "future_incident_time"}
            window = IncidentWindow(start.isoformat(), end.isoformat(), basis)
            return {**status, "status": "bounded", "reason": None, "window": window.to_dict()}
        except (ValueError, OverflowError):
            return {**status, "status": "ambiguous", "reason": "invalid_incident_time"}


def record_time(record, source_timezone="+08:00", parent_time=None):
    explicit = record.get("event_at")
    if explicit:
        return parse_instant(explicit, source_timezone), "event_at"
    match = FULL_TIME.search(record.get("text", ""))
    if match:
        return parse_instant(match[1] + "T" + match[2] + (match[3] or ""), source_timezone), "log_timestamp"
    short = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\b", record.get("text", ""))
    if short and parent_time is not None:
        local = parent_time.astimezone(fixed_timezone(source_timezone))
        return local.replace(hour=int(short[1]), minute=int(short[2]), second=int(short[3] or 0)).astimezone(timezone.utc), "linked_session_date"
    return None, "unknown"


class EvidenceWindowSelector:
    """Filter after ownership validation; only follow links from selected records."""
    def select(self, account, window, *, source_timezone="+08:00"):
        if window is None:
            return deepcopy(account), {"status": "unbounded", "excluded_client_records": 0, "excluded_session_records": 0}
        window = IncidentWindow(**window) if isinstance(window, dict) else window
        selected = {"owner_id": account.get("owner_id"), "client_events": [], "sessions": {}}
        metadata = {"status": "bounded", "window": window.to_dict(), "excluded_client_records": 0,
                    "excluded_session_records": 0, "unknown_client_timestamps": 0, "linked_records_without_timestamp": 0}
        queue, visited = [], set()
        for record in account["client_events"]:
            stamp, basis = record_time(record, source_timezone)
            if stamp is None:
                metadata["unknown_client_timestamps"] += 1
            if stamp is None or not window.contains(stamp):
                metadata["excluded_client_records"] += 1
                continue
            row = deepcopy(record)
            row.update(event_at=stamp.isoformat(), temporal_basis=basis)
            selected["client_events"].append(row)
            if isinstance(row.get("session_ref"), str):
                queue.append((row["session_ref"], stamp))
        if not selected["client_events"]:
            raise DiagnosticDataGap("timestamps_missing" if metadata["unknown_client_timestamps"] == len(account["client_events"]) and account["client_events"] else "no_records_in_window")
        while queue:
            ref, parent_stamp = queue.pop(0)
            if ref in visited:
                continue
            visited.add(ref)
            if len(visited) > 100:
                raise DiagnosticDataGap("source_truncated")
            rows = []
            for record in account["sessions"].get(ref, []):
                stamp, basis = record_time(record, source_timezone, parent_stamp)
                if stamp is not None and not window.contains(stamp):
                    metadata["excluded_session_records"] += 1
                    continue
                row = deepcopy(record)
                row.update(event_at=stamp.isoformat() if stamp else None,
                           temporal_basis=basis if stamp else "linked_session_without_event_time")
                if stamp is None:
                    metadata["linked_records_without_timestamp"] += 1
                rows.append(row)
                if isinstance(row.get("session_ref"), str):
                    queue.append((row["session_ref"], stamp or parent_stamp))
            selected["sessions"][ref] = rows
        if not any(row.get("session_ref") for row in selected["client_events"]):
            raise DiagnosticDataGap("session_reference_missing")
        return selected, metadata
