"""Print aggregate closure metrics for the local demo database (no personal records)."""
import json

from app.audit import AuditStore
from app.case_metrics import summarize_cases
from app.config import get_settings


def main():
    audit = AuditStore(get_settings().db_path)
    states = [audit.get_state(ticket["id"]) for ticket in audit.list_tickets()]
    print(json.dumps(summarize_cases(states), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
