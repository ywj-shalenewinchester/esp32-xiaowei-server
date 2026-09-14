#!/bin/bash
# Find the PID listening on port 8004 via /proc
for pid in /proc/[0-9]*; do
  p=${pid##*/}
  [ ! -d /proc/$p ] && continue
  if grep -q "0804" /proc/$p/net/tcp6 2>/dev/null || grep -q "0804" /proc/$p/net/tcp 2>/dev/null; then
    cmd=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
    echo "PID $p : $cmd"
  fi
done
