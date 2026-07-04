-- Backup Status: double-clickable dialog showing BackupOrganizer's --status output,
-- with a button to open the interactive HTML backup browser.
-- Build the app with:  osacompile -o "Backup Status.app" "Backup Status.applescript"

on run
	set pythonBin to "/opt/homebrew/bin/python3"
	set scriptPath to (POSIX path of (path to home folder)) & "DevProjects/Python/Tools/BackupOrganizer/backup_organizer.py"
	set runner to quoted form of pythonBin & " " & quoted form of scriptPath
	try
		set report to do shell script runner & " --status --no-notify"
		set answer to display dialog report with title "Backup Status" buttons {"Browse Files", "OK"} default button "OK" with icon note
		if button returned of answer is "Browse Files" then
			do shell script runner & " --browse --no-notify"
		end if
	on error errMsg number errNum
		if errNum is not -128 then
			display dialog "Could not read backup status:" & return & return & errMsg with title "Backup Status" buttons {"OK"} default button "OK" with icon stop
		end if
	end try
end run
