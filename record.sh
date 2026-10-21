#!/bin/bash
# Record the HW6 demo. Opens a Terminal window sized to the main screen, runs demo.sh in it,
# records only that window region with screencapture (max 150 s), trims the take to the demo's
# real length and speeds it to 58 s. Needs Screen Recording permission for Terminal.
cd "$(dirname "$0")"; mkdir -p video runs; rm -f runs/demo_timing video/take_full.mov video/t1.mov video/t2.mov
W=1512; TOP=25; H=860   # main screen is 1512x982 points; leave the menu bar and the dock out
osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  set w to do script "cd ~/kya-gate && ./demo.sh"
  delay 0.5
  set bounds of front window to {0, $TOP, $W, $((TOP+H))}
  try
    set font size of front window to 15
  end try
end tell
APPLESCRIPT
sleep 1
screencapture -V 150 -x -R 0,$TOP,$W,$H video/take_full.mov
FF=$(.venv/bin/python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())" 2>/dev/null)
if [ -z "$FF" ]; then uv pip install --python .venv/bin/python imageio-ffmpeg >/dev/null && FF=$(.venv/bin/python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"); fi
S=$(sed -n 1p runs/demo_timing); E=$(sed -n 2p runs/demo_timing)
if [ -z "$E" ]; then echo "demo did not finish inside 150 s; see runs/ and video/take_full.mov"; exit 1; fi
LEN=$((E - S + 4)); F=$(echo "scale=4; 58/$LEN" | bc)
"$FF" -y -loglevel error -t $LEN -i video/take_full.mov -filter:v "setpts=${F}*PTS,fps=15" -an -c:v libx264 -pix_fmt yuv420p video/hw6_demo_60s.mp4
echo "done: video/hw6_demo_60s.mp4 (demo ran ${LEN}s, speed factor $F)"
