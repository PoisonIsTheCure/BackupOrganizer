-- Backup Status: double-clickable dialog showing BackupOrganizer's --status output.
-- Build the app with:  osacompile -o "Backup Status.app" "Backup Status.applescript"

on run
	set pythonBin to "/opt/homebrew/bin/python3"
	set scriptPath to (POSIX path of (path to home folder)) & "DevProjects/Python/Tools/BackupOrganizer/backup_organizer.py"
	try
		set report to do shell script quoted form of pythonBin & " " & quoted form of scriptPath & " --status --no-notify"
		display dialog report with title "Backup Status" buttons {"OK"} default button "OK" with icon note
	on error errMsg number errNum
		display dialog "Could not read backup status:" & return & return & errMsg with title "Backup Status" buttons {"OK"} default button "OK" with icon stop
	end try
end run
