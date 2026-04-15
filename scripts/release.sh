#!/bin/bash
# Release script for memory-compiler
# Usage:
#   bash scripts/release.sh patch   # 1.0.0 → 1.0.1
#   bash scripts/release.sh minor   # 1.0.1 → 1.1.0
#   bash scripts/release.sh major   # 1.1.0 → 2.0.0
#   bash scripts/release.sh 1.2.3   # explicit version

set -e

cd "$(dirname "$0")/.."

[ ! -f VERSION ] && { echo "VERSION file missing"; exit 1; }
CURRENT=$(cat VERSION)

if [ -z "$1" ]; then
    echo "Usage: $0 {patch|minor|major|X.Y.Z}"
    echo "Current: $CURRENT"
    exit 1
fi

case "$1" in
    patch|minor|major)
        IFS='.' read -ra P <<< "$CURRENT"
        MAJOR=${P[0]}; MINOR=${P[1]}; PATCH=${P[2]}
        case "$1" in
            patch) PATCH=$((PATCH+1)) ;;
            minor) MINOR=$((MINOR+1)); PATCH=0 ;;
            major) MAJOR=$((MAJOR+1)); MINOR=0; PATCH=0 ;;
        esac
        NEW="$MAJOR.$MINOR.$PATCH"
        ;;
    [0-9]*.[0-9]*.[0-9]*)
        NEW="$1"
        ;;
    *)
        echo "Invalid: $1 (expected patch|minor|major|X.Y.Z)"
        exit 1
        ;;
esac

echo "Bump: $CURRENT → $NEW"
read -p "Confirm? [y/N] " yn
[ "$yn" != "y" ] && { echo "Aborted"; exit 0; }

echo "$NEW" > VERSION
git add VERSION CHANGELOG.md 2>/dev/null || true
git commit -m "release: v$NEW" || { echo "Nothing to commit"; exit 1; }
git tag -a "v$NEW" -m "Release v$NEW"
git push origin master
git push origin "v$NEW"

echo ""
echo "Released v$NEW"
echo "Autodeploy will pick up changes on NAS within ~1 min"
