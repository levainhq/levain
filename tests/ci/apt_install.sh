#!/usr/bin/env bash
# Install apt packages on a throwaway Ubuntu CI runner, surviving one unreachable mirror.
#   usage: bash tests/ci/apt_install.sh <package>...
#
# Why it exists: the runner's apt sources point at azure.archive.ubuntu.com, and its DNS sometimes does not answer
# ("Temporary failure resolving 'azure.archive.ubuntu.com'", scheduled run 43039008564 on 11-11 failed both attempts
# 49 minutes apart, and the same sha was green on a push run). That is the runner's network, not a code defect, and
# `apt-get update ... || true` followed by a bare `install` reported it as an install failure with no word about
# the mirror. Here: try the runner's mirror; on a failed update, point the sources at archive.ubuntu.com and try
# once more; if that fails too, print ONE plain line saying the mirror was unreachable and exit 1 (the job is still
# red: a runner that cannot install bubblewrap proves nothing about the code, and a silent green would be worse).
# The sources edit changes the runner's own throwaway VM only.
set -u

if [ "$#" -eq 0 ]; then
  echo "apt_install.sh: no packages named" >&2
  exit 2
fi

APT_OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)

try_install() {
  sudo timeout 300 apt-get "${APT_OPTS[@]}" update -q && sudo timeout 300 apt-get "${APT_OPTS[@]}" install -y -q "$@"
}

if try_install "$@"; then
  exit 0
fi

echo "apt_install.sh: the runner's mirror did not answer; retrying on archive.ubuntu.com" >&2
# The runner's sources name the mirror through a mirror list file (`URIs: mirror+file:/etc/apt/apt-mirrors.txt`, the
# "Get:1 file:/etc/apt/apt-mirrors.txt Mirrorlist" line in the failing log), and the list holds azure.archive.ubuntu.com;
# a classic sources.list may name the host directly. Rewrite both spellings, in whichever file the runner has.
for f in /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list; do
  if [ -f "$f" ]; then
    sudo sed -i -E -e 's#mirror\+file:/etc/apt/apt-mirrors\.txt#http://archive.ubuntu.com/ubuntu/#g' \
                   -e 's#https?://azure\.archive\.ubuntu\.com/ubuntu#http://archive.ubuntu.com/ubuntu#g' "$f"
  fi
done

if try_install "$@"; then
  exit 0
fi

echo "apt_install.sh: NO UBUNTU MIRROR WAS REACHABLE from this runner (azure.archive.ubuntu.com, then archive.ubuntu.com)." >&2
echo "apt_install.sh: this is the runner's network, not a defect in the code under test; the package step never ran: $*" >&2
exit 1
