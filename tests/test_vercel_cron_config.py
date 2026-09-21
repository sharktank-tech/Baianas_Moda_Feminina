import json
from pathlib import Path


def test_stock_cleanup_cron_is_hobby_compatible_daily_schedule():
    config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    matching = [
        cron
        for cron in config.get("crons", [])
        if cron.get("path")
        == "/internal/maintenance/release-expired-reservations"
    ]

    assert matching == [
        {
            "path": "/internal/maintenance/release-expired-reservations",
            "schedule": "17 9 * * *",
        }
    ]
