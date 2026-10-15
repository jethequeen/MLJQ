<#
    MLJQ - installe la tache planifiee du poller.

    (ASCII sans accents: PowerShell 5.1 lit un .ps1 sans BOM en ANSI, pas en UTF-8.)

    A LANCER SUR LA MACHINE PROPRIETAIRE (celle que poller_owner.json nomme), dans une
    session PowerShell normale - aucun droit administrateur n'est necessaire.

    Ce qu'elle fait: a l'ouverture de session, puis toutes les 5 minutes, elle execute une
    passe de la file du telephone et de l'auto-apply, sans fenetre et sans GUI ouvert.

    Pourquoi "a l'ouverture de session" et non "au demarrage": le projet vit sur Google
    Drive, et le lecteur G: n'existe que dans une session ouverte. Une tache lancee au boot
    ne trouverait rien. D'ou aussi le delai de 2 minutes, le temps que Drive monte.

    Pourquoi seulement sur Plex: une seule machine fait le travail, sinon deux pollers
    reecrivent les memes fichiers (voir sortpack/lock.py). Le verrou le garantit deja a
    l'execution - ce script refuse en plus de s'installer ailleurs, pour qu'on ne pose pas
    la tache sur le portable par distraction.

    Desinstaller:  Unregister-ScheduledTask -TaskName 'MLJQ poller' -Confirm:$false
#>
param(
    [string] $Machine = 'Plex',
    [int]    $Minutes = 5,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$nom = 'MLJQ poller'

$lanceur = Join-Path $PSScriptRoot 'run_poller_hidden.vbs'
if (-not (Test-Path $lanceur)) { throw "Introuvable : $lanceur" }

if ($env:COMPUTERNAME -ne $Machine -and -not $Force) {
    Write-Host ""
    Write-Host "  Cette machine s'appelle '$env:COMPUTERNAME', pas '$Machine'." -ForegroundColor Yellow
    Write-Host "  Une seule machine doit tenir le poller. Rien n'a ete installe."
    Write-Host "  (-Force pour passer outre, -Machine <nom> pour viser une autre machine.)"
    Write-Host ""
    exit 1
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
                                  -Argument ('"{0}"' -f $lanceur) `
                                  -WorkingDirectory $PSScriptRoot

# Declencheur: ouverture de session de CET utilisateur, + 2 min pour laisser monter Drive.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = 'PT2M'

# Repetition: toutes les N minutes, sans fin. On emprunte l'objet Repetition d'un
# declencheur "once", la seule facon propre de l'exprimer en PowerShell 5.1.
$modele = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                                   -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
                                   -RepetitionDuration ([TimeSpan]::MaxValue)
$trigger.Repetition = $modele.Repetition

# IgnoreNew: si une passe deborde sur la suivante, on ne la double pas. Le verrou local
# (lock.single_instance) dit la meme chose cote Python - deux filets valent mieux qu'un,
# celui-ci ne couvrant pas le GUI ouvert en parallele.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries `
                                         -StartWhenAvailable `
                                         -MultipleInstances IgnoreNew `
                                         -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $nom -Action $action -Trigger $trigger -Settings $settings `
                       -Description "Traite la file du telephone et l'auto-apply toutes les $Minutes minutes, sans GUI." `
                       -Force | Out-Null

Write-Host ""
Write-Host "  Tache '$nom' installee sur $env:COMPUTERNAME." -ForegroundColor Green
Write-Host "  - a l'ouverture de session (+2 min), puis toutes les $Minutes minutes"
Write-Host "  - sans fenetre, sans GUI"
Write-Host "  - journal : $(Join-Path $PSScriptRoot 'reports\remote.log')"
Write-Host ""
Write-Host "  Essai immediat :  Start-ScheduledTask -TaskName '$nom'"
Write-Host ""
