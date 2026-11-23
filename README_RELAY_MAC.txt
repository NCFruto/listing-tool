LISTING TOOL RELAY - Quick Start (Mac)
========================================

WHAT THIS IS
------------
The Listing Tool now lives at one shared website everyone uses:

    https://209-38-94-2.sslip.io

This "Relay" is a small helper that runs in the background on YOUR
Mac. realestate.com.au blocks requests coming from the shared
website's server directly, so when you click "Get Listing" on the
shared site, it quietly asks this helper (running on your machine) to
fetch the page instead, using your own internet connection. Everything
else - templates, past listings, photos - still lives on the shared
site as normal.

FIRST TIME ONLY
---------------
1. Make sure Google Chrome is installed (https://google.com/chrome)
2. Double-click setup_relay.command
   - If macOS says it "cannot be opened because it is from an
     unidentified developer": right-click (or Control-click) the file,
     choose "Open", then click "Open" again in the dialog that appears.
     You only need to do this once.
   - If double-clicking does nothing at all (this zip came from a
     Windows machine, so the file may not be marked as runnable yet):
     open Terminal, drag this folder into the Terminal window to fill
     in its path, then run:
         chmod +x *.command
     Then double-click setup_relay.command again.
3. A Terminal window opens and installs everything automatically
   (2-3 minutes). It also checks for Python 3 - if you don't have it,
   the script will open the download page for you; install it, then
   run setup_relay.command again.
4. When it finishes, it starts the helper for you. Open
   https://209-38-94-2.sslip.io in your browser and log in with the
   team password.

EVERY OTHER TIME
----------------
1. Double-click the "Listing Tool Relay" shortcut on your Desktop
   (created automatically during setup)
2. Leave that Terminal window open in the background
3. Open https://209-38-94-2.sslip.io in your browser as normal
4. Close the Terminal window when you're done for the day

NEED HELP?
----------
Contact NC.
