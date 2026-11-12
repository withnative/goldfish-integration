#!/usr/bin/env python3
"""Build a recipient archive with no fixtures, auth, state, or legacy mirror."""
import argparse
from pathlib import Path
import tarfile

FILES = ['goldfish_review.py', 'native_client.py', 'native_reader.py', 'config.default.json', 'README.md',
         'skills/goldfish-review/SKILL.md', 'skills/goldfish-review/drift.md',
         'skills/goldfish/SKILL.md']


def package(destination):
    root = Path(__file__).resolve().parent
    with tarfile.open(destination, 'w:gz') as archive:
        for name in FILES:
            archive.add(root / name, arcname='goldfish-review-kit/' + name, recursive=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    package(args.destination)
    print(args.destination)
