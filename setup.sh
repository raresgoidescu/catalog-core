#!/bin/sh
#
# Clone every repository the apps in this directory need into repos/.
#
# The list is not maintained here: it is every path the apps link in their
# own setup.sh (check_exists_and_create_symlink "<path>"). GitHub names
# follow the convention
#     unikraft    -> unikraft/unikraft
#     libs/<x>    -> unikraft/lib-<x>
#     apps/<x>    -> unikraft/app-<x>
#
# Usage: ./setup.sh [branch]      clone missing repos (default: staging)
#        ./setup.sh --list        print "<path> <github repo>" and exit

BRANCH="${1:-${BRANCH:-staging}}"
ORG="${ORG:-unikraft}"

cd "$(dirname "$0")" || exit 1

repo_name()
{
    case "$1" in
    unikraft) echo unikraft ;;
    libs/*)   echo "lib-${1#libs/}" ;;
    apps/*)   echo "app-${1#apps/}" ;;
    *)        echo "Unknown repository path '$1'" 1>&2; return 1 ;;
    esac
}

paths=$(sed -n 's/^[[:space:]]*check_exists_and_create_symlink[[:space:]]*"\([^"]*\)"[[:space:]]*$/\1/p' */setup.sh | sort -u)

for path in $paths; do
    name=$(repo_name "$path") || exit 1
    if test "$BRANCH" = "--list"; then
        echo "$path $ORG/$name"
        continue
    fi
    test -d "repos/$path" || git clone -b "$BRANCH" "https://github.com/$ORG/$name" "repos/$path" || exit 1
done
