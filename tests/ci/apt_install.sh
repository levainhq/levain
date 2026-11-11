#!/usr/bin/env bash
# Install apt packages on a throwaway Ubuntu CI runner, surviving one unreachable mirror.
#   usage: bash tests/ci/apt_install.sh <package>...
#
# Why it exists: the runner's apt sources reach the Ubuntu archive through a mirror list that names
# azure.archive.ubuntu.com, and that host's DNS sometimes does not answer ("Temporary failure resolving
# 'azure.archive.ubuntu.com'": scheduled run 43039008564 on 11-11 failed both attempts 49 minutes apart, and runs
# on 11-11 at 16:05Z and 16:19Z failed the same way while a push run of the same sha had been green). That is the
# runner's network, not a defect in the code under test, and the old step reported it as a plain install failure.
#
# The rule, as before this script: an `update` that fails does not stop the step (a runner image carries third-party
# repos that time out on their own, and a package that is already installed needs no index); the INSTALL decides. Only
# when the update failed AND the install then failed is the mirror suspected: point the runner's sources at
# archive.ubuntu.com and try once more. An install that fails with a healthy update (a bad package name, a held dpkg
# lock, a full disk) is NOT called a mirror outage and takes no fallback: it exits 1 with apt's own words above it.
# The sources edit changes the runner's own throwaway VM only, and the lines it left are printed.
set -u

if [ "$#" -eq 0 ]; then
  echo "apt_install.sh: no packages named" >&2
  exit 2
fi

APT_OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=120)
UPDATE_FAILED=0

# update (its failure is noted, never fatal), then the install; returns the install's status
attempt() {
  UPDATE_FAILED=0
  sudo timeout -k 10 300 apt-get "${APT_OPTS[@]}" update -q || UPDATE_FAILED=1
  sudo timeout -k 10 300 apt-get "${APT_OPTS[@]}" install -y -q "$@"
}

if attempt "$@"; then
  exit 0
fi

if [ "$UPDATE_FAILED" -eq 0 ]; then
  echo "apt_install.sh: the install of '$*' failed while the package index updated fine, so this is not a mirror outage (read apt's message above)." >&2
  exit 1
fi

echo "apt_install.sh: the update and then the install failed; retrying on archive.ubuntu.com" >&2
# The runner's sources name the mirror through a mirror list file (`URIs: mirror+file:/etc/apt/apt-mirrors.txt`, the
# "Get:1 file:/etc/apt/apt-mirrors.txt Mirrorlist" line in the failing log), and the list holds azure.archive.ubuntu.com;
# a classic sources.list may name the host directly. Rewrite both spellings, in whichever file the runner has.
for f in /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list; do
  if [ -f "$f" ]; then
    sudo sed -i -E -e 's#mirror\+file:/etc/apt/apt-mirrors\.txt#http://archive.ubuntu.com/ubuntu/#g' \
                   -e 's#https?://azure\.archive\.ubuntu\.com/ubuntu#http://archive.ubuntu.com/ubuntu#g' "$f" \
      || echo "apt_install.sh: could not rewrite $f" >&2
    grep -Hn 'archive.ubuntu.com' "$f" >&2 || true
  fi
done

if attempt "$@"; then
  exit 0
fi

echo "apt_install.sh: the update and the install of '$*' failed on azure.archive.ubuntu.com and again on archive.ubuntu.com." >&2
echo "apt_install.sh: that points at the runner's network (read apt's message above), not at the code under test." >&2
exit 1
