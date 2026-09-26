akasyx Duplicate Detector — build a duplicate-free archive folder on your Mac
============================================================================

Move files into an archive folder only if their content is not already there.
Duplicates (same content, any name) stay where they are, so nothing is lost.
Everything runs on this Mac. Your files never leave it: the app makes no
network connections.

Requirements
------------
- A Mac with Apple Silicon (M1 or later), macOS 13 or later.

Install
-------
1. Move "akasyx Duplicate Detector.app" to your Applications folder.
2. Open it, choose an archive folder and a source folder, and run "add".
   Try "dry run" first to see what would be moved.

Where your data is stored
-------------------------
The archive database (the record of what is in each archive folder), the
working database and the logs / CSV reports are stored in
  ~/Library/Application Support/akasyx-duplicate-detector/
The app shows this location under "Advanced settings" with a button to open
it in Finder.

Back up this folder together with your archive folders. Without the archive
database, the app no longer knows which files it has archived (running
"verify" can pick them up again as unregistered).

Uninstall
---------
Deleting the app does NOT delete your data, so reinstalling keeps working
with your existing archive folders. To remove everything:
1. Quit the app and delete "akasyx Duplicate Detector.app".
2. Delete ~/Library/Application Support/akasyx-duplicate-detector/
   (in Finder: Go > Go to Folder…, then paste the path).
Your archive folders and the files in them are not touched either way. Each
archive folder contains a small hidden ".akasyx" folder, which you can delete
if you no longer use it as an archive.

Third-party software notices: THIRD_PARTY_LICENSES.txt
© at-first OÜ (akasyx)
