"""Read-only audit of each record's latest transition; no publication or index changes."""
import json
from collections import Counter
from pathlib import Path

from experiment_archive.changes import classify
from experiment_archive.store import Archive, atomic_write, canonical


def main():
    root = Path(__file__).resolve().parents[1]
    archive = Archive(root / 'archive')
    manifest, snapshot = archive.snapshot()
    rows = []
    for identity in manifest['experiments']:
        current = archive.record(identity, manifest)
        history = archive.history(identity)
        receipt = next(item for item in history if item['revision'] == current['revision'])
        parent = receipt.get('previous_revision')
        if not parent:
            continue
        previous = archive.record(identity, manifest, revision=parent)
        rows.append({'id': identity, 'revision': current['revision'], 'previous_revision': parent,
                     'changes': classify(previous, current['metadata'], current['note'], current['metrics'], current['comparisons'])})
    report = {'archive_snapshot_sha256': snapshot, 'scope': 'Latest published transition per record; no history removed',
              'transitions': len(rows), 'counts': dict(Counter('+'.join(row['changes']) for row in rows)), 'records': rows}
    atomic_write(root / 'reports/revision_churn_audit.json', canonical(report))
    print(json.dumps({key: value for key, value in report.items() if key != 'records'}, indent=2))


if __name__ == '__main__':
    main()
