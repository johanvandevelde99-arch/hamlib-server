This reads cat data from any radio via hamlib server
This app also streams tcp cat info to another app or another pc outside the network
To be used with logging programs installed remotely, I use OPSLOG.
Features:
  * Downloads the latest official Hamlib Windows build from GitHub
  * Lists all supported radios (from rigctl -l) with a search box
  * Detects COM ports, sets baud rate / TCP port / CI-V address / PTT
  * Start / Stop rigctld, live log output, "Read frequency" test button
  * Remembers your settings
  * Remote TCP relay: lets another computer use this radio by selecting
    "Hamlib NET rigctl" with this PC's IP:port, with a live CAT traffic monitor
  * Dark UI in the style of OpsLog (Graphite dark / Warm dark / High contrast)
  * Reverse connection: the radio PC dials OUT to the remote PC (no inbound port
    forwarding needed); the remote PC runs this app in "Accept" mode
