#!/bin/sh
# Phone screenshots for the README, made tidy: the status bar (clock, other apps' notifications) and the
# navigation bar cut off, the scroll indicator at the left edge painted out, rounded corners, a faint light
# edge (so a dark screen still shows on a dark card) and a soft shadow. Needs ImageMagick.
#
#   docs/screens/polish.sh RAW.png OUT.png [MORE_TOP] [CROP]
#     RAW: adb exec-out screencap -p > RAW.png; MORE_TOP: pixels more to cut from the top, of RAW;
#     CROP: WxH+X+Y of RAW, cut out instead of the whole screen (a sheet, a notification, the top of a list),
#           shrunk by the same factor as a whole screen, so its text is the same size as in the phone pictures
set -eu
raw=$1 out=$2 more=${3:-0} crop=${4:-}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
w=$(identify -format %w "$raw") h=$(identify -format %h "$raw")
top=$((h * 58 / 1000 + more)) bottom=$((h * 22 / 1000))   # status bar and gesture bar, as a share of the height
[ -n "$crop" ] || crop="${w}x$((h - top - bottom))+0+$top"
cw=${crop%%x*}
# the scroll indicator (a grey bar at the left edge, a fifth to two fifths of the way down): covered by the
# background at the very edge, repeated across it
sy=$((h * 22 / 100)) sh=$((h * 20 / 100)) s=$((w * 8 / 1000))
convert "$raw" \( +clone -crop "${s}x${sh}+$((w * 2 / 1000))+$sy" +repage \
    \( +clone +clone +clone \) +append \) -geometry "+$((w * 8 / 1000))+$sy" -composite \
  -crop "$crop" +repage -resize "$((cw * 600 / w))x" "$tmp/screen.png"
sw=$(identify -format %w "$tmp/screen.png") sh=$(identify -format %h "$tmp/screen.png")
convert -size "${sw}x${sh}" xc:none -fill white -draw "roundrectangle 0,0 $((sw - 1)),$((sh - 1)) 34,34" "$tmp/mask.png"
convert "$tmp/screen.png" -alpha set "$tmp/mask.png" -compose DstIn -composite \
  -fill none -stroke 'rgba(255,255,255,0.14)' -strokewidth 2 -draw "roundrectangle 1,1 $((sw - 2)),$((sh - 2)) 33,33" \
  "$tmp/round.png"
convert "$tmp/round.png" \( +clone -background black -shadow 45x12+0+8 \) +swap -background none \
  -layers merge +repage -define png:color-type=6 "$out"
