' MLJQ - lance process_remote.bat SANS fenetre.
'
' (ASCII sans accents: wscript.exe lit les .vbs en ANSI, pas en UTF-8.)
'
' La tache planifiee passe aux 5 minutes. Sans ce lanceur, une console noire clignoterait
' a l'ecran autant de fois - et la machine proprietaire est aussi celle sur laquelle on
' travaille. Le "0" de sh.Run est la fenetre cachee, le "False" dit de ne pas attendre.
Dim sh, ici
Set sh = CreateObject("WScript.Shell")
ici = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run """" & ici & "process_remote.bat""", 0, False
