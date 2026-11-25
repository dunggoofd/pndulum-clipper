#!/bin/bash
# usage: notify.sh <title> <message>
title="${1:-dbo}"
msg="${2:-}"
case "$(uname -s)" in
  Darwin)
    t="${title//\"/\'}"; m="${msg//\"/\'}"
    /usr/bin/osascript -e "display notification \"$m\" with title \"$t\"" 2>/dev/null
    ;;
  *)
    if command -v powershell.exe >/dev/null 2>&1; then
      t="${title//\'/}"; m="${msg//\'/}"
      powershell.exe -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; \$n=New-Object System.Windows.Forms.NotifyIcon; \$n.Icon=[System.Drawing.SystemIcons]::Information; \$n.Visible=\$true; \$n.ShowBalloonTip(8000,'$t','$m','Info'); Start-Sleep 9; \$n.Dispose()" >/dev/null 2>&1 &
    else
      echo "[$title] $msg" >&2
    fi
    ;;
esac
