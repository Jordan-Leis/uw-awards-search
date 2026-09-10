"""Print the number of awards still missing detail data.

Used by CI to decide whether to re-run scrape_details.py (which is resumable),
kept as its own script so the workflow doesn't have to embed Python inside a
YAML block scalar inside a shell command substitution.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from waterloo_awards import db
from waterloo_awards.config import DB_PATH

conn = db.connect(DB_PATH)
print(db.count_unscraped(conn))
conn.close()
