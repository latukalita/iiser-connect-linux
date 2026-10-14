#!/bin/bash
# usage: tools/shot.sh <page: welcome|account|home|settings> <dark|light> <out.png>
# Runs the GUI via XWayland with the software renderer and grabs the window with ImageMagick.
# Point IISER_CONNECT_DIR / IISER_CONNECT_TEST_BASE at throwaway data first (see tests/).
page=$1; scheme=$2; out=$3
title="IISER Connect"; [ "$page" = settings ] && title="Settings"
cd "$(dirname "$0")/.."
GDK_BACKEND=x11 GSK_RENDERER=cairo IISER_CONNECT_PAGE=$page IISER_CONNECT_SCHEME=$scheme \
  timeout 9 python3 -m iiser_connect gui >/dev/null 2>&1 &
pid=$!
sleep 4
wid=$(xwininfo -root -tree 2>/dev/null | grep "\"$title\"" | grep -v "1x1+" | head -1 | awk '{print $1}')
[ -n "$wid" ] && import -window "$wid" "$out"
kill $pid 2>/dev/null; wait $pid 2>/dev/null
[ -n "$wid" ] && echo "saved $out" || echo "no window found"
